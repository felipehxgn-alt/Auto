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

Uso (dentro do GitHub Actions, ou local com as env vars setadas):
    python diagnostico_fotos_ml.py data/item_ids_catalogo.csv
"""

import os
import sys
import csv
import time
import json

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


def preparar_aba_resultado(ss):
    try:
        ws = ss.worksheet(ABA_RESULTADO)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=ABA_RESULTADO, rows=5000, cols=10)
    ws.append_row(
        ["ITEM_ID", "Status Atual (ML)", "Link Oficial", "Veredito", "Motivo",
         "Qtd. Fotos", "Fotos Baixadas Para Revisão (Dropbox)"],
        value_input_option="USER_ENTERED",
    )
    return ws


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
def main():
    if len(sys.argv) < 2:
        print("Uso: python diagnostico_fotos_ml.py data/item_ids_catalogo.csv")
        sys.exit(1)

    with open(sys.argv[1], newline="") as f:
        reader = csv.DictReader(f)
        item_ids = [row["ITEM_ID"] for row in reader if row.get("ITEM_ID")]

    print(f"{len(item_ids)} anúncios a processar.")

    dbx_token = obter_dropbox_token() if DROPBOX_REFRESH_TOKEN else None
    ss = abrir_planilha()
    ws = preparar_aba_resultado(ss)

    linhas_buffer = []
    ok_count = 0
    corrigir_count = 0

    for idx, item_id in enumerate(item_ids, start=1):
        info = consultar_item(item_id)
        if info is None:
            linhas_buffer.append([item_id, "ERRO_CONSULTA", "", "Verificar manualmente", "", 0, ""])
            continue

        status_atual = info["status_atual"]
        permalink = info["permalink"]
        fotos = info["fotos"]

        if not fotos:
            linhas_buffer.append([item_id, status_atual, permalink, "Precisa Correção", "Sem fotos no anúncio", 0, ""])
            corrigir_count += 1
            continue

        # classifica cada foto; se qualquer uma não estiver padronizada, marca pra correção
        motivos = []
        todas_ok = True
        fotos_problema = []
        for foto_url in fotos:
            padronizada, motivo = foto_esta_padronizada(foto_url)
            if not padronizada:
                todas_ok = False
                motivos.append(motivo)
                fotos_problema.append(foto_url)

        if todas_ok:
            linhas_buffer.append([item_id, status_atual, permalink, "OK", "", len(fotos), ""])
            ok_count += 1
        else:
            pasta_dropbox = ""
            if dbx_token:
                pasta_destino = f"{DROPBOX_REVISAR_FOTOS_PATH}/{item_id}"
                for i, foto_url in enumerate(fotos_problema, start=1):
                    try:
                        conteudo = requests.get(foto_url, timeout=30).content
                        dbx_subir(dbx_token, f"{pasta_destino}/foto_{i}.jpg", conteudo)
                    except Exception as e:
                        print(f"  falha baixando foto de {item_id}: {e!r}")
                pasta_dropbox = pasta_destino
            linhas_buffer.append([item_id, status_atual, permalink, "Precisa Correção", "; ".join(motivos), len(fotos), pasta_dropbox])
            corrigir_count += 1

        if idx % 50 == 0:
            print(f"  {idx}/{len(item_ids)} processados — OK: {ok_count} | Precisa Correção: {corrigir_count}")
            ws.append_rows(linhas_buffer, value_input_option="USER_ENTERED")
            linhas_buffer = []

    if linhas_buffer:
        ws.append_rows(linhas_buffer, value_input_option="USER_ENTERED")

    print(f"\nConcluído. OK: {ok_count} | Precisa Correção: {corrigir_count} | Total: {len(item_ids)}")
    print(f"Resultado na aba '{ABA_RESULTADO}' da planilha.")


if __name__ == "__main__":
    main()
