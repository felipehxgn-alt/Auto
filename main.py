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
  GOOGLE_SERVICE_ACCOUNT_JSON, PLANILHA_ANUNCIOS_ML_ID (integracao com a
  planilha de anuncios - aba Staging)

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

from sheets_integration import adicionar_ao_staging, STATUS_AGUARDANDO_PESQUISA, subir_video_drive

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


_cache_selos_reais = {}


def _buscar_selo_real(nome_arquivo, tamanho):
    """Busca um selo real (imagem propria enviada pelo usuario, ex:
    Confianca/Garantia) em LOGOS HEXAGON, remove o fundo (rembg - as
    imagens originais tem fundo branco solido, nao transparente) e
    redimensiona pra caber no tamanho do selo. Cacheia em memoria por
    nome de arquivo, ja que e o mesmo selo repetido em todo o lote."""
    if nome_arquivo in _cache_selos_reais:
        bruto = _cache_selos_reais[nome_arquivo]
    else:
        bruto = _buscar_logo_em(DROPBOX_LOGOS_HEXAGON_PATH, nome_arquivo)
        _cache_selos_reais[nome_arquivo] = bruto
    if not bruto:
        return None
    try:
        sem_fundo = remover_fundo(bruto)
    except Exception as e:
        print(f"Remocao de fundo do selo '{nome_arquivo}' falhou, usando original: {repr(e)}")
        sem_fundo = bruto
    img = Image.open(io.BytesIO(sem_fundo)).convert("RGBA")
    escala = min(tamanho / img.width, tamanho / img.height)
    novo_tam = (max(1, int(img.width * escala)), max(1, int(img.height * escala)))
    return img.resize(novo_tam, Image.LANCZOS)


# ============================================================
# OPENAI - LEITURA DE SKU E MARCA NA FOTO DA CAIXA
# ============================================================
def identificar_sku_marca(imagem_bytes, tentativa=1):
    """Retorna (sku_ou_None, marca_ou_None, confiante:bool,
    rotacao_graus:int). rotacao_graus e quantos graus (sentido
    horario) a foto precisa girar pra ficar reta - 0, 90, 180 ou 270.
    Nunca inventa sku/marca - se o modelo nao tiver certeza,
    confiante=False.

    tentativa: so usado pro texto do log (1a ou 2a leitura), nao afeta
    o comportamento - serve pra rastrear no log do GitHub Actions qual
    chamada e qual, sem precisar adivinhar pela ordem das linhas."""
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
                            "codigo SKU e o nome da MARCA impressos DE FABRICA "
                            "na caixa (texto/tipografia original da embalagem), "
                            "mesmo que a foto esteja de lado ou de cabeca pra "
                            "baixo. IMPORTANTE - ONDE ESTA O SKU DE VERDADE: o "
                            "SKU real fica no corpo principal da caixa, "
                            "tipicamente logo ABAIXO ou ao lado do nome da "
                            "MARCA/fabricante e da descricao do produto (ex: "
                            "'CJ. DE CAPSULAS', 'FILTRO', etc) - e parte do "
                            "design grafico original da embalagem, mesma cor/ "
                            "textura do resto da caixa. IMPORTANTE - IGNORAR "
                            "TICKETS/ETIQUETAS DE ESTOQUE: se houver um TICKET "
                            "ou ETIQUETA SEPARADA colada/grampeada na caixa - "
                            "geralmente um pedaco de papel destacado, com "
                            "formato de tíquete (aba recortada, picote, canto "
                            "arredondado) ou cor/textura diferente do resto da "
                            "impressao da caixa - o numero nele e um codigo "
                            "INTERNO DE ESTOQUE, NUNCA o SKU do produto, mesmo "
                            "que pareca um codigo valido. Isso vale tanto pra "
                            "numero impresso quanto manuscrito nesse "
                            "ticket/etiqueta. Extraia o SKU SOMENTE do texto "
                            "grafico principal da caixa, nunca de um ticket/ "
                            "etiqueta destacada. Se nao conseguir identificar "
                            "com clareza qual numero e o SKU principal da "
                            "caixa (em vez de um ticket de estoque), retorne "
                            "sku=null e confiante=false em vez de arriscar. "
                            "IMPORTANTE - MAO/DEDO NA FOTO: se houver mao, "
                            "dedo ou pessoa segurando a caixa cobrindo PARTE "
                            "da foto, isso sozinho NAO derruba a confianca - "
                            "leia normalmente o texto de fabrica ainda visivel "
                            "nas partes descobertas; responda com confianca "
                            "normal se esse texto visivel for claro e legivel. "
                            "So marque confiante=false por causa da mao/dedo "
                            "se ela cobrir especificamente o SKU ou a marca a "
                            "ponto de impedir a leitura desses campos. "
                            "IMPORTANTE: a MARCA e o nome do fabricante "
                            "(ex: NGK, ELRING, BOSCH) - NUNCA um pais de "
                            "origem. Palavras como 'JAPAN', 'MADE IN JAPAN', "
                            "'CHINA', 'GERMANY' ou similares, mesmo gravadas "
                            "em destaque na caixa ou na peca, NAO sao marca - "
                            "ignore esse texto ao preencher o campo marca. Se "
                            "o unico texto identificavel for um pais de "
                            "origem (sem nome de fabricante em lugar nenhum), "
                            "retorne marca=null e confiante=false. Tambem diga "
                            "quantos graus, no sentido horario, a FOTO (nao o "
                            "texto) precisa fisicamente girar pra ficar reta - "
                            "com o texto na horizontal, de pe, como se "
                            "estivesse sendo lido num livro. IMPORTANTE: "
                            "informe a rotacao real da foto mesmo que voce "
                            "consiga ler o texto perfeitamente do jeito que "
                            "esta - sua capacidade de ler texto de lado nao "
                            "significa que a foto esta reta. rotacao=0 e SO "
                            "pra fotos onde o texto ja esta na horizontal, sem "
                            "nenhuma inclinacao de lado ou de cabeca pra baixo. "
                            "Se o texto estiver na vertical (de lado), SEMPRE "
                            "retorne 90, 180 ou 270, nunca 0. Responda SOMENTE "
                            "em JSON no formato "
                            '{"sku": "...", "marca": "...", "confiante": true/false, '
                            '"rotacao": 0} sem nenhum texto adicional. Se nao '
                            'conseguir ler algo, use null nesse campo. '
                            '"confiante" deve ser false se o texto estiver '
                            "ilegivel, cortado ou ambiguo."
                        ),
                    },
                    {
                        "type": "image_url",
                        # detail=high forca a IA a analisar a foto em resolucao
                        # completa (varios tiles), em vez do padrao "auto" que
                        # costuma reduzir a imagem antes de olhar - critico
                        # aqui porque o SKU/marca geralmente ocupa uma fracao
                        # pequena da foto inteira.
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}", "detail": "high"},
                    },
                ],
            }
        ],
        "max_tokens": 200,
        "temperature": 0,
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
        print(f"Leitura da caixa (tentativa {tentativa}) - resposta bruta da IA: {texto}")
        dados = json.loads(texto)
        sku = (dados.get("sku") or "").strip() or None
        marca = (dados.get("marca") or "").strip() or None
        confiante = bool(dados.get("confiante", False)) and sku and marca
        rotacao = dados.get("rotacao", 0) or 0
        rotacao = rotacao if rotacao in (0, 90, 180, 270) else 0
        print(
            f"Leitura da caixa (tentativa {tentativa}) - interpretado: "
            f"sku={sku!r} marca={marca!r} confiante={confiante} rotacao_detectada={rotacao}"
        )
        return sku, marca, confiante, rotacao
    except Exception as e:
        print(f"Falha lendo SKU/marca na caixa (tentativa {tentativa}): {repr(e)}")
        return None, None, False, 0


# ============================================================
# REMOCAO DE FUNDO (rembg - biblioteca gratuita, roda local, sem API paga)
# ============================================================
def corrigir_rotacao(imagem_bytes, graus_horario):
    if not graus_horario:
        return imagem_bytes
    img = Image.open(io.BytesIO(imagem_bytes)).convert("RGB")
    mapa_transpose = {
        90: Image.ROTATE_270,
        180: Image.ROTATE_180,
        270: Image.ROTATE_90,
    }
    metodo = mapa_transpose.get(graus_horario)
    if metodo is not None:
        img = img.transpose(metodo)
    else:
        img = img.rotate(-graus_horario, expand=True, resample=Image.BICUBIC)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def remover_fundo(imagem_bytes):
    from rembg import remove as rembg_remove
    return rembg_remove(imagem_bytes)


def ler_caixa_com_retentativas(imagem_bytes, numero_tentativa_inicial):
    sku, marca, confiante, rotacao = identificar_sku_marca(imagem_bytes, tentativa=numero_tentativa_inicial)
    if rotacao:
        print(f"IA detectou rotacao de {rotacao} graus, mas a correcao automatica esta desativada - foto mantida como veio")
    if not confiante:
        # a leitura da IA nao e 100% deterministica - antes de desistir dessa
        # foto e testar outra posicao, tenta mais uma vez na MESMA imagem
        print("Primeira leitura sem confianca - tentando novamente na mesma foto antes de trocar de posicao")
        sku2, marca2, confiante2, rotacao2 = identificar_sku_marca(imagem_bytes, tentativa=numero_tentativa_inicial + 1)
        if rotacao2:
            print(f"IA detectou rotacao de {rotacao2} graus, mas a correcao automatica esta desativada - foto mantida como veio")
        if confiante2:
            sku, marca, confiante = sku2, marca2, confiante2
    return sku, marca, confiante, imagem_bytes


# ============================================================
# PIL - COMPOSICAO DA IMAGEM FINAL
# ============================================================
def _retangulo_logo(canvas_size, logo_bytes):
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

    if logo_bytes:
        escala_normal = min(area_util / produto.width, area_util / produto.height)
        w_normal, h_normal = produto.width * escala_normal, produto.height * escala_normal
        pos_normal = ((CANVAS_SIZE - w_normal) / 2, (CANVAS_SIZE - h_normal) / 2)
        rect_produto = (pos_normal[0], pos_normal[1], pos_normal[0] + w_normal, pos_normal[1] + h_normal)
        rect_logo = _retangulo_logo(CANVAS_SIZE, logo_bytes)
        area_logo = (rect_logo[2] - rect_logo[0]) * (rect_logo[3] - rect_logo[1])
        cobertura = _area_sobreposicao(rect_produto, rect_logo) / area_logo if area_logo else 0
        if cobertura > 0.35:
            area_util = area_util * 0.85

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


COR_HEXAGON = (17, 85, 165, 255)
COR_ELRING = (196, 30, 30, 255)


def _desenhar_texto_curvo(canvas_rgba, texto, centro, raio, fonte, cor):
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


def _desenhar_icone_h(tamanho, cor):
    from PIL import ImageDraw

    img = Image.new("RGBA", (tamanho, tamanho), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    largura_linha = max(2, int(tamanho * 0.14))
    w, h = tamanho, tamanho

    draw.line([(w * 0.22, h * 0.12), (w * 0.22, h * 0.88)], fill=cor, width=largura_linha)
    draw.line([(w * 0.78, h * 0.12), (w * 0.78, h * 0.88)], fill=cor, width=largura_linha)
    draw.line([(w * 0.22, h * 0.5), (w * 0.78, h * 0.5)], fill=cor, width=largura_linha)
    return img


def _desenhar_icone_escudo_check(tamanho, cor):
    from PIL import ImageDraw

    img = Image.new("RGBA", (tamanho, tamanho), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    largura_linha = max(2, int(tamanho * 0.06))

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

    check = [(w * 0.30, h * 0.48), (w * 0.45, h * 0.63), (w * 0.72, h * 0.32)]
    draw.line(check, fill=cor, width=largura_linha, joint="curve")
    return img


def _criar_selo_redondo(tamanho, texto_arco, texto_central, icone_img, cor):
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


def aplicar_selo_original(canvas_rgba, cor):
    tamanho = int(canvas_rgba.width * LOGO_MAX_RATIO * 1.15)
    icone_h = _desenhar_icone_h(int(tamanho * 0.20), cor)
    selo = _criar_selo_redondo(tamanho, "PRODUTO ORIGINAL", "100%", icone_h, cor)
    margem = int(canvas_rgba.width * LOGO_MARGEM_RATIO)
    pos = (canvas_rgba.width - tamanho - margem, canvas_rgba.height - tamanho - margem)
    canvas_rgba.paste(selo, pos, selo)
    return canvas_rgba


def aplicar_selo_garantia(canvas_rgba, cor):
    tamanho = int(canvas_rgba.width * LOGO_MAX_RATIO * 1.15)
    icone_escudo = _desenhar_icone_escudo_check(int(tamanho * 0.22), cor)
    selo = _criar_selo_redondo(tamanho, "GARANTIA DE FABRICA", "90 DIAS", icone_escudo, cor)
    margem = int(canvas_rgba.width * LOGO_MARGEM_RATIO)
    pos = (margem, canvas_rgba.height - tamanho - margem)
    canvas_rgba.paste(selo, pos, selo)
    return canvas_rgba


def aplicar_selo_confianca_real(canvas_rgba):
    tamanho = int(canvas_rgba.width * LOGO_MAX_RATIO * 1.15)
    selo = _buscar_selo_real("SELO QUALIDADE", tamanho)
    if not selo:
        print("AVISO: selo 'SELO QUALIDADE' nao encontrado em LOGOS HEXAGON - pulando selo de confianca")
        return canvas_rgba
    margem = int(canvas_rgba.width * LOGO_MARGEM_RATIO)
    pos = (canvas_rgba.width - selo.width - margem, canvas_rgba.height - selo.height - margem)
    canvas_rgba.paste(selo, pos, selo)
    return canvas_rgba


def aplicar_selo_garantia_real(canvas_rgba):
    tamanho = int(canvas_rgba.width * LOGO_MAX_RATIO * 1.15)
    selo = _buscar_selo_real("SELO GARANTIA", tamanho)
    if not selo:
        print("AVISO: selo 'SELO GARANTIA' nao encontrado em LOGOS HEXAGON - pulando selo de garantia")
        return canvas_rgba
    margem = int(canvas_rgba.width * LOGO_MARGEM_RATIO)
    pos = (margem, canvas_rgba.height - selo.height - margem)
    canvas_rgba.paste(selo, pos, selo)
    return canvas_rgba


def editar_produto(bytes_brutos, logo_bytes, aplicar_selos=False, cor_selo=COR_HEXAGON):
    try:
        sem_fundo = remover_fundo(bytes_brutos)
    except Exception as e:
        import traceback
        print(f"Remocao de fundo (rembg) falhou, usando imagem original: {repr(e)}")
        traceback.print_exc()
        sem_fundo = bytes_brutos
    canvas = compor_produto_em_canvas(sem_fundo, logo_bytes)
    if aplicar_selos:
        if cor_selo == COR_ELRING:
            canvas = aplicar_selo_original(canvas, cor_selo)
            canvas = aplicar_selo_garantia(canvas, cor_selo)
        else:
            canvas = aplicar_selo_confianca_real(canvas)
            canvas = aplicar_selo_garantia_real(canvas)
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
def detectar_regiao_mao(imagem_bytes):
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
                            "Esta e uma foto de produto (fundo branco). Existe "
                            "mao, dedo ou pessoa segurando a peca na foto? Se "
                            "existir, de a caixa retangular aproximada que "
                            "cobre a mao/dedo (com uma margem de folga), em "
                            "coordenadas normalizadas de 0 a 1 (origem no "
                            "canto superior esquerdo). Responda SOMENTE em "
                            'JSON: {"tem_mao": true/false, "x": 0.0, '
                            '"y": 0.0, "largura": 0.0, "altura": 0.0} '
                            "sem texto adicional. Se tem_mao for false, os "
                            "outros campos podem ser 0."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                    },
                ],
            }
        ],
        "max_tokens": 150,
    }
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        texto = resp.json()["choices"][0]["message"]["content"].strip()
        texto = texto.strip("`").replace("json\n", "").strip()
        dados = json.loads(texto)
        if not dados.get("tem_mao"):
            return None
        x = max(0.0, min(1.0, float(dados.get("x", 0))))
        y = max(0.0, min(1.0, float(dados.get("y", 0))))
        w = max(0.0, min(1.0 - x, float(dados.get("largura", 0.3))))
        h = max(0.0, min(1.0 - y, float(dados.get("altura", 0.3))))
        return (x, y, w, h)
    except Exception as e:
        print(f"Deteccao de regiao da mao falhou (nao bloqueia): {repr(e)}")
        return None


def remover_mao_com_ia(imagem_bytes, regiao):
    img = Image.open(io.BytesIO(imagem_bytes)).convert("RGB")
    tam_original = img.size
    tam_api = (1024, 1024)
    img_api = img.resize(tam_api, Image.LANCZOS)

    x, y, w, h = regiao
    margem = 0.03
    px = max(0, int((x - margem) * tam_api[0]))
    py = max(0, int((y - margem) * tam_api[1]))
    pw = min(tam_api[0] - px, int((w + 2 * margem) * tam_api[0]))
    ph = min(tam_api[1] - py, int((h + 2 * margem) * tam_api[1]))

    mascara = Image.new("RGBA", tam_api, (255, 255, 255, 255))
    area_editavel = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    mascara.paste(area_editavel, (px, py))

    buf_img = io.BytesIO()
    img_api.save(buf_img, format="PNG")
    buf_img.seek(0)
    buf_mask = io.BytesIO()
    mascara.save(buf_mask, format="PNG")
    buf_mask.seek(0)

    resp = requests.post(
        "https://api.openai.com/v1/images/edits",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        files={
            "image": ("imagem.png", buf_img, "image/png"),
            "mask": ("mascara.png", buf_mask, "image/png"),
        },
        data={
            "model": "gpt-image-1",
            "prompt": (
                "Remove a mao/dedo dessa area da foto de produto. "
                "Preencha com fundo branco liso identico ao resto da "
                "foto; se a mao estiver cobrindo parte da peca, "
                "complete o formato/textura da peca de forma realista "
                "e coerente com o resto dela. Nao adicione nenhum "
                "objeto ou elemento novo, nao altere o resto da foto."
            ),
            "size": "1024x1024",
        },
        timeout=90,
    )
    resp.raise_for_status()
    b64_resultado = resp.json()["data"][0]["b64_json"]
    img_resultado = Image.open(io.BytesIO(base64.b64decode(b64_resultado))).convert("RGB")
    img_resultado = img_resultado.resize(tam_original, Image.LANCZOS)
    buf_final = io.BytesIO()
    img_resultado.save(buf_final, format="JPEG", quality=95)
    return buf_final.getvalue()


def verificar_qualidade_foto(imagem_editada_bytes):
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


def registrar_no_staging(sku, marca, pasta_lote):
    """Grava o SKU aprovado na aba Staging da planilha de anuncios,
    pra a pesquisa de dados poder comecar. NUNCA deve derrubar o
    processamento do lote - se a planilha falhar por qualquer motivo
    (credencial, rede, etc.), so registra o erro e segue o robo
    normalmente; a midia ja foi publicada com sucesso de qualquer
    forma."""
    try:
        adicionar_ao_staging(
            sku=sku,
            marca=marca or "",
            caminho_midia=pasta_lote,
            status=STATUS_AGUARDANDO_PESQUISA,
        )
    except Exception as e:
        print(f"AVISO: falha ao registrar SKU {sku} no Staging da planilha (nao bloqueia o lote): {repr(e)}")
        enviar_alerta(
            "Robo de Midias - falha ao registrar no Staging",
            f"SKU {sku} foi publicado normalmente em {pasta_lote}, mas nao entrou "
            f"na aba Staging da planilha. Erro: {repr(e)}",
        )


def enviar_video_para_drive(sku, video_bytes):
    """Sobe uma copia do video pro Google Drive (pasta de backup em
    hexagontakes@gmail.com), nomeada so com o SKU. Igual ao registro no
    Staging, NUNCA bloqueia o processamento do lote - o video ja esta
    salvo no Dropbox de qualquer forma, isso e so um backup extra."""
    try:
        subir_video_drive(sku, video_bytes)
    except Exception as e:
        print(f"AVISO: falha ao subir video do SKU {sku} pro Drive (nao bloqueia o lote): {repr(e)}")
        enviar_alerta(
            "Robo de Midias - falha no backup de video pro Drive",
            f"SKU {sku}: video ja esta salvo no Dropbox normalmente, mas o backup "
            f"pro Google Drive falhou. Erro: {repr(e)}",
        )


def processar_lote(lote):
    caixa_arq = lote[0]
    capa_arq = lote[1]
    angulos = lote[2:-1]
    video = lote[-1]

    caixa_bytes_bruto = dbx_baixar(caixa_arq["path_lower"])
    sku, marca, confiante, caixa_bytes_original = ler_caixa_com_retentativas(caixa_bytes_bruto, 1)

    papeis_trocados = False
    capa_bytes_bruto_alternativo = None
    if not confiante:
        print("Leitura da posicao 1 (caixa) sem confianca - testando se a posicao 2 (capa) e a caixa de verdade")
        capa_bytes_bruto = dbx_baixar(capa_arq["path_lower"])
        sku_c, marca_c, confiante_c, capa_bytes_rotacionado = ler_caixa_com_retentativas(capa_bytes_bruto, 3)
        if confiante_c:
            print("Ordem de captura invertida confirmada: a foto 2 e a caixa, a foto 1 e o produto - trocando os papeis")
            sku, marca, confiante = sku_c, marca_c, confiante_c
            caixa_bytes_original = capa_bytes_rotacionado
            capa_bytes_bruto_alternativo = caixa_bytes_bruto
            papeis_trocados = True
        else:
            print("Posicao 2 tambem nao e uma caixa legivel - segue normal pra revisao manual")

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    identificador = sku or f"LOTE_{timestamp}"
    logo_bytes = dbx_buscar_logo(marca)

    aprovado = bool(confiante and logo_bytes)

    if aprovado:
        pasta_lote = f"{DROPBOX_DEST_ROOT}/{identificador}"
    else:
        if not confiante and not logo_bytes:
            motivo_pasta = "SKU_ILEGIVEL_SEM_LOGO"
        elif not confiante:
            motivo_pasta = "SKU_ILEGIVEL"
        else:
            marca_pasta = (marca or "MARCA_DESCONHECIDA").strip().upper().replace(" ", "_")
            motivo_pasta = f"SEM_LOGO_{marca_pasta}"
        pasta_lote = f"{DROPBOX_SOURCE_PATH}/_REVISAR/{motivo_pasta}/{identificador}"

    pasta_originais = f"{pasta_lote}/_ORIGINAIS"

    fotos_com_problema = []

    def subir_foto_produto(nome_arquivo, bytes_editados):
        if aprovado:
            regiao_mao = detectar_regiao_mao(bytes_editados)
            if regiao_mao:
                try:
                    bytes_editados = remover_mao_com_ia(bytes_editados, regiao_mao)
                    print(f"{nome_arquivo}: mao detectada e removida automaticamente")
                except Exception as e:
                    print(f"{nome_arquivo}: remocao automatica da mao falhou: {repr(e)}")
                ok, motivo = verificar_qualidade_foto(bytes_editados)
                if not ok:
                    fotos_com_problema.append((nome_arquivo, motivo))
                    dbx_subir(f"{pasta_lote}/_VERIFICAR/{nome_arquivo}", bytes_editados)
                    return
        dbx_subir(f"{pasta_lote}/{nome_arquivo}", bytes_editados)

    icone_hexagon_bytes = _buscar_logo_em(DROPBOX_LOGOS_HEXAGON_PATH, "HEXAGON LOGO")
    cor_selo = COR_ELRING if (marca and marca.strip().upper() == "ELRING") else COR_HEXAGON

    bruto = capa_bytes_bruto_alternativo if papeis_trocados else dbx_baixar(capa_arq["path_lower"])
    subir_foto_produto(
        f"{identificador}_Capa.jpg",
        editar_produto(bruto, logo_bytes, aplicar_selos=False, cor_selo=cor_selo),
    )

    for i, arq in enumerate(angulos, start=2):
        bruto = dbx_baixar(arq["path_lower"])
        nome_final = f"{identificador}_{str(i).zfill(2)}.jpg"
        subir_foto_produto(nome_final, editar_produto(bruto, logo_bytes))

    dbx_subir(
        f"{pasta_lote}/{identificador}_Caixa.jpg",
        editar_produto(caixa_bytes_original, logo_bytes),
    )

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
    if aprovado:
        enviar_video_para_drive(identificador, video_bytes)

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
        # SKU aprovado e publicado com sucesso -> registra na planilha
        # (aba Staging) pra pesquisa de dados poder comecar. So chega
        # aqui depois que TODA a midia ja subiu, entao uma falha aqui
        # nunca compromete as fotos/video ja publicados.
        registrar_no_staging(identificador, marca, pasta_lote)

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
