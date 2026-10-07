# cnj-processual

Servidor MCP (Model Context Protocol) em Python para consulta processual judicial brasileira, usando as APIs públicas do CNJ:

| Tool | O que faz | API |
|---|---|---|
| `consultar_processo_datajud` | Metadados e movimentações de um processo (classe, órgão julgador, movimentos) | DataJud (CNJ) |
| `consultar_publicacoes_djen` | Publicações e intimações no Diário de Justiça Eletrônico Nacional, por processo ou OAB | Comunica PJe (DJEN) |
| `baixar_certidao_djen` | Baixa a certidão PDF de uma publicação e devolve o caminho do arquivo salvo | Comunica PJe (DJEN) |

Transporte: **stdio** (uso local, um processo por usuário). SDK: `mcp` (FastMCP) + `httpx` assíncrono.

## Licença

Este projeto é distribuído sob a [PolyForm Noncommercial License 1.0.0](LICENSE.md): o código é
público e qualquer uso não comercial é livre (estudo, pesquisa, testes, uso pessoal). **Uso
comercial exige uma licença separada**, incluindo hospedar este serviço para terceiros, cobrar por
acesso, ou usar dentro de uma empresa com fins lucrativos. Para licenciamento comercial, contato:
acarvalho.adv.sp@gmail.com

Também existe uma versão hospedada (multiusuário, acessível via navegador ou direto pelo Claude,
sem precisar instalar nada localmente); para saber mais, use o mesmo contato acima.

## 1. Instalação

Requer Python 3.10+.

Com `pip`:

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS
pip install -e .
```

Ou com `uv`:

```bash
uv sync
```

## 2. Rodar standalone (teste)

```bash
python server.py
```

O processo fica aguardando mensagens MCP via stdin/stdout, então sem um cliente ele parece travado. Isso é esperado. Logs vão para stderr.

Para testar as tools sem o protocolo MCP, chamando as funções diretamente contra as APIs reais:

```bash
python test_manual.py
```

O script usa o processo `1006314-27.2023.8.26.0005` (TJSP) como caso de teste. Ele precisa de acesso à internet a partir de um IP brasileiro, porque o DJEN bloqueia IPs de fora do Brasil.

## 3. Registrar no Claude Desktop ou Claude Code

Edite o arquivo de configuração do Claude Desktop (`claude_desktop_config.json`) ou do Claude Code, usando o Python do venv com caminho absoluto:

```json
{
  "mcpServers": {
    "cnj-processual": {
      "command": "C:\\caminho\\para\\cnj-processual\\.venv\\Scripts\\python.exe",
      "args": ["C:\\caminho\\para\\cnj-processual\\server.py"]
    }
  }
}
```

No Linux/macOS, use o caminho `.venv/bin/python`. Opcionalmente, defina a variável de ambiente
`DATAJUD_API_KEY` em um bloco `env` se o CNJ rotacionar a chave pública padrão (veja a seção
Desenvolvimento).

## 4. Uso de cada tool

### `consultar_processo_datajud`

Parâmetros:
- `tribunal` (obrigatório): sigla minúscula, ex: `"tjsp"`, `"trf1"`, `"stj"`.
- `numero_processo` (obrigatório): 20 dígitos, com ou sem máscara.

Exemplo:

```json
{
  "tribunal": "tjsp",
  "numero_processo": "1006314-27.2023.8.26.0005"
}
```

Retorno (resumido):

```json
{
  "encontrado": true,
  "total": 1,
  "processos": [
    {
      "numeroProcesso": "10063142720238260005",
      "tribunal": "TJSP",
      "grau": "G1",
      "classe": "EMBARGOS DE TERCEIRO CÍVEL",
      "dataAjuizamento": "...",
      "orgaoJulgador": "...",
      "totalMovimentos": 42,
      "movimentos": [
        {"codigo": 123, "nome": "Conclusos para despacho", "dataHora": "2026-08-19T10:00:00"}
      ]
    }
  ]
}
```

Se o processo não existir no tribunal informado, o retorno é `{"encontrado": false, "mensagem": "..."}`. Isso não é erro.

Limitações:
- **Não traz nomes de partes.** A API DataJud não expõe esse dado por política de proteção de dados.
- **A API é lenta e instável.** Observamos respostas de cerca de 48s e, em horários de pico, HTTP 504 e 429. A tool usa timeout de 70s e 2 tentativas antes de reportar falha. Uma consulta pode levar mais de um minuto.

### `consultar_publicacoes_djen`

Exige ao menos um entre `numero_processo` e `numero_oab`. Todos os parâmetros são opcionais.

Parâmetros:
- `numero_processo`: 20 dígitos, máscara removida automaticamente.
- `sigla_tribunal`: ex: `"TJSP"`.
- `numero_oab`: ex: `"330659"`.
- `uf_oab`: 2 letras, ex: `"SP"`.
- `data_disponibilizacao_inicio`, `data_disponibilizacao_fim`: `YYYY-MM-DD`.
- `pagina` (padrão 1), `itens_por_pagina` (padrão 50).

Exemplo por processo:

```json
{
  "numero_processo": "1006314-27.2023.8.26.0005",
  "itens_por_pagina": 5
}
```

Exemplo por OAB e período:

```json
{
  "numero_oab": "330659",
  "uf_oab": "SP",
  "data_disponibilizacao_inicio": "2026-08-01",
  "data_disponibilizacao_fim": "2026-08-31"
}
```

Retorno (resumido):

```json
{
  "total": 14,
  "pagina": 1,
  "itens": [
    {
      "data_disponibilizacao": "2026-08-20",
      "siglaTribunal": "TJSP",
      "nomeOrgao": "UPJ da 1ª a 5ª Varas Cíveis - Regional V - São Miguel Paulista",
      "tipoComunicacao": "Intimação",
      "tipoDocumento": "DESPACHO/DECISÃO",
      "nomeClasse": "EMBARGOS DE TERCEIRO CíVEL",
      "numeroprocessocommascara": "1006314-27.2023.8.26.0005",
      "hash": "2wyKMz7lRxOsxkeiyTKBA82YEJaAPk",
      "link": "https://...",
      "destinatarios": [{"nome": "...", "polo": "A"}],
      "destinatarioadvogados": [{"nome": "...", "numero_oab": "330659", "uf_oab": "SP"}],
      "texto": "Texto da publicação em texto puro..."
    }
  ]
}
```

Limitações:
- **Busca por OAB pode ser incompleta.** Tribunais gravam a OAB com sufixos variados (`"123456"`, `"123456-O"`, `"123456-A"`). A v1 não tenta esses sufixos automaticamente.
- **HTTP 403** significa geo-bloqueio: o IP de saída está fora do Brasil.
- **Falhas transitórias** (timeout, erro de conexão, HTTP 500, 502, 503 ou 504) são repetidas uma vez antes de virar erro.
- **Intervalo de datas:** `data_disponibilizacao_inicio` não pode ser posterior a `data_disponibilizacao_fim`.

### `baixar_certidao_djen`

Parâmetro:
- `hash`: valor do campo `hash` retornado por `consultar_publicacoes_djen`.

Exemplo:

```json
{
  "hash": "2wyKMz7lRxOsxkeiyTKBA82YEJaAPk"
}
```

Retorno:

```json
{
  "arquivo": "C:\\Users\\ariel\\AppData\\Local\\Temp\\cnj-processual\\certidoes\\certidao_2wyKMz7lRxOsxkeiyTKBA82YEJaAPk.pdf",
  "bytes": 61383
}
```

O PDF fica em `<diretório temporário>/cnj-processual/certidoes/`. O conteúdo binário não vai na resposta MCP. A cada novo download, certidões com mais de 7 dias são removidas dessa pasta.

## Tratamento de erros

Nenhuma exceção sai das tools. Toda falha volta como `{"erro": true, "mensagem": "..."}` com texto legível para o modelo ou para o usuário.

## Desenvolvimento

- `server.py`: servidor MCP e as três tools.
- `test_unit.py`: testes unitários offline (31 casos), com respostas HTTP simuladas. Não precisam de rede nem de IP brasileiro:
  ```bash
  python -m unittest test_unit -v
  ```
- `test_manual.py`: teste de ponta a ponta contra as APIs reais. Sai com código 1 se alguma verificação falhar.
- A chave pública do DataJud está em `server.py` (`DATAJUD_API_KEY_PADRAO`), com origem documentada em https://datajud-wiki.cnj.jus.br/api-publica/. Se o CNJ rotacioná-la, defina a variável de ambiente `DATAJUD_API_KEY`.
