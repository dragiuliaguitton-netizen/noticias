"""Camada opcional de IA (Claude) para resumo em português, classificação
refinada, limitações e ideias de conteúdo.

Princípios (reforçados no prompt e no código):
  - A IA recebe SOMENTE o texto coletado da fonte (título + resumo/trecho) e
    não pode acrescentar fatos, números ou conclusões que não estejam nele.
  - Os tetos de segurança de radar/classificacao.py continuam valendo depois
    da IA (ex.: notícia sem fonte primária nunca vira 🔴).
  - Qualquer falha da API (limite, rede, recusa) faz o item seguir no modo
    heurístico — o radar nunca para por causa da IA.

Ative definindo ANTHROPIC_API_KEY. Modelo: RADAR_MODEL ou `modelo_ia` no YAML.
"""

from __future__ import annotations

import json
import os
import re
from typing import Literal, Optional

from .util import log, truncar

try:  # dependência opcional
    import anthropic
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore[assignment]
    BaseModel = object  # type: ignore[assignment,misc]

    def Field(*a, **k):  # type: ignore[no-redef]  # noqa: N802
        return None


SISTEMA = """Você é o filtro científico de um radar de atualização em Dermatologia \
para uma médica em formação em Dermatologia no Brasil, que também produz conteúdo \
educativo no Instagram para um público majoritariamente de mulheres jovens.

Regras inegociáveis:
1. Use APENAS as informações do texto fornecido. Nunca acrescente dados, números, \
nomes de estudos, conclusões ou contexto que não estejam no texto. Se algo não está \
no texto, diga que não foi informado.
2. Nunca apresente associação como causalidade. Nunca transforme resultado \
preliminar ou pré-clínico em recomendação clínica.
3. Diferencie status de terapias: aprovado → em análise → em estudo → experimental. \
Nunca apresente terapia experimental como tratamento estabelecido.
4. Diferencie evidência científica, opinião de especialista, hipótese, notícia e \
marketing de empresa. Publicidade de marca não é evidência.
5. Não invente relevância. Critérios:
   - muito_relevante: pode mudar prática clínica, segurança, diagnóstico ou \
tratamento (ex.: aprovação regulatória, alerta de segurança, guideline, ensaio fase 3 \
em periódico de alto impacto).
   - relevante: importante para atualização profissional, sem mudança imediata de conduta.
   - interessante: bom para conhecimento ou educação do público.
   - baixa: não merece entrar no relatório.
6. Ideia de conteúdo para Instagram SOMENTE se o item for relevante ou muito \
relevante E houver uma pergunta, dúvida frequente ou informação útil para o público. \
Caso contrário, retorne ideia_conteudo = null. Sem sensacionalismo, sem prometer \
resultados, sem gerar medo, sem induzir tratamento, sem propaganda disfarçada. \
Linguagem clara, didática, elegante e acessível; explique termos técnicos.
7. Alerta extraordinário (alerta_extraordinario=true) apenas para: alerta regulatório, \
retirada de produto, risco grave, mudança importante de recomendação, aprovação \
extremamente relevante ou descoberta de grande impacto — e somente se a fonte for \
oficial ou científica.
Escreva tudo em português do Brasil."""


if anthropic is not None:

    class IdeiaConteudo(BaseModel):
        tema: str
        formato: Literal["Reel", "Carrossel", "Stories", "Post único", "FAQ", "Mito x verdade"]
        gancho: str = Field(description="Frase de abertura, sem sensacionalismo")
        mensagem_principal: str
        o_que_o_publico_precisa_entender: str
        cta: str = Field(description="Chamada para ação educativa, sem induzir tratamento")

    class AnaliseItem(BaseModel):
        dermatologico: bool = Field(description="O conteúdo tem relação real com Dermatologia?")
        titulo_pt: str = Field(description="Título objetivo em português")
        o_que_aconteceu: str = Field(description="2 a 4 frases, somente com fatos do texto")
        por_que_importa: str = Field(description="Relevância para a Dermatologia")
        tipo_evidencia: Literal[
            "estudo observacional", "ensaio clínico", "ensaio clínico randomizado", "revisão",
            "revisão sistemática", "metanálise", "guideline", "consenso",
            "comunicado regulatório", "notícia", "estudo experimental (pré-clínico)",
            "relato de caso", "carta/editorial (opinião)", "marketing de empresa", "outro"]
        limitacoes: list[str] = Field(description="Limitações importantes; lista vazia se não houver")
        status_terapia: Optional[Literal["aprovado", "em análise", "em estudo", "experimental"]] = None
        relevancia: Literal["muito_relevante", "relevante", "interessante", "baixa"]
        justificativa_relevancia: str
        alerta_extraordinario: bool
        eh_publicidade: bool
        ideia_conteudo: Optional[IdeiaConteudo] = None


class AnalisadorIA:
    def __init__(self, cfg: dict):
        self.ativo = bool(os.getenv("ANTHROPIC_API_KEY")) and anthropic is not None
        self.modelo = os.getenv("RADAR_MODEL") or cfg.get("modelo_ia", "claude-opus-5")
        self.limite = int(os.getenv("RADAR_MAX_IA") or cfg.get("max_itens_ia_por_dia", 40))
        self.usados = 0
        self.falhas = 0
        self.cliente = anthropic.Anthropic(max_retries=3) if self.ativo else None
        if not self.ativo:
            log.info("IA desativada (sem ANTHROPIC_API_KEY): usando modo heurístico.")

    def disponivel(self) -> bool:
        return self.ativo and self.usados < self.limite and self.falhas < 5

    # ----------------------------------------------------------------------
    def analisar(self, item: dict) -> dict | None:
        """Devolve dict com a análise ou None (item segue heurístico)."""
        if not self.disponivel():
            return None
        self.usados += 1
        conteudo = (
            f"FONTE: {item.get('fonte')} ({item.get('tipo_fonte')}; "
            f"{'agregado via ' + item['agregador'] if item.get('agregador') else 'acesso direto'})\n"
            f"TIPOS DE PUBLICAÇÃO (PubMed): {', '.join(item.get('tipos_publicacao') or []) or 'n/a'}\n"
            f"DATA: {item.get('publicado_em') or 'não informada'}\n"
            f"TÍTULO ORIGINAL: {item.get('titulo_original')}\n\n"
            f"TEXTO DISPONÍVEL:\n{truncar(item.get('resumo_original') or '(sem resumo — apenas título)', 5000)}\n\n"
            f"Classificação automática preliminar: evidência={item.get('tipo_evidencia')}, "
            f"status={item.get('status_terapia')}, assunto={item.get('assunto')}.\n"
            "Analise o item seguindo as regras."
        )
        try:
            resp = self.cliente.messages.parse(
                model=self.modelo,
                max_tokens=16000,
                system=SISTEMA,
                messages=[{"role": "user", "content": conteudo}],
                output_format=AnaliseItem,
                output_config={"effort": "medium"},
            )
            if resp.stop_reason == "refusal" or resp.parsed_output is None:
                log.warning("IA não analisou %s (stop_reason=%s)", item["id"], resp.stop_reason)
                return None
            return resp.parsed_output.model_dump()
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            self.falhas = 99  # chave inválida: não adianta tentar os demais itens
            log.error("IA desativada nesta execução: credencial rejeitada (%s)", e.status_code)
        except anthropic.RateLimitError as e:
            self.falhas += 1
            log.warning("IA: limite de uso atingido (%s)", e)
        except anthropic.APIStatusError as e:
            self.falhas += 1
            log.warning("IA: erro HTTP %s — %s", e.status_code, truncar(str(e), 200))
        except anthropic.APIConnectionError as e:
            self.falhas += 1
            log.warning("IA: falha de conexão — %s", e)
        except Exception as e:  # noqa: BLE001 — validação/parsing
            self.falhas += 1
            log.warning("IA: resposta inválida — %s", truncar(str(e), 200))
        return None

    # ----------------------------------------------------------------------
    def localizar_fonte_primaria(self, item: dict, dominios_ok: list[str]) -> dict | None:
        """Para notícias potencialmente importantes: busca na web a fonte
        original (comunicado, estudo, guideline). Retorna {url, titulo,
        confirma, divergencias} ou None. O domínio devolvido é conferido pelo
        chamador contra a lista de domínios confiáveis."""
        if not self.disponivel():
            return None
        self.usados += 1
        pedido = (
            "Localize a FONTE PRIMÁRIA (comunicado oficial, estudo publicado, guideline ou "
            "registro regulatório) da notícia abaixo. Use a busca na web. Depois responda "
            "SOMENTE com um JSON: {\"url\": str|null, \"titulo\": str|null, "
            "\"confirma\": true|false, \"divergencias\": str|null}. "
            "`confirma` = a fonte primária sustenta a manchete? `divergencias` = diferenças "
            "objetivas entre a notícia e a fonte primária (ou null).\n\n"
            f"NOTÍCIA: {item.get('titulo_original')}\nVEÍCULO: {item.get('fonte')}\n"
            f"TRECHO: {truncar(item.get('resumo_original') or '', 800)}"
        )
        try:
            resp = self.cliente.messages.create(
                model=self.modelo,
                max_tokens=16000,
                messages=[{"role": "user", "content": pedido}],
                tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 4,
                        "allowed_domains": dominios_ok[:60]}],
                output_config={"effort": "medium"},
            )
            texto = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            m = re.search(r"\{.*\}", texto, re.S)
            return json.loads(m.group(0)) if m else None
        except Exception as e:  # noqa: BLE001
            self.falhas += 1
            log.warning("IA: busca de fonte primária falhou — %s", truncar(str(e), 200))
            return None
