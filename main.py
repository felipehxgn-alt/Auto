"""
Robo de Midias - Automacao de fotos/videos de produtos (ELRING/Hexagon e outras marcas)
Armazenamento: 100% Dropbox (entrada, saida e logos).

ORDEM DE CAPTURA (no celular/camera):
  1. Foto da caixa (SKU + marca impressos)
  2. Foto de capa do produto
  3..N. Fotos de angulo/detalhe do produto
  Ultimo arquivo = video (fecha o lote)

NOMENCLATURA DE SAIDA (pasta <DEST_ROOT>/Marca/SKU):
  SKU_Capa.jpg   -> foto de capa
  SKU_02.jpg, SKU_03.jpg, ...  -> angulos, em ordem
  SKU_Caixa.jpg  -> foto da caixa (fundo limpo)
  SKU.mp4        -> video (nome = so o SKU)
"""

import os
import io
import json
import base64
import smtplib
import datetime
from email.mime.text import MIMEText

import requests
from PIL import Image

# ============================================================
# CONFIGURACOES (lidas do ambiente / GitHub Secrets)
# ============================================================
DROPBOX_ACCESS_TOKEN = os.environ["DROPBOX_ACCESS_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
PHOTOROOM_API_KEY = os.environ["PHOTOROOM_API_KEY"]

DROPBOX_SOURCE_PATH = os.environ.get("DROPBOX_SOURCE_PATH", "/01_ENTRADA_BRUTA")
DROPBOX_DEST_ROOT = os.environ.get("DROPBOX_DEST_ROOT", "/MIDIA_FINAL")
DROPBOX_LOGOS_PATH = os.environ.get("DROPBOX_LOGOS_PATH", "/LOGOS")

GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "felipehxgn@gmail.com")

CANVAS_SIZE = 1200
MARGEM_RATIO = 0.15
LOGO_WIDTH_RATIO = 0.15
LOGO_MARGEM_RATIO = 0.15
LOTE_INCOMPLETO_MINUTOS = 10

VIDEO_EXTS = (".mp4", ".mov")

DBX_API = "https://api.dropboxapi.com/2"
DBX_CONTENT = "https://content.dropboxapi.com/2"
DBX_HEADERS_JSON = {
    "Authorization": f"Bearer {DROPBOX_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}


# ============================================================
# DROPBOX
# ============================================================
def dbx_listar_pasta(path):
    entradas = []
    resp = requests.post(
        f"{DBX_API}/files/list_folder",
        headers=DBX_HEADERS_JSON,
        json={"path": path},
        timeout=60,
    )
    if resp.status_code == 409:
        return []
    resp.raise_for_status()
    dados = resp.json()
    entradas.extend(dados.get("entries", []))
    while dados.get("has_more"):
        resp = requests.post(
            f"{DBX_API}/files/list_folder/continue",
            headers=DBX_HEADERS_JSON,
            json={"cursor": dados["cursor"]},
            timeout=60,
        )
        resp.raise_for_status()
        dados = resp.json()
        entradas.extend(dados.get("entries", []))

    arquivos = [e for e in entradas if e.get(".tag") == "file"]
    arquivos.sort(key=lambda e: e.get("server_modified", e["name"]))
    return arquivos


def dbx_baixar(path_lower):
    resp = requests.post(
        f"{DBX_CONTENT}/files/download",
        headers={
            "Authorization": f"Bearer {DROPBOX_ACCESS_TOKEN}",
            "Dropbox-API-Arg": json.dumps({"path": path_lower}),
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.content


def dbx_subir(path_destino, conteudo_bytes):
    resp = requests.post(
        f"{DBX_CONTENT}/files/upload",
        headers={
            "Authorization": f"Bearer {DROPBOX_ACCESS_TOKEN}",
            "Dropbox-API-Arg": json.dumps({"path": path_destino, "mode": "overwrite"}),
            "Content-Type": "application/octet-stream",
        },
        data=conteudo_bytes,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def dbx_garantir_pasta(path):
    resp = requests.post(
        f"{DBX_API}/files/create_folder_v2",
        headers=DBX_HEADERS_JSON,
        json={"path": path},
        timeout=30,
    )
    if resp.status_code not in (200, 409):
        resp.raise_for_status()


def dbx_mover(from_path, to_path):
    dbx_garantir_pasta(os.path.dirname(to_path))
    resp = requests.post(
        f"{DBX_API}/files/move_v2",
        headers=DBX_HEADERS_JSON,
        json={"from_path": from_path, "to_path": to_path, "autorename": True},
        timeout=30,
    )
    resp.raise_for_status()


def dbx_buscar_logo(marca):
    arquivos = dbx_listar_pasta(DROPBOX_LOGOS_PATH)
    alvo = marca.strip().upper()
    for f in arquivos:
        nome_sem_ext = os.path.splitext(f["name"])[0].strip().upper()
        if nome_sem_ext == alvo:
            return dbx_baixar(f["path_lower"])
    return None


# ============================================================
# OPENAI - LEITURA DE SKU E MARCA NA FOTO DA CAIXA
# ============================================================
def identificar_sku_marca(imagem_bytes):
    img_b64 = base64.b64encode(imagem_bytes).decode("utf-8")
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Esta e uma foto de uma caixa de autopeca. Leia o "
                            "codigo SKU e o nome da MARCA impressos na caixa. "
                            "Responda SOMENTE em JSON no formato "
                            '{"sku": "...", "marca": "...", "confiante": true/false} '
                            "sem nenhum texto adicional. 'confiante' deve ser false "
                            "se o texto estiver ilegivel, cortado ou ambiguo."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                    },
                ],
            }
        ],
        "max_tokens": 200,
    }
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    texto = resp.json()["choices"][0]["message"]["content"].strip()
    texto = texto.replace("```json", "").replace("```", "").strip()
    dados = json.loads(texto)
    return dados["sku"].strip(), dados["marca"].strip(), bool(dados.get("confiante", False))


# ============================================================
# PHOTOROOM - REMOCAO DE FUNDO
# ============================================================
def remover_fundo(imagem_bytes):
    resp = requests.post(
        "https://sdk.photoroom.com/v1/segment",
        headers={"x-api-key": PHOTOROOM_API_KEY},
        files={"image_file": ("imagem.jpg", imagem_bytes)},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


# ============================================================
# PIL - COMPOSICAO DA IMAGEM FINAL
# ============================================================
def compor_produto_em_canvas(imagem_bytes_sem_fundo, logo_bytes):
    produto = Image.open(io.BytesIO(imagem_bytes_sem_fundo)).convert("RGBA")
    bbox = produto.getbbox()
    if bbox:
        produto = produto.crop(bbox)

    area_util = CANVAS_SIZE * (1 - 2 * MARGEM_RATIO)
    escala = min(area_util / produto.width, area_util / produto.height)
    novo_w, novo_h = int(produto.width * escala), int(produto.height * escala)
    produto = produto.resize((novo_w, novo_h), Image.LANCZOS)

    canvas = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (255, 255, 255, 255))

    if logo_bytes:
        canvas = colar_logo(canvas, logo_bytes)

    pos = ((CANVAS_SIZE - novo_w) // 2, (CANVAS_SIZE - novo_h) // 2)
    canvas.paste(produto, pos, produto)
    return canvas


def colar_logo(canvas_rgba, logo_bytes):
    logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
    largura_alvo = int(canvas_rgba.width * LOGO_WIDTH_RATIO)
    escala = largura_alvo / logo.width
    logo = logo.resize((largura_alvo, int(logo.height * escala)), Image.LANCZOS)

    margem = int(canvas_rgba.width * LOGO_MARGEM_RATIO)
    pos = (margem, margem)
    canvas_rgba.paste(logo, pos, logo)
    return canvas_rgba


def imagem_para_jpg_bytes(imagem_rgba):
    fundo = Image.new("RGB", imagem_rgba.size, (255, 255, 255))
    fundo.paste(imagem_rgba, mask=imagem_rgba.split()[3])
    buf = io.BytesIO()
    fundo.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ============================================================
# ALERTAS POR E-MAIL
# ============================================================
def enviar_alerta(assunto, corpo):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD):
        print(f"[ALERTA - e-mail nao configurado] {assunto}: {corpo}")
        return
    msg = MIMEText(corpo)
    msg["Subject"] = assunto
    msg["From"] = GMAIL_USER
    msg["To"] = ALERT_EMAIL_TO
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(msg)


# ============================================================
# MONTAGEM DOS LOTES
# ============================================================
def eh_video(nome_arquivo):
    return nome_arquivo.lower().endswith(VIDEO_EXTS)


def montar_lotes(arquivos):
    lotes = []
    atual = []
    for f in arquivos:
        atual.append(f)
        if eh_video(f["name"]):
            lotes.append(atual)
            atual = []
    lote_pendente = atual if atual else None
    return lotes, lote_pendente


def checar_lote_pendente(lote_pendente):
    if not lote_pendente:
        return
    primeiro = lote_pendente[0]
    criado_em = datetime.datetime.fromisoformat(
        primeiro["server_modified"].replace("Z", "+00:00")
    )
    agora = datetime.datetime.now(datetime.timezone.utc)
    minutos = (agora - criado_em).total_seconds() / 60
    if minutos > LOTE_INCOMPLETO_MINUTOS:
        enviar_alerta(
            "Robo de Midias - lote incompleto",
            f"O lote iniciado com '{primeiro['name']}' esta ha {minutos:.0f} min "
            f"sem video de fechamento. Arquivos no lote: "
            f"{[a['name'] for a in lote_pendente]}",
        )


# ============================================================
# PROCESSAMENTO DE UM LOTE
# ============================================================
def processar_lote(lote):
    caixa = lote[0]
    capa = lote[1]
    angulos = lote[2:-1]
    video = lote[-1]

    caixa_bytes_original = dbx_baixar(caixa["path_lower"])
    sku, marca, confiante = identificar_sku_marca(caixa_bytes_original)

    logo_bytes = dbx_buscar_logo(marca)

    if not confiante or not logo_bytes:
        motivo = []
        if not confiante:
            motivo.append("SKU/marca nao lidos com confianca na foto da caixa")
        if not logo_bytes:
            motivo.append(f"logo da marca '{marca}' nao encontrado em {DROPBOX_LOGOS_PATH}")
        for arq in lote:
            destino = f"{DROPBOX_SOURCE_PATH}/_REVISAR/{arq['name']}"
            dbx_mover(arq["path_lower"], destino)
        enviar_alerta(
            "Robo de Midias - lote precisa de revisao manual",
            f"SKU detectado: {sku} | Marca detectada: {marca}\n"
            f"Motivo: {', '.join(motivo)}\n"
            f"Arquivos movidos para _REVISAR: {[a['name'] for a in lote]}",
        )
        print(f"Lote movido para _REVISAR (SKU={sku}, Marca={marca}): {motivo}")
        return

    pasta_sku = f"{DROPBOX_DEST_ROOT}/{marca}/{sku}"

    bruto = dbx_baixar(capa["path_lower"])
    sem_fundo = remover_fundo(bruto)
    canvas = compor_produto_em_canvas(sem_fundo, logo_bytes)
    dbx_subir(f"{pasta_sku}/{sku}_Capa.jpg", imagem_para_jpg_bytes(canvas))

    for i, arq in enumerate(angulos, start=2):
        bruto = dbx_baixar(arq["path_lower"])
        sem_fundo = remover_fundo(bruto)
        canvas = compor_produto_em_canvas(sem_fundo, logo_bytes)
        nome_final = f"{sku}_{str(i).zfill(2)}.jpg"
        dbx_subir(f"{pasta_sku}/{nome_final}", imagem_para_jpg_bytes(canvas))

    caixa_sem_fundo = remover_fundo(caixa_bytes_original)
    canvas_caixa = compor_produto_em_canvas(caixa_sem_fundo, logo_bytes)
    dbx_subir(f"{pasta_sku}/{sku}_Caixa.jpg", imagem_para_jpg_bytes(canvas_caixa))

    video_bytes = dbx_baixar(video["path_lower"])
    dbx_subir(f"{pasta_sku}/{sku}.mp4", video_bytes)

    for arq in lote:
        destino = f"{DROPBOX_SOURCE_PATH}/_PROCESSADOS/{arq['name']}"
        dbx_mover(arq["path_lower"], destino)

    print(f"Lote processado: SKU={sku} Marca={marca} ({len(lote)} arquivos)")


# ============================================================
# MAIN
# ============================================================
def main():
    arquivos = dbx_listar_pasta(DROPBOX_SOURCE_PATH)
    arquivos = [
        f for f in arquivos
        if "/_processados/" not in f["path_lower"] and "/_revisar/" not in f["path_lower"]
    ]

    lotes, lote_pendente = montar_lotes(arquivos)
    checar_lote_pendente(lote_pendente)

    for lote in lotes:
        try:
            processar_lote(lote)
        except Exception as e:
            nomes = [a["name"] for a in lote]
            enviar_alerta(
                "Robo de Midias - erro tecnico",
                f"Falha processando lote {nomes}: {repr(e)}",
            )
            print(f"ERRO no lote {nomes}: {repr(e)}")


if __name__ == "__main__":
    main()
