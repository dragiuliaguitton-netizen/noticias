"""Validação de fontes, tipagem de evidência e classificação de relevância.

Este módulo é 100% determinístico (sem IA). Ele:
  1. atribui o NÍVEL da fonte (oficial > periódico > imprensa médica > imprensa geral);
  2. identifica o TIPO DE EVIDÊNCIA (guideline, metanálise, ensaio, observacional…);
  3. identifica o STATUS de terapias (aprovado → em análise → em estudo → experimental);
  4. identifica o ASSUNTO dermatológico;
  5. calcula a RELEVÂNCIA (🔴/🟠/🟡/⚪) com regras explícitas e auditáveis;
  6. aplica TETOS de segurança: notícia sem fonte primária nunca vira 🔴,
     marketing nunca é tratado como evidência, pré-clínico nunca é conduta.

Quando há ANTHROPIC_API_KEY, a IA refina resumo/relevância (radar/ia.py), mas
os tetos definidos aqui continuam valendo.
"""

from __future__ import annotations

import re

from .util import dominio, truncar

RELEVANCIAS = {
    "muito_relevante": "🔴 MUITO RELEVANTE",
    "relevante": "🟠 RELEVANTE",
    "interessante": "🟡 INTERESSANTE",
    "baixa": "⚪ BAIXA RELEVÂNCIA",
}
ORDEM_REL = {"muito_relevante": 3, "relevante": 2, "interessante": 1, "baixa": 0}
EMOJI = {"muito_relevante": "🔴", "relevante": "🟠", "interessante": "🟡", "baixa": "⚪"}

STATUS_TERAPIA = ("aprovado", "em análise", "em estudo", "experimental")

# ------------------------------------------------------------- ASSUNTOS ----
ASSUNTOS: list[tuple[str, str]] = [
    ("Melanoma", r"melanom"),
    ("Carcinoma basocelular", r"basal cell|basocelular|\bbcc\b"),
    ("Carcinoma espinocelular", r"squamous cell carcinoma|espinocelular|\bcscc\b"),
    ("Câncer de pele (geral)", r"skin cancer|c[aâ]ncer de pele|keratinocyte carcinoma|nonmelanoma|n[aã]o melanoma|merkel"),
    ("Dermatoscopia", r"dermoscop|dermatoscop"),
    ("Inteligência artificial", r"artificial intelligence|intelig[eê]ncia artificial|deep learning|machine learning|\bai\b|\bia\b|neural network|large language"),
    ("Fotoproteção / protetor solar", r"sunscreen|photoprotect|fotoprote|protetor solar|filtro solar|\bspf\b|\bfps\b|ultraviolet|\buv\b"),
    ("Acne", r"\bacne"),
    ("Rosácea", r"rosacea|ros[aá]cea"),
    ("Dermatite atópica", r"atopic dermatitis|dermatite at[oó]pica|\beczema"),
    ("Dermatite seborreica", r"seborr?h?eic|seborreica"),
    ("Psoríase", r"psoria"),
    ("Urticária", r"urticari"),
    ("Hidradenite supurativa", r"hidradenit"),
    ("Vitiligo", r"vitiligo"),
    ("Alopecias", r"alopeci|hair loss|queda de cabelo|calv[ií]cie|minoxidil|finasterid|dutasterid"),
    ("Couro cabeludo", r"scalp|couro cabeludo|dandruff|caspa"),
    ("Unhas", r"\bnail|onych|\bunha"),
    ("Hanseníase", r"leprosy|hansen"),
    ("Infecções cutâneas", r"tinea|dermatophyt|fung|scabies|escabiose|impetig|cellulitis|celulite infecciosa|herpes|mpox|monkeypox|wart|verruga|syphilis|s[ií]filis|leishmani"),
    ("Doenças bolhosas", r"pemphig|penfig|bullous|bolhos|epidermolysis"),
    ("Doenças autoimunes", r"lupus|dermatomyositis|dermatomiosite|scleroderma|esclerodermia|morphea|morfeia"),
    ("Doenças genéticas / raras", r"genodermat|ichthyos|ictiose|neurofibromatos|rare disease|doen[cç]a rara|epidermolysis"),
    ("Hiperpigmentação / melasma", r"melasma|hyperpigment|hiperpigment|mancha|dark spot|tranexamic|tranex[aâ]mico"),
    ("Toxina botulínica", r"botulinum|botul[ií]nica|botox|\bneurotoxin"),
    ("Preenchedores / bioestimuladores", r"filler|preench|hyaluronic acid|[aá]cido hialur[oô]nico|poly-?l-?lactic|polilat|calcium hydroxylapatite|hidroxiapatita|bioestimul|skinbooster"),
    ("Lasers e tecnologias", r"laser|intense pulsed light|luz intensa pulsada|\bipl\b|radiofrequen|ultrasound|ultrassom|hifu|microneedl|microagulh|peeling|chemical peel"),
    ("Complicações de procedimentos", r"complication|complica[cç]|vascular occlusion|oclus[aã]o vascular|necrosis|necrose|blindness|cegueira|adverse event"),
    ("Cosméticos / dermocosméticos", r"cosmetic|cosm[eé]tic|dermocosm|retino|retinol|tretino|azelaic|azelaico|vitamin c|vitamina c|ascorbic|niacinamid|moisturi|hidratant|skin barrier|barreira cut"),
    ("Dermatologia pediátrica", r"pediatric|paediatric|child|infant|crian[cç]|pedi[aá]tric|neonat"),
    ("Dermatologia na gestação", r"pregnan|gesta[cç]|gestante|gr[aá]vida|lactation|lacta[cç]"),
    ("Dermatologia geriátrica", r"elderly|older adult|geriatric|idos|geri[aá]tric"),
]
_ASSUNTOS_RE = [(n, re.compile(p, re.I)) for n, p in ASSUNTOS]


def identificar_assuntos(texto: str) -> list[str]:
    return [n for n, r in _ASSUNTOS_RE if r.search(texto or "")] or ["Dermatologia geral"]


# ------------------------------------------------------ NÍVEL DA FONTE -----

def nivel_fonte(item: dict, cfg: dict) -> int:
    """1 = oficial/primária; 2 = científica; 3 = imprensa médica; 4 = geral."""
    if item.get("tipo_fonte") in ("regulatorio", "sociedade", "registro_ensaios"):
        return 1
    if item.get("agregador") == "PubMed" or item.get("tipo_fonte") == "periodico":
        return 2
    doms = cfg.get("dominios", {})
    for url in (item.get("url_veiculo"), item.get("url")):
        d = dominio(url or "")
        if not d or d == "news.google.com":
            continue
        for nivel, chave in ((1, "nivel1"), (2, "nivel2"), (3, "nivel3")):
            if any(d == x or d.endswith("." + x) for x in doms.get(chave, [])):
                return nivel
    return 4


ROTULO_NIVEL = {1: "fonte oficial/primária", 2: "fonte científica (periódico/base)",
                3: "imprensa médica especializada", 4: "imprensa geral"}


# ------------------------------------------------- TIPO DE EVIDÊNCIA -------

def tipo_evidencia(item: dict) -> str:
    tipos = " | ".join(item.get("tipos_publicacao") or []).lower()
    txt = f"{item.get('titulo_original', '')} {item.get('resumo_original', '')[:1500]}".lower()
    if item.get("tipo_fonte") == "regulatorio":
        return "comunicado regulatório"
    if item.get("tipo_fonte") == "registro_ensaios":
        return "ensaio clínico (registro com resultados)"
    if item.get("tipo_fonte") == "noticia":
        return "notícia"  # manchete não é evidência, mesmo que cite um ensaio
    if "retracted publication" in tipos or "retraction of publication" in tipos:
        return "retratação"
    if "erratum" in tipos or "published erratum" in tipos:
        return "errata"
    if "practice guideline" in tipos or "guideline" in tipos or re.search(r"\bguideline|diretriz", txt[:300]):
        return "guideline"
    if "consensus" in tipos or re.search(r"\bconsensus|consenso|delphi", txt[:300]):
        return "consenso"
    if "meta-analysis" in tipos or re.search(r"meta-?analys|metan[aá]lise", txt[:300]):
        return "metanálise"
    if "systematic review" in tipos or "systematic review" in txt[:300]:
        return "revisão sistemática"
    if "randomized controlled trial" in tipos or re.search(r"randomi[sz]ed|phase (3|iii)|fase 3", txt[:600]):
        return "ensaio clínico randomizado"
    if "clinical trial" in tipos or re.search(r"\btrial\b|ensaio cl[ií]nico|phase (2|ii)\b", txt[:600]):
        return "ensaio clínico"
    if re.search(r"\b(mice|murine|mouse|rats?|in vitro|zebrafish|organoid|cell line|pr[eé]-?cl[ií]nic|preclinical)\b", txt):
        return "estudo experimental (pré-clínico)"
    if "review" in tipos:
        return "revisão narrativa"
    if "case reports" in tipos or re.search(r"\bcase report|relato de caso", txt[:300]):
        return "relato de caso"
    if "letter" in tipos or "comment" in tipos or "editorial" in tipos:
        return "carta/editorial (opinião)"
    if "observational study" in tipos or re.search(
            r"cohort|coorte|case-control|caso-controle|cross-sectional|transversal|retrospective|registry|population-based", txt):
        return "estudo observacional"
    if item.get("agregador") == "PubMed":
        return "artigo científico (tipo não especificado)"
    return "outro"


PESO_EVIDENCIA = {
    "guideline": 4, "consenso": 3, "comunicado regulatório": 4, "comunicado oficial": 4, "metanálise": 3,
    "revisão sistemática": 3, "ensaio clínico randomizado": 3, "ensaio clínico": 2,
    "ensaio clínico (registro com resultados)": 2, "estudo observacional": 1,
    "revisão narrativa": 1, "retratação": 3, "estudo experimental (pré-clínico)": 0,
    "relato de caso": 0, "carta/editorial (opinião)": 0, "errata": 0, "notícia": 0,
    "artigo científico (tipo não especificado)": 1, "outro": 0,
}

# ---------------------------------------------------- STATUS DA TERAPIA ----

_APROVADO = re.compile(
    r"\b(fda approv\w*|approved by the fda|approves|approval of|aprova\w*|aprovad\w*|"
    r"marketing authori[sz]ation|granted approval|registro concedido|european commission approv\w*)\b", re.I)
_ANALISE = re.compile(
    r"\b(accepted for review|priority review|under review|chmp (positive )?opinion|"
    r"submitted (a |an )?(new drug application|biologics license|nda|bla|snda|sbla)|"
    r"pdufa|filing|em an[aá]lise|pedido de registro|submiss[aã]o)\b", re.I)
_ESTUDO = re.compile(r"\b(phase (1|2|3|i|ii|iii)|fase (1|2|3)|clinical trial|ensaio cl[ií]nico|randomi[sz]ed|trial)\b", re.I)
_EXPERIMENTAL = re.compile(
    r"\b(mice|murine|mouse|in vitro|preclinical|pr[eé]-?cl[ií]nic|animal model|modelo animal|organoid|zebrafish)\b", re.I)
_TERAPIA = re.compile(
    r"\b(treatment|therapy|drug|mab\b|\w+mab|\w+tinib|\w+citinib|inhibitor|cream|ointment|"
    r"topical|biologic|tratamento|terapia|medicamento|f[aá]rmaco|pomada|creme|t[oó]pico|biol[oó]gico|vaccine|vacina)\b", re.I)


def status_terapia(item: dict, evidencia: str) -> str | None:
    txt = f"{item.get('titulo_original', '')} {item.get('resumo_original', '')[:800]}"
    if not _TERAPIA.search(txt):
        return None
    if evidencia == "estudo experimental (pré-clínico)" or _EXPERIMENTAL.search(txt):
        return "experimental"
    titulo = item.get("titulo_original", "")
    if _ANALISE.search(titulo) or (item.get("tipo_fonte") != "periodico" and _ANALISE.search(txt)):
        return "em análise"
    if _APROVADO.search(titulo) or (item.get("tipo_fonte") == "regulatorio" and _APROVADO.search(txt)):
        return "aprovado"
    if evidencia.startswith("ensaio") or _ESTUDO.search(txt):
        return "em estudo"
    return None


# -------------------------------------------------------- SEGURANÇA --------

_SEGURANCA = re.compile(
    r"\b(recall\w*|recolhiment\w*|recolhe\w*|safety (communication|alert|warning)|boxed warning|"
    r"black box|alerta\w*|warning letter|withdraw\w*|suspens\w*|suspend\w*|proib\w*|banned|"
    r"interdi\w*|contaminat\w*|contamina\w*|falsifica\w*|counterfeit|irregular\w*|farmacovigil\w*|"
    r"pharmacovigil\w*|adverse event report\w*|risk of|risco de|benzene|benzeno|dear healthcare)\b", re.I)

_MARKETING = re.compile(
    r"\b(launch\w*|lan[cç]a\w*|lan[cç]amento|new product|novo produto|now available|"
    r"announces|anuncia|brand|marca|collection|cole[cç][aã]o|promo\w*|desconto|discount|"
    r"best (sunscreen|serum|moisturizer)|melhores? (protetor|s[eé]rum|hidratante)|review of the)\b", re.I)


def eh_alerta_seguranca(item: dict) -> bool:
    txt = f"{item.get('titulo_original', '')} {item.get('resumo_original', '')[:500]}"
    return bool(_SEGURANCA.search(txt))


def parece_marketing(item: dict) -> bool:
    if item.get("agregador") == "PubMed" or item.get("tipo_fonte") in ("regulatorio", "registro_ensaios"):
        return False
    return bool(_MARKETING.search(item.get("titulo_original", "")))


# ------------------------------------------------------- RELEVÂNCIA --------

_PERIODICOS_TOP = re.compile(
    r"new england|n engl j med|lancet|jama|bmj|nature|br j dermatol|british journal of dermatology|"
    r"j am acad dermatol|journal of the american academy of dermatology|j invest dermatol|"
    r"journal of investigative dermatology|j eur acad dermatol|journal of the european academy", re.I)


def pontuar(item: dict) -> tuple[int, list[str]]:
    """Pontuação transparente. Devolve (pontos, justificativas)."""
    pts, porque = 0, []
    ev = item["tipo_evidencia"]
    pe = PESO_EVIDENCIA.get(ev, 0)
    if pe:
        pts += pe
        porque.append(f"evidência: {ev} (+{pe})")
    nv = item["nivel_fonte"]
    bonus_nivel = {1: 3, 2: 1, 3: 0, 4: -1}[nv]
    pts += bonus_nivel
    porque.append(f"fonte: {ROTULO_NIVEL[nv]} ({bonus_nivel:+d})")
    if _PERIODICOS_TOP.search(item.get("periodico") or item.get("fonte") or ""):
        pts += 1
        porque.append("periódico de alto impacto (+1)")
    st = item.get("status_terapia")
    if st == "aprovado":
        pts += 3
        porque.append("aprovação regulatória (+3)")
    elif st == "em análise":
        pts += 1
        porque.append("terapia em análise regulatória (+1)")
    if item.get("alerta_seguranca"):
        pts += 3 if nv == 1 else 1
        porque.append("tema de segurança")
    if item.get("marketing"):
        pts -= 3
        porque.append("tom publicitário (−3)")
    if ev in ("relato de caso", "carta/editorial (opinião)", "errata"):
        pts -= 1
    if len(item.get("mencoes") or []) >= 2:
        pts += 1
        porque.append("repercussão em várias fontes (+1)")
    return pts, porque


def nivel_por_pontos(pts: int) -> str:
    if pts >= 8:
        return "muito_relevante"
    if pts >= 5:
        return "relevante"
    if pts >= 3:
        return "interessante"
    return "baixa"


def aplicar_tetos(item: dict, relevancia: str) -> tuple[str, list[str]]:
    """Regras de segurança que NENHUMA etapa (nem a IA) pode ultrapassar."""
    obs = []
    teto = "muito_relevante"
    if item["nivel_fonte"] >= 3 and not item.get("fonte_primaria_confirmada"):
        teto = "interessante"
        obs.append("Notícia sem fonte primária confirmada: relevância limitada a 🟡.")
    if item.get("marketing"):
        teto = min(teto, "interessante", key=ORDEM_REL.get)
        obs.append("Conteúdo com características de publicidade — não é evidência científica.")
    if item["tipo_evidencia"] == "estudo experimental (pré-clínico)":
        teto = min(teto, "relevante", key=ORDEM_REL.get)
        obs.append("Resultado pré-clínico: não representa tratamento estabelecido.")
    if item["tipo_evidencia"] in ("relato de caso", "carta/editorial (opinião)", "errata"):
        teto = min(teto, "interessante", key=ORDEM_REL.get)
    if not item.get("dermatologico", True):
        teto = "baixa"
        obs.append("Sem relação dermatológica direta.")
    if ORDEM_REL[relevancia] > ORDEM_REL[teto]:
        return teto, obs
    return relevancia, obs


# ------------------------------------------------ RESUMO HEURÍSTICO --------

def resumo_heuristico(item: dict) -> str:
    """Sem IA: usa as CONCLUSÕES do próprio resumo (texto dos autores), nunca
    inventa. Em inglês quando a fonte é em inglês — sinalizado no relatório."""
    txt = item.get("resumo_original") or ""
    m = re.search(r"(?:^|\n)(CONCLUSIONS?|CONCLUSION AND RELEVANCE|CONCLUSIONS AND RELEVANCE|"
                  r"INTERPRETATION|CONCLUS[AÃ]O|CONCLUS[OÕ]ES)\s*:\s*(.+)", txt, re.I)
    if m:
        return truncar(m.group(2).strip(), 600)
    frases = re.split(r"(?<=[.!?])\s+", txt.replace("\n", " "))
    return truncar(" ".join(frases[:3]).strip(), 500)


def limitacoes_heuristicas(item: dict) -> list[str]:
    lim = []
    ev = item["tipo_evidencia"]
    txt = item.get("resumo_original") or ""
    if ev == "estudo observacional":
        lim.append("Estudo observacional: mostra associação, não prova causalidade.")
    if ev == "estudo experimental (pré-clínico)":
        lim.append("Estudo em laboratório/animais: resultados podem não se repetir em humanos.")
    if ev in ("relato de caso", "carta/editorial (opinião)"):
        lim.append("Baixo nível de evidência (caso isolado/opinião).")
    if ev == "revisão narrativa":
        lim.append("Revisão narrativa: seleção de estudos não sistemática.")
    m = re.search(r"\b(\d{1,3}(?:[ ,.]\d{3})+|\d+)\s+(patients|participants|subjects|adults|children|pacientes)\b", txt, re.I)
    if m:
        n = int(re.sub(r"\D", "", m.group(1)))
        if n < 60:
            lim.append(f"Amostra pequena (n≈{n}).")
    if re.search(r"\b(funded|sponsored|financiad)\w* by\b", txt, re.I) and item.get("tipo_evidencia", "").startswith("ensaio"):
        lim.append("Ensaio financiado pela indústria — verificar conflitos de interesse.")
    if item.get("nivel_fonte", 4) >= 3:
        lim.append("Informação de imprensa — conferir na fonte original antes de usar profissionalmente.")
    if item.get("agregador") == "PubMed" and not txt:
        lim.append("Resumo não disponível no PubMed — leitura do texto completo necessária.")
    return lim


# ------------------------------------------------------- ENRIQUECER --------

def enriquecer(item: dict, cfg: dict) -> dict:
    """Aplica toda a classificação determinística ao item (in place)."""
    texto = f"{item.get('titulo_original', '')} {item.get('resumo_original', '')[:1500]}"
    item["nivel_fonte"] = nivel_fonte(item, cfg)
    item["tipo_evidencia"] = tipo_evidencia(item)
    if item["tipo_evidencia"] == "notícia" and item["nivel_fonte"] == 1:
        # notícia cujo veículo É o órgão oficial/sociedade (ex.: gov.br/anvisa, sbd.org.br)
        item["tipo_evidencia"] = "comunicado oficial"
    item["status_terapia"] = status_terapia(item, item["tipo_evidencia"])
    item["assuntos"] = identificar_assuntos(texto)
    item["assunto"] = item["assuntos"][0]
    item["alerta_seguranca"] = eh_alerta_seguranca(item)
    item["marketing"] = parece_marketing(item)
    item.setdefault("dermatologico", True)
    item["fonte_primaria_confirmada"] = item["nivel_fonte"] <= 2
    pts, porque = pontuar(item)
    rel, obs = aplicar_tetos(item, nivel_por_pontos(pts))
    item["pontuacao"] = pts
    item["justificativa_relevancia"] = porque
    item["relevancia"] = rel
    item["observacoes_validacao"] = obs
    item.setdefault("resumo", resumo_heuristico(item))
    item.setdefault("titulo", item.get("titulo_original"))
    item.setdefault("limitacoes", limitacoes_heuristicas(item))
    item.setdefault("modo_analise", "heuristico")
    return item


def reclassificar(item: dict) -> None:
    """Recalcula relevância após mudanças (ex.: corroboração encontrada)."""
    pts, porque = pontuar(item)
    base = item.get("relevancia_ia") or nivel_por_pontos(pts)
    rel, obs = aplicar_tetos(item, base)
    item["pontuacao"], item["justificativa_relevancia"] = pts, porque
    item["relevancia"], item["observacoes_validacao"] = rel, obs
