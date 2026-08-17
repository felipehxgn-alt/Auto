"""
Robo de Midias - Automacao de fotos/videos de produtos (ELRING/Hexagon e outras marcas)
Armazenamento: 100% Dropbox (entrada, saida e logos).

ORDEM DE CAPTURA (no celular/camera):
  1. Foto da caixa (SKU + marca impressos)
  2. Foto de capa do produto
  3..N. Fotos de angulo/detalhe do produto
  Ultimo arquivo = video (fecha o lote)

REGRA DE OURO: a edicao das fotos (remover fundo, montar no canvas, logo)
SEMPRE acontece, mesmo se a leitura da caixa falhar ou o logo nao for
encontrado. O que muda e SO o destino:
  - Leitura confiavel + logo encontrado  -> vai pra MIDIA_FINAL/SKU
    (pronto pra anunciar - organizado so por SKU, sem data nem marca,
    pra facilitar achar depois pela busca do Dropbox)
  - Qualquer duvida (leitura ruim, marca sem logo cadastrado, erro na
    identificacao) -> vai pra 01_ENTRADA_BRUTA/_REVISAR/AAAA-MM-DD/<pasta-do-lote>,
    com as fotos JA EDITADAS. Se so faltou o SKU certo, o usuario so
    precisa renomear a pasta - nao precisa reprocessar nada.

Cada lote (aprovado ou nao) sempre cai numa pasta PROPRIA, nunca solto
na raiz. Os arquivos originais (inclusive a foto da caixa) sempre saem
da entrada bruta - cada movimentacao e feita arquivo por arquivo, entao
uma falha isolada nunca trava ou deixa outro arquivo perdido.

NOMENCLATURA DENTRO DA PASTA DO LOTE:
  <ID>_Capa.jpg   -> foto de capa
  <ID>_02.jpg, <ID>_03.jpg, ...  -> angulos, em ordem
  <ID>_Caixa.jpg  -> foto da caixa (fundo limpo)
  <ID>.mp4        -> video
  (<ID> = SKU lido na caixa; se a leitura falhar totalmente, um
  identificador provisorio LOTE_AAAAMMDD_HHMMSS e usado no lugar)

Segredos esperados (GitHub Secrets -> variaveis de ambiente):
  DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN (recomendado
  - nao expira, o script renova o access token sozinho a cada execucao)
  OU DROPBOX_ACCESS_TOKEN (modo antigo, expira em poucas horas)
  OPENAI_API_KEY

Remocao de fundo: usa a biblioteca gratuita "rembg" (roda local, sem
API paga, sem chave). PHOTOROOM_API_KEY NAO e mais obrigatorio - so
fica reservado pra quando o recurso Gerada_IA (fundo por IA) for
implementado no futuro.

Variaveis opcionais (tem default):
  DROPBOX_SOURCE_PATH   (default: /01_ENTRADA_BRUTA - raiz de trabalho,
                          nunca recebe arquivo solto diretamente)
  DROPBOX_INBOX_PATH    (default: /01_ENTRADA_BRUTA/A_PROCESSAR - AQUI que
                          o usuario salva as fotos/video novos do dia)
  DROPBOX_DEST_ROOT     (default: /MIDIA_FINAL)
  DROPBOX_LOGOS_PATH    (default: /LOGOS)

Segredos OPCIONAIS (alertas por e-mail via Gmail SMTP - ainda nao
cadastrados):
  GMAIL_USER, GMAIL_APP_PASSWORD, ALERT_EMAIL_TO
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
# CONFIGURACOES
# ============================================================
def _limpo(valor):
    """Remove espacos/quebras de linha extras que podem vir de copiar e
    colar um Secret no GitHub - causa comum de 'malformed' ou 'invalid
    client' mesmo com o valor certo."""
    return valor.strip() if valor else valor


DROPBOX_APP_KEY = _limpo(os.environ.get("DROPBOX_APP_KEY"))
DROPBOX_APP_SECRET = _limpo(os.environ.get("DROPBOX_APP_SECRET"))
DROPBOX_REFRESH_TOKEN = _limpo(os.environ.get("DROPBOX_REFRESH_TOKEN"))
DROPBOX_ACCESS_TOKEN_FIXO = _limpo(os.environ.get("DROPBOX_ACCESS_TOKEN"))  # modo antigo, so fallback


def obter_dropbox_access_token():
    """Se houver refresh token configurado, troca ele por um access
    token novo (curta duracao, ~4h) a cada execucao - nunca expira de
    verdade porque e renovado sozinho toda vez que o robo roda. Se nao
    houver refresh token configurado, usa o DROPBOX_ACCESS_TOKEN fixo
    (modo antigo, so funciona por algumas horas)."""
    if DROPBOX_REFRESH_TOKEN and DROPBOX_APP_KEY and DROPBOX_APP_SECRET:
        resp = requests.post(
            "https://api.dropboxapi.com/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": DROPBOX_REFRESH_TOKEN,
            },
            auth=(DROPBOX_APP_KEY, DROPBOX_APP_SECRET),
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Falha renovando token do Dropbox: {resp.status_code} - {resp.text}")
        return resp.json()["access_token"]
    if DROPBOX_ACCESS_TOKEN_FIXO:
        return DROPBOX_ACCESS_TOKEN_FIXO
    raise RuntimeError(
        "Nenhuma credencial do Dropbox configurada: defina DROPBOX_REFRESH_TOKEN "
        "+ DROPBOX_APP_KEY + DROPBOX_APP_SECRET (recomendado), ou DROPBOX_ACCESS_TOKEN "
        "(temporario, expira em horas)."
    )


DROPBOX_ACCESS_TOKEN = obter_dropbox_access_token()

DBX_API = "https://api.dropboxapi.com/2"
DBX_CONTENT = "https://content.dropboxapi.com/2"
DBX_HEADERS_JSON = {
    "Authorization": f"Bearer {DROPBOX_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
PHOTOROOM_API_KEY = os.environ.get("PHOTOROOM_API_KEY")  # nao usado pra remover fundo (isso agora e rembg, gratis); fica reservado pro futuro recurso Gerada_IA

DROPBOX_SOURCE_PATH = os.environ.get("DROPBOX_SOURCE_PATH", "/01_ENTRADA_BRUTA")
# pasta onde o usuario efetivamente salva as fotos/video novos do dia -
# fica DENTRO de 01_ENTRADA_BRUTA, como irma de _REVISAR,
# assim a raiz de 01_ENTRADA_BRUTA nunca fica com arquivo solto
DROPBOX_INBOX_PATH = os.environ.get("DROPBOX_INBOX_PATH", f"{DROPBOX_SOURCE_PATH}/A_PROCESSAR")
DROPBOX_DEST_ROOT = os.environ.get("DROPBOX_DEST_ROOT", "/MIDIA_FINAL")
DROPBOX_LOGOS_PATH = os.environ.get("DROPBOX_LOGOS_PATH", "/LOGOS")

GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "felipehxgn@gmail.com")

CANVAS_SIZE = 1200
MARGEM_RATIO = 0.05
LOGO_MAX_RATIO = 0.18  # caixa maxima (largura E altura) que o logo pode ocupar - nunca estica alem disso, seja qual for o formato do logo original
LOGO_MARGEM_RATIO = 0.05
LOTE_INCOMPLETO_MINUTOS = 10

VIDEO_EXTS = (".mp4", ".mov")


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


def dbx_mover_um_arquivo(from_path, to_path):
    """Move um unico arquivo. Nunca deixa uma falha isolada travar o
    restante do lote - quem chama decide o que fazer se der erro."""
    dbx_garantir_pasta(os.path.dirname(to_path))
    resp = requests.post(
        f"{DBX_API}/files/move_v2",
        headers=DBX_HEADERS_JSON,
        json={"from_path": from_path, "to_path": to_path, "autorename": True},
        timeout=30,
    )
    resp.raise_for_status()


def mover_lote_com_tolerancia(lote, pasta_destino):
    """Move cada arquivo original do lote pra pasta_destino, um de cada
    vez. Se um falhar, registra e continua com os outros - nenhum
    arquivo original fica perdido/esquecido na entrada bruta por causa
    de erro em outro arquivo do mesmo lote."""
    falhas = []
    for arq in lote:
        destino = f"{pasta_destino}/{arq['name']}"
        try:
            dbx_mover_um_arquivo(arq["path_lower"], destino)
        except Exception as e:
            falhas.append((arq["name"], repr(e)))
    if falhas:
        enviar_alerta(
            "Robo de Midias - falha movendo arquivos originais",
            f"Pasta destino: {pasta_destino}\nFalhas: {falhas}",
        )


def dbx_buscar_logo(marca):
    if not marca:
        return None
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
    """Retorna (sku_ou_None, marca_ou_None, confiante:bool,
    rotacao_graus:int). rotacao_graus e quantos graus (sentido
    horario) a foto precisa girar pra ficar reta - 0, 90, 180 ou 270.
    Nunca inventa sku/marca - se o modelo nao tiver certeza,
    confiante=False."""
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
                            "codigo SKU e o nome da MARCA impressos na caixa, "
                            "mesmo que a foto esteja de lado ou de cabeca pra "
                            "baixo. Tambem diga quantos graus, no sentido "
                            "horario, a foto precisa girar pra o texto ficar "
                            "na posicao normal de leitura (0, 90, 180 ou 270). "
                            "Responda SOMENTE em JSON no formato "
                            '{"sku": "...", "marca": "...", "confiante": true/false, '
                            '"rotacao": 0} sem nenhum texto adicional. Se nao '
                            'conseguir ler algo, use null nesse campo. '
                            '"confiante" deve ser false se o texto estiver '
                            "ilegivel, cortado ou ambiguo."
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
    try:
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
        sku = (dados.get("sku") or "").strip() or None
        marca = (dados.get("marca") or "").strip() or None
        confiante = bool(dados.get("confiante", False)) and sku and marca
        rotacao = dados.get("rotacao", 0) or 0
        rotacao = rotacao if rotacao in (0, 90, 180, 270) else 0
        return sku, marca, confiante, rotacao
    except Exception as e:
        print(f"Falha lendo SKU/marca na caixa: {repr(e)}")
        return None, None, False, 0


# ============================================================
# REMOCAO DE FUNDO (rembg - biblioteca gratuita, roda local, sem API paga)
# ============================================================
def corrigir_rotacao(imagem_bytes, graus_horario):
    """Gira a foto pra ficar reta, se a IA detectou que ela esta de
    lado/cabeca pra baixo. graus_horario: 0, 90, 180 ou 270."""
    if not graus_horario:
        return imagem_bytes
    img = Image.open(io.BytesIO(imagem_bytes))
    img = img.convert("RGB").rotate(-graus_horario, expand=True)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def remover_fundo(imagem_bytes):
    from rembg import remove as rembg_remove
    return rembg_remove(imagem_bytes)


# ============================================================
# PIL - COMPOSICAO DA IMAGEM FINAL
# ============================================================
def _retangulo_logo(canvas_size, logo_bytes):
    """Calcula o retangulo (x1,y1,x2,y2) que o logo vai ocupar no canto
    superior esquerdo, sem precisar abrir/colar de verdade - usado so
    pra checar sobreposicao com a peca antes de decidir o tamanho dela."""
    logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
    caixa_max = int(canvas_size * LOGO_MAX_RATIO)
    escala = min(caixa_max / logo.width, caixa_max / logo.height)
    largura, altura = int(logo.width * escala), int(logo.height * escala)
    margem = int(canvas_size * LOGO_MARGEM_RATIO)
    return (margem, margem, margem + largura, margem + altura)


def _area_sobreposicao(retangulo_a, retangulo_b):
    ax1, ay1, ax2, ay2 = retangulo_a
    bx1, by1, bx2, by2 = retangulo_b
    largura_i = max(0, min(ax2, bx2) - max(ax1, bx1))
    altura_i = max(0, min(ay2, by2) - max(ay1, by1))
    return largura_i * altura_i


def compor_produto_em_canvas(imagem_bytes_sem_fundo, logo_bytes):
    produto = Image.open(io.BytesIO(imagem_bytes_sem_fundo)).convert("RGBA")
    bbox = produto.getbbox()
    if bbox:
        produto = produto.crop(bbox)

    area_util = CANVAS_SIZE * (1 - 2 * MARGEM_RATIO)

    # se a peca for cobrir demais o canto do logo, encolhe um pouco so
    # nesse caso - assim o logo continua aparecendo, sem precisar de
    # efeito de "atras/flutuando" que nao escala bem pra milhares de fotos
    if logo_bytes:
        escala_normal = min(area_util / produto.width, area_util / produto.height)
        w_normal, h_normal = produto.width * escala_normal, produto.height * escala_normal
        pos_normal = ((CANVAS_SIZE - w_normal) / 2, (CANVAS_SIZE - h_normal) / 2)
        rect_produto = (pos_normal[0], pos_normal[1], pos_normal[0] + w_normal, pos_normal[1] + h_normal)
        rect_logo = _retangulo_logo(CANVAS_SIZE, logo_bytes)
        area_logo = (rect_logo[2] - rect_logo[0]) * (rect_logo[3] - rect_logo[1])
        cobertura = _area_sobreposicao(rect_produto, rect_logo) / area_logo if area_logo else 0
        if cobertura > 0.35:
            area_util = area_util * 0.85  # encolhe ~15% so quando realmente cobre demais o logo

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

    # caixa maxima padronizada: nenhum logo, seja largo, quadrado ou
    # vertical, ultrapassa essa dimensao - garante tamanho visual
    # consistente entre marcas diferentes
    caixa_max = int(canvas_rgba.width * LOGO_MAX_RATIO)
    escala = min(caixa_max / logo.width, caixa_max / logo.height)
    novo_w, novo_h = int(logo.width * escala), int(logo.height * escala)
    logo = logo.resize((novo_w, novo_h), Image.LANCZOS)

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


def _carregar_fonte(tamanho):
    """Tenta carregar uma fonte bonita (DejaVu Bold, comum em runners
    Linux); se nao achar, usa a fonte padrao do PIL (mais feia, mas
    nunca quebra)."""
    from PIL import ImageFont
    caminhos_possiveis = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for caminho in caminhos_possiveis:
        try:
            return ImageFont.truetype(caminho, tamanho)
        except Exception:
            continue
    return ImageFont.load_default()


def aplicar_selo_original(canvas_rgba, caixa_bytes_original):
    """So na foto de CAPA: cola uma miniatura da foto real da caixa +
    faixa 'PRODUTO ORIGINAL' por baixo, no canto inferior direito
    (oposto ao logo, que fica no superior esquerdo)."""
    from PIL import ImageDraw

    tam_thumb = int(canvas_rgba.width * LOGO_MAX_RATIO)
    faixa_altura = int(tam_thumb * 0.28)

    caixa_img = Image.open(io.BytesIO(caixa_bytes_original)).convert("RGB")
    lado = min(caixa_img.width, caixa_img.height)
    esquerda = (caixa_img.width - lado) // 2
    topo = (caixa_img.height - lado) // 2
    caixa_img = caixa_img.crop((esquerda, topo, esquerda + lado, topo + lado))
    caixa_img = caixa_img.resize((tam_thumb, tam_thumb), Image.LANCZOS)

    selo = Image.new("RGBA", (tam_thumb, tam_thumb + faixa_altura), (255, 255, 255, 255))
    selo.paste(caixa_img, (0, 0))

    draw = ImageDraw.Draw(selo)
    draw.rectangle([0, tam_thumb, tam_thumb, tam_thumb + faixa_altura], fill=(20, 20, 20, 255))
    fonte = _carregar_fonte(max(10, int(faixa_altura * 0.45)))
    texto = "PRODUTO ORIGINAL"
    bbox = draw.textbbox((0, 0), texto, font=fonte)
    texto_w, texto_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if texto_w > tam_thumb * 0.95:
        # texto nao coube na largura do thumb - encolhe a fonte proporcionalmente
        fonte = _carregar_fonte(max(8, int(faixa_altura * 0.45 * (tam_thumb * 0.95 / texto_w))))
        bbox = draw.textbbox((0, 0), texto, font=fonte)
        texto_w, texto_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pos_texto = ((tam_thumb - texto_w) // 2, tam_thumb + (faixa_altura - texto_h) // 2 - bbox[1])
    draw.text(pos_texto, texto, fill=(255, 255, 255, 255), font=fonte)

    # borda branca fina ao redor de tudo, pra destacar do fundo do produto
    borda = 4
    selo_com_borda = Image.new("RGBA", (selo.width + borda * 2, selo.height + borda * 2), (255, 255, 255, 255))
    selo_com_borda.paste(selo, (borda, borda))

    margem = int(canvas_rgba.width * LOGO_MARGEM_RATIO)
    pos = (canvas_rgba.width - selo_com_borda.width - margem, canvas_rgba.height - selo_com_borda.height - margem)
    canvas_rgba.paste(selo_com_borda, pos, selo_com_borda)
    return canvas_rgba


def aplicar_selo_garantia(canvas_rgba):
    """So na foto de CAPA: faixa 'GARANTIA 90 DIAS DE FABRICA' no canto
    inferior esquerdo - so texto, sem miniatura, aproveitando o espaco
    em branco que sobra nesse canto."""
    from PIL import ImageDraw

    largura_max = int(canvas_rgba.width * (LOGO_MAX_RATIO + 0.05))
    altura_faixa = int(canvas_rgba.width * LOGO_MAX_RATIO * 0.28)

    fonte = _carregar_fonte(max(10, int(altura_faixa * 0.42)))
    texto = "GARANTIA 90 DIAS\nDE FABRICA"
    draw_temp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    bbox = draw_temp.multiline_textbbox((0, 0), texto, font=fonte, align="center")
    texto_w, texto_h = bbox[2] - bbox[0], bbox[3] - bbox[1]

    padding = int(altura_faixa * 0.18)
    largura_selo = min(largura_max, texto_w + padding * 2)
    altura_selo = texto_h + padding * 2

    selo = Image.new("RGBA", (largura_selo, altura_selo), (20, 20, 20, 255))
    draw = ImageDraw.Draw(selo)
    pos_texto = ((largura_selo - texto_w) // 2 - bbox[0], (altura_selo - texto_h) // 2 - bbox[1])
    draw.multiline_text(pos_texto, texto, fill=(255, 255, 255, 255), font=fonte, align="center")

    borda = 4
    selo_com_borda = Image.new("RGBA", (selo.width + borda * 2, selo.height + borda * 2), (255, 255, 255, 255))
    selo_com_borda.paste(selo, (borda, borda))

    margem = int(canvas_rgba.width * LOGO_MARGEM_RATIO)
    pos = (margem, canvas_rgba.height - selo_com_borda.height - margem)
    canvas_rgba.paste(selo_com_borda, pos, selo_com_borda)
    return canvas_rgba


def editar_produto(bytes_brutos, logo_bytes, selo_caixa_bytes=None):
    """Remove fundo + monta no canvas. Se a remocao de fundo (rembg)
    falhar, a foto continua indo com fundo original (evita perder a
    foto), mas registra o erro completo no log pra facilitar
    diagnostico. selo_caixa_bytes: se informado (so na capa), cola o
    selo 'PRODUTO ORIGINAL' com miniatura da caixa."""
    try:
        sem_fundo = remover_fundo(bytes_brutos)
    except Exception as e:
        import traceback
        print(f"Remocao de fundo (rembg) falhou, usando imagem original: {repr(e)}")
        traceback.print_exc()
        sem_fundo = bytes_brutos
    canvas = compor_produto_em_canvas(sem_fundo, logo_bytes)
    if selo_caixa_bytes:
        canvas = aplicar_selo_original(canvas, selo_caixa_bytes)
        canvas = aplicar_selo_garantia(canvas)
    return imagem_para_jpg_bytes(canvas)


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
def verificar_qualidade_foto(imagem_editada_bytes):
    """Usa a mesma IA de visao pra checar se a foto (ja editada) tem
    algum problema obvio: mao/dedo segurando a peca, ou coisa clara
    demais errada no enquadramento. Retorna (ok:bool, motivo:str).
    Em caso de erro tecnico, assume ok=True (nao bloqueia por causa de
    falha da checagem em si - so bloqueia por problema real)."""
    img_b64 = base64.b64encode(imagem_editada_bytes).decode("utf-8")
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Esta e uma foto de produto editada pra catalogo de "
                            "e-commerce (fundo branco). Verifique SO isto: aparece "
                            "mao, dedo ou pessoa segurando a peca na foto? Responda "
                            "SOMENTE em JSON: "
                            '{"tem_problema": true/false, "motivo": "..."} '
                            "sem texto adicional. motivo deve ser curto (poucas "
                            "palavras) ou vazio se nao houver problema."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                    },
                ],
            }
        ],
        "max_tokens": 100,
    }
    try:
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
        tem_problema = bool(dados.get("tem_problema", False))
        motivo = dados.get("motivo") or ""
        return (not tem_problema), motivo
    except Exception as e:
        print(f"Checagem de qualidade da foto falhou (nao bloqueia): {repr(e)}")
        return True, ""


def processar_lote(lote):
    caixa = lote[0]
    capa = lote[1]
    angulos = lote[2:-1]
    video = lote[-1]

    caixa_bytes_original = dbx_baixar(caixa["path_lower"])
    sku, marca, confiante, rotacao = identificar_sku_marca(caixa_bytes_original)
    if rotacao:
        caixa_bytes_original = corrigir_rotacao(caixa_bytes_original, rotacao)
        print(f"Foto da caixa girada {rotacao} graus pra ficar reta")

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    data_hoje = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    identificador = sku or f"LOTE_{timestamp}"
    logo_bytes = dbx_buscar_logo(marca)

    aprovado = bool(confiante and logo_bytes)

    if aprovado:
        pasta_lote = f"{DROPBOX_DEST_ROOT}/{identificador}"
    else:
        pasta_lote = f"{DROPBOX_SOURCE_PATH}/_REVISAR/{data_hoje}/{identificador}"

    # pasta dos ORIGINAIS: sempre uma subpasta _ORIGINAIS dentro da propria
    # pasta do lote (MIDIA_FINAL/<SKU>/_ORIGINAIS ou _REVISAR/.../_ORIGINAIS)
    # - um unico padrao, tudo do mesmo lote junto num so lugar
    pasta_originais = f"{pasta_lote}/_ORIGINAIS"

    # --- Edita e sobe TODAS as fotos, aprovado ou nao ---
    fotos_com_problema = []

    def subir_foto_produto(nome_arquivo, bytes_editados):
        """Se o lote foi aprovado, checa qualidade individual da foto -
        problema (ex: mao na foto) manda so essa foto pra _VERIFICAR,
        sem afetar as outras fotos boas do mesmo lote."""
        if aprovado:
            ok, motivo = verificar_qualidade_foto(bytes_editados)
            if not ok:
                fotos_com_problema.append((nome_arquivo, motivo))
                dbx_subir(f"{pasta_lote}/_VERIFICAR/{nome_arquivo}", bytes_editados)
                return
        dbx_subir(f"{pasta_lote}/{nome_arquivo}", bytes_editados)

    bruto = dbx_baixar(capa["path_lower"])
    subir_foto_produto(
        f"{identificador}_Capa.jpg",
        editar_produto(bruto, logo_bytes, selo_caixa_bytes=caixa_bytes_original),
    )

    for i, arq in enumerate(angulos, start=2):
        bruto = dbx_baixar(arq["path_lower"])
        nome_final = f"{identificador}_{str(i).zfill(2)}.jpg"
        subir_foto_produto(nome_final, editar_produto(bruto, logo_bytes))

    dbx_subir(
        f"{pasta_lote}/{identificador}_Caixa.jpg",
        editar_produto(caixa_bytes_original, logo_bytes),
    )

    video_bytes = dbx_baixar(video["path_lower"])
    dbx_subir(f"{pasta_lote}/{identificador}.mp4", video_bytes)

    # --- Move TODOS os originais (inclusive a caixa) pra fora da entrada ---
    mover_lote_com_tolerancia(lote, pasta_originais)

    if not aprovado:
        motivo = []
        if not confiante:
            motivo.append("SKU/marca nao lidos com confianca na foto da caixa")
        if not logo_bytes:
            motivo.append(f"logo da marca '{marca or '(nao identificada)'}' nao encontrado em {DROPBOX_LOGOS_PATH}")
        enviar_alerta(
            "Robo de Midias - lote precisa de revisao manual",
            f"Pasta: {pasta_lote}\n"
            f"SKU detectado: {sku or '(nenhum)'} | Marca detectada: {marca or '(nenhuma)'}\n"
            f"Motivo: {', '.join(motivo)}\n"
            f"Fotos ja editadas estao nessa pasta - so falta renomear/ajustar.",
        )
        print(f"Lote em _REVISAR ({pasta_lote}): {motivo}")
    else:
        if fotos_com_problema:
            enviar_alerta(
                "Robo de Midias - fotos com problema dentro de lote aprovado",
                f"Pasta: {pasta_lote}\n"
                f"O lote foi aprovado, mas estas fotos ficaram em _VERIFICAR: "
                f"{fotos_com_problema}",
            )
        print(f"Lote publicado: {pasta_lote}" + (f" ({len(fotos_com_problema)} foto(s) em _VERIFICAR)" if fotos_com_problema else ""))


# ============================================================
# MAIN
# ============================================================
def main():
    arquivos = dbx_listar_pasta(DROPBOX_INBOX_PATH)
    arquivos = [
        f for f in arquivos
        if "/_processados/" not in f["path_lower"] and "/_revisar/" not in f["path_lower"]
    ]

    lotes, lote_pendente = montar_lotes(arquivos)
    checar_lote_pendente(lote_pendente)

    for lote in lotes:
        if len(lote) < 3:
            # video "orfao" (sem caixa/capa por perto) - nao da pra
            # processar, so joga pra revisao manual sem tentar indexar
            nomes = [a["name"] for a in lote]
            timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
            data_hoje = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
            pasta_revisar = f"{DROPBOX_SOURCE_PATH}/_REVISAR/{data_hoje}/LOTE_INCOMPLETO_{timestamp}"
            mover_lote_com_tolerancia(lote, pasta_revisar)
            enviar_alerta(
                "Robo de Midias - lote invalido/orfao",
                f"Arquivos sem caixa/capa correspondentes, movidos para "
                f"{pasta_revisar}: {nomes}",
            )
            print(f"Lote invalido (orfao) movido para {pasta_revisar}: {nomes}")
            continue
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
