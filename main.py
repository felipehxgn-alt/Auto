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
  DROPBOX_ROOT           (default: /AUTOMACAO_ANUNCIOS - pasta guarda-chuva
                          que reune tudo do projeto)
  DROPBOX_SOURCE_PATH   (default: /AUTOMACAO_ANUNCIOS/01_ENTRADA_BRUTA -
                          raiz de trabalho, nunca recebe arquivo solto)
  DROPBOX_INBOX_PATH    (default: .../01_ENTRADA_BRUTA/A_PROCESSAR - AQUI
                          que o usuario salva as fotos/video novos do dia)
  DROPBOX_DEST_ROOT     (default: /AUTOMACAO_ANUNCIOS/MIDIA_FINAL)
  DROPBOX_LOGOS_PATH    (default: /AUTOMACAO_ANUNCIOS/LOGOS - marcas gerais)
  DROPBOX_LOGOS_HEXAGON_PATH (default: /AUTOMACAO_ANUNCIOS/LOGOS HEXAGON -
                          checada ANTES da geral, produtos marca propria)

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

DROPBOX_ROOT = os.environ.get("DROPBOX_ROOT", "/AUTOMACAO_ANUNCIOS")  # pasta guarda-chuva que reune tudo do projeto

DROPBOX_SOURCE_PATH = os.environ.get("DROPBOX_SOURCE_PATH", f"{DROPBOX_ROOT}/01_ENTRADA_BRUTA")
# pasta onde o usuario efetivamente salva as fotos/video novos do dia -
# fica DENTRO de 01_ENTRADA_BRUTA, como irma de _REVISAR,
# assim a raiz de 01_ENTRADA_BRUTA nunca fica com arquivo solto
DROPBOX_INBOX_PATH = os.environ.get("DROPBOX_INBOX_PATH", f"{DROPBOX_SOURCE_PATH}/A_PROCESSAR")
DROPBOX_DEST_ROOT = os.environ.get("DROPBOX_DEST_ROOT", f"{DROPBOX_ROOT}/MIDIA_FINAL")
DROPBOX_LOGOS_PATH = os.environ.get("DROPBOX_LOGOS_PATH", f"{DROPBOX_ROOT}/LOGOS")
# pasta separada so pra logos de produtos da marca propria Hexagon -
# checada ANTES da pasta geral de logos, pra dar prioridade
DROPBOX_LOGOS_HEXAGON_PATH = os.environ.get("DROPBOX_LOGOS_HEXAGON_PATH", f"{DROPBOX_ROOT}/LOGOS HEXAGON")

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


def _buscar_logo_em(pasta, alvo):
    arquivos = dbx_listar_pasta(pasta)
    for f in arquivos:
        nome_sem_ext = os.path.splitext(f["name"])[0].strip().upper()
        if nome_sem_ext == alvo:
            return dbx_baixar(f["path_lower"])
    return None


def dbx_buscar_logo(marca):
    if not marca:
        return None
    alvo = marca.strip().upper()
    # pasta Hexagon primeiro (prioridade pra produtos da marca propria)
    logo = _buscar_logo_em(DROPBOX_LOGOS_HEXAGON_PATH, alvo)
    if logo:
        return logo
    return _buscar_logo_em(DROPBOX_LOGOS_PATH, alvo)


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


COR_HEXAGON = (17, 85, 165, 255)  # azul da marca Hexagon
COR_ELRING = (196, 30, 30, 255)   # vermelho da marca ELRING


def _desenhar_texto_curvo(canvas_rgba, texto, centro, raio, fonte, cor):
    """Desenha texto acompanhando o arco superior de um circulo -
    tecnica classica de 'carimbo redondo': cada letra e desenhada
    numa mini-imagem separada, rotacionada no angulo certo e colada
    na posicao correspondente do circulo."""
    import math
    from PIL import ImageDraw

    cx, cy = centro
    draw_temp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    larguras = []
    for ch in texto:
        bbox = draw_temp.textbbox((0, 0), ch, font=fonte)
        larguras.append(max(bbox[2] - bbox[0], 4) + 3)
    largura_total_px = sum(larguras)
    angulo_total = largura_total_px / raio
    angulo_atual = -angulo_total / 2

    for ch, larg in zip(texto, larguras):
        angulo_char = angulo_atual + (larg / raio) / 2
        ang_rad = angulo_char - math.pi / 2
        x = cx + raio * math.cos(ang_rad)
        y = cy + raio * math.sin(ang_rad)

        tam_tmp = 70
        char_img = Image.new("RGBA", (tam_tmp, tam_tmp), (0, 0, 0, 0))
        d = ImageDraw.Draw(char_img)
        d.text((tam_tmp / 2, tam_tmp / 2), ch, font=fonte, fill=cor, anchor="mm")
        rotacao_graus = -math.degrees(angulo_char)
        char_rotado = char_img.rotate(rotacao_graus, resample=Image.BICUBIC, center=(tam_tmp / 2, tam_tmp / 2))
        canvas_rgba.paste(char_rotado, (int(x - tam_tmp / 2), int(y - tam_tmp / 2)), char_rotado)
        angulo_atual += larg / raio


def _desenhar_icone_escudo_check(tamanho, cor):
    """Desenha um icone de escudo com check dentro, no mesmo estilo
    vetorial de linha fina usado no resto do selo - usado na Garantia
    pra nao repetir o icone H duas vezes."""
    from PIL import ImageDraw

    img = Image.new("RGBA", (tamanho, tamanho), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    largura_linha = max(2, int(tamanho * 0.06))

    # escudo: pentagono com base arredondada (ponta pra baixo)
    w, h = tamanho, tamanho
    pontos = [
        (w * 0.5, h * 0.04),
        (w * 0.92, h * 0.20),
        (w * 0.92, h * 0.52),
        (w * 0.5, h * 0.97),
        (w * 0.08, h * 0.52),
        (w * 0.08, h * 0.20),
    ]
    draw.line(pontos + [pontos[0]], fill=cor, width=largura_linha, joint="curve")

    # check dentro do escudo
    check = [(w * 0.30, h * 0.48), (w * 0.45, h * 0.63), (w * 0.72, h * 0.32)]
    draw.line(check, fill=cor, width=largura_linha, joint="curve")
    return img


def _criar_selo_redondo(tamanho, texto_arco, texto_central, icone_img, cor):
    """Monta um carimbo redondo: anel duplo, texto curvo no arco
    superior, texto grande central, e um icone (ja pronto, imagem PIL
    RGBA) centralizado abaixo do texto central. Cor e parametro pra
    poder variar por marca (ex: azul Hexagon, vermelho ELRING)."""
    from PIL import ImageDraw

    selo = Image.new("RGBA", (tamanho, tamanho), (0, 0, 0, 0))
    draw = ImageDraw.Draw(selo)
    centro = (tamanho // 2, tamanho // 2)
    borda_externa = max(3, int(tamanho * 0.022))

    draw.ellipse([borda_externa, borda_externa, tamanho - borda_externa, tamanho - borda_externa],
                 outline=cor, width=max(2, int(tamanho * 0.022)))
    draw.ellipse([int(tamanho * 0.05), int(tamanho * 0.05), tamanho - int(tamanho * 0.05), tamanho - int(tamanho * 0.05)],
                 outline=cor, width=max(1, int(tamanho * 0.007)))

    fonte_arco = _carregar_fonte(max(9, int(tamanho * 0.065)))
    _desenhar_texto_curvo(selo, texto_arco, centro, int(tamanho * 0.38), fonte_arco, cor)

    fonte_central = _carregar_fonte(max(16, int(tamanho * 0.13)))
    bbox = draw.textbbox((0, 0), texto_central, font=fonte_central)
    texto_w, texto_h = bbox[2] - bbox[0], bbox[3] - bbox[1]

    if icone_img:
        y_icone = int(tamanho * 0.30)
        selo.paste(icone_img, (centro[0] - icone_img.width // 2, y_icone), icone_img)
        y_texto = y_icone + icone_img.height + int(tamanho * 0.03)
    else:
        y_texto = int(tamanho * 0.42)

    draw.text((centro[0] - texto_w / 2 - bbox[0], y_texto - bbox[1]), texto_central, font=fonte_central, fill=cor)
    return selo


def aplicar_selo_original(canvas_rgba, icone_hexagon_bytes, cor):
    """So na foto de CAPA: carimbo redondo 'PRODUTO ORIGINAL' + icone H
    da Hexagon, no canto inferior direito (oposto ao logo). Cor varia
    por marca (azul Hexagon, vermelho ELRING)."""
    tamanho = int(canvas_rgba.width * LOGO_MAX_RATIO * 1.15)
    icone_h = None
    if icone_hexagon_bytes:
        try:
            icone_h = Image.open(io.BytesIO(icone_hexagon_bytes)).convert("RGBA")
            tam_icone = int(tamanho * 0.22)
            icone_h.thumbnail((tam_icone, tam_icone), Image.LANCZOS)
        except Exception:
            icone_h = None
    selo = _criar_selo_redondo(tamanho, "PRODUTO ORIGINAL", "100%", icone_h, cor)
    margem = int(canvas_rgba.width * LOGO_MARGEM_RATIO)
    pos = (canvas_rgba.width - tamanho - margem, canvas_rgba.height - tamanho - margem)
    canvas_rgba.paste(selo, pos, selo)
    return canvas_rgba


def aplicar_selo_garantia(canvas_rgba, icone_hexagon_bytes, cor):
    """So na foto de CAPA: carimbo redondo 'GARANTIA DE FABRICA' + icone
    de escudo+check (nao repete o H, ja usado no selo Original), no
    canto inferior esquerdo. Cor varia por marca."""
    tamanho = int(canvas_rgba.width * LOGO_MAX_RATIO * 1.15)
    icone_escudo = _desenhar_icone_escudo_check(int(tamanho * 0.22), cor)
    selo = _criar_selo_redondo(tamanho, "GARANTIA DE FABRICA", "90 DIAS", icone_escudo, cor)
    margem = int(canvas_rgba.width * LOGO_MARGEM_RATIO)
    pos = (margem, canvas_rgba.height - tamanho - margem)
    canvas_rgba.paste(selo, pos, selo)
    return canvas_rgba


def editar_produto(bytes_brutos, logo_bytes, aplicar_selos=False, icone_hexagon_bytes=None, cor_selo=COR_HEXAGON):
    """Remove fundo + monta no canvas. Se a remocao de fundo (rembg)
    falhar, a foto continua indo com fundo original (evita perder a
    foto), mas registra o erro completo no log pra facilitar
    diagnostico. aplicar_selos=True (so na capa): cola os 2 selos
    redondos (Produto Original + Garantia de Fabrica), com o icone H
    da Hexagon se disponivel (senao os selos saem so com texto).
    cor_selo: azul Hexagon por padrao, vermelho pra produtos ELRING."""
    try:
        sem_fundo = remover_fundo(bytes_brutos)
    except Exception as e:
        import traceback
        print(f"Remocao de fundo (rembg) falhou, usando imagem original: {repr(e)}")
        traceback.print_exc()
        sem_fundo = bytes_brutos
    canvas = compor_produto_em_canvas(sem_fundo, logo_bytes)
    if aplicar_selos:
        canvas = aplicar_selo_original(canvas, icone_hexagon_bytes, cor_selo)
        canvas = aplicar_selo_garantia(canvas, icone_hexagon_bytes, cor_selo)
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
    identificador = sku or f"LOTE_{timestamp}"
    logo_bytes = dbx_buscar_logo(marca)

    aprovado = bool(confiante and logo_bytes)

    if aprovado:
        pasta_lote = f"{DROPBOX_DEST_ROOT}/{identificador}"
    else:
        # organiza por MOTIVO do problema, nao por data - assim da pra ir
        # direto na causa (ex: "SEM_LOGO_NGK" agrupa todos os lotes que so
        # faltam o logo dessa marca especifica)
        if not confiante and not logo_bytes:
            motivo_pasta = "SKU_ILEGIVEL_SEM_LOGO"
        elif not confiante:
            motivo_pasta = "SKU_ILEGIVEL"
        else:
            marca_pasta = (marca or "MARCA_DESCONHECIDA").strip().upper().replace(" ", "_")
            motivo_pasta = f"SEM_LOGO_{marca_pasta}"
        pasta_lote = f"{DROPBOX_SOURCE_PATH}/_REVISAR/{motivo_pasta}/{identificador}"

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

    icone_hexagon_bytes = _buscar_logo_em(DROPBOX_LOGOS_HEXAGON_PATH, "HEXAGON LOGO")
    cor_selo = COR_ELRING if (marca and marca.strip().upper() == "ELRING") else COR_HEXAGON

    bruto = dbx_baixar(capa["path_lower"])
    subir_foto_produto(
        f"{identificador}_Capa.jpg",
        editar_produto(bruto, logo_bytes, aplicar_selos=True, icone_hexagon_bytes=icone_hexagon_bytes, cor_selo=cor_selo),
    )

    for i, arq in enumerate(angulos, start=2):
        bruto = dbx_baixar(arq["path_lower"])
        nome_final = f"{identificador}_{str(i).zfill(2)}.jpg"
        subir_foto_produto(nome_final, editar_produto(bruto, logo_bytes))

    dbx_subir(
        f"{pasta_lote}/{identificador}_Caixa.jpg",
        editar_produto(caixa_bytes_original, logo_bytes),
    )

    # --- Banners fixos obrigatorios (penultima e ultima foto), em TODA
    # marca que vendemos, exceto ELRING (que tem tratamento proprio) ---
    if aprovado and marca and marca.strip().upper() != "ELRING":
        entrega_bytes = _buscar_logo_em(DROPBOX_LOGOS_HEXAGON_PATH, "ENTREGA HEXAGON")
        logo_hex_bytes = icone_hexagon_bytes
        if entrega_bytes:
            img = Image.open(io.BytesIO(entrega_bytes)).convert("RGB")
            buf = io.BytesIO(); img.save(buf, format="JPEG", quality=95)
            dbx_subir(f"{pasta_lote}/{identificador}_ZZ_EntregaHexagon.jpg", buf.getvalue())
        else:
            print(f"AVISO: banner 'ENTREGA HEXAGON' nao encontrado em {DROPBOX_LOGOS_HEXAGON_PATH}")
        if logo_hex_bytes:
            img = Image.open(io.BytesIO(logo_hex_bytes)).convert("RGB")
            buf = io.BytesIO(); img.save(buf, format="JPEG", quality=95)
            dbx_subir(f"{pasta_lote}/{identificador}_ZZZ_LogoHexagon.jpg", buf.getvalue())
        else:
            print(f"AVISO: banner 'HEXAGON LOGO' nao encontrado em {DROPBOX_LOGOS_HEXAGON_PATH}")

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
            pasta_revisar = f"{DROPBOX_SOURCE_PATH}/_REVISAR/LOTE_INCOMPLETO/LOTE_{timestamp}"
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
