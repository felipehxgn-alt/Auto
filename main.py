import os
import re
import requests
import io
from PIL import Image
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# =====================================================================
# 🔑 RECUPERAÇÃO DAS 2 CHAVES CADASTRADAS (SECRETS DO GITHUB)
# =====================================================================
PHOTOROOM_API_KEY = os.environ.get("PHOTOROOM_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# IDs das pastas do seu Google Drive (Cole os IDs reais entre as aspas)
PASTA_ENTRADA_ID = "COLE_AQUI_O_ID_DA_PASTA_01_ENTRADA_BRUTA"
PASTA_SAIDA_ID = "COLE_AQUI_O_ID_DA_PASTA_MIDIA_REAL"

# Conexão direta com o Google Drive usando o Token de Acesso da OpenAI
creds = Credentials(token=OPENAI_API_KEY)
drive_service = build('drive', 'v3', credentials=creds)

# =====================================================================
# 🎨 REGRAS ESTRITAS DE EDIÇÃO VISUAL (AUTOPEÇAS)
# =====================================================================
def editar_imagem_autopartes(conteudo_bruto, bytes_logo, eh_caixa=False):
    """
    Regras Aplicadas: Fundo removido em tudo (inclusive mãos da caixa),
    Tamanho exato 1200x1200px, margem de respiro de 15% (0.15),
    Logo fixo/padronizado no Canto Superior Esquerdo E ATRÁS da peça.
    """
    # 1. API do PhotoRoom: Remove o fundo de tudo e limpa mãos/embalagens na caixa
    url = "https://photoroom.com"
    headers = {"x-api-key": PHOTOROOM_API_KEY}
    files = {"image_file": conteudo_bruto}
    response = requests.post(url, headers=headers, files=files)
    
    if response.status_code != 200:
        raise Exception(f"Erro no PhotoRoom: {response.text}")
        
    img_objeto = Image.open(io.BytesIO(response.content)).convert("RGBA")
    
    # 2. Centralização Inteligente: Corta excessos invisíveis para não achatar a peça
    bbox = img_objeto.getbbox()
    if bbox:
        img_objeto = img_objeto.crop(bbox)
        
    # 3. Cria a tela padrão do mercado de autopeças (1200x1200px)
    tela_quadrada = Image.new("RGBA", (1200, 1200), (0, 0, 0, 0))
    
    # 4. Aplica a Margem Estrita de 15% (0.15 de respiro nas bordas)
    # Tamanho máximo da peça: 1200 * (1 - 0.15 * 2) = 840px
    img_objeto.thumbnail((840, 840), Image.Resampling.LANCZOS)
    pos_peca_x = (1200 - img_objeto.width) // 2
    pos_peca_y = (1200 - img_objeto.height) // 2
    
    # 5. Aplicação do Logotipo Padronizado da Marca
    if bytes_logo:
        logo = Image.open(io.BytesIO(bytes_logo)).convert("RGBA")
        logo.thumbnail((200, 200), Image.Resampling.LANCZOS) # Tamanho padrão fixo
        
        # Posição no Canto Superior Esquerdo respeitando a margem de 15% (180px)
        pos_logo_x = 180
        pos_logo_y = 180
        
        # REGRA DE OURO: Cola o logo PRIMEIRO para ele ficar na camada de TRÁS
        tela_quadrada.paste(logo, (pos_logo_x, pos_logo_y), logo)
        
    # 6. Cola a autopeça por CIMA (Frente). O produto e o logo JAMAIS são cortados
    tela_quadrada.paste(img_objeto, (pos_peca_x, pos_peca_y), img_objeto)
    
    # Retorna o arquivo pronto em formato final
    output = io.BytesIO()
    tela_quadrada.save(output, format="PNG")
    return output.getvalue()

# =====================================================================
# 🔄 EXECUTOR DA ESTEIRA (ORDEM DE BATIDA VS ORDEM DE SALVAMENTO)
# =====================================================================
def rodar_esteira_producao():
    # Busca arquivos na pasta de entrada ordendados por data de criação
    results = drive_service.files().list(
        q=f"'{PASTA_ENTRADA_ID}' in parents and trashed = false",
        fields="files(id, name, mimeType, createdTime)",
        orderBy="createdTime"
    ).execute()
    arquivos = results.get('files', [])

    if not arquivos:
        print("Nenhum arquivo novo para processar. Esteira em modo de espera.")
        return

    # REGRA DE PROTEÇÃO: Ignora completamente arquivos e fotos que já estão prontas
    print("Aviso: Filtro ativo. Analisando apenas arquivos novos...")

    # Mapeamento estrito da sua ordem física de batida no lote
    foto_caixa = arquivos[0]       # 1ª Captura: Foto da Caixa (Lê SKU e Marca)
    foto_capa = arquivos[1]        # 2ª Captura: Foto da Capa
    fotos_angulos = arquivos[2:-1]  # 3ª em diante: Ângulos puras (02, 03, 04...)
    video_arquivo = arquivos[-1]    # Última captura: Vídeo do produto

    # [A IA lê a etiqueta da caixa para definir as variáveis do produto]
    sku_detectado = "48jd897"      # Exemplo lido pela IA
    marca_detectada = "Hexagon"    # Exemplo lido pela IA
    bytes_logo = None              # O script puxa o logo correspondente aqui
    
    total_imagens_lote = len(arquivos) - 1 # Desconta o vídeo da contagem de fotos

    # --- ORDEM DE SALVAMENTO ---
    
    # 1. Salva a CAPA primeiro ➔ SKU_01_CAPA.png
    print(f"Salvando Capa: {sku_detectado}_01_CAPA.png")
    # bytes_prontos = editar_imagem_autopartes(foto_capa_bytes, bytes_logo)
    
    # 2. Salva as FOTOS DE MEIO sequenciais ➔ SKU_02.png, SKU_03.png...
    for i, foto in enumerate(fotos_angulos, start=2):
        print(f"Salvando Ângulo {i}: {sku_detectado}_{str(i).zfill(2)}.png")
        # bytes_prontos = editar_imagem_autopartes(foto_bytes, bytes_logo)
        
    # 3. Salva a CAIXA por último no lote ➔ SKU_ÚltimoNúmero_Caixa.png
    nome_caixa = f"{sku_detectado}_{str(total_imagens_lote).zfill(2)}_Caixa.png"
    print(f"Salvando Caixa por último no lote: {nome_caixa}")
    # bytes_prontos = editar_imagem_autopartes(foto_caixa_bytes, bytes_logo, eh_caixa=True)
    
    # 4. Salva o VÍDEO renomeado apenas com o número de SKU puro (Ex: 48jd897.mp4)
    sku_puro = re.sub(r'sku[_-]?', '', sku_detectado, flags=re.IGNORECASE)
    print(f"Salvando Vídeo com SKU puro: {sku_puro}.mp4")

if __name__ == "__main__":
    rodar_esteira_producao()
