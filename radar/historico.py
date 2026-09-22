"""Histórico persistente + deduplicação.

O histórico é um arquivo JSON versionado no próprio repositório
(`data/historico.json`), o que dá rastreabilidade total: cada execução gera
um commit, então é possível auditar o que foi encontrado, quando e de onde.

Cada item guarda:
  - o que foi encontrado (título, resumo, link, identificadores DOI/PMID/NCT)
  - quando foi encontrado (encontrado_em) e publicado/atualizado
  - qual fonte publicou (fonte, tipo_fonte, nivel_fonte, agregador)
  - a qual assunto pertence (assunto)
  - se já entrou em relatório (relatorios: lista de datas)
  - se já virou ideia de conteúdo (ideia_conteudo / ideia_usada_em)
  - se já disparou alerta extraordinário (alerta_enviado_em)
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import timedelta
from difflib import SequenceMatcher
from pathlib import Path

from .util import DADOS, agora, iso, log, normalizar, parse_data, url_canonica

ARQUIVO = DADOS / "historico.json"
VERSAO = 1

# palavras muito comuns que não ajudam a comparar títulos
_VAZIAS = set("""a an the of in on for and or with to from by at as is are be was were
vs versus its their this that these those into among between after before during
o os as um uma de da do das dos em no na nos nas para por com sem e ou que ao aos
""".split())


def _tokens(titulo: str) -> set[str]:
    return {t for t in normalizar(titulo).split() if t not in _VAZIAS and len(t) > 2}


def similaridade(a: str, b: str) -> float:
    """Combina sobreposição de palavras (Jaccard) e sequência de caracteres."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    jac = len(ta & tb) / len(ta | tb)
    if jac < 0.35:  # atalho: títulos claramente diferentes (evita custo do SequenceMatcher)
        return jac
    seq = SequenceMatcher(None, normalizar(a), normalizar(b)).ratio()
    return max(jac, seq)


def gerar_id(item: dict) -> str:
    ids = item.get("identificadores") or {}
    for chave in ("doi", "pmid", "nct"):
        if ids.get(chave):
            base = f"{chave}:{ids[chave]}"
            break
    else:
        base = f"url:{url_canonica(item.get('url', ''))}|{normalizar(item.get('titulo_original', ''))}"
    return hashlib.sha1(base.encode()).hexdigest()[:16]


class Historico:
    def __init__(self, caminho: str | Path | None = None):
        self.caminho = Path(caminho or os.getenv("RADAR_HISTORICO") or ARQUIVO)
        self.itens: dict[str, dict] = {}
        self.execucoes: list[dict] = []
        self._indices_ok = False
        self._carregar()

    # --------------------------------------------------------- disco ---
    def _carregar(self) -> None:
        if not self.caminho.exists():
            log.info("Histórico novo será criado em %s", self.caminho)
            return
        with open(self.caminho, encoding="utf-8") as f:
            dados = json.load(f)
        self.itens = {i["id"]: i for i in dados.get("itens", [])}
        self.execucoes = dados.get("execucoes", [])

    def salvar(self) -> None:
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        dados = {
            "versao": VERSAO,
            "atualizado_em": iso(agora()),
            "total_itens": len(self.itens),
            "execucoes": self.execucoes[-400:],
            "itens": sorted(self.itens.values(), key=lambda i: i.get("encontrado_em") or "",
                            reverse=True),
        }
        # escrita atômica: nunca deixa um JSON corrompido se o processo cair
        fd, tmp = tempfile.mkstemp(dir=self.caminho.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.caminho)

    # ------------------------------------------------------ índices ----
    def _indexar(self) -> None:
        self._por_doi, self._por_pmid, self._por_nct, self._por_url = {}, {}, {}, {}
        for i in self.itens.values():
            ids = i.get("identificadores") or {}
            if ids.get("doi"):
                self._por_doi[ids["doi"]] = i["id"]
            if ids.get("pmid"):
                self._por_pmid[ids["pmid"]] = i["id"]
            if ids.get("nct"):
                self._por_nct.setdefault(ids["nct"], i["id"])
            if i.get("url"):
                self._por_url[url_canonica(i["url"])] = i["id"]
        self._indices_ok = True

    # ------------------------------------------------ deduplicação -----
    def encontrar_duplicata(self, item: dict, limiar: float = 0.82) -> tuple[str | None, str]:
        """Retorna (id_existente, motivo) ou (None, '').

        Ordem de comparação: identificadores fortes (DOI, PMID) → URL →
        título/assunto semelhante em janela de 120 dias. NCT sozinho NÃO é
        duplicata: um mesmo ensaio pode gerar publicações novas legítimas
        (tratadas como "atualização de assunto já monitorado").
        """
        if not self._indices_ok:
            self._indexar()
        ids = item.get("identificadores") or {}
        if ids.get("doi") and ids["doi"] in self._por_doi:
            return self._por_doi[ids["doi"]], "mesmo DOI"
        if ids.get("pmid") and ids["pmid"] in self._por_pmid:
            return self._por_pmid[ids["pmid"]], "mesmo PMID"
        u = url_canonica(item.get("url", ""))
        if u and u in self._por_url:
            return self._por_url[u], "mesma URL"

        titulo = item.get("titulo_original", "")
        limite = agora() - timedelta(days=120)
        for existente in self.itens.values():
            d = parse_data(existente.get("encontrado_em"))
            if d and d < limite:
                continue
            for t in (existente.get("titulo_original", ""), existente.get("titulo", "")):
                if t and similaridade(titulo, t) >= limiar:
                    return existente["id"], "título equivalente"
        return None, ""

    def relacionado_por_ensaio(self, item: dict) -> str | None:
        if not self._indices_ok:
            self._indexar()
        nct = (item.get("identificadores") or {}).get("nct")
        return self._por_nct.get(nct) if nct else None

    def adicionar(self, item: dict) -> None:
        self.itens[item["id"]] = item
        self._indices_ok = False

    def registrar_mencao(self, id_existente: str, item: dict, motivo: str) -> None:
        """Mesma informação vista de novo: não é notícia nova, mas conta como
        corroboração/repercussão (útil para 'assuntos ganhando atenção')."""
        ex = self.itens[id_existente]
        mencoes = ex.setdefault("mencoes", [])
        chave = url_canonica(item.get("url", ""))
        if any(m.get("url") == chave for m in mencoes) or chave == url_canonica(ex.get("url", "")):
            return
        mencoes.append({
            "fonte": item.get("fonte"), "url": chave, "tipo_fonte": item.get("tipo_fonte"),
            "visto_em": iso(agora()), "motivo": motivo,
        })

    # ------------------------------------------------------ consultas --
    def recentes(self, dias: int) -> list[dict]:
        limite = agora() - timedelta(days=dias)
        return [i for i in self.itens.values()
                if (parse_data(i.get("encontrado_em")) or limite) >= limite]

    def registrar_execucao(self, resumo: dict) -> None:
        self.execucoes.append(resumo)
