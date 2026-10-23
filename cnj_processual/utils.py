import os
import re
from html.parser import HTMLParser
from typing import Any

import httpx

from cnj_processual.config import DATAJUD_API_KEY_PADRAO

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
    extrator = _ExtratorTexto()
    extrator.feed(conteudo)
    extrator.close()
    return extrator.texto()
