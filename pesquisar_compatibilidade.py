"""
Pesquisar Compatibilidade - Automacao de pesquisa de compatibilidade por SKU
==============================================================================

O QUE FAZ:
  Le a aba "Staging" da planilha (linhas com Status = "Aguardando Pesquisa"),
  e pra cada SKU:

    1. Se a MARCA da peca tem um catalogo em PDF na pasta do Google Drive
       -> le o PDF, procura o SKU, e so preenche a compatibilidade se achar
          com clareza (nunca chuta).
    2. Se a marca NAO tem PDF
       -> faz busca na web (via IA com acesso a busca real) em sites
          oficiais/confiaveis, e so preenche se achar uma fonte clara.
    3. VALIDACAO EXTRA: a montadora extraida (PDF ou web) e conferida
       contra a base oficial de montadoras do Mercado Livre
       (Base_Compatibilidade_Mercado_Livre.xlsx, bundled no repositorio).
       Se vier um nome de montadora que NAO existe nessa base (provavel
       erro/invencao), NAO aprova sozinho - cai pra revisao manual, mesmo
       que a IA tenha dito "confiante".
    4. Se nao achar em NENHUM dos casos acima, ou a validacao falhar
       -> NAO preenche a compatibilidade. Marca Status = "VERIFICAR_MANUAL"
          e grava o motivo, pra alguem olhar na mao depois. Nunca escreve
          um "talvez".
    5. Sempre que preenche de verdade, grava TAMBEM de onde veio a
       informacao (coluna "Fonte da Pesquisa") - nome do PDF, ou a URL
       exata usada na busca.

REGRA DE OURO (pedida explicitamente): nenhuma informacao com duvida entra
na planilha. Duvida = nao preenche, so aponta que precisa de revisao manual.

ONDE FICAM OS CATALOGOS:
  Uma pasta no Google Drive (ex: "CATALOGOS_PECAS"), com um PDF por marca.
  O nome do arquivo (sem extensao) e comparado com a Marca (peca) do SKU,
  ignorando espaco/underscore/hifen/acento/maiuscula (mesma logica ja usada
  pro robo de midias na hora de achar o logo certo).

ARQUIVO DE VALIDACAO (precisa estar no repositorio):
  data/base_compatibilidade_mercado_livre.xlsx
  (a mesma planilha de referencia usada no projeto "carrosweb" - colunas
  Marca/Modelo/Ano/Submodelo, baixada do Mercado Livre)

SEGREDOS ESPERADOS (GitHub Secrets -> variaveis de ambiente):
  GOOGLE_SERVICE_ACCOUNT_JSON  (o mesmo ja usado pelo robo de midias)
  PLANILHA_ANUNCIOS_ML_ID      (o mesmo ja usado pelo robo de midias)
  OPENAI_API_KEY                (o mesmo ja usado pelo robo de midias)
  DRIVE_CATALOGOS_FOLDER_ID     (NOVO - id da pasta do Drive com os PDFs)

IMPORTANTE - ACESSO A PASTA DO DRIVE:
  A conta de servico (robo-midias@automacao-505720.iam.gserviceaccount.com)
  precisa ser convidada como "Leitor" na pasta de catalogos no Drive, senao
  o script nao enxerga os PDFs. Isso e feito uma vez so, direto no Google
  Drive (botao Compartilhar).

IMPORTANTE - COLUNA NOVA NA PLANILHA:
  Esse script espera uma coluna "Fonte da Pesquisa" na aba Staging (coluna
  J, logo depois de "Status", que e a coluna I). Se ela nao existir ainda,
  precisa ser criada manualmente uma vez (so o cabecalho na linha 3, mesma
  linha dos outros titulos de coluna) - o script so escreve o conteudo.

COMO RODAR:
  python pesquisar_compatibilidade.py
  (workflow_dispatch manual no GitHub Actions - nao roda em cron sozinho,
  pra voce controlar quando disparar)
"""

import os
import io
import re
import json
import unicodedata

import requests
import gspread
import openpyxl
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ============================================================
# CONFIGURACAO
# ============================================================
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
PLANILHA_ID = os.environ["PLANILHA_ANUNCIOS_ML_ID"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
DRIVE_CATALOGOS_FOLDER_ID = os.environ["DRIVE_CATALOGOS_FOLDER_ID"]

ABA_STAGING = "Staging"

# Mesma estrutura real do sheets_integration.py - cabecalho na linha 3,
# dados a partir da linha 4.
STAGING_HEADER_ROW = 3
STAGING_FIRST_DATA_ROW = 4

STAGING_COLS = [
    "SKU",                             # A
    "Marca (peça)",                    # B
    "Montadora",                       # C
    "Veículos Compatíveis",            # D
    "Motor(es)",                       # E
    "Ano",                             # F
    "Caminho Mídia (pasta do SKU)",    # G
    "Data Adicionado",                 # H
    "Status",                          # I
    "Fonte da Pesquisa",               # J - coluna nova, precisa existir na planilha
]
COL_LETRA = {nome: chr(ord("A") + i) for i, nome in enumerate(STAGING_COLS)}

STATUS_AGUARDANDO_PESQUISA = "Aguardando Pesquisa"
STATUS_COMPLETO = "Completo"
STATUS_VERIFICAR_MANUAL = "VERIFICAR_MANUAL"

CAMINHO_BASE_ML = os.path.join(os.path.dirname(__file__), "data", "base_compatibilidade_mercado_livre.xlsx")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

MODELO_TEXTO = "gpt-4o-mini"
MODELO_BUSCA_WEB = "gpt-5-search-api"  # modelo da OpenAI com busca web real embutida (gpt-4o-search-preview foi descontinuado)


# ============================================================
# NORMALIZACAO (mesma logica ja usada no main.py e no carrosweb)
# ============================================================
def normalizar(texto):
    if not texto:
        return ""
    texto = str(texto).upper()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]", "", texto)


# ============================================================
# BASE DE VALIDACAO (montadoras oficiais do Mercado Livre)
# ============================================================
def carregar_montadoras_validas():
    if not os.path.exists(CAMINHO_BASE_ML):
        print(f"AVISO: base de validacao '{CAMINHO_BASE_ML}' nao encontrada - "
              f"validacao de montadora fica DESATIVADA nessa execucao.")
        return set(), []

    wb = openpyxl.load_workbook(CAMINHO_BASE_ML, data_only=True, read_only=True)
    ws = wb.active
    marcas_originais = sorted(set(
        row[0] for row in ws.iter_rows(min_row=2, values_only=True) if row[0]
    ))
    wb.close()
    marcas_normalizadas = {normalizar(m) for m in marcas_originais}
    return marcas_normalizadas, marcas_originais


def montadora_e_valida(montadora, marcas_validas_normalizadas):
    if not montadora:
        return False
    if normalizar(montadora) in {"UNIVERSAL", "DIVERSAS", "DIVERSOS", "NAOSEAPLICA"}:
        return True
    return normalizar(montadora) in marcas_validas_normalizadas


# ============================================================
# AUTENTICACAO GOOGLE (Sheets via gspread + Drive via google-api-python-client)
# ============================================================
def autenticar_google():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    credenciais = Credentials.from_service_account_info(info, scopes=SCOPES)
    cliente_sheets = gspread.authorize(credenciais)
    planilha = cliente_sheets.open_by_key(PLANILHA_ID)
    aba_staging = planilha.worksheet(ABA_STAGING)
    drive = build("drive", "v3", credentials=credenciais)
    return aba_staging, drive


# ============================================================
# PLANILHA - LEITURA E ESCRITA (via gspread, mesma lib do sheets_integration.py)
# ============================================================
def ler_linhas_pendentes(aba_staging):
    todas = aba_staging.get_all_values()
    if len(todas) <= STAGING_HEADER_ROW:
        return []

    linhas_dados = todas[STAGING_HEADER_ROW:]
    idx_sku = STAGING_COLS.index("SKU")
    idx_marca = STAGING_COLS.index("Marca (peça)")
    idx_status = STAGING_COLS.index("Status")

    pendentes = []
    for offset, linha in enumerate(linhas_dados):
        linha_completa = linha + [""] * (len(STAGING_COLS) - len(linha))
        sku = linha_completa[idx_sku].strip()
        marca = linha_completa[idx_marca].strip()
        status = linha_completa[idx_status].strip()
        if status == STATUS_AGUARDANDO_PESQUISA and sku:
            numero_linha_real = STAGING_FIRST_DATA_ROW + offset
            pendentes.append({"linha": numero_linha_real, "sku": sku, "marca": marca})
    return pendentes


def escrever_resultado(aba_staging, numero_linha, montadora, veiculos, motor, ano, status, fonte):
    intervalo = f"{COL_LETRA['Montadora']}{numero_linha}:{COL_LETRA['Fonte da Pesquisa']}{numero_linha}"
    aba_staging.update(range_name=intervalo, values=[[montadora, veiculos, motor, ano, status, fonte]], value_input_option="USER_ENTERED")


def marcar_verificar_manual(aba_staging, numero_linha, motivo):
    intervalo = f"{COL_LETRA['Status']}{numero_linha}:{COL_LETRA['Fonte da Pesquisa']}{numero_linha}"
    aba_staging.update(range_name=intervalo, values=[[STATUS_VERIFICAR_MANUAL, motivo]], value_input_option="USER_ENTERED")


# ============================================================
# DRIVE - ACHAR E BAIXAR O PDF DA MARCA
# ============================================================
def listar_pdfs_catalogo(drive):
    arquivos = []
    page_token = None
    while True:
        resposta = drive.files().list(
            q=f"'{DRIVE_CATALOGOS_FOLDER_ID}' in parents and mimeType='application/pdf' and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
        ).execute()
        arquivos.extend(resposta.get("files", []))
        page_token = resposta.get("nextPageToken")
        if not page_token:
            break
    return arquivos


def achar_pdf_da_marca(marca, pdfs_disponiveis):
    alvo = normalizar(marca)
    if not alvo:
        return None
    for arq in pdfs_disponiveis:
        nome_sem_ext = re.sub(r"\.pdf$", "", arq["name"], flags=re.IGNORECASE)
        if normalizar(nome_sem_ext) == alvo:
            return arq
    return None


def baixar_pdf(drive, file_id):
    request = drive.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    concluido = False
    while not concluido:
        _, concluido = downloader.next_chunk()
    buffer.seek(0)
    return buffer.read()


# ============================================================
# EXTRACAO DE TEXTO DO PDF E BUSCA DO SKU
# ============================================================
def extrair_contexto_do_sku_no_pdf(pdf_bytes, sku):
    import pdfplumber

    sku_normalizado = normalizar(sku)
    trechos_encontrados = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for numero_pagina, pagina in enumerate(pdf.pages, start=1):
            texto = pagina.extract_text() or ""
            texto_normalizado = normalizar(texto)
            if sku_normalizado and sku_normalizado in texto_normalizado:
                trechos_encontrados.append({"pagina": numero_pagina, "texto": texto[:4000]})
    return trechos_encontrados


def extrair_compatibilidade_de_texto(sku, marca, trechos, nome_arquivo, marcas_validas_originais):
    if not trechos:
        return {"confiante": False, "motivo": "SKU nao encontrado no texto do PDF"}

    texto_combinado = "\n\n---PAGINA NOVA---\n\n".join(
        f"[Pagina {t['pagina']}]\n{t['texto']}" for t in trechos[:5]
    )
    lista_montadoras = ", ".join(marcas_validas_originais) if marcas_validas_originais else ""

    instrucao_lista = (
        "IMPORTANTE: quando preencher o campo montadora com uma montadora "
        "especifica (nao universal), use EXATAMENTE um destes nomes, com a "
        f"mesma grafia (essa e a lista oficial de montadoras do Mercado "
        f"Livre): {lista_montadoras}\n\n"
        if lista_montadoras else ""
    )

    payload = {
        "model": MODELO_TEXTO,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Este e um trecho extraido do catalogo oficial em PDF da marca "
                    f"'{marca}', nas paginas onde o codigo de peca '{sku}' aparece.\n\n"
                    f"{texto_combinado}\n\n"
                    "Com base SOMENTE nesse texto, extraia a compatibilidade veicular "
                    "dessa peca especifica (codigo mencionado acima). Se a peca for "
                    "universal (nao amarrada a montadora/modelo especifico), diga isso "
                    "claramente e deixe montadora='Universal'.\n\n"
                    + instrucao_lista +
                    "Se o texto NAO deixar claro a compatibilidade dessa peca "
                    "especifica (ambiguo, cortado, ou o codigo aparece mas sem dado "
                    "de aplicacao junto), responda confiante=false - NUNCA arrisque "
                    "um palpite.\n\n"
                    "Responda SOMENTE em JSON, sem texto adicional, no formato:\n"
                    '{"confiante": true/false, "montadora": "...", '
                    '"veiculos_compativeis": "...", "motor": "...", "ano": "...", '
                    '"motivo": "..."} \n'
                    "(motivo so precisa ser preenchido quando confiante=false)"
                ),
            }
        ],
        "max_tokens": 400,
        "temperature": 0,
    }
    resposta = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    resposta.raise_for_status()
    texto_resposta = resposta.json()["choices"][0]["message"]["content"].strip()
    texto_resposta = texto_resposta.replace("```json", "").replace("```", "").strip()
    dados = json.loads(texto_resposta)
    dados["fonte"] = f"PDF: {nome_arquivo}"
    return dados


# ============================================================
# BUSCA NA WEB (pra marcas sem PDF)
# ============================================================
def buscar_compatibilidade_na_web(sku, marca, marcas_validas_originais):
    lista_montadoras = ", ".join(marcas_validas_originais) if marcas_validas_originais else ""

    instrucao_lista = (
        "IMPORTANTE: quando preencher o campo montadora com uma montadora "
        "especifica, use EXATAMENTE um destes nomes, com a mesma grafia "
        f"(lista oficial do Mercado Livre): {lista_montadoras}\n\n"
        if lista_montadoras else ""
    )

    payload = {
        "model": MODELO_BUSCA_WEB,
        "web_search_options": {},
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Pesquise na web a compatibilidade veicular exata da peca "
                    f"'{sku}' da marca '{marca}' (autopeca, mercado brasileiro). "
                    "Priorize o site oficial do fabricante; na falta dele, use "
                    "um distribuidor/loja confiavel que cite claramente a "
                    "aplicacao (montadora, modelo, ano, motor).\n\n"
                    "Se a peca for universal (nao amarrada a um veiculo "
                    "especifico), diga isso e deixe montadora='Universal'.\n\n"
                    + instrucao_lista +
                    "Se NAO encontrar uma fonte que confirme claramente a "
                    "aplicacao desse codigo especifico, responda confiante=false - "
                    "nunca arrisque um palpite baseado em codigos parecidos ou peca "
                    "semelhante de outra marca.\n\n"
                    "Responda SOMENTE em JSON, sem texto adicional, no formato:\n"
                    '{"confiante": true/false, "montadora": "...", '
                    '"veiculos_compativeis": "...", "motor": "...", "ano": "...", '
                    '"fonte_url": "...", "motivo": "..."} \n'
                    "(fonte_url e obrigatorio quando confiante=true. motivo so "
                    "quando confiante=false.)"
                ),
            }
        ],
        "max_tokens": 500,
    }
    resposta = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=90,
    )
    resposta.raise_for_status()
    texto_resposta = resposta.json()["choices"][0]["message"]["content"].strip()
    texto_resposta = texto_resposta.replace("```json", "").replace("```", "").strip()
    dados = json.loads(texto_resposta)
    dados["fonte"] = dados.get("fonte_url", "") or ""
    return dados


# ============================================================
# PROCESSAMENTO DE UM SKU
# ============================================================
def processar_sku(aba_staging, drive, item, pdfs_disponiveis, marcas_validas_normalizadas, marcas_validas_originais):
    sku = item["sku"]
    marca = item["marca"]
    numero_linha = item["linha"]

    print(f"Pesquisando SKU '{sku}' (marca: '{marca}')...")

    pdf_da_marca = achar_pdf_da_marca(marca, pdfs_disponiveis)

    if pdf_da_marca:
        print(f"  Catalogo em PDF encontrado: '{pdf_da_marca['name']}' - lendo...")
        try:
            pdf_bytes = baixar_pdf(drive, pdf_da_marca["id"])
            trechos = extrair_contexto_do_sku_no_pdf(pdf_bytes, sku)
            resultado = extrair_compatibilidade_de_texto(sku, marca, trechos, pdf_da_marca["name"], marcas_validas_originais)
        except Exception as e:
            print(f"  ERRO lendo/processando o PDF: {repr(e)}")
            marcar_verificar_manual(aba_staging, numero_linha, f"Erro tecnico lendo PDF '{pdf_da_marca['name']}': {repr(e)}")
            return
    else:
        print("  Sem PDF cadastrado pra essa marca - buscando na web...")
        try:
            resultado = buscar_compatibilidade_na_web(sku, marca, marcas_validas_originais)
        except Exception as e:
            print(f"  ERRO na busca web: {repr(e)}")
            marcar_verificar_manual(aba_staging, numero_linha, f"Erro tecnico na busca web: {repr(e)}")
            return

    if not resultado.get("confiante"):
        motivo = resultado.get("motivo", "Nao foi possivel confirmar com clareza")
        marcar_verificar_manual(aba_staging, numero_linha, motivo)
        print(f"  SEM CONFIANCA - marcado pra revisao manual: {motivo}")
        return

    montadora = resultado.get("montadora", "")
    if not montadora_e_valida(montadora, marcas_validas_normalizadas):
        motivo = (
            f"IA disse confiante, mas a montadora '{montadora}' nao existe na base "
            f"oficial do Mercado Livre - provavel erro, revisar na mao"
        )
        marcar_verificar_manual(aba_staging, numero_linha, motivo)
        print(f"  REPROVADO NA VALIDACAO: {motivo}")
        return

    escrever_resultado(
        aba_staging,
        numero_linha,
        montadora=montadora,
        veiculos=resultado.get("veiculos_compativeis", ""),
        motor=resultado.get("motor", ""),
        ano=resultado.get("ano", ""),
        status=STATUS_COMPLETO,
        fonte=resultado.get("fonte", ""),
    )
    print(f"  OK - compatibilidade confirmada e validada (fonte: {resultado.get('fonte', '')})")


# ============================================================
# MAIN
# ============================================================
def main():
    aba_staging, drive = autenticar_google()

    pendentes = ler_linhas_pendentes(aba_staging)
    if not pendentes:
        print("Nenhum SKU com Status = 'Aguardando Pesquisa' encontrado.")
        return

    print(f"{len(pendentes)} SKU(s) pendente(s) de pesquisa.\n")

    pdfs_disponiveis = listar_pdfs_catalogo(drive)
    print(f"{len(pdfs_disponiveis)} catalogo(s) em PDF disponivel(is) na pasta do Drive.")

    marcas_validas_normalizadas, marcas_validas_originais = carregar_montadoras_validas()
    print(f"{len(marcas_validas_originais)} montadora(s) na base de validacao do Mercado Livre.\n")

    for item in pendentes:
        try:
            processar_sku(aba_staging, drive, item, pdfs_disponiveis, marcas_validas_normalizadas, marcas_validas_originais)
        except Exception as e:
            print(f"ERRO inesperado processando SKU '{item['sku']}': {repr(e)}")
            try:
                marcar_verificar_manual(aba_staging, item["linha"], f"Erro tecnico inesperado: {repr(e)}")
            except Exception:
                pass

    print("\nConcluido.")


if __name__ == "__main__":
    main()
