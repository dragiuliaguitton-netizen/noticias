"""Testes de ponta a ponta do radar com dados FICTÍCIOS (tests/fixtures).

Rodar:  python -m unittest discover -s tests -v

Cobre: coleta de múltiplas fontes (PT/EN), falha isolada de fonte, deduplicação
(DOI/PMID/URL/título), histórico persistente, classificação e tetos de
segurança, identificação de estudos/guidelines, ideias de conteúdo, relatório
semanal, alerta extraordinário e envio real por SMTP para um servidor local.
"""

from __future__ import annotations

import asyncore
import email
import json
import os
import shutil
import smtpd
import tempfile
import threading
import unittest
import warnings
from email import policy
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

RAIZ = Path(__file__).resolve().parent.parent
FIX = RAIZ / "tests" / "fixtures"

from radar import classificacao as cl  # noqa: E402
from radar.fontes import Coletor, parse_pubmed_xml, parse_rss  # noqa: E402
from radar.historico import Historico, similaridade  # noqa: E402
from radar.pipeline import processar_dia  # noqa: E402
from radar.relatorio import gerar_e_enviar_semanal, montar_relatorio  # noqa: E402
from radar.util import carregar_config  # noqa: E402


# ------------------------------------------------------------------ fakes --

class RespostaFalsa:
    def __init__(self, conteudo: bytes, status=200):
        self.content = conteudo
        self.text = conteudo.decode("utf-8")
        self.status_code = status

    def json(self):
        return json.loads(self.text)


class HttpFalso:
    """Roteia URLs para fixtures. EMA simula fonte fora do ar."""
    tentativas = 1

    def __init__(self):
        self.chamadas = []

    def get(self, url, params=None, **kw):
        self.chamadas.append((url, params))
        if "esearch.fcgi" in url:
            termo = (params or {}).get("term", "")
            ids = []
            if "JAMA Dermatol" in termo:
                ids = ["99000001"]
            elif "Br J Dermatol" in termo:
                ids = ["99000002"]
            elif "J Invest Dermatol" in termo:
                ids = ["99000003", "99000001"]  # repetido de propósito
            elif "J Eur Acad" in termo:
                ids = ["99000004"]
            return RespostaFalsa(json.dumps({"esearchresult": {"idlist": ids}}).encode())
        if "efetch.fcgi" in url:
            return RespostaFalsa((FIX / "pubmed_efetch.xml").read_bytes())
        if "ema.europa.eu" in url:
            raise ConnectionError("EMA fora do ar (simulado)")
        if "fda.gov" in url and "press-releases" in url:
            return RespostaFalsa((FIX / "fda_rss.xml").read_bytes())
        if "fda.gov" in url or "jamanetwork" in url:
            return RespostaFalsa(b"<rss><channel></channel></rss>")
        if "news.google.com" in url:
            if "hl=pt-BR" in url and "Anvisa+cosm" in url:
                return RespostaFalsa((FIX / "gnews_pt.xml").read_bytes())
            if "hl=en-US" in url and "FDA+approval" in url:
                return RespostaFalsa((FIX / "gnews_en.xml").read_bytes())
            if "hl=en-US" in url and "safety+alert" in url:
                return RespostaFalsa(b"isto nao e xml")  # página mudou de estrutura
            return RespostaFalsa(b"<rss><channel></channel></rss>")
        if "clinicaltrials.gov" in url:
            if (params or {}).get("query.cond") == "atopic dermatitis":
                return RespostaFalsa((FIX / "ctgov.json").read_bytes())
            return RespostaFalsa(b'{"studies": []}')
        raise AssertionError(f"URL inesperada: {url}")


class ServidorSMTP(smtpd.SMTPServer):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.mensagens = []

    def process_message(self, peer, mailfrom, rcpttos, data, **kw):
        self.mensagens.append({"de": mailfrom, "para": rcpttos,
                               "msg": email.message_from_bytes(data, policy=policy.default)})


# ------------------------------------------------------------------ testes --

class TestParsers(unittest.TestCase):
    def test_pubmed(self):
        itens = parse_pubmed_xml((FIX / "pubmed_efetch.xml").read_text())
        self.assertEqual(len(itens), 4)
        a = itens[0]
        self.assertEqual(a["identificadores"]["pmid"], "99000001")
        self.assertEqual(a["identificadores"]["doi"], "10.9999/jamadermatol.2026.0001")
        self.assertEqual(a["identificadores"]["nct"], "NCT09999991")
        self.assertIn("Randomized Controlled Trial", a["tipos_publicacao"])
        self.assertTrue(a["publicado_em"].startswith("2026-09-20"))

    def test_google_news_remove_veiculo_do_titulo(self):
        itens = parse_rss((FIX / "gnews_pt.xml").read_bytes(), "Google News", "noticia", "pt", "q")
        self.assertEqual(itens[0]["fonte"], "Anvisa")
        self.assertFalse(itens[0]["titulo_original"].endswith(" - Anvisa"))
        self.assertEqual(itens[1]["titulo_original"], "Truque viral de skincare apaga rugas da noite para o dia")

    def test_similaridade(self):
        self.assertGreater(similaridade(
            "Viral skincare hack erases wrinkles overnight",
            "This viral skincare hack erases wrinkles overnight, users say"), 0.6)
        self.assertLess(similaridade("Psoriasis biologic trial", "Melanoma screening with AI"), 0.2)


class TestPontaAPonta(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="radar-teste-"))
        self.env_antigo = dict(os.environ)
        os.environ.update({
            "RADAR_NOW": "2026-09-22T12:00:00+00:00",
            "RADAR_HISTORICO": str(self.tmp / "historico.json"),
            "RADAR_DIARIO": str(self.tmp / "diario"),
            "RADAR_RELATORIOS": str(self.tmp / "relatorios"),
            "RADAR_SAIDA": str(self.tmp / "saida"),
            "EMAIL_TO": "giulia@example.com",
            "EMAIL_FROM": "radar@example.com",
            "SMTP_HOST": "127.0.0.1",
            "SMTP_STARTTLS": "0",
            "SMTP_ALLOW_NO_AUTH": "1",
        })
        for k in ("ANTHROPIC_API_KEY", "SMTP_USER", "SMTP_PASSWORD", "RESEND_API_KEY"):
            os.environ.pop(k, None)
        self.mapa = {}
        self.smtp = ServidorSMTP(("127.0.0.1", 0), None, decode_data=False, map=self.mapa)
        os.environ["SMTP_PORT"] = str(self.smtp.socket.getsockname()[1])
        self.parar = threading.Event()

        def laco():
            while not self.parar.is_set():
                asyncore.loop(timeout=0.05, count=1, map=self.mapa)
        self.thread = threading.Thread(target=laco, daemon=True)
        self.thread.start()
        self.cfg = carregar_config()

    def tearDown(self):
        self.parar.set()
        self.thread.join(2)
        self.smtp.close()
        os.environ.clear()
        os.environ.update(self.env_antigo)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dia(self):
        col = Coletor(self.cfg, http=HttpFalso())
        return processar_dia(self.cfg, coletor=col), col

    def test_fluxo_completo(self):
        # ---------------- Dia 1: coleta e classificação ----------------
        r1, col = self._dia()
        s = r1["stats"]
        self.assertGreater(s["brutos"], 8)
        fontes_com_erro = {e["fonte"] for e in r1["erros"]}
        self.assertIn("EMA — News", fontes_com_erro, "falha da EMA deve ser registrada")
        self.assertTrue(any("safety alert" in f for f in fontes_com_erro), "XML inválido registrado")

        h = Historico()
        por_titulo = {i["titulo_original"]: i for i in h.itens.values()}
        self.assertNotIn("Stock market closes higher", por_titulo, "fora do escopo dermatológico")
        # PMID repetido em duas consultas → um único item
        self.assertEqual(sum(1 for i in h.itens.values()
                             if (i.get("identificadores") or {}).get("pmid") == "99000001"), 1)
        # ensaio fase 3 em periódico de alto impacto
        ensaio = por_titulo["Exemplimab vs Placebo in Moderate-to-Severe Atopic Dermatitis: A Phase 3 Randomized Clinical Trial"]
        self.assertEqual(ensaio["tipo_evidencia"], "ensaio clínico randomizado")
        self.assertEqual(ensaio["status_terapia"], "em estudo")
        self.assertIn(ensaio["relevancia"], ("relevante", "muito_relevante"))
        self.assertIn("fictional trial", ensaio["resumo"])  # conclusão dos autores, sem invenção
        # guideline
        gl = por_titulo["European guideline on the management of acne: 2026 update"]
        self.assertEqual(gl["tipo_evidencia"], "guideline")
        # observacional → limitação de causalidade
        obs = por_titulo["Sunscreen use and melanoma risk: a population-based cohort study"]
        self.assertEqual(obs["tipo_evidencia"], "estudo observacional")
        self.assertTrue(any("causalidade" in l for l in obs["limitacoes"]))
        # pré-clínico → experimental e nunca 🔴
        pre = por_titulo["A novel topical compound reverses hair follicle miniaturization in mice"]
        self.assertEqual(pre["status_terapia"], "experimental")
        self.assertNotEqual(pre["relevancia"], "muito_relevante")
        # aprovação FDA (fonte oficial) → 🔴 + aprovado
        fda = por_titulo["FDA Approves Exemplimab for Moderate-to-Severe Atopic Dermatitis"]
        self.assertEqual(fda["status_terapia"], "aprovado")
        self.assertEqual(fda["relevancia"], "muito_relevante")
        # notícia que repercute a aprovação: corroborada pela fonte primária
        noticia = por_titulo["FDA clears exemplimab, a new injectable for eczema"]
        self.assertTrue(noticia["fonte_primaria_confirmada"])
        # notícia viral sem fonte primária: teto 🟡
        viral = por_titulo["This viral skincare hack erases wrinkles overnight, TikTok users say"]
        self.assertFalse(viral["fonte_primaria_confirmada"])
        self.assertIn(viral["relevancia"], ("interessante", "baixa"))
        # mesma notícia viral por outro veículo = duplicata por título → vira menção
        self.assertNotIn("This viral skincare hack erases wrinkles overnight, TikTok users claim", por_titulo)
        self.assertEqual(len(viral["mencoes"]), 1)
        self.assertEqual(noticia["fonte_primaria_id"], fda["id"], "prefere fonte oficial")
        self.assertTrue(any(m["motivo"] == "repercussão na imprensa" for m in fda["mencoes"]))
        self.assertFalse(any("n≈0" in l for l in obs["limitacoes"]))
        # Anvisa (veículo oficial gov.br) + recolhimento → alerta
        anvisa = por_titulo["Anvisa determina recolhimento de lote de protetor solar por contaminação"]
        self.assertEqual(anvisa["nivel_fonte"], 1)
        self.assertTrue(anvisa["alerta_seguranca"])
        self.assertEqual(anvisa["relevancia"], "muito_relevante")
        self.assertIsNotNone(anvisa["alerta_enviado_em"])
        # ClinicalTrials: mesmo NCT do artigo → atualização de assunto monitorado
        ct = [i for i in h.itens.values() if i.get("tipo_fonte") == "registro_ensaios"][0]
        self.assertEqual(ct.get("atualizacao_de"), ensaio["id"])
        # ideias de conteúdo só para 🔴/🟠
        for i in h.itens.values():
            if i.get("ideia_conteudo"):
                self.assertGreaterEqual(cl.ORDEM_REL[i["relevancia"]], 2)
        self.assertTrue(any(i.get("ideia_conteudo") for i in h.itens.values()))
        # registro diário
        self.assertTrue((self.tmp / "diario" / "2026-09-22.md").exists())

        # alertas extraordinários enviados por SMTP real (servidor local)
        alertas = [m for m in self.smtp.mensagens if m["msg"]["Subject"].startswith("🚨 ALERTA DERMATOLOGIA")]
        self.assertGreaterEqual(len(alertas), 2)  # FDA + Anvisa
        corpo = alertas[0]["msg"].get_body(("plain",)).get_content()
        for trecho in ("1. O que aconteceu", "2. Quem comunicou", "3. Quando", "4. Quem pode ser afetado",
                       "5. O que ainda não se sabe", "6. Fonte original"):
            self.assertIn(trecho, corpo)
        n_alertas = len(alertas)

        # ---------------- Dia 2: mesmos dados → nada novo -------------
        r2, _ = self._dia()
        self.assertEqual(r2["stats"]["novos"], 0)
        self.assertGreater(r2["stats"]["duplicados"], 8)
        self.assertEqual(len([m for m in self.smtp.mensagens
                              if m["msg"]["Subject"].startswith("🚨")]), n_alertas, "alerta não repete")

        # ---------------- Relatório semanal ---------------------------
        rel = montar_relatorio(Historico(), self.cfg)
        self.assertGreaterEqual(len(rel["destaques"]), 5)
        self.assertTrue(rel["estudos"])
        self.assertTrue(rel["alertas"])
        self.assertTrue(rel["ideias"])
        self.assertTrue(any(s == "aprovado" for s, _ in rel["tratamentos"]))
        self.assertTrue(any(s == "em estudo" for s, _ in rel["tratamentos"]))
        aprovados = dict(rel["tratamentos"])["aprovado"]
        self.assertTrue(all(i["nivel_fonte"] == 1 for i in aprovados), "aprovação só de fonte oficial")

        res = gerar_e_enviar_semanal(self.cfg)
        self.assertTrue(res["enviado"], res)
        semanais = [m for m in self.smtp.mensagens if "Radar Semanal" in m["msg"]["Subject"]]
        self.assertEqual(len(semanais), 1)
        msg = semanais[0]["msg"]
        self.assertEqual(msg["Subject"], "🧴 Radar Semanal de Dermatologia — 22/09/2026")
        texto = msg.get_body(("plain",)).get_content()
        for sec in ("1. O que você precisa saber esta semana", "2. Novidades em tratamentos",
                    "3. Estudos que valem a leitura", "4. Alertas de segurança",
                    "5. Oportunidades para meu Instagram", "6. Assuntos que estão ganhando atenção",
                    "7. Muito barulho, pouca evidência", "Auditoria"):
            self.assertIn(sec, texto)
        self.assertIn("https://pubmed.ncbi.nlm.nih.gov/99000001/", texto)
        anexos = [p.get_filename() for p in msg.iter_attachments()]
        self.assertIn("radar-semanal-2026-09-22.md", anexos)

        # itens marcados como reportados → não se repetem no próximo relatório
        h = Historico()
        self.assertTrue(any(i.get("relatorios") for i in h.itens.values()))
        self.assertTrue(any(i.get("ideia_usada_em") for i in h.itens.values()))
        rel2 = montar_relatorio(h, self.cfg)
        ids1 = set(rel["todos"])
        self.assertFalse(ids1 & set(rel2["todos"]))

    def test_sem_email_configurado_nao_finge_envio(self):
        self._dia()
        for k in ("SMTP_HOST", "SMTP_ALLOW_NO_AUTH"):
            os.environ.pop(k)
        res = gerar_e_enviar_semanal(self.cfg)
        self.assertFalse(res["enviado"])
        self.assertIn("Nenhum provedor", res["erro"])
        self.assertTrue(list((self.tmp / "saida").glob("semanal-*.eml")))
        # nada marcado como reportado, pois não foi enviado
        self.assertFalse(any(i.get("relatorios") for i in Historico().itens.values()))

    def test_ia_nao_ultrapassa_tetos(self):
        """Mesmo que a IA diga 🔴, notícia sem fonte primária fica em 🟡."""
        from radar.pipeline import _aplicar_analise
        item = {"id": "x", "titulo_original": "Miracle cream cures psoriasis, blog says",
                "resumo_original": "", "tipo_fonte": "noticia", "fonte": "Blog",
                "url": "https://blog.example/x", "identificadores": {}, "tipos_publicacao": []}
        cl.enriquecer(item, self.cfg)
        _aplicar_analise(item, {
            "dermatologico": True, "titulo_pt": "Creme milagroso", "o_que_aconteceu": "x",
            "por_que_importa": "x", "tipo_evidencia": "notícia", "limitacoes": [],
            "status_terapia": "aprovado", "relevancia": "muito_relevante",
            "justificativa_relevancia": "x", "alerta_extraordinario": True, "eh_publicidade": False,
            "ideia_conteudo": None})
        self.assertEqual(item["relevancia"], "interessante")
        self.assertNotEqual(item["status_terapia"], "aprovado", "IA não pode 'promover' status")


if __name__ == "__main__":
    unittest.main()
