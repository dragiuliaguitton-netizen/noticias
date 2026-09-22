"""Execução diária: coletar → validar → deduplicar → classificar → resumir →
ideias de conteúdo → alertas → armazenar."""

from __future__ import annotations

import os
import re
from datetime import timedelta
from pathlib import Path

from . import classificacao as cl
from .fontes import Coletor, eh_dermatologico
from .historico import Historico, gerar_id, similaridade
from .ia import AnalisadorIA
from .util import DADOS, agora, dominio, iso, log, parse_data, truncar

DIARIO = DADOS / "diario"

# nomes de fármacos/tecnologias: se uma notícia e um artigo/comunicado citam o
# mesmo, é forte sinal de que tratam do mesmo acontecimento
_NOMES_ESPECIFICOS = re.compile(r"\b([a-z]{4,}(?:mab|nib|tinib|citinib|kinra|cept|stat|lisib|imod|arof|last))\b", re.I)


def processar_dia(cfg: dict, coletor: Coletor | None = None, historico: Historico | None = None,
                  ia: AnalisadorIA | None = None, mailer=None) -> dict:
    inicio = agora()
    coletor = coletor or Coletor(cfg)
    hist = historico or Historico()
    ia = ia or AnalisadorIA(cfg)
    limiar = float(cfg.get("limiar_similaridade_titulo", 0.82))
    janela = int(cfg.get("janela_dias", 3))

    brutos = coletor.coletar_tudo()
    stats = {"brutos": len(brutos), "novos": 0, "duplicados": 0, "nao_dermatologicos": 0,
             "atualizacoes": 0, "antigos": 0, "analisados_ia": 0, "fontes_primarias_localizadas": 0}
    novos: list[dict] = []

    # ---------------------------------------------- 1. dedup + validação ---
    for bruto in brutos:
        if not (bruto.get("titulo_original") or "").strip():
            continue
        if bruto.get("agregador") != "PubMed" and not eh_dermatologico(
                bruto["titulo_original"] + " " + bruto.get("resumo_original", "")):
            stats["nao_dermatologicos"] += 1
            continue
        bruto["id"] = gerar_id(bruto)
        dup, motivo = hist.encontrar_duplicata(bruto, limiar)
        if dup:
            hist.registrar_mencao(dup, bruto, motivo)
            stats["duplicados"] += 1
            continue
        item = dict(bruto)
        item["encontrado_em"] = iso(agora())
        item["relatorios"] = []
        item["ideia_conteudo"] = None
        item["ideia_usada_em"] = None
        item["alerta_enviado_em"] = None
        item["mencoes"] = []
        rel = hist.relacionado_por_ensaio(item)
        if rel:
            item["atualizacao_de"] = rel
            stats["atualizacoes"] += 1
        pub = parse_data(item.get("publicado_em"))
        if pub and pub < agora() - timedelta(days=janela + 45):
            # já existia antes: voltou a circular ou só foi indexado agora
            item["conteudo_antigo"] = True
            stats["antigos"] += 1
        cl.enriquecer(item, cfg)
        if item.get("conteudo_antigo"):
            item["relevancia"] = min(item["relevancia"], "interessante", key=cl.ORDEM_REL.get)
            item["observacoes_validacao"].append(
                f"Conteúdo antigo (publicado em {pub:%d/%m/%Y}) — não é novidade.")
        hist.adicionar(item)
        novos.append(item)
    stats["novos"] = len(novos)
    log.info("Novos: %d | duplicados: %d | fora do escopo: %d",
             len(novos), stats["duplicados"], stats["nao_dermatologicos"])

    # ------------------------------ 2. corroboração notícia ↔ fonte primária --
    primarios = [i for i in hist.recentes(45) if i.get("nivel_fonte", 4) <= 2]
    for item in novos:
        if item["nivel_fonte"] >= 3:
            _corroborar(item, primarios, hist)
            cl.reclassificar(item)

    # ------------------------------------------------------------- 3. IA ----
    candidatos = sorted(
        (i for i in novos if i["pontuacao"] >= 3 or i["alerta_seguranca"] or i["nivel_fonte"] == 1),
        key=lambda i: (-cl.ORDEM_REL[i["relevancia"]], -i["pontuacao"]))
    dominios_ok = (cfg.get("dominios", {}).get("nivel1", []) + cfg.get("dominios", {}).get("nivel2", []))
    for item in candidatos:
        if not ia.disponivel():
            break
        # notícia com potencial alto e sem fonte primária → tentar localizá-la
        if item["nivel_fonte"] >= 3 and not item["fonte_primaria_confirmada"] and (
                item["alerta_seguranca"] or item.get("status_terapia") in ("aprovado", "em análise")):
            achado = ia.localizar_fonte_primaria(item, dominios_ok)
            if achado and achado.get("url"):
                d = dominio(achado["url"])
                if any(d == x or d.endswith("." + x) for x in dominios_ok):
                    item["fonte_primaria_url"] = achado["url"]
                    item["fonte_primaria_titulo"] = achado.get("titulo")
                    item["fonte_primaria_confirmada"] = bool(achado.get("confirma"))
                    if achado.get("divergencias"):
                        item["divergencias"] = achado["divergencias"]
                    stats["fontes_primarias_localizadas"] += 1
        analise = ia.analisar(item)
        if analise:
            _aplicar_analise(item, analise)
            stats["analisados_ia"] += 1

    # ---------------------------------- 4. ideias de conteúdo sem IA -------
    for item in novos:
        if not item.get("ideia_conteudo") and cl.ORDEM_REL[item["relevancia"]] >= 2:
            item["ideia_conteudo"] = ideia_heuristica(item)

    # ------------------------------------------------------ 5. alertas -----
    alertas = [i for i in novos if deve_alertar(i)]
    stats["alertas"] = len(alertas)
    if alertas and os.getenv("RADAR_ALERTAS", "1") != "0":
        from .relatorio import enviar_alerta
        for item in alertas:
            try:
                if enviar_alerta(item, mailer=mailer):
                    item["alerta_enviado_em"] = iso(agora())
            except Exception as e:  # noqa: BLE001
                coletor._erro("E-mail de alerta", e)

    # ------------------------------------------------- 6. armazenar -------
    resumo = {
        "tipo": "diaria",
        "inicio": iso(inicio),
        "fim": iso(agora()),
        "modo": "ia" if stats["analisados_ia"] else "heuristico",
        "stats": stats,
        "por_fonte": coletor.stats,
        "erros": coletor.erros,
        "relevancia": {k: sum(1 for i in novos if i["relevancia"] == k) for k in cl.ORDEM_REL},
    }
    hist.registrar_execucao(resumo)
    hist.salvar()
    escrever_diario(novos, resumo)
    return resumo


# ======================================================================

def _corroborar(item: dict, primarios: list[dict], hist: Historico | None = None) -> None:
    """Procura, entre fontes oficiais/científicas recentes, o documento que
    sustenta a notícia. Em empate, prefere a fonte oficial (nível 1)."""
    nomes = {n.lower() for n in _NOMES_ESPECIFICOS.findall(item["titulo_original"])}
    melhor, chave = None, (0.0, 0)
    for p in primarios:
        s = similaridade(item["titulo_original"], p.get("titulo_original", ""))
        if nomes and nomes & {n.lower() for n in _NOMES_ESPECIFICOS.findall(
                p.get("titulo_original", "") + " " + (p.get("resumo_original") or "")[:600])}:
            s = max(s, 0.6)
        k = (s, -p.get("nivel_fonte", 4))
        if k > chave:
            melhor, chave = p, k
    if melhor and chave[0] >= 0.5:
        if hist is not None:  # repercussão na imprensa conta para o item primário
            hist.registrar_mencao(melhor["id"], item, "repercussão na imprensa")
        item["fonte_primaria_confirmada"] = True
        item["fonte_primaria_id"] = melhor["id"]
        item["fonte_primaria_url"] = melhor.get("url")
        item["fonte_primaria_titulo"] = melhor.get("titulo") or melhor.get("titulo_original")


def _aplicar_analise(item: dict, a: dict) -> None:
    item["modo_analise"] = "ia"
    item["dermatologico"] = a["dermatologico"]
    item["titulo"] = a["titulo_pt"]
    item["resumo"] = a["o_que_aconteceu"]
    item["por_que_importa"] = a["por_que_importa"]
    item["limitacoes"] = a["limitacoes"] or item.get("limitacoes", [])
    # tipo de evidência: o PubMed (metadado oficial) prevalece quando explícito
    if not item.get("tipos_publicacao") or item["tipo_evidencia"] in (
            "artigo científico (tipo não especificado)", "outro", "notícia"):
        item["tipo_evidencia"] = a["tipo_evidencia"]
    if a["eh_publicidade"]:
        item["marketing"] = True
    if a.get("status_terapia"):
        # nunca "promover" o status além do que a heurística detectou
        ordem = {"experimental": 0, "em estudo": 1, "em análise": 2, "aprovado": 3}
        atual = item.get("status_terapia")
        regulatorio = a["status_terapia"] in ("aprovado", "em análise")
        if atual is None:
            # sem detecção prévia, status regulatório só com fonte confiável
            if not regulatorio or item.get("fonte_primaria_confirmada"):
                item["status_terapia"] = a["status_terapia"]
        elif ordem[a["status_terapia"]] <= ordem[atual]:
            item["status_terapia"] = a["status_terapia"]
    item["relevancia_ia"] = a["relevancia"]
    item["justificativa_ia"] = a["justificativa_relevancia"]
    item["alerta_ia"] = a["alerta_extraordinario"]
    cl.reclassificar(item)
    if a.get("ideia_conteudo") and cl.ORDEM_REL[item["relevancia"]] >= 2:
        item["ideia_conteudo"] = {**a["ideia_conteudo"], "fonte_medica": _citacao(item),
                                  "origem": "ia"}


def _citacao(item: dict) -> str:
    ids = item.get("identificadores") or {}
    extra = f" DOI: {ids['doi']}" if ids.get("doi") else (f" PMID: {ids['pmid']}" if ids.get("pmid") else "")
    return f"{item.get('fonte')} ({item.get('publicado_em', '')[:10]}).{extra} {item.get('url')}"


FORMATO_POR_EVIDENCIA = {
    "guideline": "Carrossel", "consenso": "Carrossel", "comunicado regulatório": "Stories",
    "metanálise": "Carrossel", "revisão sistemática": "Carrossel",
    "ensaio clínico randomizado": "Reel", "ensaio clínico": "Reel",
    "estudo observacional": "Mito x verdade",
}


def ideia_heuristica(item: dict) -> dict:
    """Rascunho SEM IA — sempre marcado para revisão humana."""
    assunto = item.get("assunto", "Dermatologia")
    if item.get("alerta_seguranca"):
        gancho = f"Atenção: o que muda com o novo comunicado sobre {assunto.lower()}?"
        formato = "Stories"
    elif item.get("status_terapia") == "aprovado":
        gancho = f"Nova opção aprovada em {assunto.lower()}: o que isso significa na prática?"
        formato = "Carrossel"
    elif item["tipo_evidencia"] == "estudo observacional":
        gancho = f"Um estudo relacionou algo a {assunto.lower()} — mas relação não é causa."
        formato = "Mito x verdade"
    else:
        gancho = f"O que a ciência trouxe de novo sobre {assunto.lower()}?"
        formato = FORMATO_POR_EVIDENCIA.get(item["tipo_evidencia"], "Post único")
    return {
        "tema": f"{assunto}: {truncar(item.get('titulo') or item['titulo_original'], 110)}",
        "formato": formato,
        "gancho": gancho,
        "mensagem_principal": truncar(item.get("resumo") or "", 280),
        "o_que_o_publico_precisa_entender": (
            "Contexto do tipo de evidência: " + item["tipo_evidencia"]
            + (f"; status: {item['status_terapia']}" if item.get("status_terapia") else "")
            + ". Não substitui avaliação individual com dermatologista."),
        "cta": "Salve para consultar depois e leve suas dúvidas à consulta.",
        "fonte_medica": _citacao(item),
        "origem": "rascunho automático (sem IA) — revisar antes de usar",
    }


def deve_alertar(item: dict) -> bool:
    if item.get("alerta_enviado_em") or item.get("conteudo_antigo"):
        return False
    if item["relevancia"] != "muito_relevante":
        return False
    if item.get("alerta_ia") is not None:
        return bool(item["alerta_ia"]) and item["nivel_fonte"] <= 2
    # sem IA: somente segurança/aprovação vindos de fonte oficial
    return item["nivel_fonte"] == 1 and (item["alerta_seguranca"] or item.get("status_terapia") == "aprovado")


# ----------------------------------------------------------------------

def escrever_diario(novos: list[dict], resumo: dict, pasta: Path | None = None) -> Path:
    """Registro diário (formato da seção 12) — apenas itens 🔴/🟠/🟡."""
    pasta = pasta or Path(os.getenv("RADAR_DIARIO") or DIARIO)
    pasta.mkdir(parents=True, exist_ok=True)
    dia = agora().strftime("%Y-%m-%d")
    arq = pasta / f"{dia}.md"
    rel = [i for i in novos if i["relevancia"] != "baixa"]
    rel.sort(key=lambda i: (-cl.ORDEM_REL[i["relevancia"]], -i["pontuacao"]))
    linhas = [f"# Registro diário — {agora():%d/%m/%Y}", "",
              f"Itens brutos: {resumo['stats']['brutos']} · novos: {resumo['stats']['novos']} · "
              f"duplicados descartados: {resumo['stats']['duplicados']} · "
              f"registrados aqui (🔴/🟠/🟡): {len(rel)} · modo: {resumo['modo']}", ""]
    if resumo["erros"]:
        linhas += ["<details><summary>Fontes com falha nesta execução "
                   f"({len(resumo['erros'])})</summary>", ""]
        linhas += [f"- {e['fonte']}: {e['erro']}" for e in resumo["erros"]]
        linhas += ["", "</details>", ""]
    for i in rel:
        linhas += bloco_item_md(i)
    if arq.exists():  # mais de uma execução no mesmo dia: acrescenta
        anterior = arq.read_text(encoding="utf-8")
        linhas = [anterior.rstrip(), "", f"## Execução adicional ({agora():%H:%M} UTC)", ""] + linhas[2:]
    arq.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    return arq


def bloco_item_md(i: dict) -> list[str]:
    ids = i.get("identificadores") or {}
    ident = " · ".join(f"{k.upper()}: {v}" for k, v in ids.items())
    em_ingles = i.get("modo_analise") != "ia" and i.get("idioma") == "en"
    linhas = [
        f"### {cl.EMOJI[i['relevancia']]} {i.get('titulo') or i['titulo_original']}",
        "",
    ]
    if i.get("atualizacao_de"):
        linhas.append("> **Atualização de assunto já monitorado.**")
        linhas.append("")
    linhas += [
        f"**O que aconteceu?** {i.get('resumo') or '—'}"
        + (" _(trecho original em inglês — análise por IA desativada)_" if em_ingles and i.get("resumo") else ""),
        "",
    ]
    if i.get("por_que_importa"):
        linhas += [f"**Por que isso importa?** {i['por_que_importa']}", ""]
    linhas += [
        f"**Evidência:** {i['tipo_evidencia']}"
        + (f" · **Status da terapia:** {i['status_terapia']}" if i.get("status_terapia") else ""),
        "",
        "**Limitações:** " + ("; ".join(i.get("limitacoes") or []) or "não identificadas no texto disponível"),
        "",
        f"**Fonte principal:** {i.get('fonte')} ({cl.ROTULO_NIVEL[i['nivel_fonte']]})"
        + (f" — via {i['agregador']}" if i.get("agregador") else ""),
        "",
        f"**Data:** publicado {i.get('publicado_em', '')[:10] or 'n/i'}"
        + (f" · atualizado {i['atualizado_em'][:10]}" if i.get("atualizado_em") else "")
        + f" · encontrado {i['encontrado_em'][:10]}",
        "",
        f"**Link:** {i.get('url')}" + (f" · {i['url_doi']}" if i.get("url_doi") else ""),
        "",
    ]
    if ident:
        linhas += [f"**Identificadores:** {ident}", ""]
    if i.get("fonte_primaria_url") and i["nivel_fonte"] >= 3:
        linhas += [f"**Fonte primária localizada:** {i.get('fonte_primaria_titulo') or ''} {i['fonte_primaria_url']}", ""]
    if i.get("divergencias"):
        linhas += [f"**As fontes divergem sobre:** {i['divergencias']}", ""]
    for o in i.get("observacoes_validacao") or []:
        linhas.append(f"> ⚠️ {o}")
    linhas += [f"**Relevância:** {cl.RELEVANCIAS[i['relevancia']]}", "", f"<sub>ID: {i['id']}</sub>", "", "---", ""]
    return linhas
