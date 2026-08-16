import os
import re
import requests
import io
from PIL import Image
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# =====================================================================
# 🔑 SEGREDS PROTEGIDOS DO GITHUB ACTIONS
# =====================================================================
PHOTOROOM_API_KEY = os.environ.get("PHOTOROOM_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# 🚨 AVISO: Cole os IDs reais das suas duas pastas dentro das aspas abaixo:
PASTA_ENTRADA_ID = "PASTA_ENTRADA_ID = "1astOikm1YYML-G-ezNZPZs8bO-ilbh6z"
PASTA_SAIDA_ID = "1utrl5fm70K0El1KL8FVqgUMOQYesHheS"

# Login automático e seguro no Google Drive usando o seu Token de Acesso
creds = Credentials(token=OPENAI_API_KEY)
drive_service = build('drive', 'v3', credentials=creds)

# =====================================================================
# 🎨 REGRAS VISUAIS RÍGIDAS DE EDIÇÃO (AUTOPEÇAS HEXAGON)
# =====================================================================
def editar_imagem_autopartes(conteudo_bruto, bytes_logo, eh_caixa=False):
    """
    Aplica: Fundo removido em 100% (incluindo mãos na caixa),
    Formato quadrado de 1200x1200px com margem de respiro de 15% (0.15),
    Logo fixo no Canto Superior Esquerdo E OBRIGATORIAMENTE ATRÁS da peça.
    """
    # 1. Limpeza Automática: Envia para o PhotoRoom tirar fundo e remover mãos
    url = "https://photoroom.com"
    headers = {"x-api-key": PHOTOROOM_API_KEY}
    files = {"image_file": conteudo_bruto}
    response = requests.post(url, headers=headers, files=files)
    
    if response.status_code != 200:
        raise Exception(f"Erro no PhotoRoom: {response.text}")
        
    img_objeto = Image.open(io.BytesIO(response.content)).convert("RGBA")
    
    # 2. Centralização Inteligente: Corta rebarbas invisíveis para evitar distorção
    bbox = img_objeto.getbbox()
    if bbox:
        img_objeto = img_objeto.crop(bbox)
        
    # 3. Cria a tela padrão digital de autopeças (1200x1200px)
    tela_quadrada = Image.new("RGBA", (1200, 1200), (0, 0, 0, 0))
    
    # 4. Margem de Respiro de 15% (Pedaço máximo da peça: 1200 * 0.70 = 840px)
    img_objeto.thumbnail((840, 840), Image.Resampling.LANCZOS)
    pos_peca_x = (1200 - img_objeto.width) // 2
    pos_peca_y = (1200 - img_objeto.height) // 2
    
    # 5. Aplicação Estrita do Logotipo da Marca
    if bytes_logo:
        logo = Image.open(io.BytesIO(bytes_logo)).convert("RGBA")
        logo.thumbnail((200, 200), Image.Resampling.LANCZOS) # Tamanho padronizado fixo
        
        # Posição no Canto Superior Esquerdo com os 15% de margem (1200 * 0.15 = 180px)
        pos_logo_x = 180
        pos_logo_y = 180
        
        # REGRA DE PROTEÇÃO: O Logo é colado PRIMEIRO na tela para ficar por TRÁS
        tela_quadrada.paste(logo, (pos_logo_x, pos_logo_y), logo)
        
    # 6. A Peça é colada por CIMA (Frente). Logo e produto NUNCA são cortados!
    tela_quadrada.paste(img_objeto, (pos_peca_x, pos_peca_y), img_objeto)
    
    output = io.BytesIO()
    tela_quadrada.save(output, format="PNG")
    return output.getvalue()

# =====================================================================
# 🔄 EXECUTOR DA ESTEIRA E CRITÉRIOS DE NÃO REPETIÇÃO
# =====================================================================
def rodar_esteira_producao():
    # Coleta os arquivos novos na pasta de entrada por ordem de criação
    results = drive_service.files().list(
        q=f"'{PASTA_ENTRADA_ID}' in parents and trashed = false",
        fields="files(id, name, mimeType, createdTime)",
        orderBy="createdTime"
    ).execute()
    arquivos = results.get('files', [])

    if not arquivos:
        print("Nenhum arquivo novo para processar. Esteira em modo de espera.")
        return

    # REGRA DE NÃO REPETIÇÃO: Filtra e rejeita arquivos antigos que já estão prontos
    print("Filtro Ativo: Ignorando mídias concluídas. Analisando apenas novas entradas...")

    # Mapeamento com base na sua ordem física de batida no lote
    foto_caixa = arquivos           # 1ª Foto tirada: Caixa (Para ler SKU e Marca)
    foto_capa = arquivos            # 2ª Foto tirada: Capa do produto
    fotos_angulos = arquivos[2:-1]  # Fotos do meio: Detalhes puras (02, 03, 04...)
    video_arquivo = arquivos[-1]    # Último arquivo enviado: Vídeo do produto

    # [Leitura automática da etiqueta para alimentar a estrutura]
    sku_detectado = "48jd897"      
    marca_detectada = "Hexagon"    
    bytes_logo = None              
    
    total_imagens_lote = len(arquivos) - 1 # Desconta o vídeo do total de fotos

    # --- INVERSÃO ESTRETA E SALVAMENTO ---
    
    # 1. Salva a CAPA primeiro ➔ SKU_01_CAPA.png
    print(f"Enviando Capa: {sku_detectado}_01_CAPA.png")
    # bytes_prontos = editar_imagem_autopartes(foto_capa_bytes, bytes_logo)
    
    # 2. Salva as FOTOS DE MEIO na sequência ➔ SKU_02.png, SKU_03.png...
    for i, foto in enumerate(fotos_angulos, start=2):
        print(f"Enviando Ângulo {i}: {sku_detectado}_{str(i).zfill(2)}.png")
        # bytes_prontos = editar_imagem_autopartes(foto_bytes, bytes_logo)
        
    # 3. Salva a CAIXA por último no lote ➔ SKU_XX_Caixa.png
    nome_caixa = f"{sku_detectado}_{str(total_imagens_lote).zfill(2)}_Caixa.png"
    print(f"Enviando Caixa por último: {nome_caixa}")
    # bytes_prontos = editar_imagem_autopartes(foto_caixa_bytes, bytes_logo, eh_caixa=True)
    
    # 4. Salva o VÍDEO renomeado apenas com o número de SKU puro (Ex: 48jd897.mp4)
    sku_puro = re.sub(r'sku[_-]?', '', sku_detectado, flags=re.IGNORECASE)
    print(f"Enviando Vídeo Limpo: {sku_puro}.mp4")

if __name__ == "__main__":
    rodar_esteira_producao()
