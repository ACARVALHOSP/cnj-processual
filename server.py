"""Servidor MCP "cnj-processual": consulta processual judicial brasileira via APIs públicas do CNJ.

Tools expostas:
- consultar_processo_datajud: metadados e movimentações de processo (API Pública DataJud).
- consultar_publicacoes_djen: publicações/intimações no DJEN (API Comunica PJe).
- baixar_certidao_djen: baixa a certidão PDF de uma publicação e salva em arquivo temporário.

Transporte: stdio. Atenção: nada deve ser impresso em stdout (ele é o canal do protocolo);
logs vão para stderr.
"""

import asyncio
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cnj-processual")

mcp = FastMCP("cnj-processual")

# --- Configuração -----------------------------------------------------------

DATAJUD_BASE_URL = "https://api-publica.datajud.cnj.jus.br"
# Chave pública oficial divulgada pelo CNJ em https://datajud-wiki.cnj.jus.br/api-publica/.
# O CNJ pode rotacioná-la a qualquer momento: se as consultas começarem a retornar 401/403,
# verifique a wiki e defina a variável de ambiente DATAJUD_API_KEY com a chave atual.
DATAJUD_API_KEY_PADRAO = "cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw=="

DJEN_BASE_URL = "https://comunicaapi.pje.jus.br/api/v1/comunicacao"

# A API DataJud é frequentemente lenta (observado ~48s em teste real); timeouts curtos
# geram falso negativo, por isso o limite é alto e há uma nova tentativa.
DATAJUD_TIMEOUT_S = 70.0
DATAJUD_TENTATIVAS = 2
DATAJUD_BACKOFF_S = 3.0

DJEN_TIMEOUT_S = 30.0
DJEN_TENTATIVAS = 2  # 1 tentativa inicial + 1 retry para falhas transitórias
DJEN_BACKOFF_S = 2.0
DJEN_STATUS_TRANSITORIOS = {500, 502, 503, 504}

CERTIDAO_DIR = Path(tempfile.gettempdir()) / "cnj-processual" / "certidoes"
CERTIDAO_RETENCAO_DIAS = 7

# Graus de jurisdição usados pelo DataJud no campo "grau".
_DESCRICAO_GRAU = {
    "G1": "1º grau (primeira instância)",
    "G2": "2º grau (recurso ao tribunal de justiça ou TRF)",
    "JE": "juizado especial",
    "TR": "turma recursal",
    "STJ": "Superior Tribunal de Justiça",
    "STF": "Supremo Tribunal Federal",
    "TST": "Tribunal Superior do Trabalho",
    "TSE": "Tribunal Superior Eleitoral",
    "STM": "Superior Tribunal Militar",
}

# Transporte HTTP injetável para testes (httpx.MockTransport). Em produção fica None,
# e o httpx usa a rede normalmente.
_TRANSPORTE_HTTP: httpx.AsyncBaseTransport | None = None


def _cliente(timeout: float, **kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout), transport=_TRANSPORTE_HTTP, **kwargs)


def _datajud_api_key() -> str:
    return os.environ.get("DATAJUD_API_KEY", "").strip() or DATAJUD_API_KEY_PADRAO


def _so_digitos(valor: str | None) -> str:
    """Remove máscara e qualquer caractere não numérico (ex: '1006314-27.2023.8.26.0005' -> '10063142720238260005')."""
    return re.sub(r"\D", "", valor or "")


def _erro(mensagem: str, **extra) -> dict:
    return {"erro": True, "mensagem": mensagem, **extra}


def _total_hits(hits: dict) -> int:
    """Lê o total do Elasticsearch, que pode vir como {"value": n} (ES 7+) ou como inteiro."""
    total = hits.get("total", 0)
    if isinstance(total, dict):
        total = total.get("value", 0)
    try:
        return int(total or 0)
    except (TypeError, ValueError):
        return 0


def _json_da_resposta(resp: httpx.Response) -> tuple[Any, dict | None]:
    """Devolve (dados, None) ou (None, erro) se o corpo não for JSON."""
    try:
        return resp.json(), None
    except ValueError:
        return None, _erro(f"Resposta não é JSON válido (HTTP {resp.status_code}): {resp.text[:300]}")


# --- Utilitários de texto (HTML do DJEN) -------------------------------------

class _ExtratorTexto(HTMLParser):
    """Extrai texto puro de HTML, ignorando o conteúdo de <style>, <script> e <head>."""

    _IGNORADAS = {"style", "script", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._partes: list[str] = []
        self._ignorando = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._IGNORADAS:
            self._ignorando += 1
        elif tag in {"br", "p", "div", "tr", "li", "section", "article", "header", "table"}:
            self._partes.append("\n")
        elif tag in {"td", "th"}:
            self._partes.append(" ")

    def handle_endtag(self, tag):
        if tag in self._IGNORADAS and self._ignorando:
            self._ignorando -= 1
        elif tag in {"p", "div", "tr", "li", "section", "article", "table"}:
            self._partes.append("\n")

    def handle_data(self, data):
        if not self._ignorando:
            self._partes.append(data)

    def texto(self) -> str:
        bruto = "".join(self._partes)
        linhas = [re.sub(r"[ \t\xa0]+", " ", linha).strip() for linha in bruto.splitlines()]
        # Colapsa sequências de linhas vazias em no máximo uma.
        saida: list[str] = []
        for linha in linhas:
            if linha or (saida and saida[-1]):
                saida.append(linha)
        return "\n".join(saida).strip()


def _html_para_texto(conteudo: str | None) -> str:
    """Decodifica entidades HTML (ex: &aacute;) e remove tags, devolvendo texto legível."""
    if not conteudo:
        return ""
    extrator = _ExtratorTexto()  # convert_charrefs=True já decodifica entidades (&aacute; -> á)
    extrator.feed(conteudo)
    extrator.close()
    return extrator.texto()


# --- Tool 1: DataJud ---------------------------------------------------------

@mcp.tool()
async def consultar_processo_datajud(tribunal: str, numero_processo: str) -> dict:
    """Consulta metadados de um processo judicial (classe, órgão julgador, data de ajuizamento,
    grau e lista de movimentações) na API Pública do DataJud (CNJ).

    Parâmetros:
    - tribunal: sigla minúscula do tribunal, como "tjsp", "trf1" ou "stj".
    - numero_processo: número do processo CNJ de 20 dígitos. Aceita com ou sem máscara
      (ex: "1006314-27.2023.8.26.0005" ou "10063142720238260005").

    Retorno: dicionário com "encontrado" (bool) e, quando encontrado, "processos", uma lista em
    que cada item traz classe, dataAjuizamento, orgaoJulgador, tribunal, grau, grau_descricao
    (ex.: "1º grau", "2º grau") e movimentos (codigo, nome, dataHora e, quando existir,
    complementosTabelados). Se o processo não existir nesse tribunal, retorna encontrado=False com
    mensagem explicativa (isso não é erro).

    Um mesmo número pode aparecer mais de uma vez na lista: cada item é um grau de jurisdição
    diferente (ex.: 1º grau e 2º grau). Ao apresentar o resultado, identifique o grau de cada item.

    Limitações:
    - A API DataJud NÃO expõe nomes de partes (política de proteção de dados), então o
      resultado nunca inclui partes, autores ou réus.
    - A API é lenta: esta tool usa timeout de 70s e uma nova tentativa automática em timeout,
      429 ou 502/503/504. Uma consulta com falhas pode levar mais de dois minutos.
    - Consulta apenas o tribunal informado; se não sabe o tribunal, deduza pelo número CNJ
      (segmento J.TR, ex: ".8.26." = TJSP) ou tente o tribunal superior correspondente.
    """
    try:
        tribunal_norm = (tribunal or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9]+", tribunal_norm):
            return _erro("Tribunal inválido. Use a sigla minúscula, como 'tjsp', 'trf1' ou 'stj'.")

        numero = _so_digitos(numero_processo)
        if len(numero) != 20:
            return _erro(
                f"Número de processo inválido: esperados 20 dígitos, recebidos {len(numero)}. "
                "Formato CNJ: NNNNNNN-DD.AAAA.J.TR.OOOO."
            )

        url = f"{DATAJUD_BASE_URL}/api_publica_{tribunal_norm}/_search"
        headers = {
            "Authorization": f"APIKey {_datajud_api_key()}",
            "Content-Type": "application/json",
        }
        corpo = {"query": {"match": {"numeroProcesso": numero}}}

        resposta = await _datajud_post_com_retry(url, headers, corpo)
        if isinstance(resposta, dict):  # erro estruturado já montado
            return resposta

        dados, erro_json = _json_da_resposta(resposta)
        if erro_json:
            return erro_json
        if not isinstance(dados, dict) or not isinstance(dados.get("hits"), dict):
            # Corpo sem "hits" não é "processo inexistente": é uma resposta de erro disfarçada.
            return _erro(f"Resposta inesperada da API DataJud (sem 'hits'): {str(dados)[:300]}")

        hits = dados["hits"]
        total = _total_hits(hits)
        if total == 0:
            return {
                "encontrado": False,
                "mensagem": (
                    f"Processo {numero} não encontrado no tribunal '{tribunal_norm}'. "
                    "Confira o número e a sigla do tribunal; o processo pode estar em outro tribunal."
                ),
            }

        processos = [_extrair_processo_datajud(h.get("_source") or {}) for h in hits.get("hits") or []]
        return {"encontrado": True, "total": total, "processos": processos}

    except Exception as exc:  # nunca deixar exceção vazar para o protocolo MCP
        log.exception("Falha inesperada em consultar_processo_datajud")
        return _erro(f"Erro inesperado ao consultar o DataJud: {exc}")


async def _datajud_post_com_retry(url: str, headers: dict, corpo: dict):
    """Faz o POST com timeout de 70s. Retenta em timeout, em 502/503/504 (gateway lento) e em
    429 (Elasticsearch sobrecarregado, comum em horário de pico).

    Retorna a resposta httpx em caso de sucesso, ou um dicionário de erro já pronto.
    """
    ultimo_problema = "sem detalhes"
    async with _cliente(DATAJUD_TIMEOUT_S) as cliente:
        for tentativa in range(1, DATAJUD_TENTATIVAS + 1):
            try:
                resp = await cliente.post(url, headers=headers, json=corpo)
            except httpx.TimeoutException:
                ultimo_problema = f"timeout após {DATAJUD_TIMEOUT_S:.0f}s"
            except httpx.HTTPError as exc:
                ultimo_problema = f"erro de conexão: {exc}"
            else:
                if resp.status_code in (429, 502, 503, 504):
                    ultimo_problema = f"HTTP {resp.status_code} da API DataJud (sobrecarga ou gateway lento)"
                elif resp.status_code in (401, 403):
                    return _erro(
                        f"HTTP {resp.status_code} da API DataJud: chave de API rejeitada. "
                        "O CNJ pode ter rotacionado a chave pública; consulte "
                        "https://datajud-wiki.cnj.jus.br/api-publica/ e defina DATAJUD_API_KEY."
                    )
                elif resp.status_code == 404:
                    return _erro("HTTP 404 da API DataJud: sigla de tribunal provavelmente inválida.")
                elif resp.status_code >= 400:
                    return _erro(f"HTTP {resp.status_code} da API DataJud: {resp.text[:300]}")
                else:
                    return resp

            log.warning("DataJud tentativa %d/%d falhou: %s", tentativa, DATAJUD_TENTATIVAS, ultimo_problema)
            if tentativa < DATAJUD_TENTATIVAS:
                await asyncio.sleep(DATAJUD_BACKOFF_S * tentativa)

    return _erro(
        f"Falha ao consultar o DataJud após {DATAJUD_TENTATIVAS} tentativas ({ultimo_problema}). "
        "A API é instável em horários de pico; tente novamente em alguns minutos."
    )


def _extrair_processo_datajud(fonte: dict) -> dict:
    movimentos = []
    for mov in fonte.get("movimentos") or []:
        item = {"codigo": mov.get("codigo"), "nome": mov.get("nome"), "dataHora": mov.get("dataHora")}
        if mov.get("complementosTabelados"):
            item["complementosTabelados"] = mov["complementosTabelados"]
        movimentos.append(item)

    return {
        "numeroProcesso": fonte.get("numeroProcesso"),
        "tribunal": fonte.get("tribunal"),
        "grau": fonte.get("grau"),
        "grau_descricao": _DESCRICAO_GRAU.get(fonte.get("grau") or "", fonte.get("grau")),
        "classe": (fonte.get("classe") or {}).get("nome"),
        "dataAjuizamento": fonte.get("dataAjuizamento"),
        "orgaoJulgador": (fonte.get("orgaoJulgador") or {}).get("nome"),
        "totalMovimentos": len(movimentos),
        "movimentos": movimentos,
    }


# --- Tool 2: DJEN ------------------------------------------------------------

_FORMATO_DATA = "%Y-%m-%d"


def _validar_data(nome: str, valor: str | None) -> str | None:
    """Retorna mensagem de erro se a data não estiver em YYYY-MM-DD; None se válida ou ausente."""
    if valor is None or valor == "":
        return None
    try:
        datetime.strptime(valor, _FORMATO_DATA)
    except ValueError:
        return f"{nome} inválida: use o formato YYYY-MM-DD (recebido '{valor}')."
    return None


@mcp.tool()
async def consultar_publicacoes_djen(
    numero_processo: str | None = None,
    sigla_tribunal: str | None = None,
    numero_oab: str | None = None,
    uf_oab: str | None = None,
    data_disponibilizacao_inicio: str | None = None,
    data_disponibilizacao_fim: str | None = None,
    pagina: int = 1,
    itens_por_pagina: int = 50,
) -> dict:
    """Consulta publicações e intimações no Diário de Justiça Eletrônico Nacional (DJEN), via
    API pública Comunica PJe. Não exige autenticação.

    Parâmetros (é obrigatório informar ao menos um entre numero_processo e numero_oab):
    - numero_processo: número CNJ de 20 dígitos; máscara é removida automaticamente.
    - sigla_tribunal: sigla do tribunal em maiúsculas, ex: "TJSP".
    - numero_oab: número da OAB do advogado, ex: "330659".
    - uf_oab: UF da OAB com 2 letras, ex: "SP".
    - data_disponibilizacao_inicio / data_disponibilizacao_fim: intervalo no formato YYYY-MM-DD
      (o início não pode ser posterior ao fim).
    - pagina: número da página (padrão 1).
    - itens_por_pagina: quantidade por página (padrão 50).

    Retorno: dicionário com "total" (quantidade informada pela API), "pagina" e "itens". Cada item
    traz data_disponibilizacao, siglaTribunal, nomeOrgao, tipoComunicacao, tipoDocumento,
    nomeClasse, numeroprocessocommascara, hash, link, destinatarios (nome e polo) e
    destinatarioadvogados (nome, numero_oab, uf_oab). O campo "texto" vem convertido de HTML
    para texto puro legível. O "hash" de cada item pode ser passado a baixar_certidao_djen.

    Limitações:
    - Busca por OAB: tribunais gravam a OAB com sufixos variados (ex: "123456", "123456-O",
      "123456-A"). A busca pode não encontrar tudo; não há tentativa automática com sufixos
      nesta versão.
    - Falhas transitórias (timeout, 500, 502, 503, 504) são repetidas uma vez antes de virar erro.
    - Se a API responder HTTP 403, o IP de saída está fora do Brasil (geo-bloqueio).
    """
    try:
        numero = _so_digitos(numero_processo) if numero_processo else None
        if numero_processo and len(numero) != 20:
            return _erro(
                f"Número de processo inválido: esperados 20 dígitos, recebidos {len(numero)}."
            )

        oab = (numero_oab or "").strip() or None
        if not numero and not oab:
            return _erro("Informe ao menos um entre numero_processo e numero_oab.")

        uf = (uf_oab or "").strip().upper() or None
        if uf and not re.fullmatch(r"[A-Z]{2}", uf):
            return _erro("uf_oab inválida: use 2 letras, ex: 'SP'.")

        for nome, valor in (
            ("data_disponibilizacao_inicio", data_disponibilizacao_inicio),
            ("data_disponibilizacao_fim", data_disponibilizacao_fim),
        ):
            erro_data = _validar_data(nome, valor)
            if erro_data:
                return _erro(erro_data)

        if data_disponibilizacao_inicio and data_disponibilizacao_fim:
            inicio = datetime.strptime(data_disponibilizacao_inicio, _FORMATO_DATA)
            fim = datetime.strptime(data_disponibilizacao_fim, _FORMATO_DATA)
            if inicio > fim:
                return _erro("data_disponibilizacao_inicio não pode ser posterior a data_disponibilizacao_fim.")

        if pagina < 1 or itens_por_pagina < 1:
            return _erro("pagina e itens_por_pagina devem ser inteiros maiores ou iguais a 1.")

        params = {
            "numeroProcesso": numero,
            "siglaTribunal": (sigla_tribunal or "").strip().upper() or None,
            "numeroOab": oab,
            "ufOab": uf,
            "dataDisponibilizacaoInicio": data_disponibilizacao_inicio or None,
            "dataDisponibilizacaoFim": data_disponibilizacao_fim or None,
            "pagina": pagina,
            "itensPorPagina": itens_por_pagina,
        }
        params = {k: v for k, v in params.items() if v is not None}

        resposta = await _djen_get_com_retry(DJEN_BASE_URL, params=params)
        if isinstance(resposta, dict):
            return resposta

        dados, erro_json = _json_da_resposta(resposta)
        if erro_json:
            return erro_json
        if not isinstance(dados, dict):
            return _erro("Resposta inesperada da API do DJEN (não é um objeto JSON).")
        return {
            "total": dados.get("count", 0),
            "pagina": pagina,
            "itens": [_extrair_publicacao_djen(item) for item in dados.get("items") or []],
        }

    except Exception as exc:
        log.exception("Falha inesperada em consultar_publicacoes_djen")
        return _erro(f"Erro inesperado ao consultar o DJEN: {exc}")


async def _djen_get_com_retry(url: str, params: dict | None = None):
    """GET no DJEN com timeout de 30s. Repete uma vez em timeout, erro de conexão ou HTTP
    500/502/503/504. HTTP 403 vira erro de geo-bloqueio.

    Retorna a resposta httpx em caso de sucesso, ou um dicionário de erro já pronto.
    """
    ultimo_problema = "sem detalhes"
    async with _cliente(DJEN_TIMEOUT_S, follow_redirects=True) as cliente:
        for tentativa in range(1, DJEN_TENTATIVAS + 1):
            try:
                resp = await cliente.get(url, params=params)
            except httpx.TimeoutException:
                ultimo_problema = f"timeout após {DJEN_TIMEOUT_S:.0f}s"
            except httpx.HTTPError as exc:
                ultimo_problema = f"erro de conexão: {exc}"
            else:
                if resp.status_code == 403:
                    return _erro(_mensagem_geobloqueio())
                if resp.status_code in DJEN_STATUS_TRANSITORIOS:
                    ultimo_problema = f"HTTP {resp.status_code}"
                elif resp.status_code >= 400:
                    return _erro(f"HTTP {resp.status_code} da API do DJEN: {resp.text[:300]}")
                else:
                    return resp

            log.warning("DJEN tentativa %d/%d falhou: %s", tentativa, DJEN_TENTATIVAS, ultimo_problema)
            if tentativa < DJEN_TENTATIVAS:
                await asyncio.sleep(DJEN_BACKOFF_S)

    return _erro(f"Falha ao consultar o DJEN após {DJEN_TENTATIVAS} tentativas ({ultimo_problema}).")


def _mensagem_geobloqueio() -> str:
    return (
        "HTTP 403 da API do DJEN. A API Comunica PJe bloqueia requisições de IPs fora do Brasil "
        "(geo-bloqueio). Verifique se a conexão de saída deste ambiente está no Brasil."
    )


def _extrair_publicacao_djen(item: dict) -> dict:
    return {
        "data_disponibilizacao": item.get("data_disponibilizacao"),
        "siglaTribunal": item.get("siglaTribunal"),
        "nomeOrgao": item.get("nomeOrgao"),
        "tipoComunicacao": item.get("tipoComunicacao"),
        "tipoDocumento": item.get("tipoDocumento"),
        "nomeClasse": item.get("nomeClasse"),
        "numeroprocessocommascara": item.get("numeroprocessocommascara"),
        "hash": item.get("hash"),
        "link": item.get("link"),
        "destinatarios": [
            {"nome": d.get("nome"), "polo": d.get("polo")} for d in item.get("destinatarios") or []
        ],
        "destinatarioadvogados": [
            {
                "nome": (da.get("advogado") or {}).get("nome"),
                "numero_oab": (da.get("advogado") or {}).get("numero_oab"),
                "uf_oab": (da.get("advogado") or {}).get("uf_oab"),
            }
            for da in item.get("destinatarioadvogados") or []
        ],
        "texto": _html_para_texto(item.get("texto")),
    }


# --- Tool 3 (bônus): certidão DJEN -------------------------------------------

@mcp.tool()
async def baixar_certidao_djen(hash: str) -> dict:
    """Baixa a certidão em PDF de uma publicação do DJEN e salva em arquivo temporário local.

    Parâmetro:
    - hash: valor do campo "hash" retornado por consultar_publicacoes_djen.

    Retorno: dicionário com "arquivo" (caminho absoluto do PDF salvo no servidor) e "bytes"
    (tamanho em bytes). Certidões com mais de 7 dias são removidas do diretório temporário a
    cada novo download.
    """
    try:
        hash_limpo = (hash or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9]+", hash_limpo):
            return _erro("Hash inválido: deve conter apenas letras e números, como retornado por consultar_publicacoes_djen.")

        url = f"{DJEN_BASE_URL}/{hash_limpo}/certidao"
        resposta = await _djen_get_com_retry(url)
        if isinstance(resposta, dict):
            return resposta

        conteudo = resposta.content
        if not conteudo.startswith(b"%PDF"):
            return _erro("A API do DJEN não retornou um PDF válido para este hash.")

        CERTIDAO_DIR.mkdir(parents=True, exist_ok=True)
        _limpar_certidoes_antigas()
        caminho = CERTIDAO_DIR / f"certidao_{hash_limpo}.pdf"
        caminho.write_bytes(conteudo)
        return {"arquivo": str(caminho.resolve()), "bytes": len(conteudo)}

    except Exception as exc:
        log.exception("Falha inesperada em baixar_certidao_djen")
        return _erro(f"Erro inesperado ao baixar a certidão: {exc}")


def _limpar_certidoes_antigas() -> None:
    """Remove certidões mais antigas que CERTIDAO_RETENCAO_DIAS. Falhas aqui não interrompem o download."""
    limite = time.time() - CERTIDAO_RETENCAO_DIAS * 86400
    try:
        for arquivo in CERTIDAO_DIR.glob("certidao_*.pdf"):
            if arquivo.stat().st_mtime < limite:
                arquivo.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Não foi possível limpar certidões antigas: %s", exc)


if __name__ == "__main__":
    mcp.run(transport="stdio")
