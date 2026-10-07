"""Teste manual de ponta a ponta contra as APIs reais do CNJ (sem o protocolo MCP).

Chama diretamente as funções das tools com o processo de teste 1006314-27.2023.8.26.0005 (TJSP)
e imprime os resultados. Requer acesso à internet a partir de um IP brasileiro (DJEN bloqueia
IPs de fora do Brasil).

Uso:
    python test_manual.py
"""

import asyncio
import json
import sys
from pathlib import Path

import server

PROCESSO = "1006314-27.2023.8.26.0005"
TRIBUNAL = "tjsp"

falhas: list[str] = []


def checar(condicao: bool, descricao: str) -> None:
    print(("  [OK]   " if condicao else "  [FALHA] ") + descricao)
    if not condicao:
        falhas.append(descricao)


def secao(titulo: str) -> None:
    print(f"\n=== {titulo} ===")


async def testar_datajud() -> None:
    secao(f"consultar_processo_datajud ({TRIBUNAL}, {PROCESSO})")
    resultado = await server.consultar_processo_datajud(tribunal=TRIBUNAL, numero_processo=PROCESSO)
    print(json.dumps(resultado, ensure_ascii=False, indent=2)[:1500])
    checar(not resultado.get("erro"), "sem erro na resposta")
    checar(resultado.get("encontrado") is True, "processo encontrado no DataJud")
    if resultado.get("processos"):
        proc = resultado["processos"][0]
        checar(proc.get("numeroProcesso") == "10063142720238260005", "numeroProcesso confere")
        checar(bool(proc.get("classe")), "classe preenchida")
        checar(bool(proc.get("orgaoJulgador")), "orgaoJulgador preenchido")
        checar(proc.get("totalMovimentos", 0) > 0, "movimentações retornadas")
        checar("partes" not in proc, "nenhum campo de partes exposto (esperado)")


async def testar_djen() -> dict | None:
    secao(f"consultar_publicacoes_djen (numero_processo={PROCESSO})")
    resultado = await server.consultar_publicacoes_djen(numero_processo=PROCESSO, itens_por_pagina=5)
    print(json.dumps(resultado, ensure_ascii=False, indent=2)[:1500])
    checar(not resultado.get("erro"), "sem erro na resposta")
    itens = resultado.get("itens", [])
    checar(len(itens) > 0, f"{len(itens)} publicação(ões) retornada(s)")
    if itens:
        primeiro = itens[0]
        checar(
            all(primeiro.get(c) for c in ("data_disponibilizacao", "siglaTribunal", "hash")),
            "campos data_disponibilizacao, siglaTribunal e hash preenchidos",
        )
        checar("&aacute;" not in primeiro.get("texto", "") and "<" not in primeiro.get("texto", ""),
               "texto decodificado e sem tags HTML")
        checar(bool(primeiro.get("texto")), "texto da publicação não vazio")
        return primeiro
    return None


async def testar_certidao(publicacao: dict | None) -> None:
    secao("baixar_certidao_djen")
    if publicacao is None:
        checar(False, "sem publicação para obter o hash da certidão")
        return
    resultado = await server.baixar_certidao_djen(hash=publicacao["hash"])
    print(json.dumps(resultado, ensure_ascii=False, indent=2))
    checar(not resultado.get("erro"), "sem erro na resposta")
    caminho = resultado.get("arquivo")
    checar(bool(caminho) and Path(caminho).read_bytes()[:4] == b"%PDF", "PDF salvo e com cabeçalho válido")


async def testar_validacoes() -> None:
    secao("validações de entrada (sem chamadas de rede)")
    r = await server.consultar_publicacoes_djen()
    checar(r.get("erro") is True, "DJEN sem numero_processo nem numero_oab é rejeitado")
    r = await server.consultar_publicacoes_djen(numero_processo="123")
    checar(r.get("erro") is True, "número de processo com menos de 20 dígitos é rejeitado")
    r = await server.consultar_publicacoes_djen(numero_oab="330659", data_disponibilizacao_inicio="20/08/2026")
    checar(r.get("erro") is True, "data fora do formato YYYY-MM-DD é rejeitada")
    r = await server.consultar_publicacoes_djen(
        numero_oab="330659", data_disponibilizacao_inicio="2026-08-31", data_disponibilizacao_fim="2026-08-01"
    )
    checar(r.get("erro") is True, "intervalo de datas invertido é rejeitado")
    r = await server.consultar_processo_datajud(tribunal="tj sp", numero_processo=PROCESSO)
    checar(r.get("erro") is True, "sigla de tribunal inválida é rejeitada")
    r = await server.baixar_certidao_djen(hash="../etc/passwd")
    checar(r.get("erro") is True, "hash com caracteres inválidos é rejeitado")


async def main() -> int:
    await testar_validacoes()
    await testar_datajud()
    publicacao = await testar_djen()
    await testar_certidao(publicacao)

    print("\n=== resumo ===")
    if falhas:
        print(f"{len(falhas)} verificação(ões) falharam:")
        for f in falhas:
            print(f"  - {f}")
        return 1
    print("Todas as verificações passaram.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
