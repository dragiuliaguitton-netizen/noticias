# 🧴 Radar Autônomo de Dermatologia

Busca diária automática de novidades em Dermatologia (clínica, estética, cosméticos,
medicamentos, câncer de pele, dermatoscopia e IA), com validação de fontes, deduplicação,
classificação de relevância, ideias de conteúdo para Instagram e **relatório semanal por e-mail**.

**Qualidade > quantidade · fonte primária > manchete · evidência > hype.**

---

## Como funciona

```
GitHub Actions (todo dia 06:00 BRT)
  └─ python -m radar diario
       1. Coleta ── PubMed (11 periódicos de Dermatologia + NEJM/Lancet/JAMA/BMJ/Nature
       │            filtrados por termos dermatológicos + 20 consultas temáticas)
       │         ── Feeds oficiais: FDA (press releases, MedWatch, recalls), EMA, JAMA Dermatology
       │         ── Google News em PORTUGUÊS e INGLÊS (41 consultas: SBD, Anvisa, AAD, BAD, EADV…)
       │         ── ClinicalTrials.gov (ensaios com resultados recém-postados)
       2. Validação ── nível da fonte (oficial › periódico › imprensa médica › imprensa geral)
       3. Deduplicação ── DOI, PMID, URL e título/assunto semelhante vs. histórico
       4. Classificação ── tipo de evidência, status da terapia, assunto, 🔴/🟠/🟡/⚪
       5. Corroboração ── notícia só sobe de nível se houver fonte primária correspondente
       6. IA (opcional) ── resumo em português, "por que importa", limitações, ideias de post
       7. Alerta extraordinário ── e-mail "🚨 ALERTA DERMATOLOGIA" se houver algo crítico
       8. Histórico ── data/historico.json + registro do dia em data/diario/AAAA-MM-DD.md

GitHub Actions (toda segunda 08:00 BRT)
  └─ python -m radar semanal ──► e-mail "🧴 Radar Semanal de Dermatologia — DD/MM/AAAA"
                                  (+ cópia em relatorios/)
```

### Regras de segurança do conteúdo (no código, não só no prompt)

| Regra | Onde |
|---|---|
| Notícia sem fonte primária confirmada **nunca passa de 🟡** | `classificacao.aplicar_tetos` |
| Resultado pré-clínico → status "experimental", **nunca 🔴** | `classificacao.aplicar_tetos` |
| Tom publicitário/lançamento → não é evidência, teto 🟡 | `classificacao.parece_marketing` |
| Estudo observacional → limitação "associação ≠ causalidade" | `classificacao.limitacoes_heuristicas` |
| A IA **não pode** promover status (ex.: "em estudo" → "aprovado") nem furar os tetos | `pipeline._aplicar_analise` |
| Sem IA, o resumo é a **conclusão escrita pelos autores**, nunca texto inventado | `classificacao.resumo_heuristico` |
| Conteúdo antigo que volta a circular é marcado como antigo | `pipeline.processar_dia` |
| Ideias de Instagram só para 🔴/🟠 | `pipeline` |
| Item só é marcado como "já reportado" se o e-mail realmente saiu | `relatorio.gerar_e_enviar_semanal` |

---

## Ativação (uma vez só, ~10 minutos)

> O agendamento do GitHub Actions **só roda no branch padrão** (`main`). Faça o merge deste
> branch antes.

### 1. Cadastrar os segredos
No GitHub: **Settings → Secrets and variables → Actions → New repository secret**.

| Segredo | Obrigatório | Exemplo |
|---|---|---|
| `EMAIL_TO` | ✅ | `seu-email@gmail.com` |
| `SMTP_HOST` | ✅ (ou Resend) | `smtp.gmail.com` |
| `SMTP_PORT` | ✅ (ou Resend) | `587` |
| `SMTP_USER` | ✅ (ou Resend) | `seu-email@gmail.com` |
| `SMTP_PASSWORD` | ✅ (ou Resend) | senha de app do Gmail (veja abaixo) |
| `EMAIL_FROM` | recomendado | `seu-email@gmail.com` |
| `ANTHROPIC_API_KEY` | recomendado | chave da API Claude (resumos em PT + ideias de post) |
| `NCBI_API_KEY` | opcional | chave gratuita do PubMed |
| `RESEND_API_KEY` | alternativa ao SMTP | chave do Resend |

**Senha de app do Gmail:** ative a verificação em 2 etapas na sua conta Google e gere uma senha
em <https://myaccount.google.com/apppasswords>. Use essa senha de 16 caracteres (não a senha
normal) em `SMTP_PASSWORD`.

### 2. Primeira execução
Aba **Actions → Radar Dermatologia → Run workflow → ação: `primeira-execucao`**.
Ela: verifica o acesso a cada fonte → faz a busca inicial ampla (30 dias) → envia um
e-mail de teste → envia o primeiro relatório semanal. A execução fica **vermelha** se o
e-mail não sair — nunca finge sucesso.

Depois disso o radar roda sozinho.

---

## Como alterar depois

| O quê | Onde |
|---|---|
| **E-mail de destino** | segredo `EMAIL_TO` (vários: separe por vírgula) |
| **Frequência / horário** | `.github/workflows/radar.yml`, linhas `cron` (em UTC; BRT = UTC−3) |
| **Palavras-chave de busca** | `config/radar.yaml` → `pubmed.consultas_tematicas` e `noticias.consultas.pt/en` |
| **Periódicos monitorados** | `config/radar.yaml` → `pubmed.periodicos` (abreviação NLM) |
| **Fontes / feeds RSS** | `config/radar.yaml` → `feeds` (nome, url, tipo, filtrar) |
| **Domínios confiáveis** | `config/radar.yaml` → `dominios.nivel1/2/3` |
| **Tamanho do relatório** | `config/radar.yaml` → `relatorio` (destaques, estudos, ideias) |
| **Formato do relatório** | `radar/templates/semanal.md.j2` (texto) e `semanal.html.j2` (e-mail) |
| **Regras de relevância** | `radar/classificacao.py` → `pontuar`, `nivel_por_pontos`, `aplicar_tetos` |
| **Modelo de IA / custo** | variável `RADAR_MODEL`; `max_itens_ia_por_dia` no YAML |
| **Desligar alertas extraordinários** | variável de repositório `RADAR_ALERTAS=0` |

## Uso local

```bash
pip install -r requirements.txt
cp .env.example .env    # preencha e exporte: set -a; source .env; set +a
python -m radar verificar-fontes   # testa acesso a cada fonte, IA e e-mail
python -m radar diario             # busca do dia (--janela 30 para busca ampla)
python -m radar semanal --sem-envio  # prévia do relatório em relatorios/ (não marca itens)
python -m radar testar-email
python -m radar status             # resumo do histórico
python -m unittest discover -s tests -t .   # testes de ponta a ponta
```

## Arquivos

```
config/radar.yaml            configuração editável (consultas, fontes, domínios, limites)
radar/fontes.py              coletores: PubMed, RSS, Google News, ClinicalTrials.gov
radar/historico.py           histórico persistente + deduplicação
radar/classificacao.py       nível da fonte, tipo de evidência, status, relevância, tetos
radar/ia.py                  camada opcional Claude (resumo PT, limitações, ideias, fonte primária)
radar/pipeline.py            execução diária + registro diário + alertas
radar/relatorio.py           relatório semanal (7 seções) e e-mail de alerta
radar/email_envio.py         envio via SMTP ou Resend (cópia .eml sempre salva)
radar/templates/             modelos do relatório (Markdown e HTML) e do alerta
tests/                       testes de ponta a ponta com dados FICTÍCIOS
.github/workflows/radar.yml  agendamento diário/semanal
data/historico.json          histórico (criado na 1ª execução; versionado = auditável)
data/diario/                 registro diário no formato da seção 12
relatorios/                  cópias dos relatórios semanais
.env.example                 variáveis de ambiente documentadas
```

### Histórico e auditoria
Cada item em `data/historico.json` guarda: título, link, DOI/PMID/NCT, fonte e nível da fonte,
datas de publicação/atualização/descoberta, consulta que o encontrou, assunto, tipo de evidência,
relevância e **justificativa da pontuação**, limitações, se entrou em relatório (datas), se virou
ideia de conteúdo (e quando foi usada), se gerou alerta, e menções/repercussões posteriores.
Como o arquivo é versionado, cada execução gera um commit — dá para ver exatamente o que foi
encontrado, quando e de onde.

### Robustez
Cada consulta/fonte roda isolada: erro de rede, bloqueio, XML quebrado ou API fora do ar são
registrados (aparecem na auditoria do relatório) e as demais fontes seguem. Um host que falha 3
vezes seguidas é pulado no restante da execução. Falha da IA → o item segue no modo heurístico.
