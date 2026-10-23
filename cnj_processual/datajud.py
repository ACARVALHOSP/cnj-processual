import asyncio
import logging
import re

import httpx

from cnj_processual._app import mcp
from cnj_processual.config import (
    DATAJUD_BACKOFF_S,
    DATAJUD_BASE_URL,
    DATAJUD_TENTATIVAS,
    DATAJUD_TIMEOUT_S,
    _DESCRICAO_GRAU,
)
from cnj_processual.utils import (
    _cliente,
    _datajud_api_key,
    _erro,
    _json_da_resposta,
    _so_digitos,
    _total_hits,
)

log = logging.getLogger("cnj-processual.datajud")


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
        if isinstance(resposta, dict):
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
