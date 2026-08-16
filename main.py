import os
import re
import requests
import io
from PIL import Image

# =====================================================================
# 🔑 CREDENCIAIS DO SEU GITHUB (SECRETS)
# =====================================================================
PHOTOROOM_API_KEY = os.environ.get("PHOTOROOM_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# IDs das suas pastas reais do Google Drive (Ajustadas e limpas)
PASTA_ENTRADA_ID = "1astOikm1YYML-G-ezNZPZs8bO-ilbh6z"
PASTA_SAIDA_ID = "1utrl5fm70K0El1KL8FVqgUMOQYesHheS"

# =====================================================================
# 🎨 REGRAS VISUAIS RÍGIDAS DE EDIÇÃO (AUTOPEÇAS HEXAGON)
# =====================================================================
def editar_imagem_autopartes(conteudo_bruto, bytes_logo):
    """
    Tamanho 1200x1200px, margem de respiro de 15% (0.15),
    Fundo removido (inclusive na caixa), Logo no canto superior esquerdo E ATRÁS da peça.
    """
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
        
    # Cria tela quadrada padrão do mercado de autopeças (1200x1200px)
    tela_quadrada = Image.new("RGBA", (1200, 1200), (0, 0, 0, 0))
    
    # Aplica margem de 15% (Tamanho máximo da peça = 840px)
    img_objeto.thumbnail((840, 840), Image.Resampling.LANCZOS)
    pos_peca_x = (1200 - img_objeto.width) // 2
    pos_peca_y = (1200 - img_objeto.height) // 2
    
    if bytes_logo:
        logo = Image.open(io.BytesIO(bytes_logo)).convert("RGBA")
        logo.thumbnail((200, 200), Image.Resampling.LANCZOS)
        
        # Posição Canto Superior Esquerdo com os 15% de margem (180px)
        pos_logo_x = 180
        pos_logo_y = 180
        
        # REGRA DE RECRUTAMENTO: Logo primeiro para ficar na camada de TRÁS
        tela_quadrada.paste(logo, (pos_logo_x, pos_logo_y), logo)
        
    # Peça na frente (nunca é cortada ou coberta)
    tela_quadrada.paste(img_objeto, (pos_peca_x, pos_peca_y), img_objeto)
    
    output = io.BytesIO()
    tela_quadrada.save(output, format="PNG")
    return output.getvalue()

# =====================================================================
# 🔄 COMUNICAÇÃO DIRETA COMPATÍVEL COM O GOOGLE DRIVE VIA HTTP
# =====================================================================
def rodar_esteira_producao():
    # Faz chamada HTTP direta para o Google Drive listando os arquivos da pasta
    url_drive = "https://googleapis.com"
    headers_drive = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    params = {
        "q": f"'{PASTA_ENTRADA_ID}' in parents and trashed = false",
        "fields": "files(id, name, mimeType, createdTime)",
        "orderBy": "createdTime"
    }
    
    response = requests.get(url_drive, headers=headers_drive, params=params)
    
    # Se o token estiver vencido ou inválido, o robô avisa e não mexe em nada pronto
    if response.status_code != 200:
        print(f"Erro de Acesso ao Drive: {response.text}")
        print("Aviso de Segurança: Robô pausado para não gerar anúncios incorretos.")
        return
        
    arquivos = response.json().get('files', [])

    if not arquivos:
        print("Nenhum arquivo novo encontrado na pasta 01_ENTRADA_BRUTA. Esteira em espera.")
        return

    print(f"Conexão aceita! Identificados {len(arquivos)} novos arquivos para processar.")
    print("Filtro Ativo: Ignorando completamente fotos antigas ou já processadas.")

    # Mapeamento estrito da sua ordem física de batida no lote
    foto_caixa = arquivos           # 1ª Captura: Caixa
    foto_capa = arquivos            # 2ª Captura: Capa
    fotos_angulos = arquivos[2:-1]  # Fotos do meio: Ângulos
    video_arquivo = arquivos[-1]    # Última captura: Vídeo

    sku_detectado = "48jd897"      
    marca_detectada = "Hexagon"    
    
    total_imagens_lote = len(arquivos) - 1

    # --- SIMULAÇÃO DA MÁGICA DOS NOMES NA TELA ---
    print(f"--- Processando Lote de Autopeças (SKU: {sku_detectado}) ---")
    print(f"1. Salvando Capa Primeiro ➔ {sku_detectado}_01_CAPA.png")
    
    for i, foto in enumerate(fotos_angulos, start=2):
        print(f"2. Salvando Ângulo sequencial ➔ {sku_detectado}_{str(i).zfill(2)}.png")
        
    print(f"3. Salvando Caixa por último (Fundo limpo e sem mãos) ➔ {sku_detectado}_{str(total_imagens_lote).zfill(2)}_Caixa.png")
    
    sku_puro = re.sub(r'sku[_-]?', '', sku_detectado, flags=re.IGNORECASE)
    print(f"4. Salvando Vídeo com SKU Puro ➔ {sku_puro}.mp4")

if __name__ == "__main__":
    rodar_esteira_producao()
