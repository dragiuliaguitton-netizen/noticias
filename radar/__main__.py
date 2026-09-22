"""Linha de comando do radar.

    python -m radar diario                 # busca diária (coleta → histórico → alertas)
    python -m radar semanal                # gera e envia o relatório semanal
    python -m radar semanal --sem-envio    # só gera (prévia), não marca itens
    python -m radar testar-email           # envia um e-mail de teste
    python -m radar verificar-fontes       # testa o acesso a cada fonte
    python -m radar status                 # resumo do histórico e da configuração
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

from . import classificacao as cl
from .email_envio import EmailNaoConfigurado, Mailer, destinatarios, provedor
from .fontes import Coletor
from .historico import Historico
from .ia import AnalisadorIA
from .pipeline import processar_dia
from .relatorio import gerar_e_enviar_semanal
from .util import agora, carregar_config, configurar_log, log


def cmd_diario(args, cfg) -> int:
    if args.janela:
        cfg["janela_dias"] = args.janela  # ex.: 30 na primeira execução (busca inicial ampla)
    r = processar_dia(cfg)
    print(json.dumps({k: r[k] for k in ("modo", "stats", "relevancia")}, ensure_ascii=False, indent=1))
    if r["erros"]:
        print(f"\n{len(r['erros'])} fonte(s) com falha (registradas; demais fontes processadas):")
        for e in r["erros"][:30]:
            print(f"  - {e['fonte']}: {e['erro'][:140]}")
    # só falha se NENHUMA fonte respondeu (sinal de problema de rede geral)
    return 2 if r["stats"]["brutos"] == 0 and r["erros"] else 0


def cmd_semanal(args, cfg) -> int:
    r = gerar_e_enviar_semanal(cfg, enviar=not args.sem_envio,
                               incluir_ja_reportados=args.incluir_reportados)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    if args.exigir_envio and not r["enviado"]:
        return 3
    return 0


def cmd_testar_email(args, cfg) -> int:
    corpo = (f"Teste do Radar de Dermatologia em {agora():%d/%m/%Y %H:%M} UTC.\n"
             f"Provedor: {provedor()} · Destinatários: {', '.join(destinatarios())}\n"
             "Se você recebeu esta mensagem, o envio automático está funcionando.")
    try:
        Mailer().enviar("🧴 Radar de Dermatologia — teste de envio", corpo, prefixo="teste")
        print("OK: e-mail de teste enviado.")
        return 0
    except EmailNaoConfigurado as e:
        print(f"NÃO ENVIADO: {e}")
        return 3
    except Exception as e:  # noqa: BLE001
        print(f"FALHA NO ENVIO: {type(e).__name__}: {e}")
        return 4


def cmd_verificar(args, cfg) -> int:
    cfg = dict(cfg)
    cfg["janela_dias"] = 7
    col = Coletor(cfg)
    testes = {
        "PubMed": lambda: col.http.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi",
                                       params={"db": "pubmed", "retmode": "json"}),
        "Google News": lambda: col.http.get("https://news.google.com/rss/search?q=dermatology&hl=en-US&gl=US&ceid=US:en"),
        "ClinicalTrials.gov": lambda: col.http.get("https://clinicaltrials.gov/api/v2/version"),
    }
    for f in cfg.get("feeds", []):
        testes[f["nome"]] = (lambda u=f["url"]: col.http.get(u))
    ok = 0
    col.http.tentativas = 1
    for nome, t in testes.items():
        try:
            r = t()
            print(f"  ✔ {nome}: HTTP {r.status_code} ({len(r.content)} bytes)")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ✘ {nome}: {type(e).__name__}: {str(e)[:120]}")
    print(f"\n{ok}/{len(testes)} fontes acessíveis. IA: "
          f"{'ativa (' + AnalisadorIA(cfg).modelo + ')' if AnalisadorIA(cfg).ativo else 'desativada'} · "
          f"E-mail: {provedor() or 'NÃO configurado'} → {', '.join(destinatarios()) or 'sem destinatário'}")
    return 0 if ok else 2


def cmd_status(args, cfg) -> int:
    h = Historico()
    rel = Counter(i["relevancia"] for i in h.itens.values())
    print(f"Histórico: {len(h.itens)} itens · {len(h.execucoes)} execuções registradas")
    print("  " + " · ".join(f"{cl.EMOJI[k]} {rel.get(k, 0)}" for k in cl.ORDEM_REL))
    print(f"  Já reportados: {sum(1 for i in h.itens.values() if i.get('relatorios'))} · "
          f"com ideia de conteúdo: {sum(1 for i in h.itens.values() if i.get('ideia_conteudo'))} · "
          f"ideias já usadas: {sum(1 for i in h.itens.values() if i.get('ideia_usada_em'))} · "
          f"alertas enviados: {sum(1 for i in h.itens.values() if i.get('alerta_enviado_em'))}")
    if h.execucoes:
        u = h.execucoes[-1]
        print(f"  Última execução: {u.get('tipo')} em {u.get('inicio')}")
    print(f"Consultas: PubMed {len(cfg['pubmed']['periodicos']) + len(cfg['pubmed']['periodicos_gerais']) + len(cfg['pubmed']['consultas_tematicas'])}"
          f" · notícias PT {len(cfg['noticias']['consultas'].get('pt', []))} / EN {len(cfg['noticias']['consultas'].get('en', []))}"
          f" · feeds {len(cfg.get('feeds', []))} · ClinicalTrials {len(cfg['clinicaltrials']['condicoes'])} condições")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="radar", description="Radar Autônomo de Dermatologia")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--config", help="caminho alternativo do radar.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("diario", help="executa a busca diária")
    d.add_argument("--janela", type=int, help="dias para trás (padrão: janela_dias do YAML)")
    s = sub.add_parser("semanal", help="gera e envia o relatório semanal")
    s.add_argument("--sem-envio", action="store_true", help="só gera o arquivo (prévia)")
    s.add_argument("--exigir-envio", action="store_true", help="código de saída ≠ 0 se o e-mail não sair")
    s.add_argument("--incluir-reportados", action="store_true", help="inclui itens de relatórios anteriores")
    sub.add_parser("testar-email", help="envia e-mail de teste")
    sub.add_parser("verificar-fontes", help="testa acesso às fontes")
    sub.add_parser("status", help="resumo do histórico")
    args = p.parse_args(argv)
    configurar_log(args.verbose)
    cfg = carregar_config(args.config)
    cmds = {"diario": cmd_diario, "semanal": cmd_semanal, "testar-email": cmd_testar_email,
            "verificar-fontes": cmd_verificar, "status": cmd_status}
    try:
        return cmds[args.cmd](args, cfg)
    except KeyboardInterrupt:
        return 130
    except Exception:
        log.exception("Erro inesperado")
        return 1


if __name__ == "__main__":
    sys.exit(main())
