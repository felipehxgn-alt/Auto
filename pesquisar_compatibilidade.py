"""
Pesquisar Compatibilidade - Automacao de pesquisa de compatibilidade por SKU
==============================================================================

O QUE FAZ:
  Le a aba "Staging" (linhas com Status = "Aguardando Pesquisa"), e pra
  cada SKU:

    1. Se a MARCA da peca tem um catalogo em PDF no Google Drive -> le o
       PDF e procura o SKU. Se nao tiver PDF -> busca na web (IA com
       busca real) em fontes oficiais/confiaveis.
    2. So preenche informacao quando tiver clareza (nunca chuta). Se nao
       achar nada confiavel, marca Status = "VERIFICAR_MANUAL" e registra
       o motivo na aba "Verificar" (nao so na linha - fica uma lista
       centralizada de tudo que precisa de revisao humana).
    3. VALIDACAO: a Montadora extraida e conferida contra a base oficial
       de montadoras do Mercado Livre (data/base_compatibilidade_mercado_
       livre.xlsx). Nome que nao existe la = nao aprova sozinho.
    4. Preenche tambem:
       - % de Certeza das Informacoes (estimativa da IA, baseada no
         numero de fontes confirmadas)
       - Confiavel (Sim/Nao) - resumo binario
       - Aba "Compatibilidades": uma LINHA POR combinacao Montadora +
         Modelo + Ano + Motor (formato pra upload tecnico, nao texto
         corrido)
       - Aba "Outros Anuncios por Veiculo": uma linha por variacao
         especifica de veiculo, com Titulo Comercial pronto (max 60
         caracteres, limite do Mercado Livre) alem do titulo generico
    5. Sempre grava a FONTE usada (nome do PDF, ou URL da busca web) -
       tanto na Staging quanto em cada linha das abas novas.

REGRA DE OURO: nenhuma informacao com duvida entra na planilha como
definitiva. Duvida = VERIFICAR_MANUAL + linha na aba Verificar.

ABAS QUE O SCRIPT CRIA SOZINHO SE NAO EXISTIREM (com cabecalho pronto):
  - Compatibilidades
  - Outros Anúncios por Veículo
  - Verificar
  Você NÃO precisa criar essas 3 manualmente - só rodar o script.

COLUNAS NOVAS QUE VOCÊ PRECISA CRIAR NA STAGING (essas sim, manual):
  "% de Certeza" e "Confiável (Sim/Não)", logo depois de "Fonte da
  Pesquisa". O script lê a posição de TODAS as colunas pelo NOME do
  cabeçalho (não por posição fixa), então a ordem exata não importa,
  só o nome tem que bater.

ARQUIVO DE VALIDAÇÃO (precisa estar no repositório):
  data/base_compatibilidade_mercado_livre.xlsx

SEGREDOS ESPERADOS (GitHub Secrets):
  GOOGLE_SERVICE_ACCOUNT_JSON, PLANILHA_ANUNCIOS_ML_ID, OPENAI_API_KEY,
  DRIVE_CATALOGOS_FOLDER_ID

COMO RODAR:
  python pesquisar_compatibilidade.py
  (workflow_dispatch manual no GitHub Actions)
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
ABA_COMPATIBILIDADES = "Compatibilidades"
ABA_OUTROS_ANUNCIOS = "Outros Anúncios por Veículo"
ABA_VERIFICAR = "Verificar"

STAGING_HEADER_ROW = 3
STAGING_FIRST_DATA_ROW = 4

# Colunas que PRECISAM existir na Staging (por nome, nao por posicao).
# As duas ultimas sao novas - se nao existirem, o script avisa e para
# (nao adivinha onde colocar).
COLUNAS_STAGING_NECESSARIAS = [
    "SKU", "Marca (peça)", "Montadora", "Veículos Compatíveis",
    "Motor(es)", "Ano", "Caminho Mídia (pasta do SKU)", "Data Adicionado",
    "Status", "Fonte da Pesquisa", "% de Certeza", "Confiável (Sim/Não)",
]

# Cabecalho usado SO se a aba Compatibilidades ainda nao existir (fallback).
# Se ela ja existir com outro layout (como a real: SKU | Marca | Modelo |
# Ano | Versao | Motor | Transmissao | Posicao (Lado) | Observacao), o
# script detecta e escreve nas colunas certas pelo NOME, nunca assume
# que a ordem bate com essa lista.
CABECALHO_COMPATIBILIDADES = ["SKU", "Marca", "Modelo", "Ano", "Versão", "Motor", "Transmissão", "Posição (Lado)", "Observação"]
CABECALHO_OUTROS_ANUNCIOS = ["SKU", "Montadora", "Modelo", "Ano/Motor", "Título Comercial (≤60 caracteres)", "Título Principal (genérico)"]
CABECALHO_VERIFICAR = ["SKU", "Coluna a Verificar", "Motivo", "Link da Célula"]

STATUS_AGUARDANDO_PESQUISA = "Aguardando Pesquisa"
STATUS_COMPLETO = "Completo"
STATUS_VERIFICAR_MANUAL = "VERIFICAR_MANUAL"

CAMINHO_BASE_ML = os.path.join(os.path.dirname(__file__), "data", "base_compatibilidade_mercado_livre.xlsx")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

MODELO_TEXTO = "gpt-4o-mini"
MODELO_BUSCA_WEB = "gpt-5-search-api"  # gpt-4o-search-preview foi descontinuado

TAMANHO_MAXIMO_TITULO = 60


# ============================================================
# NORMALIZACAO
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
        print(f"AVISO: base de validacao '{CAMINHO_BASE_ML}' nao encontrada - validacao de montadora DESATIVADA.")
        return set(), []
    wb = openpyxl.load_workbook(CAMINHO_BASE_ML, data_only=True, read_only=True)
    ws = wb.active
    marcas_originais = sorted(set(row[0] for row in ws.iter_rows(min_row=2, values_only=True) if row[0]))
    wb.close()
    return {normalizar(m) for m in marcas_originais}, marcas_originais


def montadora_e_valida(montadora, marcas_validas_normalizadas):
    if not montadora:
        return False
    if normalizar(montadora) in {"UNIVERSAL", "DIVERSAS", "DIVERSOS", "NAOSEAPLICA"}:
        return True
    return normalizar(montadora) in marcas_validas_normalizadas


# ============================================================
# AUTENTICACAO
# ============================================================
def autenticar_google():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    credenciais = Credentials.from_service_account_info(info, scopes=SCOPES)
    cliente_sheets = gspread.authorize(credenciais)
    planilha = cliente_sheets.open_by_key(PLANILHA_ID)
    drive = build("drive", "v3", credentials=credenciais)
    return planilha, drive


def garantir_aba(planilha, nome_aba, cabecalho):
    """Cria a aba com o cabecalho pronto se ela ainda nao existir - o
    usuario nao precisa criar essas abas na mao."""
    try:
        ws = planilha.worksheet(nome_aba)
        print(f"Aba '{nome_aba}' ja existe - reaproveitando.")
    except gspread.WorksheetNotFound:
        ws = planilha.add_worksheet(title=nome_aba, rows=2000, cols=max(10, len(cabecalho)))
        ws.append_row(cabecalho, value_input_option="USER_ENTERED")
        print(f"Aba '{nome_aba}' nao existia - criada agora com o cabecalho.")
    return ws


def achar_linha_cabecalho(aba, max_linhas=6):
    """Acha em qual linha esta o cabecalho de verdade (procura uma linha
    que tenha 'SKU' em alguma celula) - nao assume linha 1 nem linha 3,
    porque abas criadas na mao (como a Compatibilidades, que ja existia
    antes desse script) podem ter titulo/descricao em linhas antes do
    cabecalho. Retorna None se a aba estiver vazia."""
    valores = aba.get_all_values()[:max_linhas]
    for i, linha in enumerate(valores):
        if any(c.strip().upper() == "SKU" for c in linha):
            return i + 1  # numero de linha real (1-indexed)
    return None


def preparar_aba_com_cabecalho_flexivel(planilha, nome_aba, cabecalho_padrao):
    """Garante a aba e devolve (worksheet, mapa_de_colunas), respeitando
    o cabecalho JA EXISTENTE se a aba ja tiver sido criada na mao com
    outro layout - nunca assume que a ordem das colunas bate com o que
    esse script esperaria por padrao. So usa cabecalho_padrao se a aba
    for nova ou estiver vazia."""
    try:
        ws = planilha.worksheet(nome_aba)
        linha_cabecalho = achar_linha_cabecalho(ws)
        if linha_cabecalho is None:
            ws.append_row(cabecalho_padrao, value_input_option="USER_ENTERED")
            cabecalho_real = cabecalho_padrao
            print(f"Aba '{nome_aba}' existia mas estava vazia - cabecalho padrao adicionado.")
        else:
            cabecalho_real = ws.row_values(linha_cabecalho)
            print(f"Aba '{nome_aba}' ja existe com cabecalho proprio (linha {linha_cabecalho}) - escrevendo pelas colunas reais dela.")
    except gspread.WorksheetNotFound:
        ws = planilha.add_worksheet(title=nome_aba, rows=2000, cols=max(10, len(cabecalho_padrao)))
        ws.append_row(cabecalho_padrao, value_input_option="USER_ENTERED")
        cabecalho_real = cabecalho_padrao
        print(f"Aba '{nome_aba}' nao existia - criada agora com o cabecalho padrao.")
    return ws, mapa_colunas_pelo_cabecalho(cabecalho_real)


def mapa_colunas_pelo_cabecalho(cabecalho):
    return {nome.strip(): i for i, nome in enumerate(cabecalho) if nome.strip()}


def montar_linha_por_nomes(colunas, valores_por_nome):
    """Monta uma linha (lista) na largura da aba real, colocando cada
    valor na coluna certa PELO NOME. valores_por_nome e uma lista de
    (lista_de_nomes_aceitos, valor) - tenta cada nome aceito em ordem
    ate achar um que exista no cabecalho real; se nenhum existir, esse
    valor e simplesmente omitido (nunca quebra o script por causa de
    uma coluna que nao existe na aba)."""
    linha = [""] * (max(colunas.values()) + 1 if colunas else 0)
    for nomes_aceitos, valor in valores_por_nome:
        for nome in nomes_aceitos:
            if nome in colunas:
                linha[colunas[nome]] = valor
                break
    return linha


# ============================================================
# PLANILHA - STAGING (leitura/escrita por nome de coluna)
# ============================================================
def ler_linhas_pendentes(aba_staging):
    todas = aba_staging.get_all_values()
    if len(todas) <= STAGING_HEADER_ROW:
        return [], {}

    cabecalho = todas[STAGING_HEADER_ROW - 1]
    colunas = mapa_colunas_pelo_cabecalho(cabecalho)

    faltando = [c for c in COLUNAS_STAGING_NECESSARIAS if c not in colunas]
    if faltando:
        raise RuntimeError(
            f"Coluna(s) esperada(s) não encontrada(s) no cabeçalho real da "
            f"aba Staging (linha {STAGING_HEADER_ROW}): {faltando}. "
            f"Confere se o nome está escrito EXATAMENTE igual."
        )

    largura_minima = max(colunas.values()) + 1
    linhas_dados = todas[STAGING_HEADER_ROW:]

    pendentes = []
    for offset, linha in enumerate(linhas_dados):
        linha_completa = linha + [""] * (largura_minima - len(linha))
        sku = linha_completa[colunas["SKU"]].strip()
        marca = linha_completa[colunas["Marca (peça)"]].strip()
        status = linha_completa[colunas["Status"]].strip()
        if status == STATUS_AGUARDANDO_PESQUISA and sku:
            numero_linha_real = STAGING_FIRST_DATA_ROW + offset
            pendentes.append({"linha": numero_linha_real, "sku": sku, "marca": marca})
    return pendentes, colunas


def escrever_resultado_staging(aba_staging, colunas, numero_linha, montadora, veiculos, motor, ano, status, fonte, certeza, confiavel):
    valores_por_coluna = {
        "Montadora": montadora, "Veículos Compatíveis": veiculos, "Motor(es)": motor,
        "Ano": ano, "Status": status, "Fonte da Pesquisa": fonte,
        "% de Certeza": certeza, "Confiável (Sim/Não)": confiavel,
    }
    # agrupa em um unico range contiguo quando possivel; como as colunas
    # podem estar espalhadas (usuario pode ter reordenado), atualiza uma
    # celula de cada vez - mais chamadas, mas 100% a prova de posicao
    for nome_coluna, valor in valores_por_coluna.items():
        numero_coluna = colunas[nome_coluna] + 1  # gspread e 1-indexed
        aba_staging.update_cell(numero_linha, numero_coluna, valor)


def marcar_verificar_manual_staging(aba_staging, colunas, numero_linha, motivo):
    aba_staging.update_cell(numero_linha, colunas["Status"] + 1, STATUS_VERIFICAR_MANUAL)
    aba_staging.update_cell(numero_linha, colunas["% de Certeza"] + 1, 0)
    aba_staging.update_cell(numero_linha, colunas["Confiável (Sim/Não)"] + 1, "Não")


def link_da_celula(aba, numero_linha, nome_coluna, colunas_aba):
    numero_coluna = colunas_aba[nome_coluna] + 1
    letra_coluna = gspread.utils.rowcol_to_a1(1, numero_coluna).rstrip("1")
    return f"https://docs.google.com/spreadsheets/d/{PLANILHA_ID}/edit#gid={aba.id}&range={letra_coluna}{numero_linha}"


# ============================================================
# DRIVE - PDFs
# ============================================================
def listar_pdfs_catalogo(drive):
    arquivos, page_token = [], None
    while True:
        resposta = drive.files().list(
            q=f"'{DRIVE_CATALOGOS_FOLDER_ID}' in parents and mimeType='application/pdf' and trashed=false",
            fields="nextPageToken, files(id, name)", pageToken=page_token,
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


def extrair_contexto_do_sku_no_pdf(pdf_bytes, sku):
    import pdfplumber
    sku_normalizado = normalizar(sku)
    trechos = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for numero_pagina, pagina in enumerate(pdf.pages, start=1):
            texto = pagina.extract_text() or ""
            if sku_normalizado and sku_normalizado in normalizar(texto):
                trechos.append({"pagina": numero_pagina, "texto": texto[:4000]})
    return trechos


# ============================================================
# PROMPT COMPARTILHADO (PDF e busca web pedem a MESMA estrutura de saida)
# ============================================================
def instrucao_formato_resposta(lista_montadoras):
    instrucao_lista = (
        "Quando preencher 'montadora' (aqui e dentro de cada item de "
        "'combinacoes'), use EXATAMENTE um destes nomes, mesma grafia "
        f"(lista oficial de montadoras do Mercado Livre): {lista_montadoras}\n\n"
        if lista_montadoras else ""
    )
    return (
        instrucao_lista +
        "Se a peca for universal (nao amarrada a montadora/modelo "
        "especifico), monte 'combinacoes' como uma lista vazia [] e "
        "'variacoes_anuncio' com 1 item generico so.\n\n"
        "Para CADA combinacao real de veiculo compativel, crie um item em "
        "'combinacoes' E um item correspondente em 'variacoes_anuncio'.\n\n"
        "'titulo_comercial' em cada item de variacoes_anuncio: titulo "
        "pronto pro Mercado Livre, INCLUINDO marca da peca + tipo da peca "
        "+ montadora + modelo, NUNCA ultrapassando "
        f"{TAMANHO_MAXIMO_TITULO} caracteres (conte os caracteres antes de "
        "responder - se passar do limite, corte palavras menos "
        "importantes, nunca corte no meio de uma palavra).\n\n"
        "'certeza_percentual': sua estimativa de 0 a 100 de confianca "
        "nessa informacao, baseada em quantas fontes/evidencias no texto "
        "confirmam a aplicacao (nao invente uma fonte que nao existe so "
        "pra justificar numero alto).\n\n"
        "Se NAO houver informacao clara o suficiente, responda "
        "confiante=false e deixe combinacoes/variacoes_anuncio vazios - "
        "NUNCA arrisque um palpite.\n\n"
        "Responda SOMENTE em JSON, sem texto adicional, formato exato:\n"
        '{"confiante": true/false, "montadora": "...", '
        '"veiculos_compativeis": "...", "motor": "...", "ano": "...", '
        '"certeza_percentual": 0, '
        '"combinacoes": [{"montadora": "...", "modelo": "...", '
        '"ano_inicio": "...", "ano_fim": "...", "motor": "..."}], '
        '"variacoes_anuncio": [{"montadora": "...", "modelo": "...", '
        '"ano_motor": "...", "titulo_comercial": "...", '
        '"titulo_generico": "..."}], '
        '"motivo": "..."}'
    )


def extrair_compatibilidade_de_texto(sku, marca, trechos, nome_arquivo, marcas_validas_originais):
    if not trechos:
        return {"confiante": False, "motivo": "SKU nao encontrado no texto do PDF"}

    texto_combinado = "\n\n---PAGINA NOVA---\n\n".join(
        f"[Pagina {t['pagina']}]\n{t['texto']}" for t in trechos[:5]
    )
    lista_montadoras = ", ".join(marcas_validas_originais) if marcas_validas_originais else ""

    payload = {
        "model": MODELO_TEXTO,
        "messages": [{
            "role": "user",
            "content": (
                f"Trecho extraido do catalogo oficial em PDF da marca "
                f"'{marca}', paginas onde o codigo '{sku}' aparece.\n\n"
                f"{texto_combinado}\n\n"
                "Com base SOMENTE nesse texto, extraia a compatibilidade "
                "veicular dessa peca especifica.\n\n"
                + instrucao_formato_resposta(lista_montadoras)
            ),
        }],
        "max_tokens": 900,
        "temperature": 0,
    }
    resposta = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload, timeout=60,
    )
    resposta.raise_for_status()
    texto_resposta = resposta.json()["choices"][0]["message"]["content"].strip()
    texto_resposta = texto_resposta.replace("```json", "").replace("```", "").strip()
    dados = json.loads(texto_resposta)
    dados["fonte"] = f"PDF: {nome_arquivo}"
    return dados


def buscar_compatibilidade_na_web(sku, marca, marcas_validas_originais):
    lista_montadoras = ", ".join(marcas_validas_originais) if marcas_validas_originais else ""

    payload = {
        "model": MODELO_BUSCA_WEB,
        "web_search_options": {},
        "messages": [{
            "role": "user",
            "content": (
                f"Pesquise na web a compatibilidade veicular exata da peca "
                f"'{sku}' da marca '{marca}' (autopeca, mercado brasileiro). "
                "Priorize o site oficial do fabricante; na falta dele, use "
                "um distribuidor/loja confiavel com aplicacao clara.\n\n"
                + instrucao_formato_resposta(lista_montadoras) +
                '\n\nInclua tambem "fonte_url" (obrigatorio se confiante=true) '
                "com a URL exata da pagina usada."
            ),
        }],
        "max_tokens": 1200,
    }
    resposta = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload, timeout=90,
    )
    resposta.raise_for_status()
    texto_resposta = resposta.json()["choices"][0]["message"]["content"].strip()
    texto_resposta = texto_resposta.replace("```json", "").replace("```", "").strip()
    dados = json.loads(texto_resposta)
    dados["fonte"] = dados.get("fonte_url", "") or ""
    return dados


def truncar_titulo(titulo):
    """Ultima linha de defesa - se a IA passar do limite mesmo assim,
    corta por palavra inteira em vez de no meio (nunca confia 100% que a
    IA respeitou a instrucao)."""
    if len(titulo) <= TAMANHO_MAXIMO_TITULO:
        return titulo
    cortado = titulo[:TAMANHO_MAXIMO_TITULO]
    if " " in cortado:
        cortado = cortado.rsplit(" ", 1)[0]
    return cortado.strip()


# ============================================================
# PROCESSAMENTO DE UM SKU
# ============================================================
def processar_sku(planilha, aba_staging, colunas_staging, drive, item, pdfs_disponiveis,
                   marcas_validas_normalizadas, marcas_validas_originais,
                   aba_compat, colunas_compat, aba_outros, aba_verificar):
    sku, marca, numero_linha = item["sku"], item["marca"], item["linha"]
    print(f"Pesquisando SKU '{sku}' (marca: '{marca}')...")

    pdf_da_marca = achar_pdf_da_marca(marca, pdfs_disponiveis)

    if pdf_da_marca:
        print(f"  Catalogo em PDF encontrado: '{pdf_da_marca['name']}' - lendo...")
        try:
            pdf_bytes = baixar_pdf(drive, pdf_da_marca["id"])
            trechos = extrair_contexto_do_sku_no_pdf(pdf_bytes, sku)
            resultado = extrair_compatibilidade_de_texto(sku, marca, trechos, pdf_da_marca["name"], marcas_validas_originais)
        except Exception as e:
            marcar_verificar_manual_staging(aba_staging, colunas_staging, numero_linha, f"Erro tecnico lendo PDF: {repr(e)}")
            aba_verificar.append_row([sku, "Montadora/Compatibilidade", f"Erro lendo PDF: {repr(e)}", link_da_celula(aba_staging, numero_linha, "Status", colunas_staging)], value_input_option="USER_ENTERED")
            print(f"  ERRO: {repr(e)}")
            return
    else:
        print("  Sem PDF cadastrado - buscando na web...")
        try:
            resultado = buscar_compatibilidade_na_web(sku, marca, marcas_validas_originais)
        except Exception as e:
            marcar_verificar_manual_staging(aba_staging, colunas_staging, numero_linha, f"Erro tecnico na busca web: {repr(e)}")
            aba_verificar.append_row([sku, "Montadora/Compatibilidade", f"Erro na busca web: {repr(e)}", link_da_celula(aba_staging, numero_linha, "Status", colunas_staging)], value_input_option="USER_ENTERED")
            print(f"  ERRO: {repr(e)}")
            return

    if not resultado.get("confiante"):
        motivo = resultado.get("motivo", "Nao foi possivel confirmar com clareza")
        marcar_verificar_manual_staging(aba_staging, colunas_staging, numero_linha, motivo)
        aba_verificar.append_row([sku, "Montadora/Compatibilidade", motivo, link_da_celula(aba_staging, numero_linha, "Status", colunas_staging)], value_input_option="USER_ENTERED")
        print(f"  SEM CONFIANCA: {motivo}")
        return

    montadora = resultado.get("montadora", "")
    if not montadora_e_valida(montadora, marcas_validas_normalizadas):
        motivo = f"IA disse confiante, mas montadora '{montadora}' nao existe na base oficial ML"
        marcar_verificar_manual_staging(aba_staging, colunas_staging, numero_linha, motivo)
        aba_verificar.append_row([sku, "Montadora", motivo, link_da_celula(aba_staging, numero_linha, "Montadora", colunas_staging)], value_input_option="USER_ENTERED")
        print(f"  REPROVADO NA VALIDACAO: {motivo}")
        return

    certeza = resultado.get("certeza_percentual", 0)
    confiavel = "Sim" if certeza >= 70 else "Não"

    escrever_resultado_staging(
        aba_staging, colunas_staging, numero_linha,
        montadora=montadora, veiculos=resultado.get("veiculos_compativeis", ""),
        motor=resultado.get("motor", ""), ano=resultado.get("ano", ""),
        status=STATUS_COMPLETO, fonte=resultado.get("fonte", ""),
        certeza=certeza, confiavel=confiavel,
    )

    for combinacao in resultado.get("combinacoes", []):
        ano = combinacao.get("ano_inicio", "")
        ano_fim = combinacao.get("ano_fim", "")
        if ano_fim and ano_fim != ano:
            ano = f"{ano}-{ano_fim}" if ano else ano_fim
        linha = montar_linha_por_nomes(colunas_compat, [
            (["SKU"], sku),
            (["Marca", "Montadora"], combinacao.get("montadora", "")),
            (["Modelo"], combinacao.get("modelo", "")),
            (["Ano"], ano),
            (["Motor"], combinacao.get("motor", "")),
            # Versao/Transmissao/Posicao (Lado): a IA nao tem como saber
            # isso com confianca so pelo catalogo/busca geral - fica em
            # branco de proposito, nunca inventa, alguem preenche na mao
            # se precisar pra compatibilidade oficial do ML.
            (["Observação", "Observacao", "Fonte"], resultado.get("fonte", "")),
        ])
        aba_compat.append_row(linha, value_input_option="USER_ENTERED")

    for variacao in resultado.get("variacoes_anuncio", []):
        titulo_comercial = truncar_titulo(variacao.get("titulo_comercial", ""))
        aba_outros.append_row([
            sku, variacao.get("montadora", ""), variacao.get("modelo", ""),
            variacao.get("ano_motor", ""), titulo_comercial,
            variacao.get("titulo_generico", ""),
        ], value_input_option="USER_ENTERED")

    if certeza < 70:
        aba_verificar.append_row([
            sku, "% de Certeza", f"Certeza de apenas {certeza}% - revisar antes de publicar",
            link_da_celula(aba_staging, numero_linha, "% de Certeza", colunas_staging),
        ], value_input_option="USER_ENTERED")

    print(f"  OK - confirmado (certeza {certeza}%, {len(resultado.get('combinacoes', []))} combinacao(oes), fonte: {resultado.get('fonte', '')})")


# ============================================================
# MAIN
# ============================================================
def main():
    planilha, drive = autenticar_google()
    aba_staging = planilha.worksheet(ABA_STAGING)

    aba_compat, colunas_compat = preparar_aba_com_cabecalho_flexivel(planilha, ABA_COMPATIBILIDADES, CABECALHO_COMPATIBILIDADES)
    aba_outros = garantir_aba(planilha, ABA_OUTROS_ANUNCIOS, CABECALHO_OUTROS_ANUNCIOS)
    aba_verificar = garantir_aba(planilha, ABA_VERIFICAR, CABECALHO_VERIFICAR)

    pendentes, colunas_staging = ler_linhas_pendentes(aba_staging)
    if not pendentes:
        print("Nenhum SKU com Status = 'Aguardando Pesquisa' encontrado.")
        return

    print(f"{len(pendentes)} SKU(s) pendente(s) de pesquisa.\n")

    pdfs_disponiveis = listar_pdfs_catalogo(drive)
    print(f"{len(pdfs_disponiveis)} catalogo(s) em PDF disponivel(is).")

    marcas_validas_normalizadas, marcas_validas_originais = carregar_montadoras_validas()
    print(f"{len(marcas_validas_originais)} montadora(s) na base de validacao.\n")

    for item in pendentes:
        try:
            processar_sku(planilha, aba_staging, colunas_staging, drive, item, pdfs_disponiveis,
                          marcas_validas_normalizadas, marcas_validas_originais,
                          aba_compat, colunas_compat, aba_outros, aba_verificar)
        except Exception as e:
            print(f"ERRO inesperado processando SKU '{item['sku']}': {repr(e)}")
            try:
                marcar_verificar_manual_staging(aba_staging, colunas_staging, item["linha"], f"Erro tecnico inesperado: {repr(e)}")
            except Exception:
                pass

    print("\nConcluido.")


if __name__ == "__main__":
    main()
