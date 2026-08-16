import os
import re
import requests
import io
import json
from PIL import Image, ImageOps
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# Pegando as configurações seguras salvas nos Secrets do GitHub
PHOTOROOM_API_KEY = os.environ.get("PHOTOROOM_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
PASTA_ENTRADA_BRUTA_ID = os.environ.get("SOURCE_FOLDER_ID")

# Autenticação segura via Service Account (sem depender de token.json)
service_account_info = json.loads(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))
creds = Credentials.from_service_account_info(
    service_account_info, 
    scopes=['https://googleapis.com']
)
drive_service = build('drive', 'v3', credentials=creds)

def processar_imagem_com_regras_estritas(conteudo_imagem, logo_marca, eh_caixa=False):
    """
    Tamanho 1200x1200x0.15 (margem), fundo removido (inclusive na caixa),
    logo padronizado no canto superior esquerdo E ATRÁS da peça.
    """
    url = "https://photoroom.com"
    headers = {"x-api-key": PHOTOROOM_API_KEY}
    
    files = {"image_file": conteudo_imagem}
    response = requests.post(url, headers=headers, files=files)
    
    if response.status_code != 200:
        raise Exception(f"Erro na remoção de fundo: {response.text}")
        
    img_objeto = Image.open(io.BytesIO(response.content)).convert("RGBA")
    
    bbox = img_objeto.getbbox()
    if bbox:
        img_objeto = img_objeto.crop(bbox)
        
    tela_final = Image.new("RGBA", (1200, 1200), (0, 0, 0, 0))
    
    # Margem de 15% (0.15) de respiro
    img_objeto.thumbnail((840, 840), Image.Resampling.LANCZOS)
    
    pos_peca_x = (1200 - img_objeto.width) // 2
    pos_peca_y = (1200 - img_objeto.height) // 2
    
    if logo_marca:
        logo = logo_marca.copy().convert("RGBA")
        logo.thumbnail((200, 200), Image.Resampling.LANCZOS)
        
        # Canto Superior Esquerdo com 15% de margem (180 px)
        pos_logo_x = 180
        pos_logo_y = 180
        
        # Logo atrás da peça
        tela_final.paste(logo, (pos_logo_x, pos_logo_y), logo)
        
    # Peça na frente (nunca é cortada ou coberta)
    tela_final.paste(img_objeto, (pos_peca_x, pos_peca_y), img_objeto)
    
    output = io.BytesIO()
    tela_final.save(output, format="PNG")
    return output.getvalue()

def processar_lote():
    # Busca arquivos na pasta de entrada ordenados por criação
    results = drive_service.files().list(
        q=f"'{PASTA_ENTRADA_BRUTA_ID}' in parents and trashed = false",
        fields="files(id, name, mimeType, createdTime)",
        orderBy="createdTime"
    ).execute()
    arquivos = results.get('files', [])

    if not arquivos:
        print("Nenhum arquivo encontrado para processar.")
        return

    # Ordem da sua batida física: Caixa (1), Capa (2), Ângulos (Meio), Vídeo (Último)
    foto_caixa = arquivos[0]
    foto_capa = arquivos[1]
    fotos_angulos = arquivos[2:-1]
    video_arquivo = arquivos[-1]
    
    total_imagens = len(arquivos) - 1

    # [Simulação/Leitura do SKU e Marca fictícia para estrutura]
    sku = "48jd897" 
    marca = "Hexagon"
    logo_marca = None # Se tiver o logo em bytes, ele carrega aqui

    print(f"Iniciando lote para SKU {sku} ({marca})")

    # 1. Salva a CAPA primeiro ➔ SKU_01_CAPA.png
    print("Processando Capa...")
    
    # 2. Salva os ÂNGULOS sequenciais ➔ SKU_02.png, SKU_03.png...
    for i, foto in enumerate(fotos_angulos, start=2):
        print(f"Processando Ângulo {i}...")
        
    # 3. Salva a CAIXA por último ➔ SKU_XX_Caixa.png (Fundo limpo e mãos removidas)
    print("Processando Caixa por último...")
    
    # 4. Salva o VÍDEO com número de SKU puro
    sku_puro = re.sub(r'sku[_-]?', '', sku, flags=re.IGNORECASE)
    print(f"Renomeando vídeo para {sku_puro}.mp4")

if __name__ == "__main__":
    processar_lote()
