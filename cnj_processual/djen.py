import asyncio
import logging
import re
import time
from datetime import datetime

import httpx

from cnj_processual._app import mcp
from cnj_processual.config import (
    CERTIDAO_DIR,
    CERTIDAO_RETENCAO_DIAS,
    DJEN_BACKOFF_S,
    DJEN_BASE_URL,
    DJEN_STATUS_TRANSITORIOS,
    DJEN_TENTATIVAS,
    DJEN_TIMEOUT_S,
)
from cnj_processual.utils import (
    _cliente,
    _erro,
    _html_para_texto,
    _json_da_resposta,
    _so_digitos,
)

log = logging.getLogger("cnj-processual.djen")

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
