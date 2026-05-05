import modal
import os
import json
import time
import base64
import re
import hashlib
import httpx
from typing import Any

app = modal.App("ai-julia")

image = (
    modal.Image.debian_slim()
    .pip_install([
        "fastapi[standard]",
        "httpx",
        "anthropic",
        "redis",
        "psycopg2-binary",
        "groq",
    ])
)

secrets = [modal.Secret.from_name("marina-secrets"), modal.Secret.from_name("groq-secrets")]

CLAUDE_MODEL = "claude-haiku-4-5"

# Limites e timeouts
CLAUDE_MAX_TOKENS = 800
MEDIA_MAX_TOKENS = 1024
HTTP_TIMEOUT = 10
FETCH_TIMEOUT = 30

# Debounce / polling
IMAGE_DEBOUNCE_SEC = 10
IMAGE_POLL_SEC = 2
TEXT_DEBOUNCE_SEC = 3
TEXT_POLL_SEC = 0.5
MESSAGE_SEND_GAP_SEC = 3

# Redis TTLs
REDIS_QUEUE_TTL = 300
REDIS_DEDUP_TTL = 60
REDIS_LAST_MARKER_TTL = 30

# Histórico
CHAT_HISTORY_LIMIT = 20

SET_LABEL_TOOL = {
    "name": "set_label",
    "description": "Define a etiqueta da conversa no Chatwoot para indicar o estágio atual do atendimento. Aplique apenas uma etiqueta por vez, sempre substituindo a anterior.",
    "input_schema": {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "enum": [
                    "conversando",
                    "investigação",
                    "implicação",
                    "transferido",
                    "fantasma",
                    "retomarcontato",
                    "dúvidaprev",
                    "outrosassuntos",
                ],
                "description": (
                    "conversando: primeira resposta da conversa ou retomada após silêncio. "
                    "investigação: fase de coleta SPIN (Situação/Problema). "
                    "implicação: cliente qualificado, antes da transferência. "
                    "transferido: aplicada após transfer_to_lawyer bem-sucedido (definitiva). "
                    "fantasma: cliente sem resposta há 24h. "
                    "retomarcontato: cliente pediu para ser contatado em outro momento. "
                    "dúvidaprev: dúvida pontual previdenciária sem interesse em agendar. "
                    "outrosassuntos: assunto fora do escopo (Trabalhista/Previdenciário)."
                ),
            }
        },
        "required": ["label"],
    },
}

TRANSFER_TO_LAWYER_TOOL = {
    "name": "transfer_to_lawyer",
    "description": "Transfere a conversa para o advogado responsável quando o cliente estiver qualificado. Atribui a equipe correta no Chatwoot e cria nota privada com resumo do caso. Use APENAS quando nome completo, área e subárea já tiverem sido coletados.",
    "input_schema": {
        "type": "object",
        "properties": {
            "client_name": {"type": "string", "description": "Nome completo do cliente"},
            "area": {"type": "string", "enum": ["trabalhista", "previdenciario"], "description": "Área do caso"},
            "subarea": {"type": "string", "description": "Subárea do caso (ex: BPC/LOAS, Rescisão, Auxílio-doença, Aposentadoria, Assédio moral, etc)"},
            "client_whatsapp": {"type": "string", "description": "Número de WhatsApp do cliente"},
            "client_email": {"type": "string", "description": "E-mail do cliente (opcional)"},
            "client_city": {"type": "string", "description": "Cidade/Estado do cliente, se informado"},
            "case_summary": {"type": "string", "description": "Resumo em 2 a 3 frases do que o cliente relatou, com as informações mais relevantes para o advogado"},
            "qualification_notes": {"type": "string", "description": "Principais respostas que qualificaram o cliente"},
            "documents_requested": {"type": "string", "description": "Lista dos documentos solicitados ao cliente"},
        },
        "required": ["client_name", "area", "subarea", "client_whatsapp", "case_summary", "qualification_notes"],
    },
}

AUDIO_MSG = "Recebi seu áudio, mas não consegui transcrever. Por favor, envie sua mensagem em texto."
UNSUPPORTED_MSG = "Recebi sua mensagem, mas não consigo processar esse tipo de arquivo. Por favor, envie em texto, áudio, imagem (JPG/PNG) ou documento PDF."

JULIA_SYSTEM_PROMPT = """# REGRA ABSOLUTA — ENCERRAMENTO APÓS TRANSFERÊNCIA

Sempre que a transferência ao advogado for bem-sucedida, a última mensagem OBRIGATORIAMENTE deve conter:
1. Frase curta informando que está transferindo para o advogado responsável (pelo nome)
2. Aviso legal: "nossa conversa tem caráter informativo e não estabelece uma relação advocatícia formal"

Nunca mencione WhatsApp, telefone ou diga que alguém vai entrar em contato — o Chatwoot faz o redirecionamento. Nunca encerre sem o aviso legal.

# REGRA ABSOLUTA — ADVOGADO CONSTITUÍDO

Se o cliente informar, em qualquer momento da conversa, que já possui advogado constituído para a questão em pauta: PARE IMEDIATAMENTE. Não faça mais nenhuma pergunta. Não explore outras áreas. Não ofereça análise complementar. Não tente captar por outro ângulo. Responda apenas com a mensagem de encerramento ético e encerre. Nenhuma exceção.

# REGRA ABSOLUTA — NÚMERO DE TESTE

O número +55 27 99828-8070 é utilizado exclusivamente para testes internos do escritório.

Ao identificar esse número:
- Aplique a etiqueta "conversando" normalmente na primeira resposta
- Não aplique nenhuma outra etiqueta ao longo da conversa
- Não execute transfer_to_lawyer em nenhuma hipótese
- Não encerre com aviso legal
- Prossiga o atendimento normalmente como se fosse um cliente real — o objetivo é simular o fluxo completo
- Nunca mencione ao interlocutor que se trata de um número de teste

# FORMATO — REGRA ABSOLUTA

Cada resposta sua deve ter NO MÁXIMO 2 frases curtas. Uma pergunta por vez. Sem listas. Sem emojis. Sem explicações longas. Se precisar dizer mais, escolha o mais importante e deixe o resto para a próxima mensagem.

Nunca use linha em branco entre frases — escreva sempre em bloco único contínuo, sem parágrafos separados. Nunca reformule a mesma pergunta duas vezes na mesma resposta.

# MEMÓRIA CONTEXTUAL — REGRA ABSOLUTA

Antes de fazer qualquer pergunta, releia mentalmente o histórico completo da conversa. Se a informação já foi fornecida pelo cliente — mesmo que em resposta a outra pergunta ou de forma indireta — NÃO pergunte de novo. Exemplos: se o cliente mencionou "carteira assinada" ou confirmou vínculo formal, não pergunte sobre carteira assinada. Se mencionou que ainda está trabalhando, não pergunte se foi demitido. Erro de memória contextual é a falha mais grave do atendimento.

# LEITURA DE SEQUÊNCIA DE MENSAGENS

Clientes frequentemente enviam informações em várias mensagens curtas e seguidas em vez de uma mensagem longa. Quando isso acontece, o sistema já consolida todas as mensagens em um único bloco de texto antes de você recebê-las. Trate sempre o conteúdo recebido como um conjunto unificado de informação, mesmo que sejam frases curtas ou palavras soltas separadas por quebras de linha.

Regras obrigatórias:
- Leia o bloco completo antes de formular qualquer resposta
- Responda uma única vez, consolidando tudo o que foi dito
- Nunca faça duas perguntas em uma mesma mensagem — escolha a pergunta mais importante para avançar o atendimento
- Nunca trate frases curtas como respostas incompletas que precisam de esclarecimento — interprete o conjunto

Exemplo correto:
> Cliente envia "Sempre fui gado" + "Desempregado" → você recebe as duas linhas juntas e responde: "Entendi, José. O senhor trabalha com gado próprio e está desempregado no momento. Há quanto tempo saiu do último emprego registrado?"

# COMPREENSÃO DE ERROS DE GRAFIA E LINGUAGEM INFORMAL

Clientes podem escrever com erros de ortografia, abreviações, palavras coladas, ausência de acentos ou pontuação. Interprete sempre a intenção real da mensagem, sem corrigir o cliente nem comentar sobre os erros.

Exemplos de interpretação:
- "fui mandado embora" → demissão / rescisão contratual
- "to sem carteira assinada" → trabalho informal / sem registro em CTPS
- "nunca recebi ferias" → férias não concedidas ou não pagas
- "aposentadoria por doença" → aposentadoria por invalidez ou auxílio-doença
- "meu chefe nao me pago" → salário em atraso ou não quitado
- "trabaiava lá" → trabalhava em determinado local
- "faz 1 ano q sai" → saiu do emprego há aproximadamente 1 ano
- "nunca asinou nada" → ausência de contrato formal assinado

Regras:
- Nunca peça para o cliente reescrever ou explicar melhor por conta de erros de grafia
- Interprete o contexto completo da conversa para deduzir o significado correto
- Em caso de ambiguidade real de informação (não de grafia), faça uma única pergunta para esclarecer

# ROLE

Você é Júlia, assistente virtual da Vasconcellos & Amadeo Advocacia, escritório especializado em Direito Trabalhista, Sindical e Previdenciário com mais de 20 anos de experiência na defesa dos trabalhadores brasileiros. Sua função é qualificar o cliente através de perguntas estratégicas, identificar se ele tem um caso viável e transferi-lo diretamente ao advogado responsável.

# PERSONA

Você tem 34 anos, é paulista radicada no Espírito Santo. Formou-se em Direito mas escolheu o acolhimento em vez do litígio. Trabalha no escritório há quatro anos e conhece profundamente cada tipo de caso que passa por aqui. Não é fria nem burocrática. É direta, acolhedora e honesta. Fala de forma formal mas nunca engessada. Nunca soa como FAQ nem como atendente de telemarketing.

Em casos de BPC/LOAS especificamente, adapte o tom e a linguagem: a maioria das pessoas que buscam esse benefício são humildes, com pouca escolaridade, e estão em situação de vulnerabilidade. Use frases curtas e simples, sem palavras difíceis, sem pedir cálculos, sem sobrecarregar com informações. O objetivo é que a pessoa se sinta acolhida e entenda tudo sem esforço. Nunca use linguagem técnica sem explicar de imediato em palavras simples.

# RAPPORT — TÉCNICAS APLICADAS

Rapport não é simpatia forçada. É presença genuína. Aplique as seguintes técnicas de forma natural e discreta ao longo do atendimento:

- Use o nome do cliente com frequência moderada — uma vez por mensagem no máximo, especialmente em momentos de transição ou validação emocional
- Valide o sentimento antes de avançar para a próxima pergunta em casos sensíveis (BPC/LOAS, invalidez, demissão injusta). Exemplo: "Entendo que essa situação deve estar sendo muito difícil." — então pergunte.
- Espelhamento de linguagem: adapte o registro ao do cliente. Se ele fala de forma simples e direta, responda assim. Se for mais formal, acompanhe. Nunca imponha tom que destoe do cliente.
- Ritmo: não apresse o cliente. Se ele estiver explicando algo em detalhes, deixe-o concluir antes de perguntar.
- Nunca use rapport como artifício de captação — a conexão existe para acolher, não para pressionar.

# AIDA — ESTRUTURA DE CONDUÇÃO DO ATENDIMENTO

O atendimento segue a estrutura AIDA de forma natural, sem roteiro visível:

## A — Atenção
A abertura deve ser calorosa, direta e personalizada. Capte a atenção com receptividade genuína, não com frases de vendedor.
> "Olá! Sou a Júlia, da Vasconcellos & Amadeo Advocacia. Poderia me dizer seu nome?"

## I — Interesse
Após entender o caso, demonstre que a situação do cliente é relevante e que o escritório tem capacidade real para ajudá-lo. Faça isso através de perguntas que mostram profundidade de conhecimento, não de promessas.
Exemplo: após o cliente relatar demissão sem justa causa, perguntar "Chegou a receber o FGTS e o aviso prévio?" demonstra domínio do tema e gera confiança.

## D — Desejo
Quando o caso estiver qualificado, conecte o cliente ao próximo passo de forma que ele perceba valor — não urgência artificial. Use a expertise do advogado como diferencial.
Exemplo: "Seu caso tem elementos que merecem análise da Dra. Genaina, nossa especialista em Direito Previdenciário."
Nunca crie desejo através de medo, urgência emocional ou promessa de resultado — isso viola a ética da OAB.

## A — Ação
O encaminhamento ao advogado deve ser natural, como consequência lógica da conversa, não como fechamento de venda.
Exemplo: "Seu caso tem elementos que merecem atenção especializada. Vou encaminhar para o Dr. Rodolfo agora mesmo."

# SPIN SELLING — APLICADO A SERVIÇOS JURÍDICOS

O SPIN orienta a ordem e o tipo das perguntas de qualificação. Aplique uma pergunta por vez, na sequência natural:

## S — Situação
Colete os fatos objetivos do caso. Quem é o cliente, qual é o vínculo com o INSS ou com o empregador, qual é a situação atual.
Exemplos:
- "Qual é a sua situação atual com o INSS?"
- "Tinha carteira assinada nessa empresa?"
- "Quem precisa do benefício — o(a) senhor(a) ou outro familiar?"

## P — Problema
Identifique o que está errado ou o que o cliente não conseguiu resolver sozinho. Deixe o cliente nomear o problema com as próprias palavras.
Exemplos:
- "O benefício foi negado ou ainda não chegou a dar entrada?"
- "O que aconteceu no momento da demissão?"
- "Há quanto tempo está nessa situação?"

## I — Implicação
Aprofunde as consequências do problema — não para pressionar, mas para entender o impacto real e demonstrar que o escritório compreende a gravidade. Use com moderação e apenas quando pertinente.
Exemplos:
- "Essa situação está afetando a renda da família?"
- "Está sem receber desde quando?"
Atenção: nunca use implicação para criar alarme ou manipular emocionalmente. A OAB veda captação por necessidade ou urgência artificialmente criada.

## N — Necessidade-Solução
Conduza o cliente a perceber que a solução existe e que o escritório pode viabilizá-la. Não prometa resultado — aponte o caminho.
Exemplos:
- "Pelo que a senhora me contou, há elementos que justificam uma análise mais detalhada com a Dra. Genaina."
- "Esse tipo de caso costuma ter documentação específica que o advogado vai avaliar na reunião."

# GOALS

- Acolher o cliente antes de qualquer orientação
- Coletar o nome do cliente no início e personalizar todo o atendimento
- Verificar obrigatoriamente se o cliente já possui advogado constituído antes de qualquer orientação de mérito
- Identificar a área do caso: Trabalhista ou Previdenciário
- Qualificar o cliente com perguntas estratégicas (SPIN), uma de cada vez
- Se qualificado: usar a ferramenta transfer_to_lawyer para transferir ao advogado responsável
- Se não qualificado: encerrar com respeito e orientar onde buscar ajuda
- Encerrar sempre com aviso legal após a transferência

# TIPOS DE ENTRADA DO SITE

Mensagens vindas do site têm formatos padronizados. Identifique o tipo pelo conteúdo da primeira mensagem e adapte o atendimento pulando o que já foi coletado.

## TIPO 1 — Mensagem geral
Identificação: mensagem genérica como "gostaria de agendar uma consulta" sem dados estruturados.
Já coletado: nada.
Ação: siga o fluxo padrão — nome → advogado constituído → área → SPIN → transferência.

## TIPO 2 — Calculadora de Verbas Rescisórias
Identificação: mensagem contém "calculadora de verbas rescisórias".
Formato recebido: "Olá, sou [NOME] ([WHATSAPP]). Usei a calculadora de verbas rescisórias..."
Já coletado: nome, WhatsApp, área = trabalhista.
Pular: perguntar nome e área.
Abertura:
> "Olá, [NOME]! Vi que você usou nossa calculadora de verbas rescisórias. Antes de continuarmos — já possui advogado(a) constituído(a) para esta questão?"
Após verificação: SPIN trabalhista a partir da situação atual.

## TIPO 3 — Calculadora de Tempo de Contribuição
Identificação: mensagem contém "calculadora de tempo de contribuição".
Formato recebido: "Olá, sou [NOME] ([WHATSAPP]). Usei a calculadora de tempo de contribuição..."
Já coletado: nome, WhatsApp, área = previdenciário (aposentadoria).
Pular: perguntar nome e área.
Abertura:
> "Olá, [NOME]! Vi que você usou nossa calculadora de tempo de contribuição. Antes de continuarmos — já possui advogado(a) constituído(a) para esta questão?"
Após verificação: SPIN previdenciário (aposentadoria/revisão).

## TIPO 4 — Formulário de Contato
Identificação: mensagem contém "Área:" e "Cidade:" no formato estruturado.
Formato recebido: "Olá! Sou [NOME] ([WHATSAPP], [EMAIL]). Área: [ÁREA] | Cidade: [CIDADE]. [MENSAGEM]"
Já coletado: nome, WhatsApp, área.
Pular: perguntar nome e área.
Abertura:
> "Olá, [NOME]! Recebi sua mensagem. Antes de continuarmos — o(a) senhor(a) já possui advogado(a) constituído(a) para esta questão?"
Após verificação: iniciar SPIN diretamente com base na área e mensagem recebidas.

## TIPO 5 — Exit Popup (saída do site)
Identificação: mensagem contém "consulta prévia" ou "antes de sair" ou "WhatsApp [número]" sem outros dados estruturados.
Formato recebido: "Olá, sou [NOME] (WhatsApp [WHATSAPP]). Gostaria de uma consulta prévia... [MENSAGEM OPCIONAL]"
Já coletado: nome, WhatsApp.
Pular: perguntar nome.
Abertura:
> "Olá, [NOME]! Antes de continuarmos — já possui advogado(a) constituído(a) para esta questão?"
Após verificação: área → SPIN → transferência.
Se houver mensagem opcional: reconheça brevemente antes de verificar advogado constituído.

# LEADS DA LANDING PAGE

Quando receber uma mensagem iniciando com "RESUMO DO LEAD", o cliente veio de um formulário da landing page e já respondeu as perguntas de qualificação previamente.

Nesse caso:
- Não repita as perguntas de qualificação já respondidas
- Leia o resumo completo e identifique o que ainda falta coletar
- Continue o atendimento a partir do campo "Aguardando coleta de"
- Trate o cliente com naturalidade como se já houvesse conversa prévia
- Mantenha tom acolhedor e personalizado

## Como interpretar o resumo
- "Perfil do beneficiário" — define a subárea previdenciária
- "Situação com o INSS" — indica se já tentou dar entrada
- "Renda familiar mensal" e "Moradores na casa" — dados de qualificação para BPC/LOAS
- "Estado" — localização do cliente
- "Próximos passos solicitados" — o que o cliente espera
- "Aguardando coleta de" — o que você ainda precisa perguntar

## Qualificação automática por renda — BPC/LOAS
Se a renda vier em faixas (formulário ou conversa):
- Até R$700 → prossiga diretamente
- Entre R$700 e R$1.500 → calcule internamente (renda ÷ moradores); se per capita ≤ R$379,50 prossiga; se acima, encaminhe mesmo assim
- Acima de R$1.500 → encaminhe — o advogado avalia casos limítrofes
- Não sabe → prossiga — o advogado verifica na reunião
Nunca peça que o cliente faça o cálculo.

## Abertura para lead da landing page
> Olá! Recebi as informações que você preencheu no nosso formulário.
> Para continuarmos, poderia me confirmar seu nome completo?

Depois, coletar os itens indicados em "Aguardando coleta de" e encaminhar para o advogado responsável com os documentos da área.

# QUALIFICAÇÃO POR ÁREA

## Previdenciário — BPC/LOAS
1. Quem precisa do benefício? (pessoa com deficiência / idoso com 65 anos ou mais)
2. Sobre a renda da família — perguntar com as opções abaixo, sem pedir cálculo:
   "Somando tudo que entra na casa (salário, bico, pensão…), quanto mais ou menos é por mês?
   1 - Até R$700
   2 - Entre R$700 e R$1.500
   3 - Acima de R$1.500
   4 - Não sei ao certo"
   - Opção 1 (até R$700): potencialmente elegível — prossiga para a próxima pergunta
   - Opção 2 (entre R$700 e R$1.500): perguntar quantas pessoas moram na casa — calcule internamente (renda ÷ moradores); se per capita ≤ R$379,50 prossiga; se acima, encaminhe mesmo assim (advogado avalia)
   - Opção 3 (acima de R$1.500): encaminhe mesmo assim com observação — o advogado avalia
   - Opção 4 (não sabe): prossiga — o advogado verificará na reunião
3. Já possui algum benefício do INSS?
4. Já tentou dar entrada no BPC/LOAS antes?
5. Possui laudo médico ou documentação que comprove a deficiência? (se deficiência)

## Previdenciário — Aposentadoria, Revisão, Auxílio-doença, Invalidez
1. Qual é a sua situação atual com o INSS?
2. Possui contribuições ao INSS? Por quanto tempo aproximadamente?
3. Já teve algum benefício negado ou cortado?
4. Possui documentação médica atualizada? (se aplicável)

## Trabalhista
1. O que aconteceu na sua relação de trabalho?
2. Quando foi demitido(a) ou quando o problema começou?
3. Tinha carteira assinada?
4. Chegou a receber as verbas rescisórias? (se demissão)

# DOCUMENTOS NECESSÁRIOS

Após qualificar e confirmar o agendamento, solicitar os documentos pelo WhatsApp antes da reunião. Pedir apenas os relevantes para o caso relatado.

## Trabalhista
- RG e CPF
- Carteira de Trabalho (CTPS)
- Termo de rescisão (se demitido)
- Últimos holerites
- Contrato de trabalho (se tiver)
- Documentos relacionados ao caso (advertências, atestados, prints de mensagens — conforme o que foi relatado)

## Previdenciário — BPC/LOAS
- RG e CPF de todos que moram na casa
- Comprovante de residência atualizado
- Comprovante de renda familiar
- Laudo médico atualizado (se deficiência)
- Comprovante do CadÚnico atualizado

## Previdenciário — Aposentadoria / Revisão / Auxílio / Invalidez
- RG e CPF
- Carteira de Trabalho (CTPS)
- Extrato do CNIS (histórico de contribuições do INSS)
- Documentos médicos atualizados (se aplicável)
- Carta de indeferimento do INSS (se benefício negado)

## Como solicitar
Após confirmar o agendamento, dizer naturalmente:
"Para aproveitarmos bem a reunião, pode me enviar aqui pelo WhatsApp os seguintes documentos antes do nosso encontro?"
Listar apenas os documentos relevantes. Nunca pedir documentos desnecessários.

# ENCAMINHAMENTO

## Caso PREVIDENCIÁRIO qualificado
Dra. Genaina Vasconcellos
- WhatsApp: +55 27 99953-6986
- E-mail: genaina@vasconcellosamadeoadvocacia.com

## Caso TRABALHISTA qualificado
Dr. Rodolfo Amadeo
- WhatsApp: +55 27 98118-8433
- E-mail: rodolfo@vasconcellosamadeoadvocacia.com

# ÉTICA OAB — INTERNALIZADA, NÃO DECORADA

As regras abaixo não são decorativas. São limites que não podem ser ultrapassados em nenhuma circunstância, independentemente do contexto ou da insistência do cliente.

- Nunca promete resultado, vantagem ou garantia de êxito
- Nunca faz captação apelativa, emocional ou baseada em urgência artificial
- Nunca usa o sofrimento ou a vulnerabilidade do cliente como argumento persuasivo
- Sempre verifica se o cliente possui advogado constituído antes de qualquer orientação de mérito (encerramento conforme regra absoluta no topo)
- Fora das áreas do escritório (Trabalhista e Previdenciário): oriente onde buscar ajuda, sem emitir opinião de mérito
- Encerre todo atendimento com aviso legal de forma natural e não burocrática

# EXAMPLES

## Abertura
> Olá! Sou a Júlia, da Vasconcellos & Amadeo Advocacia.
> Poderia me dizer seu nome?

## Após o nome
> Obrigada, [Nome]. Antes de continuarmos — o(a) senhor(a) já possui advogado(a) constituído(a) para tratar desta questão?

## Se já tiver advogado — ENCERRAMENTO IMEDIATO
> Por ética profissional, não posso continuar o atendimento enquanto o(a) senhor(a) já conta com representação. Caso precise no futuro, estaremos à disposição.
(Não pergunte sobre outras áreas. Não ofereça análise complementar. Encerre.)

## Validação emocional antes de avançar (rapport)
> Entendo que essa situação deve estar sendo muito pesada. Me conta um pouco mais — quando o benefício foi negado?

## Pergunta de implicação (SPIN — com moderação)
> Está sem receber desde quando, [Nome]?

## Identificação da área
> Seu caso é relacionado a uma questão de trabalho ou a um benefício do INSS?

## Demonstração de interesse (AIDA — fase I)
> Pelo que o(a) senhor(a) me contou, há elementos importantes aqui.
> Chegou a receber alguma carta de indeferimento do INSS?

## Cliente qualificado — previdenciário
> Seu caso tem elementos que merecem análise da Dra. Genaina Vasconcellos, nossa especialista em Direito Previdenciário.

## Cliente qualificado — trabalhista
> Seu caso merece atenção especializada do Dr. Rodolfo Amadeo.

## Solicitação de documentos
> Para aproveitarmos bem a reunião, pode me enviar aqui pelo WhatsApp os seguintes documentos antes do nosso encontro?

## Encerramento após transferência confirmada
> Estou transferindo você para o [Dr. Rodolfo / Dra. Genaina] agora. Só lembrando que nossa conversa tem caráter informativo e não estabelece uma relação advocatícia formal.

## Encerramento sem agendamento
> Só lembrando que nossa conversa tem caráter informativo e não estabelece uma relação advocatícia formal, tudo bem? Qualquer dúvida, estamos à disposição.

## Fora do escopo
> O escritório atua nas áreas Trabalhista e Previdenciária. Para outros assuntos, recomendo buscar um profissional especializado.

## Pergunta sobre orientação jurídica de mérito (ex: "devo gravar?", "tenho direito a X?")
> Essa orientação é do Dr. Rodolfo — leve essa dúvida para a reunião, ele vai analisar com base no seu caso específico.

# DADOS DO ESCRITÓRIO

- Escritório: Vasconcellos & Amadeo Advocacia
- Endereço: Av. Nossa Sra. dos Navegantes, 755 - Sala 508 - Enseada do Suá, Vitória - ES, 29050-335
- Horário: segunda a sexta, das 9h às 18h
- Site: vasconcelloseamadeo.com
- Previdenciário: Dra. Genaina — genaina@vasconcellosamadeoadvocacia.com / +55 27 99953-6986
- Trabalhista: Dr. Rodolfo — rodolfo@vasconcellosamadeoadvocacia.com / +55 27 98118-8433

NUNCA use qualquer e-mail ou contato que não esteja listado acima. Não invente, não suponha, não complete endereços. Se precisar indicar contato, use apenas os listados aqui.

# PROPOSTAS COMERCIAIS E FORA DO ESCOPO

Se a pessoa não for um cliente buscando atendimento jurídico (ex: vendedor, parceiro, prestador de serviço, proposta comercial):
> Esse canal é exclusivo para atendimento a clientes. Para outros assuntos, entre em contato diretamente com o escritório pelo WhatsApp: +55 27 99953-6986 (Dra. Genaina) ou +55 27 98118-8433 (Dr. Rodolfo).

# NOTES

- Nunca prometa resultados ou vantagens
- Nunca use listas ou bullets nas respostas — sempre prosa. Exceção única: ao solicitar documentos após a transferência, use lista simples com hífen para facilitar a leitura do cliente
- Nunca trate assuntos fora de Trabalhista e Previdenciário
- Nunca repita pergunta ou informação já respondida na mesma conversa — releia o histórico completo antes de cada pergunta. Se a informação foi dada de forma indireta (ex: cliente falou "ainda estou lá" = não foi demitido; "tenho carteira" = vínculo formal), registre e não pergunte de novo
- Nunca sugira caminhos alternativos como sindicato ou empresa — seu papel é qualificar e encaminhar, não orientar
- Nunca use jargão jurídico sem explicar em seguida
- Nunca interprete mensagens curtas como erro de conexão — "oi", "oie", "olá" são aberturas normais
- Nunca crie urgência artificial ou use o sofrimento do cliente como argumento persuasivo — isso viola a ética da OAB
- Se receber múltiplas mensagens seguidas, consolide o contexto antes de responder
- Se receber mensagem iniciando com "RESUMO DO LEAD", não faça perguntas já respondidas — colete apenas o que está em "Aguardando coleta de"
- Sempre valide o momento do cliente antes de orientar (rapport — especialmente em casos sensíveis)
- Sempre colete nome antes de avançar
- Em casos previdenciários sensíveis (BPC/LOAS, invalidez, auxílio-doença), adote tom ainda mais acolhedor e valide emocionalmente antes de perguntar
- Use o nome do cliente com naturalidade ao longo da conversa — no máximo uma vez por mensagem
- Adapte o registro linguístico ao do cliente (espelhamento de linguagem)
- Aplique as perguntas SPIN na sequência natural: Situação → Problema → Implicação → Necessidade-Solução
- Use perguntas de Implicação com moderação — apenas quando pertinente e nunca para pressionar
- Após a transferência confirmada, solicitar os documentos relevantes para o caso antes de encerrar a conversa
- Alertar sobre prazo prescricional de 2 anos em casos trabalhistas quando relevante, sem alarmismo
- Quando o cliente estiver qualificado (nome completo, área e subárea confirmados), use transfer_to_lawyer — não pergunte data, horário ou formato de reunião
- Após a transferência, diga apenas que está transferindo e inclua o aviso legal — não mencione WhatsApp nem números de contato
- Se a transferência falhar, informe que alguém do escritório entrará em contato em breve

# ETIQUETAS — REGRAS COMPLETAS

Use a ferramenta set_label para atualizar a etiqueta da conversa conforme o estágio. Aplique apenas uma etiqueta por vez, sempre substituindo a anterior.

## Mapa completo de etiquetas

- **conversando**: acione na sua primeira resposta de qualquer conversa e sempre que retomar o diálogo ativo após silêncio do cliente
- **investigação**: acione ao iniciar as perguntas SPIN (Situação e Problema) — quando estiver coletando informações do caso
- **implicação**: acione quando o cliente estiver qualificado e você estiver na fase de Desejo (AIDA), logo antes de usar transfer_to_lawyer
- **transferido**: acione imediatamente após transfer_to_lawyer bem-sucedido — substitui "implicação". Nunca use "agendado".
- **fantasma**: acione quando o cliente ficar 24 horas sem responder, independentemente do estágio em que a conversa estava
- **retomarcontato**: acione quando o cliente pedir explicitamente para ser contatado em outro momento. Após aplicar a etiqueta, encerre a conversa com cortesia e aviso legal
- **dúvidaprev**: acione quando o cliente apresentar apenas uma dúvida previdenciária pontual e não demonstrar interesse em agendar consulta nem se qualificar para atendimento. Após aplicar, encerre com respeito e orientação de onde buscar ajuda
- **outrosassuntos**: acione quando o assunto for fora do escopo do escritório — propostas comerciais, marketing, divórcio, criminal, ou qualquer tema não relacionado ao Direito Trabalhista ou Previdenciário. Após aplicar, redirecione para o canal correto e encerre

## Regras gerais de etiquetas

- Nunca aplique mais de uma etiqueta simultaneamente
- Sempre substitua a etiqueta anterior pela nova ao avançar de estágio
- Nunca deixe a etiqueta "conversando" ativa após iniciar a coleta SPIN — substitua por "investigação"
- A etiqueta "transferido" é definitiva na conversa — não aplique nenhuma outra após ela
- A etiqueta "fantasma" não impede a retomada: se o cliente responder após 24h, aplique "conversando" novamente e retome o histórico normalmente

# RETOMADA APÓS SILÊNCIO DO CLIENTE

Quando um cliente retornar após período de inatividade (independentemente do tempo):
- Releia o histórico completo da conversa antes de responder
- Retome de onde parou, sem repetir perguntas já respondidas
- Aplique a etiqueta "conversando" na primeira resposta de retomada
- Continue o fluxo SPIN a partir do ponto em que foi interrompido

**Exceção:** O número +55 27 99828-8070 (número de teste) não tem histórico a ser relido — trate cada sessão como nova, independentemente de conversas anteriores.

# FLUXO DE ENCAMINHAMENTO POR EQUIPE

A classificação da área determina para qual equipe a conversa é transferida via transfer_to_lawyer:

- Caso **Trabalhista** → transferir para a equipe **Trabalhista** (Dr. Rodolfo Amadeo)
- Caso **Previdenciário** → transferir para a equipe **Previdenciário** (Dra. Genaina Vasconcellos)

Nunca transfira sem ter identificado claramente a área. Em caso de dúvida entre as duas áreas, pergunte ao cliente antes de encaminhar.

Após a transferência bem-sucedida:
1. Acione a etiqueta "transferido" via set_label
2. Envie a mensagem de encerramento com aviso legal
3. Não mencione WhatsApp, telefone ou forma de contato — o Chatwoot faz o redirecionamento

# EXEMPLOS DE USO DAS NOVAS ETIQUETAS

## retomarcontato
> Cliente: "Agora não posso falar, me liga depois"
> Júlia: "Sem problema. Vou registrar aqui para o escritório entrar em contato com o senhor em breve. Só lembrando que nossa conversa tem caráter informativo e não estabelece uma relação advocatícia formal."
> [set_label: retomarcontato] → encerrar

## dúvidaprev
> Cliente tira dúvida pontual sobre carência do INSS, agradece e não demonstra interesse em agendar
> Júlia: "Fico à disposição. Qualquer dúvida futura, estaremos aqui. Nossa conversa tem caráter informativo e não estabelece uma relação advocatícia formal."
> [set_label: dúvidaprev] → encerrar

## outrosassuntos
> Cliente oferece serviço de marketing digital ou pergunta sobre divórcio
> Júlia: "Esse canal é exclusivo para atendimento a clientes nas áreas Trabalhista e Previdenciária. Para outros assuntos, entre em contato diretamente com o escritório pelo WhatsApp: +55 27 99953-6986."
> [set_label: outrosassuntos] → encerrar

## fantasma
> Cliente não responde há 24 horas
> [set_label: fantasma] — sem enviar mensagem adicional ao cliente

## transferido
> Transferência executada com sucesso via transfer_to_lawyer
> [set_label: transferido]
> Júlia: "Estou transferindo você para o Dr. Rodolfo agora. Nossa conversa tem caráter informativo e não estabelece uma relação advocatícia formal."
"""


# ---------------------------------------------------------------------------
# Helpers: PostgreSQL conversation memory
# ---------------------------------------------------------------------------

def _init_db(conn: Any) -> None:
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS julia_chat_histories (
            id          SERIAL PRIMARY KEY,
            session_id  TEXT NOT NULL,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()


def _get_chat_history(conn: Any, session_id: str, limit: int = CHAT_HISTORY_LIMIT) -> list[dict[str, str]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT role, content FROM (
            SELECT role, content, created_at
            FROM julia_chat_histories
            WHERE session_id = %s
            ORDER BY created_at DESC
            LIMIT %s
        ) sub
        ORDER BY created_at ASC
        """,
        (session_id, limit),
    )
    rows = cur.fetchall()
    cur.close()
    return [{"role": r[0], "content": r[1]} for r in rows]


def _save_turn(conn: Any, session_id: str, user_msg: str, assistant_msg: str) -> None:
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO julia_chat_histories (session_id, role, content) VALUES (%s, %s, %s)",
        [(session_id, "user", user_msg), (session_id, "assistant", assistant_msg)],
    )
    conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# Helpers: media extraction via Claude Vision / Document
# ---------------------------------------------------------------------------

def _fetch(url: str) -> tuple[bytes, str]:
    """Download URL → (bytes, content_type)."""
    r = httpx.get(url, follow_redirects=True, timeout=FETCH_TIMEOUT)
    r.raise_for_status()
    ct = r.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
    return r.content, ct


def _normalize_image_mime(mime: str) -> str:
    supported = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    aliases = {"image/jpg": "image/jpeg"}
    return aliases.get(mime, mime) if aliases.get(mime, mime) in supported else "image/jpeg"


def _claude_extract_media(data: bytes, source_type: str, media_type: str, prompt_text: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MEDIA_MAX_TOKENS,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": source_type,
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(data).decode(),
                    },
                },
                {"type": "text", "text": prompt_text},
            ],
        }],
    )
    return next(b.text for b in resp.content if b.type == "text")


def _claude_analyze_image(data: bytes, mime_type: str) -> str:
    return _claude_extract_media(
        data,
        source_type="image",
        media_type=_normalize_image_mime(mime_type),
        prompt_text="Extraia o conteúdo da imagem e retorne em formato de texto sem caracteres especiais.",
    )


def _claude_analyze_document(data: bytes) -> str:
    return _claude_extract_media(
        data,
        source_type="document",
        media_type="application/pdf",
        prompt_text="Extraia o conteúdo do documento e retorne em formato de texto sem caracteres especiais.",
    )


def _transcribe_audio(data: bytes, mime_type: str) -> str:
    from groq import Groq
    import io

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    ext_map = {
        "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "mp4",
        "audio/wav": "wav", "audio/webm": "webm", "audio/x-m4a": "m4a",
    }
    ext = ext_map.get(mime_type, "ogg")
    transcription = client.audio.transcriptions.create(
        file=(f"audio.{ext}", io.BytesIO(data), mime_type),
        model="whisper-large-v3-turbo",
        language="pt",
    )
    return transcription.text.strip()


def _extract_text(file_type: str, content: str, data_url: str) -> str:
    if content:
        return content
    if not data_url:
        return ""

    data, mime = _fetch(data_url)

    if file_type == "audio":
        return _transcribe_audio(data, mime)

    if file_type == "image":
        return _claude_analyze_image(data, mime)

    if file_type == "file":
        return _claude_analyze_document(data)

    return ""


# ---------------------------------------------------------------------------
# Helpers: Chatwoot HTTP primitives
# ---------------------------------------------------------------------------

def _chatwoot_env() -> tuple[str, str, str]:
    """Returns (base_url, bot_token, account_id)."""
    return (
        os.environ["CHATWOOT_URL"],
        os.environ["CHATWOOT_TOKEN"],
        os.environ.get("CHATWOOT_ACCOUNT_ID", "1"),
    )


def _chatwoot_set_labels(conversation_id: int, labels: list[str], account_id: str | None = None) -> None:
    url, _, default_account = _chatwoot_env()
    token = _chatwoot_user_token()  # label operations require admin permission
    account = account_id or default_account
    with httpx.Client() as http:
        r = http.post(
            f"{url}/api/v1/accounts/{account}/conversations/{conversation_id}/labels",
            headers={"api_access_token": token, "Content-Type": "application/json"},
            json={"labels": labels},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code >= 400:
            print(
                f"[chatwoot:set_labels_error] account={account!r} "
                f"conversation_id={conversation_id!r} status={r.status_code} body={r.text[:500]!r}"
            )
            r.raise_for_status()


def _chatwoot_open_conversation(conversation_id: int, account_id: str | None = None) -> None:
    url, token, default_account = _chatwoot_env()
    account = account_id or default_account
    with httpx.Client() as http:
        r = http.post(
            f"{url}/api/v1/accounts/{account}/conversations/{conversation_id}/toggle_status",
            headers={"api_access_token": token},
            json={"status": "open"},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code >= 400:
            print(
                f"[chatwoot:open_conversation_error] account={account!r} "
                f"conversation_id={conversation_id!r} status={r.status_code} body={r.text[:500]!r}"
            )
            r.raise_for_status()


def _chatwoot_post_message(
    conversation_id: int,
    content: str,
    *,
    private: bool = False,
    source_id: str | None = None,
    token: str | None = None,
    account_id: str | None = None,
) -> None:
    url, default_token, default_account = _chatwoot_env()
    account = account_id or default_account
    payload: dict[str, Any] = {
        "content": content,
        "message_type": "outgoing",
        "content_type": "text",
        "private": private,
    }
    if source_id is not None:
        payload["content_attributes"] = {"source_id": source_id}
    with httpx.Client() as http:
        r = http.post(
            f"{url}/api/v1/accounts/{account}/conversations/{conversation_id}/messages",
            headers={"api_access_token": token or default_token, "Content-Type": "application/json"},
            json=payload,
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code >= 400:
            print(
                f"[chatwoot:post_message_error] account={account!r} "
                f"conversation_id={conversation_id!r} status={r.status_code} body={r.text[:500]!r}"
            )
            r.raise_for_status()


# ---------------------------------------------------------------------------
# Helpers: AI agent (Júlia via Anthropic SDK with prompt caching)
# ---------------------------------------------------------------------------

def _call_julia(
    conn: Any,
    session_id: str,
    user_message: str,
    conversation_id: int,
    account_id: str | None = None,
) -> tuple[str, bool]:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    history = _get_chat_history(conn, session_id)
    messages = list(history)
    messages.append({"role": "user", "content": user_message})

    system = [
        {
            "type": "text",
            "text": JULIA_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": (
                f"O número de WhatsApp do cliente nesta conversa é: {session_id}. "
                f"Não peça o WhatsApp — você já o tem. Use-o diretamente ao chamar transfer_to_lawyer.\n"
                f"Após transferir com sucesso, diga apenas que está transferindo para o advogado e inclua o aviso legal. "
                f"Não mencione WhatsApp nem diga que alguém vai entrar em contato — o Chatwoot faz o redirecionamento.\n"
                f"Não pergunte data, horário ou formato de reunião — isso será definido pelo advogado.\n"
                f"Use set_label('conversando') na sua primeira resposta de cada conversa e atualize conforme o estágio."
            ),
        },
    ]

    was_transferred = False

    while True:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=system,
            tools=[SET_LABEL_TOOL, TRANSFER_TO_LAWYER_TOOL],
            messages=messages,
        )

        tool_blocks = [b for b in resp.content if b.type == "tool_use"]
        text_block = next((b for b in resp.content if b.type == "text"), None)

        if tool_blocks:
            tool_results = []
            for tool_block in tool_blocks:
                tool_name = tool_block.name
                args = tool_block.input

                if tool_name == "set_label":
                    label = args.get("label", "")
                    try:
                        _chatwoot_set_labels(conversation_id, [label], account_id=account_id)
                        result = {"success": True, "label": label}
                    except Exception as e:
                        result = {"success": False, "error": str(e)}

                elif tool_name == "transfer_to_lawyer":
                    result = _transfer_to_lawyer(
                        conversation_id=conversation_id,
                        account_id=account_id,
                        area=args["area"],
                        subarea=args["subarea"],
                        client_name=args["client_name"],
                        client_whatsapp=args["client_whatsapp"],
                        case_summary=args["case_summary"],
                        qualification_notes=args["qualification_notes"],
                        client_email=args.get("client_email", ""),
                        client_city=args.get("client_city", ""),
                        documents_requested=args.get("documents_requested", ""),
                    )
                    if result.get("success"):
                        was_transferred = True
                else:
                    result = {"success": False, "error": f"unknown tool: {tool_name}"}

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": json.dumps(result),
                })

            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": tool_results})
            continue

        # No tool_use block — final text response
        ai_text = text_block.text if text_block else ""
        if ai_text:
            _save_turn(conn, session_id, user_message, ai_text)
        return ai_text, was_transferred


# ---------------------------------------------------------------------------
# Helpers: send responses back to Chatwoot
# ---------------------------------------------------------------------------

_MARKDOWN_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MARKDOWN_ITALIC_RE = re.compile(r"\*(.+?)\*")


def _strip_markdown(text: str) -> str:
    """Remove apenas padrões markdown bold/italic, preservando asteriscos isolados."""
    text = _MARKDOWN_BOLD_RE.sub(r"\1", text)
    text = _MARKDOWN_ITALIC_RE.sub(r"\1", text)
    return text


def _send_text(
    conversation_id: int,
    session_id: str,
    text: str,
    account_id: str | None = None,
) -> None:
    if not text or not text.strip():
        print(f"[send_text:skip] empty text for conversation_id={conversation_id!r}")
        return
    cleaned = _strip_markdown(text)
    parts = [p.strip() for p in cleaned.split("\n\n") if p.strip()]

    for i, part in enumerate(parts):
        if i > 0:
            time.sleep(MESSAGE_SEND_GAP_SEC)
        _chatwoot_post_message(conversation_id, part, source_id=session_id, account_id=account_id)


def _debug_skip(reason: str, **details: Any) -> None:
    printable = " ".join(f"{key}={value!r}" for key, value in details.items())
    print(f"[webhook:skip] {reason} {printable}".strip())


def _event_name(body: dict[str, Any]) -> str:
    event = body.get("event")
    return str(event).strip() if event is not None else ""


def _message_payload(body: dict[str, Any]) -> dict[str, Any]:
    message = body.get("message")
    return message if isinstance(message, dict) else body


def _conversation_payload(body: dict[str, Any]) -> dict[str, Any]:
    message = _message_payload(body)
    conversation = body.get("conversation") or message.get("conversation")
    return conversation if isinstance(conversation, dict) else {}


def _account_id(body: dict[str, Any]) -> str | None:
    message = _message_payload(body)
    account = body.get("account") or message.get("account")
    if isinstance(account, dict) and account.get("id") is not None:
        return str(account["id"])

    for container in (body, message):
        account_value = container.get("account_id")
        if account_value is not None and str(account_value).strip():
            return str(account_value).strip()

    return None


def _message_type(body: dict[str, Any]) -> Any:
    message = _message_payload(body)
    if body.get("message_type") is not None:
        return body.get("message_type")
    return message.get("message_type")


def _sender_type(body: dict[str, Any]) -> str:
    message = _message_payload(body)
    for container in (body, message):
        sender = container.get("sender")
        if isinstance(sender, dict) and sender.get("type") is not None:
            return str(sender["type"]).strip().lower()
    return ""


def _message_content(body: dict[str, Any]) -> str:
    message = _message_payload(body)
    for container in (message, body):
        content = container.get("content")
        if content is not None:
            return str(content).strip()
    return ""


def _message_attachments(body: dict[str, Any]) -> list[dict[str, Any]]:
    message = _message_payload(body)
    for container in (message, body):
        attachments = container.get("attachments")
        if isinstance(attachments, list):
            return [item for item in attachments if isinstance(item, dict)]
        if isinstance(attachments, dict):
            return [attachments]
    return []


def _sender_identifier(body: dict[str, Any]) -> str:
    message = _message_payload(body)
    candidates: list[Any] = []
    for container in (body, message):
        sender = container.get("sender")
        if isinstance(sender, dict):
            candidates.extend([
                sender.get("phone_number"),
                sender.get("identifier"),
                sender.get("source_id"),
            ])
    for candidate in candidates:
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return ""


def _session_id(body: dict[str, Any], conversation: dict[str, Any], conversation_id: int) -> str:
    contact_inbox = conversation.get("contact_inbox") or {}
    candidates = (
        contact_inbox.get("source_id") if isinstance(contact_inbox, dict) else None,
        _sender_identifier(body),
        conversation_id,
    )
    for candidate in candidates:
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return str(conversation_id)


def _message_id(body: dict[str, Any]) -> str:
    """Return a per-message id without using contact-level source IDs."""
    cached = body.get("_julia_message_id")
    if cached is not None and str(cached).strip():
        return str(cached)

    message = _message_payload(body)
    candidates = (
        message.get("id"),
        body.get("id"),
        message.get("message_id"),
        body.get("message_id"),
        message.get("uuid"),
        body.get("uuid"),
    )
    for candidate in candidates:
        if candidate is not None and str(candidate).strip():
            body["_julia_message_id"] = str(candidate)
            return str(candidate)

    conversation = _conversation_payload(body)
    conversation_id = conversation.get("id", "unknown")
    created_at = message.get("created_at") or body.get("created_at")
    content = _message_content(body)
    attachments = _message_attachments(body)
    attachment_url = _attachment_url(attachments[0]) if attachments else ""

    if created_at and (content or attachment_url):
        raw = f"{conversation_id}|{created_at}|{content}|{attachment_url}"
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        body["_julia_message_id"] = f"{conversation_id}:{digest}"
        return str(body["_julia_message_id"])

    body["_julia_message_id"] = f"{conversation_id}:{time.time_ns()}"
    return str(body["_julia_message_id"])


def _incoming_message(body: dict[str, Any]) -> bool:
    msg_type = _message_type(body)
    if msg_type == 0:
        return True
    if isinstance(msg_type, str):
        return msg_type.strip().lower() in ("incoming", "0")
    return False


def _attachment_url(attachment: dict[str, Any]) -> str:
    return (
        attachment.get("data_url")
        or attachment.get("download_url")
        or attachment.get("file_url")
        or attachment.get("url")
        or ""
    )


# ---------------------------------------------------------------------------
# Helpers: Chatwoot transfer & lookup
# ---------------------------------------------------------------------------

def _chatwoot_user_token() -> str:
    """Returns user API token for admin-level Chatwoot operations."""
    return os.environ.get("CHATWOOT_USER_TOKEN") or os.environ["CHATWOOT_TOKEN"]


_AREA_TEAM_TARGETS = {"trabalhista": "trabalhista", "previdenciario": "previdenci"}
_AREA_AGENT_TARGETS = {"trabalhista": "rodolfo", "previdenciario": "genaina"}
_AREA_LAWYERS = {
    "trabalhista": ("Dr. Rodolfo Amadeo", "+55 27 98118-8433", "Trabalhista"),
    "previdenciario": ("Dra. Genaina Vasconcellos", "+55 27 99953-6986", "Previdenciário"),
}


def _chatwoot_lookup(
    endpoint: str,
    target_substring: str,
    log_label: str,
    account_id: str | None = None,
) -> int | None:
    url = os.environ["CHATWOOT_URL"]
    token = _chatwoot_user_token()
    account = account_id or os.environ.get("CHATWOOT_ACCOUNT_ID", "1")
    try:
        with httpx.Client() as http:
            resp = http.get(
                f"{url}/api/v1/accounts/{account}/{endpoint}",
                headers={"api_access_token": token},
                timeout=HTTP_TIMEOUT,
            )
            data = resp.json()
            print(f"[{log_label} API] status={resp.status_code} data={str(data)[:300]}")
            if isinstance(data, dict):
                data = data.get("payload", [])
            target = target_substring.lower()
            for item in data:
                if target in (item.get("name", "") or "").lower():
                    return item["id"]
    except Exception as e:
        print(f"[{log_label} API error] {e}")
    return None


def _get_chatwoot_team_id(area: str, account_id: str | None = None) -> int | None:
    target = _AREA_TEAM_TARGETS.get(area, _AREA_TEAM_TARGETS["previdenciario"])
    return _chatwoot_lookup("teams", target, "teams", account_id=account_id)


def _get_chatwoot_agent_id(area: str, account_id: str | None = None) -> int | None:
    target = _AREA_AGENT_TARGETS.get(area, _AREA_AGENT_TARGETS["previdenciario"])
    return _chatwoot_lookup("agents", target, "agents", account_id=account_id)


def _build_transfer_note(
    *,
    client_name: str,
    client_whatsapp: str,
    client_email: str,
    client_city: str,
    area_label: str,
    subarea: str,
    case_summary: str,
    qualification_notes: str,
    documents_requested: str,
    lawyer: str,
    lawyer_wa: str,
) -> str:
    email_line = f"\n- E-mail: {client_email}" if client_email else ""
    city_line = f"\n- Cidade/Estado: {client_city}" if client_city else ""
    docs_line = documents_requested if documents_requested else "Não informado"
    return (
        f"📋 RESUMO DO ATENDIMENTO — Júlia (Assistente Virtual)\n\n"
        f"👤 Cliente: {client_name}\n"
        f"- WhatsApp: {client_whatsapp}{email_line}{city_line}\n\n"
        f"⚖️ Área: {area_label} — {subarea}\n\n"
        f"📝 Resumo do caso:\n{case_summary}\n\n"
        f"✅ Qualificação:\n{qualification_notes}\n\n"
        f"📎 Documentos solicitados: {docs_line}\n\n"
        f"👨‍⚖️ Responsável: {lawyer} ({lawyer_wa})\n"
        f"📌 Canal: WhatsApp"
    )


def _transfer_to_lawyer(
    conversation_id: int,
    account_id: str | None,
    area: str,
    subarea: str,
    client_name: str,
    client_whatsapp: str,
    case_summary: str,
    qualification_notes: str,
    client_email: str = "",
    client_city: str = "",
    documents_requested: str = "",
) -> dict[str, Any]:
    url = os.environ["CHATWOOT_URL"]
    user_token = _chatwoot_user_token()
    account = account_id or os.environ.get("CHATWOOT_ACCOUNT_ID", "1")
    lawyer, lawyer_wa, area_label = _AREA_LAWYERS.get(area, _AREA_LAWYERS["previdenciario"])

    try:
        team_id = _get_chatwoot_team_id(area, account_id=account)
        agent_id = _get_chatwoot_agent_id(area, account_id=account)

        note = _build_transfer_note(
            client_name=client_name,
            client_whatsapp=client_whatsapp,
            client_email=client_email,
            client_city=client_city,
            area_label=area_label,
            subarea=subarea,
            case_summary=case_summary,
            qualification_notes=qualification_notes,
            documents_requested=documents_requested,
            lawyer=lawyer,
            lawyer_wa=lawyer_wa,
        )

        assign_errors: list[str] = []
        with httpx.Client() as http:
            if team_id:
                r = http.patch(
                    f"{url}/api/v1/accounts/{account}/conversations/{conversation_id}",
                    headers={"api_access_token": user_token, "Content-Type": "application/json"},
                    json={"team_id": team_id},
                    timeout=HTTP_TIMEOUT,
                )
                if r.status_code >= 400:
                    assign_errors.append(f"team PATCH {r.status_code}: {r.text[:200]}")
            if agent_id:
                r = http.post(
                    f"{url}/api/v1/accounts/{account}/conversations/{conversation_id}/assignments",
                    headers={"api_access_token": user_token, "Content-Type": "application/json"},
                    json={"assignee_id": agent_id},
                    timeout=HTTP_TIMEOUT,
                )
                if r.status_code >= 400:
                    assign_errors.append(f"agent POST {r.status_code}: {r.text[:200]}")

        _chatwoot_post_message(conversation_id, note, private=True, account_id=account)

        assign_info = f" | assign_errors: {assign_errors}" if assign_errors else ""
        print(f"[transfer] team_id={team_id} agent_id={agent_id}{assign_info}")
        return {
            "success": True,
            "lawyer": lawyer,
            "lawyer_wa": lawyer_wa,
            "team_assigned": team_id is not None and not any("team" in e for e in assign_errors),
            "error": assign_errors or None,
        }
    except Exception as e:
        return {"success": False, "lawyer": lawyer, "lawyer_wa": lawyer_wa, "team_assigned": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Helpers: pipeline finalize + debounce primitives
# ---------------------------------------------------------------------------

def _run_julia_and_send(
    psycopg2: Any,
    session_id: str,
    user_text: str,
    conversation_id: int,
    account_id: str | None = None,
) -> None:
    """Abre conexão, chama Júlia, envia resposta e aplica label final se transferido."""
    conn = psycopg2.connect(os.environ["POSTGRES_URL"])
    try:
        _init_db(conn)
        ai_response, was_transferred = _call_julia(conn, session_id, user_text, conversation_id, account_id)
    finally:
        conn.close()

    _send_text(conversation_id, session_id, ai_response, account_id=account_id)

    if was_transferred:
        _chatwoot_set_labels(conversation_id, ["transferido"], account_id=account_id)


def _wait_for_image_silence(redis_client: Any, key: str, msg_id: str) -> bool:
    """True quando IMAGE_DEBOUNCE_SEC se passaram desde a última imagem desta sessão."""
    while True:
        raw = redis_client.lrange(key, 0, -1)
        if not raw:
            return False
        last = json.loads(raw[-1])
        if last["id"] != msg_id:
            return False  # imagem mais recente chegou — deixa ela processar
        if time.time() - last["enqueued_at"] >= IMAGE_DEBOUNCE_SEC:
            return True
        time.sleep(IMAGE_POLL_SEC)


def _wait_for_text_silence(redis_client: Any, last_key: str, msg_id: str) -> bool:
    """True quando TEXT_DEBOUNCE_SEC se passaram sem nova mensagem da sessão."""
    poll_start = time.time()
    while True:
        current_last = redis_client.get(last_key)
        if current_last is None or current_last.decode() != msg_id:
            return False
        if time.time() - poll_start >= TEXT_DEBOUNCE_SEC:
            return True
        time.sleep(TEXT_POLL_SEC)


# ---------------------------------------------------------------------------
# Modal functions
# ---------------------------------------------------------------------------

@app.function(image=image, secrets=secrets, timeout=180)
def process_message(body: dict) -> None:
    import redis as redis_lib
    import psycopg2
    import traceback

    try:
        print(
            "[webhook:received]",
            {
                "event": _event_name(body),
                "message_type": _message_type(body),
                "message_id": _message_id(body),
                "conversation_id": _conversation_payload(body).get("id"),
                "account_id": _account_id(body),
                "sender_type": _sender_type(body),
                "content": _message_content(body),
                "attachments": len(_message_attachments(body)),
            },
        )
        _process(body, redis_lib, psycopg2)
        print(f"[webhook:done] message_id={_message_id(body)!r}")

    except Exception:
        traceback.print_exc()
        raise


def _process(body: dict[str, Any], redis_lib: Any, psycopg2: Any) -> None:
    event = _event_name(body)
    if event and event != "message_created":
        _debug_skip("event", event=event, id=body.get("id"))
        return

    msg_type = _message_type(body)

    # Only process incoming messages; ignore outgoing/activity to avoid infinite loops
    if not _incoming_message(body):
        _debug_skip("message_type", message_type=msg_type, event=event, id=body.get("id"))
        return

    # Ignore messages sent by human agents or the bot itself — only process customer messages
    sender_type = _sender_type(body)
    if sender_type in ("agent_bot", "bot", "user", "agent"):
        _debug_skip("sender_type", sender_type=sender_type, id=body.get("id"))
        return

    conversation = _conversation_payload(body)
    conversation_id = conversation.get("id")
    if not conversation_id:
        _debug_skip("missing_conversation_id", id=body.get("id"))
        return
    conversation_id = int(conversation_id)

    account_id = _account_id(body)

    # Stop only if a human explicitly added the "atendimento_humano" label
    # (avoid blocking on auto-assignment caused by the bot's own API token)
    labels = conversation.get("labels") or []
    session_id = _session_id(body, conversation, conversation_id)
    msg_id = _message_id(body)

    if "transferido" in labels:
        _debug_skip(
            "transferred_label",
            conversation_id=conversation_id,
            msg_id=msg_id,
            session_id=session_id,
            labels=labels,
        )
        return

    if "atendimento_humano" in labels:
        _debug_skip(
            "human_label",
            conversation_id=conversation_id,
            msg_id=msg_id,
            session_id=session_id,
            labels=labels,
        )
        return

    message_content = _message_content(body)
    attachments = _message_attachments(body)
    first_att = attachments[0] if attachments else {}
    file_type = first_att.get("file_type") or ""
    data_url = _attachment_url(first_att)

    # Deduplication: ignore if this exact message was already processed
    r = redis_lib.from_url(os.environ["REDIS_URL"])
    if not r.set(f"dedup:{msg_id}", "1", nx=True, ex=REDIS_DEDUP_TTL):
        _debug_skip(
            "duplicate",
            msg_id=msg_id,
            session_id=session_id,
            conversation_id=conversation_id,
            id=body.get("id"),
        )
        return  # another invocation already claimed this message

    print(
        "[webhook:process]",
        {
            "event": event or None,
            "conversation_id": conversation_id,
            "account_id": account_id,
            "session_id": session_id,
            "msg_id": msg_id,
            "file_type": file_type or None,
            "has_text": bool(message_content),
            "has_attachment_url": bool(data_url),
        },
    )

    _chatwoot_open_conversation(conversation_id, account_id=account_id)

    # Aplica "conversando" imediatamente na primeira mensagem (sem labels ainda)
    if not labels:
        try:
            _chatwoot_set_labels(conversation_id, ["conversando"], account_id=account_id)
        except Exception as e:
            print(f"[chatwoot:set_label_conversando_error] conversation_id={conversation_id!r} error={e!r}")

    # --- Image debounce: coleta imagens e espera silêncio desde a última ---
    if file_type == "image":
        img_key = f"images:{session_id}"
        r.rpush(img_key, json.dumps({"data_url": data_url, "id": msg_id, "enqueued_at": time.time()}))
        r.expire(img_key, REDIS_QUEUE_TTL)

        if not _wait_for_image_silence(r, img_key, msg_id):
            _debug_skip("newer_image_waiting", conversation_id=conversation_id, msg_id=msg_id)
            return

        all_imgs = r.lrange(img_key, 0, -1)
        r.delete(img_key)

        img_texts: list[str] = []
        for raw_img in all_imgs:
            img = json.loads(raw_img)
            if img.get("data_url"):
                try:
                    data, mime = _fetch(img["data_url"])
                    text = _claude_analyze_image(data, mime)
                    if text:
                        img_texts.append(text)
                except Exception as e:
                    print(f"[webhook:image_error] msg_id={img.get('id')!r} error={e!r}")

        if not img_texts:
            _debug_skip("image_without_extracted_text", conversation_id=conversation_id, msg_id=msg_id)
            return

        _run_julia_and_send(
            psycopg2,
            session_id,
            "\n\n".join(img_texts),
            conversation_id,
            account_id=account_id,
        )
        return

    text_message = _extract_text(file_type, message_content, data_url)
    if not text_message:
        if file_type == "audio":
            _send_text(conversation_id, session_id, AUDIO_MSG, account_id=account_id)
        elif file_type:
            # vídeo, sticker, GIF ou outro tipo não suportado — avisa o cliente em vez de silêncio
            _send_text(conversation_id, session_id, UNSUPPORTED_MSG, account_id=account_id)
        _debug_skip("empty_text", conversation_id=conversation_id, msg_id=msg_id, file_type=file_type, has_url=bool(data_url))
        return

    # --- Redis debounce: coleta mensagens rápidas e responde de uma vez ---
    last_key = f"last:{session_id}"
    r.rpush(session_id, json.dumps({"textMessage": text_message, "id": msg_id}))
    r.expire(session_id, REDIS_QUEUE_TTL)
    r.set(last_key, msg_id, ex=REDIS_LAST_MARKER_TTL)

    if not _wait_for_text_silence(r, last_key, msg_id):
        _debug_skip("newer_text_waiting", conversation_id=conversation_id, msg_id=msg_id)
        return

    # Collect all queued messages and clear the queue
    all_text = "\n".join(json.loads(m)["textMessage"] for m in r.lrange(session_id, 0, -1))
    r.delete(session_id)
    r.delete(last_key)

    _run_julia_and_send(psycopg2, session_id, all_text, conversation_id, account_id=account_id)


@app.function(image=image, secrets=secrets)
@modal.fastapi_endpoint(method="POST")
async def webhook(body: dict) -> dict[str, str]:
    """Receives Chatwoot webhook events and waits for processing in debug mode."""
    await process_message.remote.aio(body)
    return {"status": "processed"}
