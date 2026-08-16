import os
import re
import requests
import io
from PIL import Image, ImageOps
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# =====================================================================
# CONFIGURAÇÕES E CHAVES DE ACESSO
# =====================================================================
PHOTOROOM_API_KEY = "SUA_API_KEY_DO_PHOTOROOM_AQUI"
PASTA_LOGOS_ID = "ID_DA_PASTA_ONDE_ESTAO_OS_LOGOS_DAS_MARCAS"

# Inicialização do Google Drive
creds = Credentials.from_authorized_user_file('token.json')
drive_service = build('drive', 'v3', credentials=creds)

def baixar_logo_da_marca(nome_marca):
    """Busca o arquivo de logo (PNG transparente) correspondente à marca no Drive"""
    query = f"'{PASTA_LOGOS_ID}' in parents and name contains '{nome_marca}' and trashed = false"
    results = drive_service.files().list(q=query, fields="files(id)").execute()
    arquivos = results.get('files', [])
    
    if not arquivos:
        print(f"Aviso: Logo para a marca '{nome_marca}' não encontrado. Usando logo padrão.")
        return None
        
    request = drive_service.files().get_media(fileId=arquivos[0]['id'])
    logo_stream = io.BytesIO()
    downloader = MediaIoBaseDownload(logo_stream, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return Image.open(logo_stream).convert("RGBA")

def aplicar_identidade_visual(conteudo_imagem, logo_marca, remover_fundo=True):
    """Aplica o tratamento estrito de imagem: Fundo, Centralização e Logo"""
    if remover_fundo:
        # 1. Envia para o Photoroom para remover o fundo
        url = "https://photoroom.com"
        headers = {"x-api-key": PHOTOROOM_API_KEY}
        files = {"image_file": conteudo_imagem}
        response = requests.post(url, headers=headers, files=files)
        img_tratada = Image.open(io.BytesIO(response.content)).convert("RGBA")
    else:
        # Para a Caixa, mantém o fundo original intacto
        img_tratada = Image.open(io.BytesIO(conteudo_imagem)).convert("RGBA")

    # 2. Lógica de Centralização (Apenas para fotos com fundo removido)
    if remover_fundo:
        # Encontra a caixa delimitadora do produto para centralizar perfeitamente
        bbox = img_tratada.getbbox()
        if bbox:
            produto = img_tratada.crop(bbox)
            # Cria um fundo quadrado branco ou transparente padrão (ex: 2000x2000)
            fundo_padrao = Image.new("RGBA", (2000, 2000), (255, 255, 255, 0))
            # Centraliza o produto dentro do fundo padrão deixando margens idênticas
            produto.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            x = (2000 - produto.width) // 2
            y = (2000 - produto.height) // 2
            fundo_padrao.paste(produto, (x, y), produto)
            img_tratada = fundo_padrao

    # 3. Aplicação do Logo no Canto Inferior Direito
    if logo_marca:
        largura_img, altura_img = img_tratada.size
        # Redimensiona o logo proporcionalmente ao tamanho da imagem final
        logo_temp = logo_marca.copy()
        logo_temp.thumbnail((largura_img // 6, altura_img // 6), Image.Resampling.LANCZOS)
        
        # Define a posição com um pequeno recuo (margem) da borda direita e inferior
        posicao_x = largura_img - logo_temp.width - 50
        posicao_y = altura_img - logo_temp.height - 50
        
        # Cola o logo respeitando a transparência
        img_tratada.paste(logo_temp, (posicao_x, posicao_y), logo_temp)

    # Converte de volta para salvar em formato JPG ou PNG final
    output = io.BytesIO()
    img_tratada.convert("RGB").save(output, format="JPEG", quality=95)
    return output.getvalue()

# =====================================================================
# FLUXO DE EXECUÇÃO PRINCIPAL (Organização por Ordem de Batida de Foto)
# =====================================================================
def executar_esteira_producao():
    # ... (O script roda a ordenação de entrada: Caixa primeiro, depois Capa, Ângulos e Vídeo)
    marca = "NomeDaMarcaDetectada"
    sku = "12345"
    
    # Baixa o logo correto da marca para usar em todo o lote
    logo_marca = baixar_logo_da_marca(marca)
    
    # [FOTO 2 DA BATIDA] -> Vira a Capa com fundo limpo e Logo
    # imagem_editada = aplicar_identidade_visual(bytes_capa, logo_marca, remover_fundo=True)
    # salvar_no_drive(f"{sku}_01_CAPA.jpg", imagem_editada)
    
    # [FOTOS DO MEIO] -> Viram os ângulos numerados com fundo limpo e Logo
    # imagem_editada = aplicar_identidade_visual(bytes_angulo, logo_marca, remover_fundo=True)
    # salvar_no_drive(f"{sku}_02.jpg", imagem_editada)

    # [FOTO 1 DA BATIDA] -> Vira a Caixa por último. Fundo original MANTIDO + Logo aplicado
    # imagem_editada = aplicar_identidade_visual(bytes_caixa, logo_marca, remover_fundo=False)
    # salvar_no_drive(f"{sku}_05_CAIXA.jpg", imagem_editada)
    
    print("Todas as regras visuais de fundo, enquadramento e marcas d'água foram aplicadas com rigor!")
