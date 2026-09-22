"""Coletores de fontes.

Cada coletor devolve uma lista de itens (dict) no formato bruto comum:

    titulo_original, url, fonte, tipo_fonte, idioma, publicado_em,
    atualizado_em, resumo_original, identificadores{doi,pmid,nct},
    tipos_publicacao, periodico, consulta

Falhas são capturadas POR CONSULTA/FONTE e registradas em `erros` — uma fonte
fora do ar nunca interrompe o restante da coleta.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from .util import Http, agora, iso, limpar_html, log, parse_data

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
GNEWS = "https://news.google.com/rss/search"
CTGOV = "https://clinicaltrials.gov/api/v2/studies"

TERMO_DERMATO = re.compile(
    r"\b(skin\w*|wrinkl\w*|rugas?|derma\w*|dermat\w*|melanom\w*|psoria\w*|eczema|acne|alopeci\w*|"
    r"vitiligo|urticari\w*|hidradenit\w*|rosacea|ros[aá]cea|pele|cut[aâ]ne\w*|"
    r"cutaneous|sunscreen\w*|protetor solar|filtro solar|carcinoma basocelular|"
    r"basal cell|squamous cell|espinocelular|botul\w*|filler\w*|preench\w*|"
    r"cosm[eé]t\w*|pemphig\w*|penfig\w*|hansen\w*|leprosy|hanseníase|prurigo|"
    r"atopic|at[oó]pica|seborr?h?e\w*|seborreic\w*|nail|unha|scalp|couro cabeludo|"
    r"hair loss|queda de cabelo|laser|tattoo|tatuag\w*|isotretino\w*|minoxidil|"
    r"dupilumab|tralokinumab|lebrikizumab|nemolizumab|upadacitinib|abrocitinib|"
    r"ruxolitinib|ritlecitinib|baricitinib|deuruxolitinib|bimekizumab|secukinumab|"
    r"deucravacitinib|roflumilast|tapinarof|spesolimab|clascoterone)\b",
    re.IGNORECASE,
)


def eh_dermatologico(texto: str) -> bool:
    return bool(TERMO_DERMATO.search(texto or ""))


class Coletor:
    def __init__(self, cfg: dict, http: Http | None = None):
        self.cfg = cfg
        self.http = http or Http()
        self.erros: list[dict] = []
        self.stats: dict[str, int] = {}

    # ------------------------------------------------------------- util ---
    def _erro(self, fonte: str, detalhe: Exception | str) -> None:
        msg = f"{type(detalhe).__name__}: {detalhe}" if isinstance(detalhe, Exception) else detalhe
        log.warning("Falha em %s — %s (seguindo com as demais fontes)", fonte, msg)
        self.erros.append({"fonte": fonte, "erro": str(msg)[:300], "quando": iso(agora())})

    def _conta(self, fonte: str, n: int) -> None:
        self.stats[fonte] = self.stats.get(fonte, 0) + n

    # ---------------------------------------------------------- coleta ----
    def coletar_tudo(self) -> list[dict]:
        itens: list[dict] = []
        for nome, func in (
            ("PubMed", self.pubmed),
            ("Feeds RSS", self.feeds),
            ("Google News", self.noticias),
            ("ClinicalTrials.gov", self.ensaios),
        ):
            try:
                novos = func()
                log.info("%-18s → %d itens brutos", nome, len(novos))
                itens.extend(novos)
            except Exception as e:  # noqa: BLE001 — robustez: nunca derruba tudo
                self._erro(nome, e)
        return itens

    # ---------------------------------------------------------- PubMed ----
    def _pubmed_params(self) -> dict:
        p = {"db": "pubmed", "retmode": "json", "tool": "radar-dermatologia"}
        if os.getenv("NCBI_API_KEY"):
            p["api_key"] = os.environ["NCBI_API_KEY"]
        if os.getenv("RADAR_CONTACT_EMAIL"):
            p["email"] = os.environ["RADAR_CONTACT_EMAIL"]
        return p

    def pubmed(self) -> list[dict]:
        cp = self.cfg.get("pubmed", {})
        dias = int(self.cfg.get("janela_dias", 3))
        fim = agora()
        ini = fim - timedelta(days=dias)
        faixa = f'("{ini:%Y/%m/%d}"[edat] : "{fim:%Y/%m/%d}"[edat])'
        n = int(cp.get("max_por_consulta", 25))

        consultas: list[tuple[str, str]] = []
        for rev in cp.get("periodicos", []):
            consultas.append((f"periódico {rev}", f'"{rev}"[ta] AND {faixa}'))
        termo = " ".join(cp.get("termo_dermatologico", "").split())
        for rev in cp.get("periodicos_gerais", []):
            consultas.append((f"periódico {rev}", f'"{rev}"[ta] AND {termo} AND {faixa}'))
        for q in cp.get("consultas_tematicas", []):
            consultas.append((q[:60], f"({q}) AND {faixa}"))

        pmids: dict[str, str] = {}
        for rotulo, termo_q in consultas:
            try:
                r = self.http.get(f"{EUTILS}/esearch.fcgi",
                                  params={**self._pubmed_params(), "term": termo_q,
                                          "retmax": n, "sort": "date"})
                for pid in r.json().get("esearchresult", {}).get("idlist", []):
                    pmids.setdefault(pid, rotulo)
            except Exception as e:  # noqa: BLE001
                self._erro(f"PubMed [{rotulo}]", e)
        if not pmids:
            return []

        itens: list[dict] = []
        ids = list(pmids)
        for i in range(0, len(ids), 100):
            lote = ids[i:i + 100]
            try:
                itens.extend(self._pubmed_detalhes(lote, pmids))
            except Exception as e:  # noqa: BLE001
                self._erro("PubMed efetch", e)
        self._conta("PubMed", len(itens))
        return itens

    def _pubmed_detalhes(self, lote: list[str], rotulos: dict[str, str]) -> list[dict]:
        params = {**self._pubmed_params(), "id": ",".join(lote), "retmode": "xml"}
        r = self.http.get(f"{EUTILS}/efetch.fcgi", params=params)
        return parse_pubmed_xml(r.text, rotulos)

    # ------------------------------------------------------------- RSS ----
    def feeds(self) -> list[dict]:
        itens: list[dict] = []
        limite = agora() - timedelta(days=int(self.cfg.get("janela_dias", 3)) + 4)
        for f in self.cfg.get("feeds", []):
            try:
                r = self.http.get(f["url"])
                novos = parse_rss(r.content, fonte=f["nome"], tipo_fonte=f.get("tipo", "noticia"),
                                  idioma=f.get("idioma", "en"), consulta=f"feed {f['nome']}")
                if f.get("filtrar"):
                    novos = [x for x in novos
                             if eh_dermatologico(x["titulo_original"] + " " + x["resumo_original"])]
                novos = [x for x in novos if _recente(x, limite)]
                self._conta(f["nome"], len(novos))
                itens.extend(novos)
            except Exception as e:  # noqa: BLE001
                self._erro(f["nome"], e)
        return itens

    # ------------------------------------------------------ Google News ---
    def noticias(self) -> list[dict]:
        cn = self.cfg.get("noticias", {})
        n = int(cn.get("max_por_consulta", 15))
        dias = int(self.cfg.get("janela_dias", 3))
        limite = agora() - timedelta(days=dias + 1)
        locais = {"pt": ("pt-BR", "BR", "BR:pt-419"), "en": ("en-US", "US", "US:en")}
        itens: list[dict] = []
        for idioma, consultas in (cn.get("consultas") or {}).items():
            hl, gl, ceid = locais.get(idioma, locais["en"])
            for q in consultas:
                url = (f"{GNEWS}?q={quote_plus(q + f' when:{dias}d')}"
                       f"&hl={hl}&gl={gl}&ceid={ceid}")
                try:
                    r = self.http.get(url)
                    novos = parse_rss(r.content, fonte="Google News", tipo_fonte="noticia",
                                      idioma=idioma, consulta=q)[:n]
                    novos = [x for x in novos if _recente(x, limite)
                             and eh_dermatologico(x["titulo_original"] + " " + x["resumo_original"])]
                    self._conta(f"Google News ({idioma})", len(novos))
                    itens.extend(novos)
                except Exception as e:  # noqa: BLE001
                    self._erro(f"Google News [{q}]", e)
        return itens

    # -------------------------------------------------- ClinicalTrials ----
    def ensaios(self) -> list[dict]:
        cc = self.cfg.get("clinicaltrials", {})
        if not cc.get("ativo", True):
            return []
        dias = int(self.cfg.get("janela_dias", 3))
        ini = (agora() - timedelta(days=dias)).strftime("%Y-%m-%d")
        itens: list[dict] = []
        for cond in cc.get("condicoes", []):
            params = {
                "query.cond": cond,
                # somente estudos com RESULTADOS postados na janela
                "filter.advanced": f"AREA[ResultsFirstPostDate]RANGE[{ini},MAX]",
                "pageSize": cc.get("max_por_condicao", 10),
                "format": "json",
            }
            try:
                r = self.http.get(CTGOV, params=params)
                novos = parse_ctgov(r.json(), cond)
                self._conta("ClinicalTrials.gov", len(novos))
                itens.extend(novos)
            except Exception as e:  # noqa: BLE001
                self._erro(f"ClinicalTrials.gov [{cond}]", e)
        return itens


def _recente(item: dict, limite: datetime) -> bool:
    d = parse_data(item.get("publicado_em"))
    return d is None or d >= limite


# ======================================================== PARSERS ============

def parse_rss(conteudo: bytes | str, fonte: str, tipo_fonte: str, idioma: str,
              consulta: str) -> list[dict]:
    """Aceita RSS 2.0 e Atom."""
    raiz = ET.fromstring(conteudo)
    ns = {"atom": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}
    itens = []
    entradas = raiz.findall(".//item") or raiz.findall(".//atom:entry", ns)
    for e in entradas:
        titulo = limpar_html(_txt(e, "title") or _txt(e, "atom:title", ns))
        link = _txt(e, "link")
        if not link:
            le = e.find("atom:link", ns)
            link = le.get("href") if le is not None else ""
        desc = limpar_html(_txt(e, "description") or _txt(e, "atom:summary", ns)
                           or _txt(e, "atom:content", ns))
        pub = (_txt(e, "pubDate") or _txt(e, "dc:date", ns) or _txt(e, "atom:published", ns)
               or _txt(e, "atom:updated", ns))
        upd = _txt(e, "atom:updated", ns)
        fonte_real = fonte
        src = e.find("source")
        url_origem = ""
        if src is not None and src.text:  # Google News informa o veículo original
            fonte_real = src.text.strip()
            url_origem = src.get("url", "")
            # Google News repete o nome do veículo no título: "Título - Veículo"
            sufixo = f" - {fonte_real}"
            if titulo.endswith(sufixo):
                titulo = titulo[: -len(sufixo)]
        if not titulo:
            continue
        itens.append({
            "titulo_original": titulo,
            "url": (link or "").strip(),
            "url_veiculo": url_origem,
            "fonte": fonte_real,
            "agregador": fonte if fonte_real != fonte else None,
            "tipo_fonte": tipo_fonte,
            "idioma": idioma,
            "publicado_em": iso(parse_data(pub)),
            "atualizado_em": iso(parse_data(upd)) if upd and upd != pub else None,
            "resumo_original": desc[:2000] if desc != titulo else "",
            "identificadores": _ids_do_texto(f"{link} {desc}"),
            "tipos_publicacao": [],
            "periodico": None,
            "consulta": consulta,
        })
    return itens


def _txt(e, tag, ns=None) -> str:
    x = e.find(tag, ns) if ns else e.find(tag)
    return (x.text or "").strip() if x is not None and x.text else ""


def _ids_do_texto(texto: str) -> dict:
    ids = {}
    m = re.search(r"\b(10\.\d{4,9}/[^\s\"<>]+)", texto or "")
    if m:
        ids["doi"] = m.group(1).rstrip(".,);").lower()
    m = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", texto or "")
    if m:
        ids["pmid"] = m.group(1)
    m = re.search(r"\b(NCT\d{8})\b", texto or "")
    if m:
        ids["nct"] = m.group(1)
    return ids


def parse_pubmed_xml(xml_txt: str, rotulos: dict[str, str] | None = None) -> list[dict]:
    rotulos = rotulos or {}
    raiz = ET.fromstring(xml_txt)
    itens = []
    for art in raiz.findall(".//PubmedArticle"):
        pmid = (art.findtext(".//MedlineCitation/PMID") or "").strip()
        a = art.find(".//Article")
        if a is None or not pmid:
            continue
        titulo = limpar_html("".join(a.find("ArticleTitle").itertext())
                             if a.find("ArticleTitle") is not None else "")
        partes = []
        for ab in a.findall(".//Abstract/AbstractText"):
            rot = ab.get("Label")
            txt = limpar_html("".join(ab.itertext()))
            partes.append(f"{rot}: {txt}" if rot else txt)
        resumo = "\n".join(partes)
        periodico = (a.findtext("Journal/Title") or a.findtext("Journal/ISOAbbreviation") or "").strip()
        tipos = [t.text.strip() for t in a.findall(".//PublicationTypeList/PublicationType") if t.text]
        doi = None
        for aid in art.findall(".//ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi" and aid.text:
                doi = aid.text.strip().lower()
        if not doi:
            for el in a.findall("ELocationID"):
                if el.get("EIdType") == "doi" and el.text:
                    doi = el.text.strip().lower()
        # data: preferir ArticleDate (eletrônica); senão PubDate; senão entrada no PubMed
        pub = None
        ad = a.find("ArticleDate")
        if ad is not None:
            pub = _data_partes(ad)
        if pub is None:
            pd = a.find("Journal/JournalIssue/PubDate")
            if pd is not None:
                pub = _data_partes(pd) or parse_data(pd.findtext("MedlineDate"))
        entrada = None
        for h in art.findall(".//PubmedData/History/PubMedPubDate"):
            if h.get("PubStatus") in ("entrez", "pubmed"):
                entrada = _data_partes(h)
                break
        revisado = None
        dr = art.find(".//MedlineCitation/DateRevised")
        if dr is not None:
            revisado = _data_partes(dr)
        ids = {"pmid": pmid}
        if doi:
            ids["doi"] = doi
        m = re.search(r"\b(NCT\d{8})\b", resumo)
        if m:
            ids["nct"] = m.group(1)
        itens.append({
            "titulo_original": titulo,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "url_doi": f"https://doi.org/{doi}" if doi else None,
            "fonte": periodico or "PubMed",
            "agregador": "PubMed",
            "tipo_fonte": "periodico",
            "idioma": "en",
            "publicado_em": iso(pub or entrada),
            "indexado_em": iso(entrada),
            "atualizado_em": iso(revisado) if revisado and entrada and revisado > entrada else None,
            "resumo_original": resumo[:6000],
            "identificadores": ids,
            "tipos_publicacao": tipos,
            "periodico": periodico,
            "consulta": rotulos.get(pmid, "PubMed"),
        })
    return itens


_MESES = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _data_partes(el) -> datetime | None:
    try:
        ano = int(el.findtext("Year"))
    except (TypeError, ValueError):
        return None
    mes_txt = (el.findtext("Month") or "1").strip()
    mes = int(mes_txt) if mes_txt.isdigit() else _MESES.get(mes_txt[:3].lower(), 1)
    dia_txt = (el.findtext("Day") or "1").strip()
    dia = int(dia_txt) if dia_txt.isdigit() else 1
    try:
        return datetime(ano, mes, dia, tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_ctgov(dados: dict, condicao: str) -> list[dict]:
    itens = []
    for s in dados.get("studies", []):
        ps = s.get("protocolSection", {})
        ident = ps.get("identificationModule", {})
        status = ps.get("statusModule", {})
        design = ps.get("designModule", {})
        nct = ident.get("nctId")
        if not nct:
            continue
        titulo = ident.get("briefTitle") or ident.get("officialTitle") or nct
        fases = design.get("phases") or []
        resumo = ps.get("descriptionModule", {}).get("briefSummary", "")
        pub = (status.get("resultsFirstPostDateStruct") or {}).get("date")
        upd = (status.get("lastUpdatePostDateStruct") or {}).get("date")
        patroc = (ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor") or {}).get("name", "")
        itens.append({
            "titulo_original": titulo,
            "url": f"https://clinicaltrials.gov/study/{nct}",
            "fonte": "ClinicalTrials.gov",
            "agregador": None,
            "tipo_fonte": "registro_ensaios",
            "idioma": "en",
            "publicado_em": iso(parse_data(pub)),
            "atualizado_em": iso(parse_data(upd)) if upd and upd != pub else None,
            "resumo_original": limpar_html(
                f"Resultados postados. Fase: {', '.join(fases) or 'n/i'}. "
                f"Status: {status.get('overallStatus', 'n/i')}. Patrocinador: {patroc}. {resumo}"
            )[:3000],
            "identificadores": {"nct": nct},
            "tipos_publicacao": [f"Ensaio clínico ({', '.join(fases)})" if fases else "Ensaio clínico"],
            "periodico": None,
            "consulta": f"ClinicalTrials.gov: {condicao}",
        })
    return itens
