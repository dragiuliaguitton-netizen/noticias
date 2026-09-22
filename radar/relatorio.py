"""Relatório semanal consolidado e alertas extraordinários."""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import classificacao as cl
from .email_envio import EmailNaoConfigurado, Mailer
from .historico import Historico
from .util import RAIZ, agora, data_br, iso, log, truncar

PASTA_RELATORIOS = RAIZ / "relatorios"
_env = Environment(loader=FileSystemLoader(RAIZ / "radar" / "templates"),
                   autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
_env.filters["data_br"] = data_br
_env.filters["truncar"] = truncar
_env.globals.update(EMOJI=cl.EMOJI, RELEVANCIAS=cl.RELEVANCIAS, ROTULO_NIVEL=cl.ROTULO_NIVEL)


def _ordenar(itens):
    return sorted(itens, key=lambda i: (-cl.ORDEM_REL[i["relevancia"]], i.get("nivel_fonte", 4),
                                        -i.get("pontuacao", 0)))


def montar_relatorio(hist: Historico, cfg: dict, incluir_ja_reportados: bool = False) -> dict:
    rc = cfg.get("relatorio", {})
    dias = int(cfg.get("janela_relatorio_dias", 7))
    semana = hist.recentes(dias)
    elegiveis = [i for i in semana
                 if (incluir_ja_reportados or not i.get("relatorios"))
                 and i["relevancia"] != "baixa" and not i.get("conteudo_antigo")]

    # 1. O que você precisa saber (5–10)
    fortes = _ordenar([i for i in elegiveis if cl.ORDEM_REL[i["relevancia"]] >= 2])
    destaques = fortes[: rc.get("max_destaques", 10)]
    if len(destaques) < rc.get("min_destaques", 5):
        extras = _ordenar([i for i in elegiveis if i["relevancia"] == "interessante"
                           and i.get("nivel_fonte", 4) <= 2 and i not in destaques])
        destaques += extras[: rc.get("min_destaques", 5) - len(destaques)]

    # 2. Novidades em tratamentos, agrupadas por status
    tratamentos = defaultdict(list)
    for i in _ordenar(elegiveis):
        if i.get("status_terapia") and cl.ORDEM_REL[i["relevancia"]] >= 1:
            tratamentos[i["status_terapia"]].append(i)
    tratamentos = [(s, tratamentos[s][:8]) for s in cl.STATUS_TERAPIA if tratamentos.get(s)]

    # 3. Estudos que valem a leitura (até 5)
    estudos = _ordenar([i for i in elegiveis if i.get("agregador") == "PubMed"
                        and cl.ORDEM_REL[i["relevancia"]] >= 2])[: rc.get("max_estudos", 5)]

    # 4. Alertas de segurança (somente fontes oficiais/científicas)
    alertas = _ordenar([i for i in elegiveis if i.get("alerta_seguranca") and i.get("nivel_fonte", 4) <= 2]
                       + [i for i in elegiveis if i.get("alerta_seguranca") and i.get("nivel_fonte", 4) >= 3
                          and i.get("fonte_primaria_confirmada")])

    # 5. Oportunidades para Instagram (3–5), priorizando as ainda não usadas
    ideias = [i for i in _ordenar(elegiveis) if i.get("ideia_conteudo") and not i.get("ideia_usada_em")]
    ideias.sort(key=lambda i: (i["ideia_conteudo"].get("origem") != "ia", -cl.ORDEM_REL[i["relevancia"]]))
    ideias = ideias[: rc.get("max_ideias_instagram", 5)]

    # 6. Assuntos ganhando atenção (fontes confiáveis, ≥3 aparições)
    confiaveis = Counter()
    for i in semana:
        if i.get("nivel_fonte", 4) <= 3 and i["relevancia"] != "baixa":
            for a in i.get("assuntos", [])[:2]:
                if a != "Dermatologia geral":
                    confiaveis[a] += 1 + len([m for m in i.get("mencoes", []) if m.get("tipo_fonte") != "noticia"])
    em_alta = [(a, n) for a, n in confiaveis.most_common(8) if n >= 3]

    # 7. Muito barulho, pouca evidência: muito divulgado, sem fonte primária
    barulho_assunto = defaultdict(list)
    for i in semana:
        if i.get("nivel_fonte", 4) >= 3 and not i.get("fonte_primaria_confirmada"):
            barulho_assunto[i.get("assunto", "?")].append(i)
    barulho = []
    for assunto, itens in barulho_assunto.items():
        total = sum(1 + len(i.get("mencoes", [])) for i in itens)
        if total >= 3:
            motivos = set()
            for i in itens:
                if i.get("marketing"):
                    motivos.add("tom publicitário/lançamento de produto")
                if i.get("tipo_evidencia") == "estudo experimental (pré-clínico)":
                    motivos.add("resultados apenas pré-clínicos")
            motivos.add("nenhuma fonte primária (estudo, guideline ou comunicado oficial) localizada")
            barulho.append({"assunto": assunto, "total": total, "exemplos": itens[:3],
                            "motivos": sorted(motivos)})
    barulho.sort(key=lambda b: -b["total"])

    # Auditoria da semana
    execucoes = [e for e in hist.execucoes
                 if e.get("tipo") == "diaria" and (e.get("inicio") or "") >= iso(agora() - timedelta(days=dias))]
    erros = Counter(e["fonte"] for ex in execucoes for e in ex.get("erros", []))
    por_fonte = Counter()
    for ex in execucoes:
        por_fonte.update(ex.get("por_fonte", {}))

    return {
        "data": agora(),
        "dias": dias,
        "destaques": destaques,
        "tratamentos": tratamentos,
        "estudos": estudos,
        "alertas": alertas,
        "ideias": ideias,
        "em_alta": em_alta,
        "barulho": barulho[:4],
        "auditoria": {
            "execucoes": len(execucoes),
            "itens_semana": len(semana),
            "elegiveis": len(elegiveis),
            "por_relevancia": Counter(i["relevancia"] for i in semana),
            "por_fonte": por_fonte.most_common(),
            "erros": erros.most_common(),
            "modo_ia": any(i.get("modo_analise") == "ia" for i in semana),
        },
        "todos": {i["id"]: i for i in destaques + estudos + alertas + ideias
                  + [x for _, l in tratamentos for x in l]},
    }


def renderizar(rel: dict) -> tuple[str, str]:
    md = _env.get_template("semanal.md.j2").render(r=rel)
    html = _env.get_template("semanal.html.j2").render(r=rel)
    return md, html


def assunto_semanal(d=None) -> str:
    return f"🧴 Radar Semanal de Dermatologia — {(d or agora()):%d/%m/%Y}"


def gerar_e_enviar_semanal(cfg: dict, hist: Historico | None = None, enviar: bool = True,
                           mailer: Mailer | None = None, incluir_ja_reportados: bool = False) -> dict:
    hist = hist or Historico()
    rel = montar_relatorio(hist, cfg, incluir_ja_reportados)
    md, html = renderizar(rel)
    pasta = Path(os.getenv("RADAR_RELATORIOS") or PASTA_RELATORIOS)
    pasta.mkdir(parents=True, exist_ok=True)
    nome = f"radar-semanal-{agora():%Y-%m-%d}"
    (pasta / f"{nome}.md").write_text(md, encoding="utf-8")
    (pasta / f"{nome}.html").write_text(html, encoding="utf-8")
    log.info("Relatório salvo em %s/%s.{md,html}", pasta, nome)

    enviado, erro = False, None
    if enviar:
        try:
            enviado = (mailer or Mailer()).enviar(
                assunto_semanal(), md, html,
                anexos=[(f"{nome}.md", md.encode(), "text/markdown"),
                        (f"{nome}.html", html.encode(), "text/html")],
                prefixo="semanal")
        except EmailNaoConfigurado as e:
            erro = str(e)
            log.error("%s", e)
        except Exception as e:  # noqa: BLE001
            erro = f"{type(e).__name__}: {e}"
            log.error("Falha no envio do relatório: %s", erro)

    # Só marca como "já reportado" se o e-mail realmente saiu — assim nada se
    # perde se o envio falhar ou se for apenas uma prévia (--sem-envio).
    if enviado:
        hoje = agora().strftime("%Y-%m-%d")
        for i in rel["todos"].values():
            if hoje not in i.setdefault("relatorios", []):
                i["relatorios"].append(hoje)
        for i in rel["ideias"]:
            i["ideia_usada_em"] = hoje
    hist.registrar_execucao({"tipo": "semanal", "inicio": iso(agora()), "enviado": enviado,
                             "erro_envio": erro, "itens": len(rel["todos"]),
                             "arquivo": f"relatorios/{nome}.md"})
    hist.salvar()
    return {"enviado": enviado, "erro": erro, "arquivo_md": str(pasta / f"{nome}.md"),
            "arquivo_html": str(pasta / f"{nome}.html"), "itens": len(rel["todos"])}


# ----------------------------------------------------------- ALERTA -------

def texto_alerta(item: dict) -> tuple[str, str]:
    titulo = item.get("titulo") or item["titulo_original"]
    afetados = {
        "Fotoproteção / protetor solar": "Usuários de protetores solares e profissionais que os prescrevem.",
        "Preenchedores / bioestimuladores": "Pacientes submetidos a procedimentos injetáveis e profissionais que os realizam.",
        "Toxina botulínica": "Pacientes submetidos a aplicação de toxina botulínica e profissionais que a aplicam.",
        "Cosméticos / dermocosméticos": "Consumidores do(s) produto(s) citado(s).",
    }.get(item.get("assunto"), "Pacientes e profissionais envolvidos com: " + ", ".join(item.get("assuntos", [])))
    nao_sabe = item.get("limitacoes") or []
    corpo = "\n".join([
        "🚨 ALERTA DERMATOLOGIA",
        "",
        f"{titulo}",
        "",
        f"1. O que aconteceu: {item.get('resumo') or item.get('titulo_original')}",
        f"2. Quem comunicou: {item.get('fonte')} ({cl.ROTULO_NIVEL[item['nivel_fonte']]})",
        f"3. Quando: publicado em {data_br(item.get('publicado_em'))}; "
        f"detectado pelo radar em {data_br(item.get('encontrado_em'))}",
        f"4. Quem pode ser afetado: {afetados}",
        "5. O que ainda não se sabe: " + ("; ".join(nao_sabe) if nao_sabe else
                                          "detalhes não informados no texto disponível — consultar a fonte original."),
        f"6. Fonte original: {item.get('fonte_primaria_url') or item.get('url')}",
        "",
        f"Tipo de evidência: {item['tipo_evidencia']}"
        + (f" · Status: {item['status_terapia']}" if item.get("status_terapia") else ""),
        "",
        "⚠️ Confira a fonte original antes de usar esta informação profissionalmente.",
        f"ID de rastreio: {item['id']}",
    ])
    return f"🚨 ALERTA DERMATOLOGIA — {truncar(titulo, 90)}", corpo


def enviar_alerta(item: dict, mailer: Mailer | None = None) -> bool:
    assunto, corpo = texto_alerta(item)
    html = _env.get_template("alerta.html.j2").render(corpo=corpo, i=item)
    try:
        return (mailer or Mailer()).enviar(assunto, corpo, html, prefixo="alerta")
    except EmailNaoConfigurado as e:
        log.error("Alerta não enviado: %s", e)
        return False
