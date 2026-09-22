"""Envio de e-mail — SMTP (ex.: Gmail com senha de app) ou Resend (API HTTPS).

Nenhuma credencial fica no código: tudo vem de variáveis de ambiente
(veja .env.example). Se nada estiver configurado, a mensagem é salva como
arquivo .eml em `saida/` e o erro é informado claramente — nunca fingimos
que o e-mail foi enviado.
"""

from __future__ import annotations

import base64
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

import requests

from .util import RAIZ, agora, log


class EmailNaoConfigurado(RuntimeError):
    pass


def destinatarios() -> list[str]:
    return [e.strip() for e in (os.getenv("EMAIL_TO") or "").split(",") if e.strip()]


def provedor() -> str | None:
    if os.getenv("RESEND_API_KEY"):
        return "resend"
    if os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD"):
        return "smtp"
    if os.getenv("SMTP_HOST") and os.getenv("SMTP_ALLOW_NO_AUTH") == "1":  # testes locais
        return "smtp"
    return None


def montar(assunto: str, texto: str, html: str | None,
           anexos: list[tuple[str, bytes, str]] | None = None) -> EmailMessage:
    msg = EmailMessage()
    remetente = os.getenv("EMAIL_FROM") or os.getenv("SMTP_USER") or "radar@localhost"
    msg["From"] = remetente
    msg["To"] = ", ".join(destinatarios()) or "destinatario-nao-configurado@localhost"
    msg["Subject"] = assunto
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="radar-dermatologia")
    msg.set_content(texto)
    if html:
        msg.add_alternative(html, subtype="html")
    for nome, dados, mime in anexos or []:
        tipo, sub = mime.split("/", 1)
        msg.add_attachment(dados, maintype=tipo, subtype=sub, filename=nome)
    return msg


def salvar_eml(msg: EmailMessage, prefixo: str) -> Path:
    pasta = Path(os.getenv("RADAR_SAIDA") or RAIZ / "saida")
    pasta.mkdir(parents=True, exist_ok=True)
    arq = pasta / f"{prefixo}-{agora():%Y%m%d-%H%M%S}.eml"
    arq.write_bytes(bytes(msg))
    return arq


class Mailer:
    """Interface simples: enviar(assunto, texto, html, anexos) -> True/False."""

    def enviar(self, assunto: str, texto: str, html: str | None = None,
               anexos: list[tuple[str, bytes, str]] | None = None, prefixo: str = "email") -> bool:
        msg = montar(assunto, texto, html, anexos)
        copia = salvar_eml(msg, prefixo)  # cópia local sempre (auditoria)
        p = provedor()
        if not destinatarios():
            raise EmailNaoConfigurado(f"EMAIL_TO não definido. Mensagem salva em {copia}.")
        if p is None:
            raise EmailNaoConfigurado(
                "Nenhum provedor de e-mail configurado (defina SMTP_* ou RESEND_API_KEY). "
                f"Mensagem salva em {copia}.")
        if p == "resend":
            self._resend(assunto, texto, html, anexos)
        else:
            self._smtp(msg)
        log.info("E-mail enviado via %s para %s — %s", p, ", ".join(destinatarios()), assunto)
        return True

    @staticmethod
    def _smtp(msg: EmailMessage) -> None:
        host = os.environ["SMTP_HOST"]
        porta = int(os.getenv("SMTP_PORT") or 587)
        usuario, senha = os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD")
        ctx = ssl.create_default_context()
        if porta == 465 or os.getenv("SMTP_SSL") == "1":
            with smtplib.SMTP_SSL(host, porta, context=ctx, timeout=60) as s:
                if usuario and senha:
                    s.login(usuario, senha)
                s.send_message(msg)
            return
        with smtplib.SMTP(host, porta, timeout=60) as s:
            if os.getenv("SMTP_STARTTLS", "1") != "0":
                s.starttls(context=ctx)
            if usuario and senha:
                s.login(usuario, senha)
            s.send_message(msg)

    @staticmethod
    def _resend(assunto, texto, html, anexos) -> None:
        corpo = {
            "from": os.getenv("EMAIL_FROM") or "Radar Dermatologia <onboarding@resend.dev>",
            "to": destinatarios(),
            "subject": assunto,
            "text": texto,
        }
        if html:
            corpo["html"] = html
        if anexos:
            corpo["attachments"] = [{"filename": n, "content": base64.b64encode(d).decode()}
                                    for n, d, _ in anexos]
        r = requests.post("https://api.resend.com/emails", json=corpo, timeout=60,
                          headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"})
        if r.status_code >= 300:
            raise RuntimeError(f"Resend HTTP {r.status_code}: {r.text[:300]}")
