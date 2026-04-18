# Júlia — Assistente Virtual Jurídica

Assistente de atendimento via WhatsApp para o escritório **Vasconcellos & Amadeo Advocacia**, especializado em Direito Trabalhista e Previdenciário. A Júlia qualifica clientes, conduz o atendimento com técnicas de SPIN Selling e AIDA, e agenda consultas automaticamente no Google Calendar.

## Funcionalidades

- **Atendimento inteligente** via WhatsApp integrado ao [Chatwoot](https://www.chatwoot.com/)
- **Qualificação de clientes** com perguntas estratégicas (SPIN Selling) nas áreas Trabalhista e Previdenciária
- **Agendamento automático** de reuniões no Google Calendar (online com Google Meet ou presencial)
- **Memória de conversa** persistida em PostgreSQL por sessão (número de WhatsApp)
- **Análise de mídia**: extração de texto de imagens (JPG/PNG) e documentos (PDF) via Claude Vision
- **Debounce de mensagens** via Redis — consolida mensagens rápidas antes de responder
- **Deduplicação** de eventos com Redis para evitar respostas duplicadas
- **Prompt caching** da Anthropic para redução de custos em requisições repetidas
- **Conformidade OAB**: nunca promete resultados, verifica advogado constituído, encerra eticamente

## Arquitetura

```
WhatsApp (cliente)
      │
      ▼
   Chatwoot  ──webhook──►  Modal (ai_julia.py)
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼               ▼
                 Redis          PostgreSQL    Anthropic API
               (debounce/     (histórico de    (Claude Haiku)
               deduplicação)   conversas)
                                               │
                                    ┌──────────┘
                                    ▼
                            Google Calendar API
                            (agendamento + Meet)
```

## Tecnologias

| Componente | Tecnologia |
|---|---|
| Runtime serverless | [Modal](https://modal.com/) |
| LLM | Anthropic Claude Haiku (`claude-haiku-4-5`) |
| Atendimento / CRM | [Chatwoot](https://www.chatwoot.com/) |
| Banco de dados | PostgreSQL (via `psycopg2`) |
| Cache / debounce | Redis |
| Calendário | Google Calendar API v3 |
| HTTP client | `httpx` |

## Pré-requisitos

- Conta no [Modal](https://modal.com/) com CLI instalado (`pip install modal`)
- Secrets configurados no Modal (ver abaixo)
- Instância do Chatwoot com webhook apontando para o endpoint gerado pelo Modal
- Projeto no Google Cloud com a Calendar API habilitada e OAuth 2.0 configurado

## Configuração de Secrets no Modal

### Secret: `marina-secrets`

| Variável | Descrição |
|---|---|
| `ANTHROPIC_API_KEY` | Chave da API da Anthropic |
| `CHATWOOT_URL` | URL base do Chatwoot (ex: `https://app.chatwoot.com`) |
| `CHATWOOT_TOKEN` | Token de acesso à API do Chatwoot |
| `CHATWOOT_ACCOUNT_ID` | ID da conta no Chatwoot (padrão: `1`) |
| `REDIS_URL` | URL de conexão Redis (ex: `redis://...`) |
| `POSTGRES_URL` | URL de conexão PostgreSQL |

### Secret: `google-calendar-secrets`

| Variável | Descrição |
|---|---|
| `GOOGLE_CLIENT_ID` | Client ID do OAuth 2.0 |
| `GOOGLE_CLIENT_SECRET` | Client Secret do OAuth 2.0 |
| `GOOGLE_REFRESH_TOKEN` | Refresh Token gerado via `setup_google_oauth.py` |
| `GOOGLE_CALENDAR_TRABALHISTA` | ID do calendário da área Trabalhista |
| `GOOGLE_CALENDAR_PREVIDENCIARIO` | ID do calendário da área Previdenciária |

## Instalação e Deploy

```bash
# 1. Instale o Modal CLI
pip install modal

# 2. Autentique no Modal
modal setup

# 3. (Primeira vez) Gere o refresh token do Google
python setup_google_oauth.py

# 4. Faça o deploy
modal deploy ai_julia.py
```

O Modal retornará a URL do webhook. Configure-a no Chatwoot em **Configurações → Integrações → Webhook**.

## Como Funciona o Atendimento

1. Cliente envia mensagem no WhatsApp
2. Chatwoot recebe e dispara o webhook para o Modal
3. A Júlia coleta o nome e verifica se há advogado constituído
4. Qualifica o caso com perguntas SPIN (Situação → Problema → Implicação → Necessidade)
5. Se qualificado, agenda reunião via Google Calendar e envia link do Meet (se online)
6. Solicita os documentos necessários antes da reunião
7. Encerra com aviso legal conforme ética da OAB

## Estrutura do Projeto

```
BOT ADV/
├── ai_julia.py          # Versão atual com agendamento Google Calendar
├── app.py               # Versão anterior (sem agendamento)
└── setup_google_oauth.py # Script para gerar refresh token do Google
```

## Áreas Atendidas

**Trabalhista** (Dr. Rodolfo Amadeo)
- Rescisão e verbas rescisórias
- Horas extras e jornada
- Assédio moral e sexual
- Vínculo empregatício e estabilidade

**Previdenciário** (Dra. Genaina Vasconcellos)
- BPC/LOAS (pessoa com deficiência e idoso)
- Aposentadoria e revisão de benefício
- Auxílio-doença e invalidez

## Desenvolvedor

**Francisco Gomes** — frandonny dev
franc2007vga@gmail.com

## Escritório

**Vasconcellos & Amadeo Advocacia**
Av. Nossa Sra. dos Navegantes, 755 — Sala 508 — Enseada do Suá, Vitória/ES
Segunda a sexta, das 9h às 18h
