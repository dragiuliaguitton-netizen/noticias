"""Utilidades compartilhadas: configuração, HTTP, datas, texto e log."""

from __future__ import annotations

import html
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
import yaml

RAIZ = Path(__file__).resolve().parent.parent
CONFIG_PADRAO = RAIZ / "config" / "radar.yaml"
DADOS = RAIZ / "data"

USER_AGENT = (
    "RadarDermatologia/1.0 (+https://github.com; uso educacional; "
    "contato via RADAR_CONTACT_EMAIL)"
)

log = logging.getLogger("radar")


def configurar_log(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def carregar_config(caminho: str | Path | None = None) -> dict:
    caminho = Path(caminho or os.getenv("RADAR_CONFIG") or CONFIG_PADRAO)
    with open(caminho, encoding="utf-8") as f:
        return yaml.safe_load(f)


def agora() -> datetime:
    """Momento atual em UTC. RADAR_NOW (ISO) permite simular datas em testes."""
    fixo = os.getenv("RADAR_NOW")
    if fixo:
        return datetime.fromisoformat(fixo).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- HTTP -------

class Http:
    """Cliente HTTP com timeout, repetição e disjuntor por host."""

    LIMITE_FALHAS_HOST = 3

    def __init__(self, tentativas: int = 3, timeout: int = 25):
        self.tentativas = tentativas
        self.timeout = timeout
        self._falhas_host: dict[str, int] = {}
        self.sessao = requests.Session()
        self.sessao.headers["User-Agent"] = USER_AGENT
        contato = os.getenv("RADAR_CONTACT_EMAIL")
        if contato:
            self.sessao.headers["From"] = contato

    def get(self, url: str, **kw) -> requests.Response:
        host = urlparse(url).netloc
        falhas = self._falhas_host
        if falhas.get(host, 0) >= self.LIMITE_FALHAS_HOST:
            # disjuntor: host já falhou várias vezes nesta execução — não insistir
            raise requests.ConnectionError(f"{host} indisponível nesta execução (ignorado após "
                                           f"{self.LIMITE_FALHAS_HOST} falhas seguidas)")
        ultimo_erro: Exception | None = None
        for i in range(self.tentativas):
            try:
                r = self.sessao.get(url, timeout=self.timeout, **kw)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
                r.raise_for_status()
                falhas[host] = 0
                return r
            except requests.RequestException as e:  # noqa: PERF203
                ultimo_erro = e
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status and 400 <= status < 500 and status != 429:
                    break  # erro do cliente: não adianta repetir
                time.sleep(2 ** i)
        assert ultimo_erro is not None
        status = getattr(getattr(ultimo_erro, "response", None), "status_code", None)
        if not (status and 400 <= status < 500):  # 404 de uma URL não derruba o host
            falhas[host] = falhas.get(host, 0) + 1
        raise ultimo_erro


# --------------------------------------------------------------- DATAS -------

_FORMATOS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
    "%Y %b %d", "%Y %b", "%Y", "%B %d, %Y", "%Y-%m",
)


def parse_data(texto: str | None) -> datetime | None:
    """Converte datas em formatos variados (RSS, PubMed, ISO) para UTC."""
    if not texto:
        return None
    texto = texto.strip()
    try:
        d = parsedate_to_datetime(texto)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        d = datetime.fromisoformat(texto.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in _FORMATOS:
        try:
            return datetime.strptime(texto, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # PubMed às vezes usa "2026 Sep 3-9" ou "2026 Sep-Oct"
    m = re.match(r"(\d{4})\s+([A-Za-z]{3})", texto)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y %b").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None
    return None


def iso(d: datetime | None) -> str | None:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds") if d else None


def data_br(d: str | datetime | None) -> str:
    if d is None:
        return "data não informada"
    if isinstance(d, str):
        d = parse_data(d)
        if d is None:
            return "data não informada"
    return d.strftime("%d/%m/%Y")


# --------------------------------------------------------------- TEXTO -------

_TAG = re.compile(r"<[^>]+>")
_ESPACO = re.compile(r"\s+")


def limpar_html(texto: str | None) -> str:
    if not texto:
        return ""
    return _ESPACO.sub(" ", html.unescape(_TAG.sub(" ", texto))).strip()


def normalizar(texto: str) -> str:
    """Minúsculas, sem acentos e sem pontuação — base para deduplicação."""
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9 ]+", " ", t.lower())
    return _ESPACO.sub(" ", t).strip()


_PARAMS_RASTREIO = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                    "utm_content", "fbclid", "gclid", "oc", "ref", "rss"}


def url_canonica(url: str) -> str:
    if not url:
        return ""
    p = urlparse(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in _PARAMS_RASTREIO]
    caminho = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower() or "https", p.netloc.lower().removeprefix("www."),
                       caminho, "", urlencode(q), ""))


def dominio(url: str) -> str:
    return urlparse(url or "").netloc.lower().removeprefix("www.")


def truncar(texto: str, n: int) -> str:
    texto = texto or ""
    return texto if len(texto) <= n else texto[: n - 1].rsplit(" ", 1)[0] + "…"
