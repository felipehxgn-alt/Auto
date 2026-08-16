import os
import re
import requests
import io
from PIL import Image

# =====================================================================
# 🔑 CREDENCIAIS DO GITHUB ACTIONS (SECRETS)
# =====================================================================
PHOTOROOM_API_KEY = os.environ.get("PHOTOROOM_API_KEY")
GOOGLE_API_KEY = os.environ.get("OPENAI_API_KEY") # Usando a chave configurada para o Drive

PASTA_ENTRADA_ID = "1astOikm1YYML-G-ezNZPZs8bO-ilbh6z"
PASTA_SAIDA_ID = "1utrl5fm70K0El1KL8FVqgUMOQYesHheS"

# =====================================================================
# 🎨 FUNÇÃO DE EDIÇÃO (1200x1200px, Fundo limpo e Logo Atrás)
# =====================================================================
def editar_imagem_autopartes(conteudo_bruto, bytes_logo):
    url = "https://photoroom.com"
    headers = {"x-api-key": PHOTOROOM_API_KEY}
    files = {"image_file": conteudo_bruto}
    
    response = requests.post(url, headers=headers, files=files)
    if response.status_code != 200:
        raise Exception(f"Erro no PhotoRoom: {response.text}")
        
    img_objeto = Image.open(io.BytesIO(response.content)).convert("RGBA")
    
    bbox = img_objeto.getbbox()
    if bbox:
        img_objeto = img_objeto.crop(bbox)
        
    tela_quadrada = Image.new("RGBA", (1200, 1200), (0, 0, 0, 0))
    img_objeto.thumbnail((840, 840), Image.Resampling.LANCZOS)
    pos_peca_x = (1200 - img_objeto.width) // 2
    pos_peca_y = (1200 - img_objeto.height) // 2
    
    if bytes_logo:
        logo = Image.open(io.BytesIO(bytes_logo)).convert("RGBA")
        logo.thumbnail((200, 200), Image.Resampling.LANCZOS)
        pos_logo_x = 180
        pos_logo_y = 180
        tela_quadrada.paste(logo, (pos_logo_x, pos_logo_y), logo)
        
    tela_quadrada.paste(img_objeto, (pos_peca_x, pos_peca_y), img_objeto)
    
    output = io.BytesIO()
    tela_quadrada.save(output, format="PNG")
    return output.getvalue()

# =====================================================================
# 📦 REQUISIÇÕES HTTP CORRIGIDAS PARA O GOOGLE DRIVE (USANDO KEY)
# =====================================================================
def criar_pasta_no_drive(nome_pasta, pasta_pai_id):
    url = f"https://googleapis.com{GOOGLE_API_KEY}"
    meta = {"name": nome_pasta, "mimeType": "application/vnd.google-apps.folder", "parents": [pasta_pai_id]}
    response = requests.post(url, json=meta)
    return response.json().get('id')

def baixar_arquivo_do_drive(file_id):
    url = f"https://googleapis.com{file_id}?alt=media&key={GOOGLE_API_KEY}"
    response = requests.get(url)
    return response.content

def subir_arquivo_para_drive(nome_arquivo, conteudo, pasta_destino_id, mime_type="image/png"):
    url_meta = f"https://googleapis.com{GOOGLE_API_KEY}"
    metadata = {"name": nome_arquivo, "parents": [pasta_destino_id]}
    files = {
        'metadata': (None, requests.utils.json.dumps(metadata), 'application/json'),
        'file': (nome_arquivo, conteudo, mime_type)
    }
    requests.post(url_meta, files=files)

# =====================================================================
# 🔄 OPERAÇÃO EM LOTE E INTEGRAÇÃO DE NOMES
# =====================================================================
def rodar_esteira_producao():
    url_drive = f"https://googleapis.com{GOOGLE_API_KEY}"
    params = {
        "q": f"'{PASTA_ENTRADA_ID}' in parents and trashed = false", 
        "fields": "files(id, name, mimeType)", 
        "orderBy": "createdTime"
    }
    
    response = requests.get(url_drive, params=params)
    
    if response.status_code != 200:
        print(f"Erro na API do Google Drive: {response.text}")
        return
        
    arquivos = response.json().get('files', [])

    if not arquivos:
        print("Nenhum arquivo novo encontrado na Entrada Bruta.")
        return

    print(f"Conexão bem-sucedida! Processando {len(arquivos)} arquivos.")

    # Mapeamento físico: Caixa (1), Capa (2), Ângulos (Meio), Vídeo (Último)
    foto_caixa = arquivos[0]
    foto_capa = arquivos[1] if len(arquivos) > 1 else arquivos[0]
    fotos_angulos = arquivos[2:-1] if len(arquivos) > 3 else []
    video_arquivo = arquivos[-1] if len(arquivos) > 2 else None

    sku_detectado = "48jd897"      
    marca_detectada = "Hexagon"    
    bytes_logo = None 
    
    total_imagens_lote = len(arquivos) - 1 if video_arquivo else len(arquivos)

    # 1. Criação das pastas de Destino
    id_pasta_marca = criar_pasta_no_drive(marca_detectada, PASTA_SAIDA_ID)
    id_pasta_sku = criar_pasta_no_drive(sku_detectado, id_pasta_marca)

    # 2. Processamento e Envio Real
    # Capa ➔ _01_CAPA
    bytes_brutos_capa = baixar_arquivo_do_drive(foto_capa['id'])
    bytes_prontos_capa = editar_imagem_autopartes(bytes_brutos_capa, bytes_logo)
    subir_arquivo_para_drive(f"{sku_detectado}_01_CAPA.png", bytes_prontos_capa, id_pasta_sku)
    
    # Ângulos sequenciais
    for i, foto in enumerate(fotos_angulos, start=2):
        bytes_brutos_ang = baixar_arquivo_do_drive(foto['id'])
        bytes_prontos_ang = editar_imagem_autopartes(bytes_brutos_ang, bytes_logo)
        subir_arquivo_para_drive(f"{sku_detectado}_{str(i).zfill(2)}.png", bytes_prontos_ang, id_pasta_sku)
        
    # Caixa por último na esteira
    bytes_brutos_caixa = baixar_arquivo_do_drive(foto_caixa['id'])
    bytes_prontos_caixa = editar_imagem_autopartes(bytes_brutos_caixa, bytes_logo)
    nome_caixa = f"{sku_detectado}_{str(total_imagens_lote).zfill(2)}_Caixa.png"
    subir_arquivo_para_drive(nome_caixa, bytes_prontos_caixa, id_pasta_sku)
    
    # Vídeo com SKU Puro
    if video_arquivo and video_arquivo['mimeType'].startswith('video/'):
        sku_puro = re.sub(r'sku[_-]?', '', sku_detectado, flags=re.IGNORECASE)
        bytes_video = baixar_arquivo_do_drive(video_arquivo['id'])
        subir_arquivo_para_drive(f"{sku_puro}.mp4", bytes_video, id_pasta_sku, mime_type=video_arquivo['mimeType'])

    print("Esteira executada! Fotos limpas e salvas no Google Drive com sucesso.")

if __name__ == "__main__":
    rodar_esteira_producao()
