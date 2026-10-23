import tempfile
from pathlib import Path

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
