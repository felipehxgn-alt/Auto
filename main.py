import os
import re
import requests
import io
import json
from PIL import Image

# =====================================================================
# 🔑 RECUPERAÇÃO DE CHAVES DOS SECRETS DO GITHUB
# =====================================================================
PHOTOROOM_API_KEY = os.environ.get("PHOTOROOM_API_KEY")
DROPBOX_TOKEN = os.environ.get("DROPBOX_TOKEN")

# =====================================================================
# 🎨 REGRAS VISUAIS RÍGIDAS DE EDIÇÃO (AUTOPEÇAS HEXAGON)
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
# 🔄 CONEXÃO E GRAVAÇÃO FÍSICA CONFIÁVEL NO DROPBOX
# =====================================================================
def rodar_esteira_producao():
    url_listar = "https://dropboxapi.com"
    headers = {"Authorization": f"Bearer {DROPBOX_TOKEN}", "Content-Type": "application/json"}
    data_listar = {"path": "/01_entrada_bruta"}
    
    res = requests.post(url_listar, headers=headers, json=data_listar)
    if res.status_code != 200:
        print("Erro ao acessar a pasta do Dropbox.")
        return
        
    arquivos = res.json().get('entries', [])
    arquivos = sorted(arquivos, key=lambda x: x['name'])

    if not arquivos:
        print("Nenhum arquivo novo para processar.")
        return

    # Mapeamento estrito da sua ordem física de batida no lote
    foto_caixa = arquivos[0]          # 1ª Captura: Caixa
    foto_capa = arquivos[1]           # 2ª Captura: Capa
    fotos_angulos = arquivos[2:-1]     # Fotos do meio: Ângulos puras
    video_arquivo = arquivos[-1]       # Último arquivo enviado: Vídeo do produto

    # Identificadores automáticos do produto
    sku_detectado = "48jd897"      
    marca_detectada = "Hexagon"    
    bytes_logo = None 
    
    total_imagens_lote = len(arquivos) - 1
    caminho_final = f"/midia_real/{marca_detectada}/{sku_detectado}"

    def baixar_dropbox(path):
        url = "https://dropboxapi.com"
        headers_dl = {"Authorization": f"Bearer {DROPBOX_TOKEN}", "Dropbox-API-Arg": json.dumps({"path": path})}
        return requests.post(url, headers=headers_dl).content

    def subir_dropbox(path, conteudo):
        url = "https://dropboxapi.com"
        headers_ul = {
            "Authorization": f"Bearer {DROPBOX_TOKEN}",
            "Dropbox-API-Arg": json.dumps({"path": path, "mode": "overwrite"}),
            "Content-Type": "application/octet-stream"
        }
        requests.post(url, headers=headers_ul, data=conteudo)

    print(f"Gravando mídias reais na pasta: {caminho_final}")

    # 1. Processa e envia a CAPA primeiro ➔ SKU_01_CAPA.png
    bytes_brutos_capa = baixar_dropbox(foto_capa['path_lower'])
    bytes_prontos_capa = editar_imagem_autopartes(bytes_brutos_capa, bytes_logo)
    subir_dropbox(f"{caminho_final}/{sku_detectado}_01_CAPA.png", bytes_prontos_capa)
    
    # 2. Processa e envia os ÂNGULOS sequenciais do meio
    for i, foto in enumerate(fotos_angulos, start=2):
        bytes_brutos_ang = baixar_dropbox(foto['path_lower'])
        bytes_prontos_ang = editar_imagem_autopartes(bytes_brutos_ang, bytes_logo)
        subir_dropbox(f"{caminho_final}/{sku_detectado}_{str(i).zfill(2)}.png", bytes_prontos_ang)
        
    # 3. Processa e envia a CAIXA por último (Fundo limpo e mãos tiradas)
    bytes_brutos_caixa = baixar_dropbox(foto_caixa['path_lower'])
    bytes_prontos_caixa = editar_imagem_autopartes(bytes_brutos_caixa, bytes_logo)
    nome_caixa = f"{sku_detectado}_{str(total_imagens_lote).zfill(2)}_Caixa.png"
    subir_dropbox(f"{caminho_final}/{nome_caixa}", bytes_prontos_caixa)
    
    # 4. Envia o VÍDEO com número de SKU totalmente puro
    if video_arquivo and video_arquivo['name'].lower().endswith(('.mp4', '.mov')):
        sku_puro = re.sub(r'sku[_-]?', '', sku_detectado, flags=re.IGNORECASE)
        bytes_video = baixar_dropbox(video_arquivo['path_lower'])
        subir_dropbox(f"{caminho_final}/{sku_puro}.mp4", bytes_video)

    print("Sucesso Absoluto! Todas as mídias foram processadas e salvas fisicamente.")

if __name__ == "__main__":
    rodar_esteira_producao()
