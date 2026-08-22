"""
diagnostico_fotos_ml.py
Roda via GitHub Actions (workflow_dispatch) — usa as credenciais que já
existem como secrets do repo: DROPBOX_*, OPENAI_API_KEY,
GOOGLE_SERVICE_ACCOUNT_JSON, PLANILHA_ANUNCIOS_ML_ID. Nenhuma credencial
nova é necessária.

O QUE FAZ, por anúncio (lido de data/item_ids_catalogo.csv):
1) Consulta o endpoint PÚBLICO do ML (sem OAuth) -> status atual, link
   oficial (permalink), lista de fotos.
2) Pra cada foto, pergunta pra uma IA de visão (gpt-4o-mini, a mesma já
   usada no robô de mídias) se ela já está no padrão novo (fundo
   branco/canvas, logo no canto) ou não.
3) Se TODAS as fotos já estão padronizadas -> marca "OK", não baixa nada.
4) Se qualquer foto não estiver padronizada (ou o anúncio não tiver
   fotos) -> marca "Precisa Correção", baixa só essas fotos pro Dropbox
   em /AUTOMACAO_ANUNCIOS/_REVISAR_FOTOS_ANTIGAS/<SKU>/, prontas pra
   comparar com o padrão novo.
5) Escreve o resultado de TODOS os anúncios numa aba nova
   "Diagnóstico Fotos ML" na planilha (Google Sheets) — não fica só
   local, fica onde vocês já acompanham tudo.

CORREÇÕES (22/08/2026) sobre o diagnóstico de falha (job cancelado após
6h pelo limite duro do GitHub Actions, sem log visível e sem retomada):
  - Log em tempo real: todo print agora usa flush=True (não depende só
    de -u no comando, funciona mesmo se alguém rodar sem a flag).
  - Processamento em PARALELO (ThreadPoolExecutor): antes era 1 item por
    vez, cada um com N chamadas de IA sequenciais — o gargalo real do
    tempo. Agora processa vários itens ao mesmo tempo (padrão: 8).
  - RETOMADA automática: no início, lê os ITEM_IDs que já estão na aba
    "Diagnóstico Fotos ML" e pula eles. Se o job for cancelado nas 6h de
    novo, o próximo run continua de onde parou em vez de reprocessar
    tudo. Pra forçar do zero, rode com --reset.
  - Checkpoint mais frequente (a cada 10 itens concluídos, não 50) —
    menos risco de perder progresso não salvo se cortar no meio.

Uso (dentro do GitHub Actions, ou local com as env vars setadas):
    python diagnostico_fotos_ml.py data/item_ids_catalogo.csv
    python diagnostico_fotos_ml.py data/item_ids_catalogo.csv --reset
"""

import os
import sys
import csv
import time
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import gspread
from google.oauth2.service_account import Credentials

# ------------------------------------------------------------------
# Config (reaproveita os mesmos secrets do robô de mídias)
# ------------------------------------------------------------------
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
DROPBOX_APP_KEY = os.environ.get("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.environ.get("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.environ.get("DROPBOX_REFRESH_TOKEN")
DROPBOX_REVISAR_FOTOS_PATH = os.environ.get(
    "DROPBOX_REVISAR_FOTOS_PATH", "/AUTOMACAO_ANUNCIOS/_REVISAR_FOTOS_ANTIGAS"
)
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
PLANILHA_ID = os.environ["PLANILHA_ANUNCIOS_ML_ID"]

API_ITEM = "https://api.mercadolibre.com/items/{item_id}"
CAMPOS = "id,status,permalink,pictures,title"

ABA_RESULTADO = "Diagnóstico Fotos ML"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

MAX_WORKERS = int(os.environ.get("DIAGNOSTICO_MAX_WORKERS", "8"))
CHECKPOINT_A_CADA = 10

# Dropbox precisa de 1 token por processo (token bucket simples, thread-safe)
_dbx_lock = threading.Lock()
_dbx_token = {"valor": None}

# gspread/append_rows não é thread-safe pra concorrência simultânea
_sheet_lock = threading.Lock()


# ------------------------------------------------------------------
# Dropbox (token renovado sozinho, mesmo padrão do robô de mídias)
# ------------------------------------------------------------------
def obter_dropbox_token():
    resp = requests.post(
        "https://api.dropboxapi.com/oauth2/token",
        data={"grant_type": "refresh_token", "refresh_token": DROPBOX_REFRESH_TOKEN},
        auth=(DROPBOX_APP_KEY, DROPBOX_APP_SECRET),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def dbx_token_atual():
    with _dbx_lock:
        if _dbx_token["valor"] is None:
            _dbx_token["valor"] = obter_dropbox_token()
        return _dbx_token["valor"]


def dbx_subir(token, path_destino, conteudo_bytes):
    resp = requests.post(
        "https://content.dropboxapi.com/2/files/upload",
        headers={
            "Authorization": f"Bearer {token}",
            "Dropbox-API-Arg": json.dumps({"path": path_destino, "mode": "overwrite"}),
            "Content-Type": "application/octet-stream",
        },
        data=conteudo_bytes,
        timeout=60,
    )
    resp.raise_for_status()


# ------------------------------------------------------------------
# Mercado Livre - endpoint público (sem OAuth)
# ------------------------------------------------------------------
def consultar_item(item_id, tentativas=3):
    url = API_ITEM.format(item_id=item_id)
    for tentativa in range(1, tentativas + 1):
        try:
            resp = requests.get(url, params={"attributes": CAMPOS}, timeout=15)
            if resp.status_code == 200:
                dados = resp.json()
                fotos = [p.get("secure_url") or p.get("url") for p in dados.get("pictures", [])]
                return {
                    "status_atual": dados.get("status", ""),
                    "permalink": dados.get("permalink", ""),
                    "fotos": fotos,
                }
            elif resp.status_code == 404:
                return {"status_atual": "NAO_ENCONTRADO", "permalink": "", "fotos": []}
            time.sleep(1.5 * tentativa)
        except requests.RequestException:
            time.sleep(1.5 * tentativa)
    return None


# ------------------------------------------------------------------
# Classificação de foto via IA de visão
# ------------------------------------------------------------------
def foto_esta_padronizada(url_foto):
    """Pergunta pra IA se a foto já segue o padrão novo (fundo branco/
    canvas uniforme, produto centralizado, logo visível no canto).
    Retorna (padronizada: bool, motivo: str)."""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Esta é uma foto de produto de um anúncio de autopeça. "
                            "Ela segue um padrão profissional de e-commerce: fundo "
                            "totalmente branco/uniforme (canvas), produto bem "
                            "centralizado, sem mão/dedo segurando a peça, sem fundo "
                            "de ambiente (mesa, chão, parede)? Responda SOMENTE em "
                            'JSON: {"padronizada": true/false, "motivo": "..."} '
                            "sem texto adicional. motivo deve ser curto (poucas "
                            "palavras)."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": url_foto}},
                ],
            }
        ],
        "max_tokens": 100,
    }
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        texto = resp.json()["choices"][0]["message"]["content"].strip()
        texto = texto.replace("```json", "").replace("```", "").strip()
        dados = json.loads(texto)
        return bool(dados.get("padronizada", False)), dados.get("motivo", "")
    except Exception as e:
        # se a checagem falhar tecnicamente, trata como "precisa revisar" -
        # nunca assume que está tudo certo por causa de erro na checagem
        return False, f"falha na checagem: {e!r}"


# ------------------------------------------------------------------
# Google Sheets
# ------------------------------------------------------------------
def abrir_planilha():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(PLANILHA_ID)


def preparar_aba_resultado(ss, reset):
    try:
        ws = ss.worksheet(ABA_RESULTADO)
        if reset:
            ws.clear()
            ws.append_row(
                ["ITEM_ID", "Status Atual (ML)", "Link Oficial", "Veredito", "Motivo",
                 "Qtd. Fotos", "Fotos Baixadas Para Revisão (Dropbox)"],
                value_input_option="USER_ENTERED",
            )
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=ABA_RESULTADO, rows=5000, cols=10)
        ws.append_row(
            ["ITEM_ID", "Status Atual (ML)", "Link Oficial", "Veredito", "Motivo",
             "Qtd. Fotos", "Fotos Baixadas Para Revisão (Dropbox)"],
            value_input_option="USER_ENTERED",
        )
    return ws


def item_ids_ja_processados(ws):
    """Lê a coluna ITEM_ID já gravada na aba (pra retomar sem reprocessar)."""
    valores = ws.col_values(1)  # coluna A
    return set(valores[1:])  # pula o cabeçalho


# ------------------------------------------------------------------
# Processamento de 1 item (roda em paralelo por thread)
# ------------------------------------------------------------------
def processar_item(item_id):
    info = consultar_item(item_id)
    if info is None:
        return [item_id, "ERRO_CONSULTA", "", "Verificar manualmente", "", 0, ""], "erro"

    status_atual = info["status_atual"]
    permalink = info["permalink"]
    fotos = info["fotos"]

    if not fotos:
        return [item_id, status_atual, permalink, "Precisa Correção", "Sem fotos no anúncio", 0, ""], "corrigir"

    # classifica as fotos do mesmo item em paralelo também (poucas por item, mas some tempo)
    motivos = []
    fotos_problema = []
    with ThreadPoolExecutor(max_workers=min(4, len(fotos))) as pool:
        resultados = list(pool.map(foto_esta_padronizada, fotos))
    for foto_url, (padronizada, motivo) in zip(fotos, resultados):
        if not padronizada:
            motivos.append(motivo)
            fotos_problema.append(foto_url)

    if not fotos_problema:
        return [item_id, status_atual, permalink, "OK", "", len(fotos), ""], "ok"

    pasta_dropbox = ""
    if DROPBOX_REFRESH_TOKEN:
        token = dbx_token_atual()
        pasta_destino = f"{DROPBOX_REVISAR_FOTOS_PATH}/{item_id}"
        for i, foto_url in enumerate(fotos_problema, start=1):
            try:
                conteudo = requests.get(foto_url, timeout=30).content
                dbx_subir(token, f"{pasta_destino}/foto_{i}.jpg", conteudo)
            except Exception as e:
                print(f"  falha baixando foto de {item_id}: {e!r}", flush=True)
        pasta_dropbox = pasta_destino

    return [item_id, status_atual, permalink, "Precisa Correção", "; ".join(motivos), len(fotos), pasta_dropbox], "corrigir"


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    reset = "--reset" in sys.argv

    if len(args) < 1:
        print("Uso: python diagnostico_fotos_ml.py data/item_ids_catalogo.csv [--reset]", flush=True)
        sys.exit(1)

    with open(args[0], newline="") as f:
        reader = csv.DictReader(f)
        item_ids = [row["ITEM_ID"] for row in reader if row.get("ITEM_ID")]

    total_catalogo = len(item_ids)
    print(f"{total_catalogo} anúncios no catálogo.", flush=True)

    ss = abrir_planilha()
    ws = preparar_aba_resultado(ss, reset)

    if not reset:
        ja_feitos = item_ids_ja_processados(ws)
        if ja_feitos:
            item_ids = [i for i in item_ids if i not in ja_feitos]
            print(f"Retomando: {len(ja_feitos)} já processados antes, {len(item_ids)} restantes.", flush=True)

    if not item_ids:
        print("Nada a processar — todos os itens já estão na aba.", flush=True)
        return

    linhas_buffer = []
    contadores = {"ok": 0, "corrigir": 0, "erro": 0}
    concluidos = 0
    lock_contadores = threading.Lock()

    def salvar_buffer():
        if linhas_buffer:
            with _sheet_lock:
                ws.append_rows(linhas_buffer, value_input_option="USER_ENTERED")
            linhas_buffer.clear()

    print(f"Processando {len(item_ids)} itens com {MAX_WORKERS} threads em paralelo...", flush=True)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futuros = {pool.submit(processar_item, item_id): item_id for item_id in item_ids}
        for futuro in as_completed(futuros):
            item_id = futuros[futuro]
            try:
                linha, tipo = futuro.result()
            except Exception as e:
                linha, tipo = [item_id, "ERRO_INESPERADO", "", "Verificar manualmente", repr(e), 0, ""], "erro"

            with lock_contadores:
                linhas_buffer.append(linha)
                contadores[tipo] += 1
                concluidos += 1
                deve_salvar = concluidos % CHECKPOINT_A_CADA == 0

            if deve_salvar:
                salvar_buffer()
                print(
                    f"  {concluidos}/{len(item_ids)} concluídos — "
                    f"OK: {contadores['ok']} | Precisa Correção: {contadores['corrigir']} | Erro: {contadores['erro']}",
                    flush=True,
                )

    salvar_buffer()

    print(
        f"\nConcluído. OK: {contadores['ok']} | Precisa Correção: {contadores['corrigir']} | "
        f"Erro: {contadores['erro']} | Total processado agora: {concluidos}",
        flush=True,
    )
    print(f"Resultado na aba '{ABA_RESULTADO}' da planilha.", flush=True)


if __name__ == "__main__":
    main()
