"""
Gerar Video Legendado - Automacao de legendas nos videos ja publicados
==============================================================================

O QUE FAZ:
  Le a aba "Principal" da planilha, procura SKUs que:
    - ja tem Montadora preenchida (ou seja, a compatibilidade ja foi
      pesquisada e promovida - ver pesquisar_compatibilidade.py)
    - ainda NAO tiveram o video legendado (coluna "Video Legendado"
      vazia)

  Pra cada um: baixa o video original (SKU.mp4) do Dropbox, gera 3
  legendas queimadas no video (nome do produto, montadora compativel,
  frase de beneficio gerada por IA), sobrescreve o mesmo arquivo no
  Dropbox, e marca a coluna "Video Legendado" = "Sim".

AS 3 LEGENDAS (3 momentos fixos: inicio / meio / fim do video, tempo
igual dividido):
  1. Nome do produto (usa "Nome do Produto (base)" se ja tiver sido
     preenchido manualmente na Principal; senao usa "Marca + SKU" como
     nome generico - fica mais fraco, mas nunca inventa um nome que
     nao foi confirmado)
  2. Montadora compativel + "MODELOS NA DESCRICAO" (mesmo padrao do
     video modelo que voce mandou)
  3. Frase de beneficio, gerada por IA a partir da Descricao Base
     (que tem Veiculos/Motor/Ano) - texto curto, no estilo do exemplo
     ("Evita a substituicao desnecessaria do corpo de borboleta
     completo.")

REGRA: se a peca for Universal (sem montadora especifica), a legenda 2
vira so o nome da peca de novo (sem "MODELOS NA DESCRICAO", que nao
faz sentido pra peca universal).

SEGREDOS ESPERADOS (GitHub Secrets -> variaveis de ambiente):
  GOOGLE_SERVICE_ACCOUNT_JSON  (o mesmo ja usado no resto do projeto)
  PLANILHA_ANUNCIOS_ML_ID      (o mesmo ja usado no resto do projeto)
  OPENAI_API_KEY                (o mesmo ja usado no resto do projeto)
  DROPBOX_APP_KEY / DROPBOX_APP_SECRET / DROPBOX_REFRESH_TOKEN
                                 (os mesmos ja usados no main.py)

IMPORTANTE - COLUNA NOVA NA PLANILHA:
  Esse script espera uma coluna "Video Legendado" na aba Principal
  (ultima coluna, depois de "Informacoes Extras"). Se nao existir
  ainda, precisa ser criada manualmente uma vez (so o cabecalho, linha
  3) - o script so escreve "Sim" nela.

REQUISITO TECNICO:
  Precisa do ffmpeg instalado (ja vem pronto nos runners do GitHub
  Actions, igual o main.py ja usa pra remover audio do video).

COMO RODAR:
  python gerar_video_legendado.py
  (workflow_dispatch manual no GitHub Actions - roda quando voce
  disparar, nao em cron automatico)
"""

import os
import io
import re
import json
import base64
import subprocess
import tempfile

import requests
import gspread
from google.oauth2.service_account import Credentials

# ============================================================
# CONFIGURACAO
# ============================================================
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
PLANILHA_ID = os.environ["PLANILHA_ANUNCIOS_ML_ID"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

DROPBOX_APP_KEY = os.environ.get("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.environ.get("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.environ.get("DROPBOX_REFRESH_TOKEN")
DROPBOX_ACCESS_TOKEN_FIXO = os.environ.get("DROPBOX_ACCESS_TOKEN")

DROPBOX_ROOT = os.environ.get("DROPBOX_ROOT", "/AUTOMACAO_ANUNCIOS")
DROPBOX_DEST_ROOT = os.environ.get("DROPBOX_DEST_ROOT", f"{DROPBOX_ROOT}/MIDIA_FINAL")

ABA_PRINCIPAL = "Principal"
PRINCIPAL_HEADER_ROW = 3
PRINCIPAL_FIRST_DATA_ROW = 4

# A posicao de cada coluna NAO e mais fixa aqui - e lida do cabecalho
# real da planilha a cada execucao (ver _mapa_colunas_pelo_cabecalho),
# porque a ordem/nome exato das colunas pode variar (acento, coluna
# inserida no meio, etc) sem que ninguem precise lembrar de atualizar
# este arquivo.
COLUNAS_NECESSARIAS = [
    "SKU",
    "Marca (peça)",
    "Montadora(s) Compatível(is) — resumo",
    "Nome do Produto (base)",
    "Descrição Base",
    "Vídeo Legendado",
]


def _mapa_colunas_pelo_cabecalho(cabecalho):
    """{'Nome da Coluna': indice_zero_based}."""
    return {nome.strip(): i for i, nome in enumerate(cabecalho) if nome.strip()}

SCOPES_SHEETS = ["https://www.googleapis.com/auth/spreadsheets"]

DBX_API = "https://api.dropboxapi.com/2"
DBX_CONTENT = "https://content.dropboxapi.com/2"

MODELO_TEXTO = "gpt-4o-mini"

# fontes ja usadas no main.py pra desenhar texto nos selos - reaproveitadas
# aqui pra manter o mesmo estilo visual em toda a automacao
CAMINHOS_FONTE = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def achar_fonte():
    for caminho in CAMINHOS_FONTE:
        if os.path.exists(caminho):
            return caminho
    return None  # ffmpeg usa uma fonte padrao do sistema se nao achar nenhuma dessas


# ============================================================
# DROPBOX (mesmo padrao de autenticacao do main.py)
# ============================================================
def obter_dropbox_access_token():
    if DROPBOX_REFRESH_TOKEN and DROPBOX_APP_KEY and DROPBOX_APP_SECRET:
        resp = requests.post(
            "https://api.dropboxapi.com/oauth2/token",
            data={"grant_type": "refresh_token", "refresh_token": DROPBOX_REFRESH_TOKEN},
            auth=(DROPBOX_APP_KEY, DROPBOX_APP_SECRET),
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Falha renovando token do Dropbox: {resp.status_code} - {resp.text}")
        return resp.json()["access_token"]
    if DROPBOX_ACCESS_TOKEN_FIXO:
        return DROPBOX_ACCESS_TOKEN_FIXO
    raise RuntimeError("Nenhuma credencial do Dropbox configurada.")


DROPBOX_ACCESS_TOKEN = None  # preenchido no main(), pra so autenticar quando o script realmente roda


def dbx_baixar(path):
    resp = requests.post(
        f"{DBX_CONTENT}/files/download",
        headers={
            "Authorization": f"Bearer {DROPBOX_ACCESS_TOKEN}",
            "Dropbox-API-Arg": json.dumps({"path": path}),
        },
        timeout=180,
    )
    resp.raise_for_status()
    return resp.content


def dbx_subir(path, conteudo_bytes):
    resp = requests.post(
        f"{DBX_CONTENT}/files/upload",
        headers={
            "Authorization": f"Bearer {DROPBOX_ACCESS_TOKEN}",
            "Dropbox-API-Arg": json.dumps({"path": path, "mode": "overwrite"}),
            "Content-Type": "application/octet-stream",
        },
        data=conteudo_bytes,
        timeout=180,
    )
    resp.raise_for_status()


# ============================================================
# PLANILHA
# ============================================================
def autenticar_sheets():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    credenciais = Credentials.from_service_account_info(info, scopes=SCOPES_SHEETS)
    cliente = gspread.authorize(credenciais)
    return cliente.open_by_key(PLANILHA_ID).worksheet(ABA_PRINCIPAL)


def ler_skus_pendentes_de_legenda(aba_principal):
    todas = aba_principal.get_all_values()
    if len(todas) <= PRINCIPAL_HEADER_ROW:
        return []

    cabecalho = todas[PRINCIPAL_HEADER_ROW - 1]
    colunas = _mapa_colunas_pelo_cabecalho(cabecalho)

    faltando = [c for c in COLUNAS_NECESSARIAS if c not in colunas]
    if faltando:
        raise RuntimeError(
            f"Coluna(s) esperada(s) não encontrada(s) no cabeçalho real da "
            f"aba Principal (linha {PRINCIPAL_HEADER_ROW}): {faltando}. "
            f"Confere se o nome está escrito EXATAMENTE igual (acentos, "
            f"maiúsculas, parênteses)."
        )

    idx_sku = colunas["SKU"]
    idx_marca = colunas["Marca (peça)"]
    idx_montadora = colunas["Montadora(s) Compatível(is) — resumo"]
    idx_nome_produto = colunas["Nome do Produto (base)"]
    idx_descricao_base = colunas["Descrição Base"]
    idx_video_legendado = colunas["Vídeo Legendado"]

    largura_minima = max(colunas.values()) + 1

    linhas_dados = todas[PRINCIPAL_HEADER_ROW:]
    pendentes = []
    for offset, linha in enumerate(linhas_dados):
        linha_completa = linha + [""] * (largura_minima - len(linha))
        sku = linha_completa[idx_sku].strip()
        montadora = linha_completa[idx_montadora].strip()
        video_legendado = linha_completa[idx_video_legendado].strip()

        if sku and montadora and not video_legendado:
            numero_linha_real = PRINCIPAL_FIRST_DATA_ROW + offset
            pendentes.append({
                "linha": numero_linha_real,
                "coluna_video_legendado": idx_video_legendado + 1,  # gspread usa 1-indexed
                "sku": sku,
                "marca": linha_completa[idx_marca].strip(),
                "montadora": montadora,
                "nome_produto": linha_completa[idx_nome_produto].strip(),
                "descricao_base": linha_completa[idx_descricao_base].strip(),
            })
    return pendentes


def marcar_video_legendado(aba_principal, numero_linha, numero_coluna):
    aba_principal.update_cell(numero_linha, numero_coluna, "Sim")


def marcar_erro(aba_principal, numero_linha, numero_coluna, motivo):
    # nao apaga nada que ja existia - so anota o erro na coluna de
    # controle, pra tentar de novo depois sem perder rastro
    aba_principal.update_cell(numero_linha, numero_coluna, f"ERRO: {motivo}")


# ============================================================
# TEXTO DAS LEGENDAS
# ============================================================
def gerar_frase_beneficio(marca, montadora, descricao_base):
    """Gera uma frase curta de beneficio no estilo do video modelo (ex:
    'Evita a substituicao desnecessaria do corpo de borboleta
    completo.'). Baseada SO no que ja foi confirmado (marca, montadora,
    veiculos/motor/ano da pesquisa) - a IA nao sabe o que a peca faz
    tecnicamente sem essa base, entao o resultado fica generico mas
    nunca inventa uma funcao que a peca nao tem."""
    payload = {
        "model": MODELO_TEXTO,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Escreva UMA frase curta (max. 12 palavras) de beneficio "
                    f"pra legenda de video de venda de autopeca, estilo direto "
                    f"e objetivo, parecido com 'Evita a substituicao "
                    f"desnecessaria do corpo de borboleta completo.'\n\n"
                    f"Marca da peca: {marca}\n"
                    f"Aplicacao: {descricao_base or montadora}\n\n"
                    "Responda SOMENTE com a frase, sem aspas, sem explicacao."
                ),
            }
        ],
        "max_tokens": 60,
        "temperature": 0.6,
    }
    resposta = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resposta.raise_for_status()
    return resposta.json()["choices"][0]["message"]["content"].strip()


def montar_legendas(item):
    nome_produto = item["nome_produto"] or f"{item['marca']} - {item['sku']}"
    montadora = item["montadora"]

    universal = montadora.strip().upper() in ("UNIVERSAL", "DIVERSAS", "DIVERSOS")
    legenda_2 = nome_produto if universal else f"{montadora}\nMODELOS NA DESCRIÇÃO"

    frase_beneficio = gerar_frase_beneficio(item["marca"], montadora, item["descricao_base"])

    return [nome_produto, legenda_2, frase_beneficio]


# ============================================================
# FFMPEG - QUEIMAR AS LEGENDAS NO VIDEO
# ============================================================
def obter_duracao_video(caminho_arquivo):
    resultado = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", caminho_arquivo],
        capture_output=True, text=True, timeout=30,
    )
    dados = json.loads(resultado.stdout)
    return float(dados["format"]["duration"])


def escapar_texto_ffmpeg(texto):
    """Escapa caracteres que o filtro drawtext do ffmpeg interpreta
    especial (: ' \\ %), senao a legenda quebra o comando inteiro."""
    texto = texto.replace("\\", "\\\\\\\\")
    texto = texto.replace(":", "\\:")
    texto = texto.replace("'", "\u2019")  # troca aspa reta por tipografica, mais simples que escapar
    texto = texto.replace("%", "\\%")
    return texto


def quebrar_linha(texto, largura_maxima=22):
    """Quebra o texto em varias linhas curtas (a legenda do video modelo
    e sempre 1-2 linhas curtas, nunca uma linha comprida) - quebra por
    palavra inteira, sem cortar no meio."""
    palavras = texto.split()
    linhas = []
    linha_atual = ""
    for palavra in palavras:
        candidato = (linha_atual + " " + palavra).strip()
        if len(candidato) > largura_maxima and linha_atual:
            linhas.append(linha_atual)
            linha_atual = palavra
        else:
            linha_atual = candidato
    if linha_atual:
        linhas.append(linha_atual)
    return "\n".join(linhas)


def montar_filtro_drawtext(texto, inicio_seg, fim_seg, fonte, posicao_y_percentual=0.82):
    texto_quebrado = quebrar_linha(texto)
    texto_escapado = escapar_texto_ffmpeg(texto_quebrado)
    parte_fonte = f"fontfile='{fonte}':" if fonte else ""
    return (
        f"drawtext={parte_fonte}"
        f"text='{texto_escapado}':"
        f"fontcolor=white:fontsize=h*0.045:"
        f"borderw=h*0.006:bordercolor=black:"
        f"x=(w-text_w)/2:y=h*{posicao_y_percentual}-text_h/2:"
        f"line_spacing=6:"
        f"enable='between(t,{inicio_seg},{fim_seg})'"
    )


def queimar_legendas_no_video(video_bytes, legendas):
    fonte = achar_fonte()

    with tempfile.NamedTemporaryFile(suffix=".mp4") as entrada, \
         tempfile.NamedTemporaryFile(suffix=".mp4") as saida:
        entrada.write(video_bytes)
        entrada.flush()

        duracao = obter_duracao_video(entrada.name)
        terco = duracao / 3

        filtros = []
        for i, texto in enumerate(legendas):
            inicio = i * terco
            fim = (i + 1) * terco
            filtros.append(montar_filtro_drawtext(texto, inicio, fim, fonte))

        filtro_completo = ",".join(filtros)

        subprocess.run(
            ["ffmpeg", "-y", "-i", entrada.name, "-vf", filtro_completo,
             "-c:v", "libx264", "-preset", "fast", "-crf", "20",
             "-c:a", "copy", saida.name],
            check=True, capture_output=True, timeout=300,
        )

        with open(saida.name, "rb") as f:
            return f.read()


# ============================================================
# PROCESSAMENTO DE UM SKU
# ============================================================
def processar_sku(aba_principal, item):
    sku = item["sku"]
    print(f"Legendando video do SKU '{sku}'...")

    caminho_video = f"{DROPBOX_DEST_ROOT}/{sku}/{sku}.mp4"

    try:
        video_original = dbx_baixar(caminho_video)
    except Exception as e:
        motivo = f"Nao consegui baixar o video em '{caminho_video}': {repr(e)}"
        print(f"  ERRO: {motivo}")
        marcar_erro(aba_principal, item["linha"], item["coluna_video_legendado"], motivo)
        return

    try:
        legendas = montar_legendas(item)
        print(f"  Legendas: {legendas}")
        video_legendado = queimar_legendas_no_video(video_original, legendas)
    except Exception as e:
        motivo = f"Falha gerando as legendas/renderizando o video: {repr(e)}"
        print(f"  ERRO: {motivo}")
        marcar_erro(aba_principal, item["linha"], item["coluna_video_legendado"], motivo)
        return

    try:
        dbx_subir(caminho_video, video_legendado)
    except Exception as e:
        motivo = f"Video legendado gerado, mas falhou ao subir pro Dropbox: {repr(e)}"
        print(f"  ERRO: {motivo}")
        marcar_erro(aba_principal, item["linha"], item["coluna_video_legendado"], motivo)
        return

    marcar_video_legendado(aba_principal, item["linha"], item["coluna_video_legendado"])
    print(f"  OK - video legendado e sobrescrito em '{caminho_video}'.")


# ============================================================
# MAIN
# ============================================================
def main():
    global DROPBOX_ACCESS_TOKEN
    DROPBOX_ACCESS_TOKEN = obter_dropbox_access_token()

    aba_principal = autenticar_sheets()
    pendentes = ler_skus_pendentes_de_legenda(aba_principal)

    if not pendentes:
        print("Nenhum SKU com Montadora preenchida e video ainda sem legenda.")
        return

    print(f"{len(pendentes)} SKU(s) pendente(s) de legenda.\n")

    for item in pendentes:
        try:
            processar_sku(aba_principal, item)
        except Exception as e:
            print(f"ERRO inesperado processando SKU '{item['sku']}': {repr(e)}")
            try:
                marcar_erro(aba_principal, item["linha"], item["coluna_video_legendado"], f"Erro tecnico inesperado: {repr(e)}")
            except Exception:
                pass

    print("\nConcluido.")


if __name__ == "__main__":
    main()
