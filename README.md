# Júlia — Assistente Virtual Jurídica

Assistente de atendimento via WhatsApp para o escritório **Vasconcellos & Amadeo Advocacia**, especializado em Direito Trabalhista e Previdenciário. A Júlia qualifica clientes, conduz o atendimento com técnicas de SPIN Selling e AIDA, e transfere o cliente para o advogado responsável.

## Funcionalidades

- **Atendimento inteligente** via WhatsApp integrado ao [Chatwoot](https://www.chatwoot.com/)
- **Qualificação de clientes** com perguntas estratégicas (SPIN Selling) nas áreas Trabalhista e Previdenciária
- **Transferência automática** para o advogado responsável (Trabalhista ou Previdenciário) com nota privada de resumo
- **Memória de conversa** persistida em PostgreSQL por sessão (número de WhatsApp)
- **Análise de mídia**: extração de texto de imagens (JPG/PNG) e documentos (PDF) via Claude Vision; transcrição de áudios via Groq Whisper
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
                    ┌──────────────┼──────────────┬──────────────┐
                    ▼              ▼               ▼              ▼
                 Redis          PostgreSQL    Anthropic API     Groq API
               (debounce/     (histórico de    (Claude Haiku)  (Whisper —
               deduplicação)   conversas)                       áudio)
```

## Tecnologias

| Componente | Tecnologia |
|---|---|
| Runtime serverless | [Modal](https://modal.com/) |
| LLM | Anthropic Claude Haiku (`claude-haiku-4-5`) |
| Atendimento / CRM | [Chatwoot](https://www.chatwoot.com/) |
| Banco de dados | PostgreSQL (via `psycopg2`) |
| Cache / debounce | Redis |
| Transcrição de áudio | Groq Whisper v3 |
| HTTP client | `httpx` |

## Pré-requisitos

- Conta no [Modal](https://modal.com/) com CLI instalado (`pip install modal`)
- Secrets configurados no Modal (ver abaixo)
- Instância do Chatwoot com webhook apontando para o endpoint gerado pelo Modal

## Configuração de Secrets no Modal

### Secret: `marina-secrets`

| Variável | Descrição |
|---|---|
| `ANTHROPIC_API_KEY` | Chave da API da Anthropic |
| `CHATWOOT_URL` | URL base do Chatwoot (ex: `https://app.chatwoot.com`) |
| `CHATWOOT_TOKEN` | Token de acesso à API do Chatwoot |
| `CHATWOOT_USER_TOKEN` | Token de usuário admin (para atribuição de equipe/agente) |
| `CHATWOOT_ACCOUNT_ID` | ID da conta no Chatwoot (padrão: `1`) |
| `REDIS_URL` | URL de conexão Redis (ex: `redis://...`) |
| `POSTGRES_URL` | URL de conexão PostgreSQL |

### Secret: `groq-secrets`

| Variável | Descrição |
|---|---|
| `GROQ_API_KEY` | Chave da API da Groq (transcrição de áudio via Whisper) |

## Instalação e Deploy

```bash
# 1. Instale o Modal CLI
pip install modal

# 2. Autentique no Modal
modal setup

# 3. Faça o deploy
modal deploy ai_julia.py
```

O Modal retornará a URL do webhook. Configure-a no Chatwoot em **Configurações → Integrações → Webhook**.

## Como Funciona o Atendimento

1. Cliente envia mensagem no WhatsApp
2. Chatwoot recebe e dispara o webhook para o Modal
3. A Júlia coleta o nome e verifica se há advogado constituído
4. Qualifica o caso com perguntas SPIN (Situação → Problema → Implicação → Necessidade)
5. Se qualificado, atribui a equipe e o advogado responsável no Chatwoot e adiciona nota privada com resumo do caso
6. Solicita os documentos necessários antes da reunião
7. Encerra com aviso legal conforme ética da OAB

## Estrutura do Projeto

```
julia-assistente-juridica-main/
├── ai_julia.py          # Lógica completa: prompt, agente, integrações
├── setup_google_oauth.py
└── README.md
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
franciscogomes.com
franc2007vga@gmail.com

## Escritório

**Vasconcellos & Amadeo Advocacia**
Av. Nossa Sra. dos Navegantes, 755 — Sala 508 — Enseada do Suá, Vitória/ES
Segunda a sexta, das 9h às 18h
