"""Servidor MCP "cnj-processual": consulta processual judicial brasileira via APIs públicas do CNJ.

Transporte: stdio. Atenção: nada deve ser impresso em stdout (ele é o canal do protocolo);
logs vão para stderr.
"""

import logging
import sys

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from cnj_processual._app import mcp  # noqa: E402
import cnj_processual.datajud  # noqa: E402, F401 — registra as tools no mcp
import cnj_processual.djen  # noqa: E402, F401 — registra as tools no mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
