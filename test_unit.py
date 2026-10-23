"""Testes unitários offline do servidor cnj-processual.

Não acessam a rede: as respostas HTTP são simuladas com httpx.MockTransport. Usa apenas
a stdlib (unittest), então não exige dependências extras.

Uso:
    python -m unittest test_unit -v
"""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

import cnj_processual._app as _app_mod
import cnj_processual.datajud as _datajud_mod
import cnj_processual.djen as _djen_mod
import cnj_processual.utils as _utils_mod


def _rodar(coro):
    return asyncio.run(coro)


class _Simulado:
    """Prepara o transporte HTTP simulado e zera os backoffs para os testes rodarem rápido."""

    def setUp(self):
        self._transporte_original = _utils_mod._TRANSPORTE_HTTP
        self._patches = [
            mock.patch.object(_datajud_mod, "DATAJUD_BACKOFF_S", 0),
            mock.patch.object(_djen_mod, "DJEN_BACKOFF_S", 0),
        ]
        for p in self._patches:
            p.start()
        self.chamadas: list[httpx.Request] = []

    def tearDown(self):
        _utils_mod._TRANSPORTE_HTTP = self._transporte_original
        for p in self._patches:
            p.stop()

    def responder(self, *respostas):
        """Cada chamada HTTP consome a próxima resposta da lista. Exceções são lançadas."""
        fila = list(respostas)

        def handler(request: httpx.Request) -> httpx.Response:
            self.chamadas.append(request)
            item = fila.pop(0) if len(fila) > 1 else fila[0]
            if isinstance(item, Exception):
                raise item
            return item

        _utils_mod._TRANSPORTE_HTTP = httpx.MockTransport(handler)


def _json(status: int, corpo) -> httpx.Response:
    return httpx.Response(status, json=corpo)


DATAJUD_OK = {
    "hits": {
        "total": {"value": 1, "relation": "eq"},
        "hits": [
            {
                "_source": {
                    "numeroProcesso": "10063142720238260005",
                    "tribunal": "TJSP",
                    "grau": "G1",
                    "classe": {"nome": "EMBARGOS DE TERCEIRO CÍVEL"},
                    "dataAjuizamento": "20230101000000",
                    "orgaoJulgador": {"nome": "1ª Vara Cível"},
                    "movimentos": [
                        {"codigo": 1, "nome": "Distribuição", "dataHora": "2023-01-01T00:00:00"},
                        {
                            "codigo": 2,
                            "nome": "Conclusos",
                            "dataHora": "2023-02-01T00:00:00",
                            "complementosTabelados": [{"nome": "Despacho"}],
                        },
                    ],
                }
            }
        ],
    }
}

DJEN_OK = {
    "count": 1,
    "items": [
        {
            "data_disponibilizacao": "2026-08-20",
            "siglaTribunal": "TJSP",
            "nomeOrgao": "1ª Vara",
            "tipoComunicacao": "Intimação",
            "tipoDocumento": "DESPACHO",
            "nomeClasse": "CLASSE",
            "numeroprocessocommascara": "1006314-27.2023.8.26.0005",
            "hash": "abc123",
            "link": "https://exemplo.test/x",
            "destinatarios": [{"nome": "FULANO", "polo": "A"}],
            "destinatarioadvogados": [
                {"advogado": {"nome": "ADV", "numero_oab": "330659", "uf_oab": "SP"}}
            ],
            "texto": "<p>Pra&ccedil;a &aacute; <b>negrito</b></p><script>x=1</script>",
        }
    ],
}


class UtilitariosTest(unittest.TestCase):
    def test_so_digitos_remove_mascara(self):
        self.assertEqual(_utils_mod._so_digitos("1006314-27.2023.8.26.0005"), "10063142720238260005")
        self.assertEqual(_utils_mod._so_digitos(None), "")

    def test_html_para_texto_decodifica_e_remove_tags_e_script(self):
        texto = _utils_mod._html_para_texto("<p>Pra&ccedil;a &aacute; <b>negrito</b></p><script>x=1</script>")
        self.assertEqual(texto, "Praça á negrito")

    def test_total_hits_aceita_dict_e_inteiro(self):
        self.assertEqual(_utils_mod._total_hits({"total": {"value": 7}}), 7)
        self.assertEqual(_utils_mod._total_hits({"total": 3}), 3)
        self.assertEqual(_utils_mod._total_hits({}), 0)
        self.assertEqual(_utils_mod._total_hits({"total": "x"}), 0)

    def test_validar_data(self):
        self.assertIsNone(_djen_mod._validar_data("x", "2026-08-01"))
        self.assertIsNone(_djen_mod._validar_data("x", None))
        self.assertIsNotNone(_djen_mod._validar_data("x", "01/08/2026"))


class DataJudTest(_Simulado, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.processo = "1006314-27.2023.8.26.0005"

    def consultar(self, tribunal="tjsp", numero=None):
        return _rodar(_datajud_mod.consultar_processo_datajud(tribunal=tribunal, numero_processo=numero or self.processo))

    def test_entrada_invalida_nao_faz_chamada_de_rede(self):
        self.responder(_json(200, DATAJUD_OK))
        self.assertTrue(self.consultar(tribunal="tj sp")["erro"])
        self.assertTrue(self.consultar(numero="123")["erro"])
        self.assertEqual(self.chamadas, [])

    def test_sucesso_extrai_campos_sem_partes(self):
        self.responder(_json(200, DATAJUD_OK))
        r = self.consultar()
        self.assertTrue(r["encontrado"])
        self.assertEqual(r["total"], 1)
        proc = r["processos"][0]
        self.assertEqual(proc["classe"], "EMBARGOS DE TERCEIRO CÍVEL")
        self.assertEqual(proc["orgaoJulgador"], "1ª Vara Cível")
        self.assertEqual(proc["totalMovimentos"], 2)
        self.assertEqual(proc["grau"], "G1")
        self.assertEqual(proc["grau_descricao"], "1º grau (primeira instância)")
        self.assertEqual(proc["movimentos"][1]["complementosTabelados"], [{"nome": "Despacho"}])
        self.assertNotIn("partes", proc)

    def test_requisicao_usa_url_header_e_corpo_corretos(self):
        self.responder(_json(200, DATAJUD_OK))
        self.consultar()
        req = self.chamadas[0]
        self.assertEqual(str(req.url), f"{_datajud_mod.DATAJUD_BASE_URL}/api_publica_tjsp/_search")
        self.assertTrue(req.headers["Authorization"].startswith("APIKey "))
        self.assertEqual(json.loads(req.content), {"query": {"match": {"numeroProcesso": "10063142720238260005"}}})

    def test_chave_pode_ser_sobrescrita_por_ambiente(self):
        self.responder(_json(200, DATAJUD_OK))
        with mock.patch.dict(os.environ, {"DATAJUD_API_KEY": "minha-chave"}):
            self.consultar()
        self.assertEqual(self.chamadas[0].headers["Authorization"], "APIKey minha-chave")

    def test_nao_encontrado_nao_e_erro(self):
        self.responder(_json(200, {"hits": {"total": {"value": 0}, "hits": []}}))
        r = self.consultar()
        self.assertFalse(r["encontrado"])
        self.assertNotIn("erro", r)

    def test_timeout_seguido_de_sucesso(self):
        import httpx as h
        self.responder(h.ReadTimeout("lento"), _json(200, DATAJUD_OK))
        r = self.consultar()
        self.assertTrue(r["encontrado"])
        self.assertEqual(len(self.chamadas), 2)

    def test_timeout_nas_duas_tentativas_vira_erro_estruturado(self):
        self.responder(httpx.ReadTimeout("lento"), httpx.ReadTimeout("lento"))
        r = self.consultar()
        self.assertTrue(r["erro"])
        self.assertIn("timeout", r["mensagem"])
        self.assertEqual(len(self.chamadas), _datajud_mod.DATAJUD_TENTATIVAS)

    def test_503_seguido_de_sucesso(self):
        self.responder(httpx.Response(503), _json(200, DATAJUD_OK))
        self.assertTrue(self.consultar()["encontrado"])

    def test_401_nao_e_repetido(self):
        self.responder(httpx.Response(401, text="no"))
        r = self.consultar()
        self.assertTrue(r["erro"])
        self.assertIn("DATAJUD_API_KEY", r["mensagem"])
        self.assertEqual(len(self.chamadas), 1)

    def test_corpo_nao_json_vira_erro(self):
        self.responder(httpx.Response(200, text="<html>oops</html>"))
        r = self.consultar()
        self.assertTrue(r["erro"])
        self.assertIn("não é JSON", r["mensagem"])

    def test_corpo_sem_hits_vira_erro(self):
        self.responder(_json(200, {"error": "algo"}))
        self.assertTrue(self.consultar()["erro"])

    def test_total_como_inteiro_funciona(self):
        corpo = json.loads(json.dumps(DATAJUD_OK))
        corpo["hits"]["total"] = 1
        self.responder(_json(200, corpo))
        self.assertEqual(self.consultar()["total"], 1)


class DjenTest(_Simulado, unittest.TestCase):
    def consultar(self, **kwargs):
        return _rodar(_djen_mod.consultar_publicacoes_djen(**kwargs))

    def test_exige_processo_ou_oab(self):
        self.responder(_json(200, DJEN_OK))
        self.assertTrue(self.consultar()["erro"])
        self.assertEqual(self.chamadas, [])

    def test_valida_processo_uf_e_datas(self):
        self.responder(_json(200, DJEN_OK))
        self.assertTrue(self.consultar(numero_processo="123")["erro"])
        self.assertTrue(self.consultar(numero_oab="1", uf_oab="SPP")["erro"])
        self.assertTrue(self.consultar(numero_oab="1", data_disponibilizacao_inicio="20/08/2026")["erro"])
        self.assertEqual(self.chamadas, [])

    def test_intervalo_invertido_e_rejeitado(self):
        self.responder(_json(200, DJEN_OK))
        r = self.consultar(numero_oab="1", data_disponibilizacao_inicio="2026-08-31", data_disponibilizacao_fim="2026-08-01")
        self.assertTrue(r["erro"])
        self.assertEqual(self.chamadas, [])

    def test_parametros_vao_na_query_com_nomes_da_api(self):
        self.responder(_json(200, DJEN_OK))
        self.consultar(numero_processo="1006314-27.2023.8.26.0005", sigla_tribunal="tjsp", itens_por_pagina=5)
        query = dict(self.chamadas[0].url.params)
        self.assertEqual(query["numeroProcesso"], "10063142720238260005")
        self.assertEqual(query["siglaTribunal"], "TJSP")
        self.assertEqual(query["itensPorPagina"], "5")
        self.assertEqual(query["pagina"], "1")
        self.assertNotIn("numeroOab", query)

    def test_sucesso_limpa_html_do_texto(self):
        self.responder(_json(200, DJEN_OK))
        r = self.consultar(numero_oab="330659")
        item = r["itens"][0]
        self.assertEqual(r["total"], 1)
        self.assertEqual(item["texto"], "Praça á negrito")
        self.assertEqual(item["destinatarioadvogados"][0]["numero_oab"], "330659")
        self.assertEqual(item["hash"], "abc123")

    def test_403_e_geobloqueio_sem_repetir(self):
        self.responder(httpx.Response(403))
        r = self.consultar(numero_oab="1")
        self.assertIn("geo-bloqueio", r["mensagem"])
        self.assertEqual(len(self.chamadas), 1)

    def test_500_seguido_de_sucesso(self):
        self.responder(httpx.Response(500), _json(200, DJEN_OK))
        self.assertEqual(self.consultar(numero_oab="1")["total"], 1)
        self.assertEqual(len(self.chamadas), 2)

    def test_502_persistente_vira_erro_apos_duas_tentativas(self):
        self.responder(httpx.Response(502), httpx.Response(502))
        r = self.consultar(numero_oab="1")
        self.assertTrue(r["erro"])
        self.assertEqual(len(self.chamadas), _djen_mod.DJEN_TENTATIVAS)

    def test_404_nao_e_repetido(self):
        self.responder(httpx.Response(404, text="nada"))
        self.assertTrue(self.consultar(numero_oab="1")["erro"])
        self.assertEqual(len(self.chamadas), 1)

    def test_timeout_seguido_de_sucesso(self):
        self.responder(httpx.ConnectTimeout("x"), _json(200, DJEN_OK))
        self.assertEqual(self.consultar(numero_oab="1")["total"], 1)


class CertidaoTest(_Simulado, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._dir_temp = tempfile.TemporaryDirectory()
        self._dir_patch = mock.patch.object(_djen_mod, "CERTIDAO_DIR", Path(self._dir_temp.name))
        self._dir_patch.start()

    def tearDown(self):
        self._dir_patch.stop()
        self._dir_temp.cleanup()
        super().tearDown()

    def baixar(self, hash_="abc123"):
        return _rodar(_djen_mod.baixar_certidao_djen(hash=hash_))

    def test_hash_invalido_nao_faz_chamada(self):
        self.responder(httpx.Response(200, content=b"%PDF-1.4"))
        self.assertTrue(self.baixar("../etc/passwd")["erro"])
        self.assertEqual(self.chamadas, [])

    def test_resposta_que_nao_e_pdf_vira_erro(self):
        self.responder(httpx.Response(200, content=b"<html>"))
        self.assertTrue(self.baixar()["erro"])
        self.assertFalse(any(Path(self._dir_temp.name).iterdir()))

    def test_sucesso_salva_pdf(self):
        self.responder(httpx.Response(200, content=b"%PDF-1.4 conteudo"))
        r = self.baixar()
        self.assertEqual(r["bytes"], len(b"%PDF-1.4 conteudo"))
        caminho = Path(r["arquivo"])
        self.assertEqual(caminho, (Path(self._dir_temp.name) / "certidao_abc123.pdf").resolve())
        self.assertEqual(caminho.read_bytes(), b"%PDF-1.4 conteudo")
        self.assertEqual(self.chamadas[0].url.path.rsplit("/", 2)[-2:], ["abc123", "certidao"])

    def test_remove_certidoes_mais_antigas_que_a_retencao(self):
        antiga = Path(self._dir_temp.name) / "certidao_antiga.pdf"
        antiga.write_bytes(b"%PDF")
        velha = time.time() - (_djen_mod.CERTIDAO_RETENCAO_DIAS + 1) * 86400
        os.utime(antiga, (velha, velha))
        recente = Path(self._dir_temp.name) / "certidao_recente.pdf"
        recente.write_bytes(b"%PDF")

        self.responder(httpx.Response(200, content=b"%PDF-1.4 novo"))
        self.baixar()

        self.assertFalse(antiga.exists())
        self.assertTrue(recente.exists())


class RegistroDeToolsTest(unittest.TestCase):
    def test_tres_tools_registradas(self):
        nomes = {t.name for t in _rodar(_app_mod.mcp.list_tools())}
        self.assertEqual(nomes, {"consultar_processo_datajud", "consultar_publicacoes_djen", "baixar_certidao_djen"})


if __name__ == "__main__":
    unittest.main()
