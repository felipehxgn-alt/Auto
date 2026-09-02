"""
sheets_integration.py

Integração Fase 1 + Fase 2 (planilha <-> robô de mídias).

Responsável por:
1) Escrever uma linha nova na aba "Staging" quando o robô aprova a mídia de um SKU.
2) Rodar a promoção automática: qualquer linha do Staging com Status = "Completo"
   é copiada pra aba "Principal" (Status = "Pendente") e removida do Staging.

Requer as libs: gspread, google-auth
    pip install gspread google-auth

Variável de ambiente esperada (secret do GitHub Actions):
    GOOGLE_SERVICE_ACCOUNT_JSON   -> conteúdo INTEIRO do arquivo JSON da service account

Variável de ambiente com o ID da planilha (mesmo padrão usado pras outras planilhas):
    PLANILHA_ANUNCIOS_ML_ID       -> ID da planilha do mês, extraído da URL do Google Sheets
"""

import os
import json
import datetime
from typing import Optional

import gspread
import requests
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ID da pasta do Drive (conta hexagontakes@gmail.com) onde os vídeos
# ficam salvos, nomeados só com o SKU. Mesma Service Account do Sheets.
DRIVE_VIDEOS_FOLDER_ID = os.environ.get("DRIVE_VIDEOS_FOLDER_ID", "1jbiYgY63l-OELrOtsAMsEZyxMFgJ8CFZ")

ABA_STAGING = "Staging"
ABA_PRINCIPAL = "Principal"

# Colunas da aba Staging, nessa ordem exata (linha 3 = cabeçalho, dados a partir da linha 4)
STAGING_COLS = [
    "SKU",
    "Marca (peça)",
    "Montadora",
    "Veículos Compatíveis",
    "Motor(es)",
    "Ano",
    "Caminho Mídia (pasta do SKU)",
    "Data Adicionado",
    "Status",
]

# Colunas da aba Principal, nessa ordem exata (cabeçalho na linha 3 também,
# mesmo padrão da Staging - confirmado direto na planilha real)
PRINCIPAL_COLS = [
    "Status Anúncio (OK/Pendente)",
    "SKU",
    "Marca (peça)",
    "Montadora(s) Compatível(is) — resumo",
    "Categoria",
    "EAN/GTIN",
    "Nacional/Importado",
    "Nome do Produto (base)",
    "Código OEM",
    "Palavras-chave",
    "Largura (cm)",
    "Altura (cm)",
    "Profundidade (cm)",
    "Peso Físico — embalagem+produto (kg)",
    "Estoque",
    "Preço (R$)",
    "Garantia",
    "Fotos (caminho pasta SKU)",
    "Descrição Base",
    "Informações Extras",
]

STAGING_HEADER_ROW = 3
STAGING_FIRST_DATA_ROW = 4

STATUS_AGUARDANDO_FOTOS = "Aguardando Fotos"
STATUS_AGUARDANDO_PESQUISA = "Aguardando Pesquisa"
STATUS_COMPLETO = "Completo"
STATUS_PENDENTE_PRINCIPAL = "Pendente"


def _get_credentials() -> Credentials:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError(
            "Variável de ambiente GOOGLE_SERVICE_ACCOUNT_JSON não encontrada. "
            "Confirme que o secret foi criado no GitHub Actions."
        )
    info = json.loads(raw)
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def _get_client() -> gspread.Client:
    """Autentica usando a Service Account (via variável de ambiente com o JSON inteiro)."""
    creds = _get_credentials()
    return gspread.authorize(creds)


def subir_video_drive(sku: str, video_bytes: bytes, extensao: str = ".mp4") -> Optional[str]:
    """
    Sobe o vídeo do SKU pro Google Drive (pasta DRIVE_VIDEOS_FOLDER_ID),
    nomeado só com o SKU (ex: 517806338.mp4) — backup fora do Dropbox,
    mesma credencial da planilha, nenhum secret novo necessário.

    Retorna o ID do arquivo criado no Drive, ou None se
    DRIVE_VIDEOS_FOLDER_ID não estiver configurado.
    """
    if not DRIVE_VIDEOS_FOLDER_ID:
        return None

    creds = _get_credentials()
    creds.refresh(Request())
    token = creds.token

    # 1) cria os metadados do arquivo (nome + pasta de destino)
    metadata = {"name": f"{sku}{extensao}", "parents": [DRIVE_VIDEOS_FOLDER_ID]}
    resp = requests.post(
        "https://www.googleapis.com/drive/v3/files",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=metadata,
        timeout=30,
    )
    resp.raise_for_status()
    file_id = resp.json()["id"]

    # 2) sobe o conteúdo do vídeo pro arquivo recém-criado
    resp2 = requests.patch(
        f"https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=media",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"},
        data=video_bytes,
        timeout=180,
    )
    resp2.raise_for_status()
    print(f"[Drive] Vídeo do SKU {sku} salvo em {DRIVE_VIDEOS_FOLDER_ID} (file_id={file_id}).")
    return file_id


def _get_spreadsheet(client: gspread.Client) -> gspread.Spreadsheet:
    sheet_id = os.environ.get("PLANILHA_ANUNCIOS_ML_ID")
    if not sheet_id:
        raise RuntimeError(
            "Variável de ambiente PLANILHA_ANUNCIOS_ML_ID não encontrada. "
            "Copie o ID da URL da planilha do mês (entre /d/ e /edit)."
        )
    return client.open_by_key(sheet_id)


def adicionar_ao_staging(
    sku: str,
    marca: str = "",
    montadora: str = "",
    veiculos_compativeis: str = "",
    motores: str = "",
    ano: str = "",
    caminho_midia: str = "",
    status: str = STATUS_AGUARDANDO_PESQUISA,
) -> None:
    """
    Chamado pelo robô assim que um SKU é aprovado (mídia publicada em MIDIA_FINAL/<SKU>/).
    Escreve uma linha nova no fim da aba Staging.

    - marca: fabricante da peça (ex: Sabo), NÃO a montadora.
    - montadora: montadora do veículo (ex: Renault).
    - veiculos_compativeis: lista de modelos, separados por vírgula (ex: "Duster, Kwid").
    - motores / ano: opcionais, quando já souber na hora do cadastro da mídia.
    """
    client = _get_client()
    ss = _get_spreadsheet(client)
    ws = ss.worksheet(ABA_STAGING)

    data_hoje = datetime.datetime.now().strftime("%d/%m/%Y")
    linha = [sku, marca, montadora, veiculos_compativeis, motores, ano, caminho_midia, data_hoje, status]

    ws.append_row(linha, value_input_option="USER_ENTERED")
    print(f"[Staging] SKU {sku} adicionado com Status='{status}'.")


def _montar_descricao_base(veiculos: str, motores: str, ano: str) -> str:
    """Junta Veículos/Motor(es)/Ano num texto legível só - usado pra não
    perder essa informação na hora de promover pra Principal (ver
    comentário em promover_linhas_completas)."""
    partes = []
    if veiculos:
        partes.append(f"Veículos: {veiculos}")
    if motores:
        partes.append(f"Motor(es): {motores}")
    if ano:
        partes.append(f"Ano: {ano}")
    return " | ".join(partes)


def promover_linhas_completas() -> int:
    """
    Varre o Staging: toda linha com Status == 'Completo' é copiada pra Principal
    (Status = 'Pendente') e removida do Staging.

    IMPORTANTE (corrigido): a Montadora e o resumo de Veículos/Motor/Ano
    pesquisados NÃO são mais perdidos nessa promoção - antes só SKU/Marca
    eram copiados. Agora Montadora vai pra coluna própria da Principal, e
    Veículos/Motor/Ano (que não tem coluna dedicada na Principal) entram
    juntos, de forma legível, na coluna "Descrição Base" - fica disponível
    pra quem for escrever o anúncio depois, e também pro script de vídeo
    legendado usar.

    Retorna o número de linhas promovidas.
    """
    client = _get_client()
    ss = _get_spreadsheet(client)
    staging = ss.worksheet(ABA_STAGING)
    principal = ss.worksheet(ABA_PRINCIPAL)

    todas = staging.get_all_values()
    if len(todas) <= STAGING_HEADER_ROW:
        return 0  # só cabeçalho/banner, nada pra processar

    linhas_dados = todas[STAGING_HEADER_ROW:]  # a partir da linha 4 (índice 3)

    idx_status = STAGING_COLS.index("Status")
    idx_sku = STAGING_COLS.index("SKU")
    idx_marca = STAGING_COLS.index("Marca (peça)")
    idx_montadora = STAGING_COLS.index("Montadora")
    idx_veiculos = STAGING_COLS.index("Veículos Compatíveis")
    idx_motores = STAGING_COLS.index("Motor(es)")
    idx_ano = STAGING_COLS.index("Ano")
    idx_caminho = STAGING_COLS.index("Caminho Mídia (pasta do SKU)")

    promovidas = 0
    # percorre de baixo pra cima pra poder deletar linhas do Staging sem
    # bagunçar os índices das linhas ainda não processadas
    for offset in range(len(linhas_dados) - 1, -1, -1):
        linha = linhas_dados[offset]
        if len(linha) <= idx_status:
            continue
        status_atual = linha[idx_status].strip()
        sku = linha[idx_sku].strip() if len(linha) > idx_sku else ""

        if status_atual == STATUS_COMPLETO and sku:
            marca = linha[idx_marca] if len(linha) > idx_marca else ""
            montadora = linha[idx_montadora] if len(linha) > idx_montadora else ""
            veiculos = linha[idx_veiculos] if len(linha) > idx_veiculos else ""
            motores = linha[idx_motores] if len(linha) > idx_motores else ""
            ano = linha[idx_ano] if len(linha) > idx_ano else ""
            caminho_midia = linha[idx_caminho] if len(linha) > idx_caminho else ""

            descricao_base = _montar_descricao_base(veiculos, motores, ano)

            # Monta a linha da aba Principal seguindo a ordem real de
            # PRINCIPAL_COLS. Campos que só são preenchidos na etapa de
            # redação do anúncio (Categoria, EAN, preço, etc.) ficam em
            # branco de propósito.
            nova_linha_principal = [""] * len(PRINCIPAL_COLS)
            nova_linha_principal[PRINCIPAL_COLS.index("Status Anúncio (OK/Pendente)")] = STATUS_PENDENTE_PRINCIPAL
            nova_linha_principal[PRINCIPAL_COLS.index("SKU")] = sku
            nova_linha_principal[PRINCIPAL_COLS.index("Marca (peça)")] = marca
            nova_linha_principal[PRINCIPAL_COLS.index("Montadora(s) Compatível(is) — resumo")] = montadora
            nova_linha_principal[PRINCIPAL_COLS.index("Fotos (caminho pasta SKU)")] = caminho_midia
            nova_linha_principal[PRINCIPAL_COLS.index("Descrição Base")] = descricao_base

            principal.append_row(nova_linha_principal, value_input_option="USER_ENTERED")

            # remove a linha correspondente do Staging (linha real na planilha)
            linha_real_na_planilha = STAGING_FIRST_DATA_ROW + offset
            staging.delete_rows(linha_real_na_planilha)

            promovidas += 1
            print(f"[Promoção] SKU {sku} movido de Staging pra Principal (Status=Pendente, Montadora='{montadora}' preservada).")

    return promovidas


if __name__ == "__main__":
    # Uso manual/teste: roda só a promoção automática.
    n = promover_linhas_completas()
    print(f"Total de linhas promovidas nessa execução: {n}")
