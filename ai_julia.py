import modal
import os
import json
import time
import base64
import httpx
from datetime import datetime, timezone, timedelta

app = modal.App("ai-julia")

image = (
    modal.Image.debian_slim()
    .pip_install([
        "fastapi[standard]",
        "httpx",
        "anthropic",
        "redis",
        "psycopg2-binary",
        "google-auth",
        "google-auth-httplib2",
        "google-api-python-client",
        "groq",
    ])
)

secrets = [modal.Secret.from_name("marina-secrets"), modal.Secret.from_name("google-calendar-secrets"), modal.Secret.from_name("groq-secrets")]

CLAUDE_MODEL = "claude-haiku-4-5"

SCHEDULE_MEETING_TOOL = {
    "name": "schedule_meeting",
    "description": "Agenda uma reunião de consulta no Google Calendar do advogado responsável. Use APENAS quando o cliente já tiver confirmado: nome completo, área, data, horário e formato. Preencha todos os campos de resumo com base na conversa completa.",
    "input_schema": {
        "type": "object",
        "properties": {
            "client_name": {"type": "string", "description": "Nome completo do cliente"},
            "area": {"type": "string", "enum": ["trabalhista", "previdenciario"], "description": "Área do caso"},
            "subarea": {"type": "string", "description": "Subárea do caso (ex: BPC/LOAS, Rescisão, Auxílio-doença, Aposentadoria, Assédio moral, etc)"},
            "date": {"type": "string", "description": "Data da reunião no formato YYYY-MM-DD"},
            "time": {"type": "string", "description": "Horário da reunião no formato HH:MM"},
            "format": {"type": "string", "enum": ["online", "presencial"], "description": "Formato da reunião"},
            "client_whatsapp": {"type": "string", "description": "Número de WhatsApp do cliente"},
            "client_email": {"type": "string", "description": "E-mail do cliente (opcional)"},
            "client_city": {"type": "string", "description": "Cidade/Estado do cliente, se informado"},
            "case_summary": {"type": "string", "description": "Resumo em 2 a 3 frases do que o cliente relatou, com as informações mais relevantes para o advogado"},
            "qualification_notes": {"type": "string", "description": "Principais respostas que qualificaram o cliente (ex: tinha carteira assinada, não recebeu verbas rescisórias, renda per capita dentro do limite BPC/LOAS, etc)"},
            "documents_requested": {"type": "string", "description": "Lista dos documentos solicitados ao cliente antes da reunião"},
        },
        "required": ["client_name", "area", "date", "time", "format", "client_whatsapp", "case_summary", "qualification_notes", "subarea"],
    },
}

AUDIO_MSG = "Recebi seu áudio, mas não consegui transcrever. Por favor, envie sua mensagem em texto."

JULIA_SYSTEM_PROMPT = """# REGRA ABSOLUTA — ENCERRAMENTO APÓS AGENDAMENTO

Sempre que um agendamento for confirmado com sucesso, a última mensagem OBRIGATORIAMENTE deve conter:
1. Confirmação da consulta (data, horário, formato)
2. Link do Meet (se online e disponível)
3. Aviso legal: "nossa conversa tem caráter informativo e não estabelece uma relação advocatícia formal"
4. Informação de que o advogado responsável entrará em contato no dia da consulta e algumas horas antes — com o número de WhatsApp correspondente:
   - Trabalhista: Dr. Rodolfo Amadeo — +55 27 98118-8433
   - Previdenciário: Dra. Genaina Vasconcellos — +55 27 99953-6986

Nunca encerre um atendimento com agendamento confirmado sem incluir esses quatro itens.

# REGRA ABSOLUTA — ADVOGADO CONSTITUÍDO

Se o cliente informar, em qualquer momento da conversa, que já possui advogado constituído para a questão em pauta: PARE IMEDIATAMENTE. Não faça mais nenhuma pergunta. Não explore outras áreas. Não ofereça análise complementar. Não tente captar por outro ângulo. Responda apenas com a mensagem de encerramento ético e encerre. Nenhuma exceção.

# FORMATO — REGRA ABSOLUTA

Cada resposta sua deve ter NO MÁXIMO 2 frases curtas. Uma pergunta por vez. Sem listas. Sem emojis. Sem explicações longas. Se precisar dizer mais, escolha o mais importante e deixe o resto para a próxima mensagem.

Nunca use linha em branco entre frases — escreva sempre em bloco único contínuo, sem parágrafos separados. Nunca reformule a mesma pergunta duas vezes na mesma resposta.

# MEMÓRIA CONTEXTUAL — REGRA ABSOLUTA

Antes de fazer qualquer pergunta, releia mentalmente o histórico completo da conversa. Se a informação já foi fornecida pelo cliente — mesmo que em resposta a outra pergunta ou de forma indireta — NÃO pergunte de novo. Exemplos: se o cliente mencionou "carteira assinada" ou confirmou vínculo formal, não pergunte sobre carteira assinada. Se mencionou que ainda está trabalhando, não pergunte se foi demitido. Erro de memória contextual é a falha mais grave do atendimento.

# ROLE

Você é Júlia, assistente virtual da Vasconcellos & Amadeo Advocacia, escritório especializado em Direito Trabalhista, Sindical e Previdenciário com mais de 20 anos de experiência na defesa dos trabalhadores brasileiros. Sua função é qualificar o cliente através de perguntas estratégicas, identificar se ele tem um caso viável e encaminhá-lo para agendamento de reunião com o advogado responsável.

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
O encaminhamento para agendamento deve ser natural, como consequência lógica da conversa, não como fechamento de venda.
Exemplo: "A reunião pode ser por videochamada ou presencial em Vitória. Qual prefere?"

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
- Coletar o e-mail do cliente logo após o nome — é obrigatório. Se o cliente não tiver, prosseguir sem
- Verificar obrigatoriamente se o cliente já possui advogado constituído antes de qualquer orientação de mérito
- Identificar a área do caso: Trabalhista ou Previdenciário
- Identificar a cidade/estado do cliente logo após identificar a área do caso (antes do SPIN) — se não for Vitória/ES, direcionar automaticamente para atendimento online
- Qualificar o cliente com perguntas estratégicas (SPIN), uma de cada vez
- Se qualificado: encaminhar para agendamento priorizando os próximos 2 dias úteis (48h)
- Se não qualificado: encerrar com respeito e orientar onde buscar ajuda
- Encerrar sempre com aviso legal e informar que o advogado responsável entrará em contato no dia da consulta e algumas horas antes

# TIPOS DE ENTRADA DO SITE

Mensagens vindas do site têm formatos padronizados. Identifique o tipo pelo conteúdo da primeira mensagem e adapte o atendimento pulando o que já foi coletado.

## TIPO 1 — Mensagem geral
Identificação: mensagem genérica como "gostaria de agendar uma consulta" sem dados estruturados.
Já coletado: nada.
Ação: siga o fluxo padrão — nome → e-mail → advogado constituído → área → cidade → SPIN → agendamento.

## TIPO 2 — Calculadora de Verbas Rescisórias
Identificação: mensagem contém "calculadora de verbas rescisórias".
Formato recebido: "Olá, sou [NOME] ([WHATSAPP]). Usei a calculadora de verbas rescisórias..."
Já coletado: nome, WhatsApp, área = trabalhista.
Pular: perguntar nome e área.
Abertura:
> "Olá, [NOME]! Vi que você usou nossa calculadora de verbas rescisórias. Pode me informar seu melhor e-mail?"
Após o e-mail: verificar advogado constituído → cidade → SPIN trabalhista a partir da situação atual.

## TIPO 3 — Calculadora de Tempo de Contribuição
Identificação: mensagem contém "calculadora de tempo de contribuição".
Formato recebido: "Olá, sou [NOME] ([WHATSAPP]). Usei a calculadora de tempo de contribuição..."
Já coletado: nome, WhatsApp, área = previdenciário (aposentadoria).
Pular: perguntar nome e área.
Abertura:
> "Olá, [NOME]! Vi que você usou nossa calculadora de tempo de contribuição. Pode me informar seu melhor e-mail?"
Após o e-mail: verificar advogado constituído → cidade → SPIN previdenciário (aposentadoria/revisão).

## TIPO 4 — Formulário de Contato
Identificação: mensagem contém "Área:" e "Cidade:" no formato estruturado.
Formato recebido: "Olá! Sou [NOME] ([WHATSAPP], [EMAIL]). Área: [ÁREA] | Cidade: [CIDADE]. [MENSAGEM]"
Já coletado: nome, WhatsApp, e-mail, área, cidade.
Pular: perguntar nome, e-mail, área e cidade.
Se cidade não for Vitória/ES → atendimento online automaticamente.
Abertura:
> "Olá, [NOME]! Recebi sua mensagem. Antes de continuarmos — o(a) senhor(a) já possui advogado(a) constituído(a) para esta questão?"
Após verificação: iniciar SPIN diretamente com base na área e mensagem recebidas.

## TIPO 5 — Exit Popup (saída do site)
Identificação: mensagem contém "consulta prévia" ou "antes de sair" ou "WhatsApp [número]" sem outros dados estruturados.
Formato recebido: "Olá, sou [NOME] (WhatsApp [WHATSAPP]). Gostaria de uma consulta prévia... [MENSAGEM OPCIONAL]"
Já coletado: nome, WhatsApp.
Pular: perguntar nome.
Abertura:
> "Olá, [NOME]! Pode me informar seu melhor e-mail para acompanharmos seu caso?"
Após o e-mail: verificar advogado constituído → área → cidade → SPIN → agendamento.
Se houver mensagem opcional: reconheça brevemente antes de pedir o e-mail.

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

Após o nome, se o e-mail não vier no resumo do lead, perguntar: "Pode me informar seu melhor e-mail para enviarmos as informações da consulta?"
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

## Formato da reunião
- Se o cliente informar que mora fora de Vitória/ES (outro estado ou outra cidade): direcionar automaticamente para videochamada, sem oferecer presencial.
  > "Como você está em [cidade/estado], nossa consulta será feita de forma online — por videochamada. Qual dia e horário funcionam melhor para você?"
- Se o cliente for de Vitória/ES ou não informar a cidade: perguntar a preferência entre videochamada ou presencial.
- Presencial: Av. Nossa Sra. dos Navegantes, 755 - Sala 508 - Enseada do Suá, Vitória - ES

## Horário disponível e prioridade de agendamento
Segunda a sexta, das 9h às 18h.
Prioridade: oferecer sempre o horário mais próximo disponível, preferencialmente dentro das próximas 48h (2 dias úteis).
Se não houver disponibilidade em 48h, oferecer o primeiro horário disponível na semana seguinte.
Nunca recusar o agendamento por falta de horário imediato — sempre encontrar a próxima data disponível.
Fora do horário de atendimento: "Nosso horário é de segunda a sexta, das 9h às 18h. Vou repassar seu contato para retornarmos assim que possível."

# ÉTICA OAB — INTERNALIZADA, NÃO DECORADA

As regras abaixo não são decorativas. São limites que não podem ser ultrapassados em nenhuma circunstância, independentemente do contexto ou da insistência do cliente.

- Nunca promete resultado, vantagem ou garantia de êxito
- Nunca faz captação apelativa, emocional ou baseada em urgência artificial
- Nunca usa o sofrimento ou a vulnerabilidade do cliente como argumento persuasivo
- Sempre verifica se o cliente possui advogado constituído antes de qualquer orientação de mérito
- Se o cliente confirmar que já possui advogado constituído — independentemente da área ou do benefício mencionado — encerre imediatamente o atendimento, sem fazer novas perguntas, sem explorar outras áreas, sem abrir frentes alternativas. O encerramento é respeitoso, definitivo e sem tentativa de captação.
- Fora das áreas do escritório (Trabalhista e Previdenciário): oriente onde buscar ajuda, sem emitir opinião de mérito
- Encerre todo atendimento com aviso legal de forma natural e não burocrática

# EXAMPLES

## Abertura
> Olá! Sou a Júlia, da Vasconcellos & Amadeo Advocacia.
> Poderia me dizer seu nome?

## Após o nome
> Obrigada, [Nome]. Pode me informar seu melhor e-mail? Vamos usá-lo para enviar as informações da consulta.

## Após o e-mail (ou se não tiver)
> Antes de continuarmos — o(a) senhor(a) já possui advogado(a) constituído(a) para tratar desta questão?

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
> A reunião pode ser por videochamada ou presencial em Vitória. Qual prefere?

## Cliente qualificado — trabalhista
> Seu caso merece atenção especializada do Dr. Rodolfo Amadeo.
> A reunião pode ser por videochamada ou presencial em Vitória. Qual prefere?

## Solicitação de documentos
> Para aproveitarmos bem a reunião, pode me enviar aqui pelo WhatsApp os seguintes documentos antes do nosso encontro?

## Encerramento após agendamento confirmado
> Perfeito, [Nome]! Sua consulta está agendada. Só lembrando que nossa conversa tem caráter informativo e não estabelece uma relação advocatícia formal. O [Dr. Rodolfo / Dra. Genaina] entrará em contato com você no dia da consulta e algumas horas antes para confirmar — pelo WhatsApp +55 27 98118-8433 (Rodolfo) ou +55 27 99953-6986 (Genaina).

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
- Nunca faça mais de uma pergunta por mensagem
- Máximo de 2 frases por mensagem — seja direta, nunca explique o que vai fazer, apenas faça
- Nunca use listas ou bullets nas respostas — sempre prosa. Exceção única: ao solicitar documentos após o agendamento, use lista simples com hífen para facilitar a leitura do cliente
- Nunca continue o atendimento após o cliente informar que já possui advogado — não explore outras áreas, outros benefícios nem outras questões. O encerramento é imediato e definitivo nessa conversa.
- Nunca trate assuntos fora de Trabalhista e Previdenciário
- Nunca repita pergunta ou informação já respondida na mesma conversa — releia o histórico completo antes de cada pergunta. Se a informação foi dada de forma indireta (ex: cliente falou "ainda estou lá" = não foi demitido; "tenho carteira" = vínculo formal), registre e não pergunte de novo
- Nunca envie resposta com parágrafo duplo (\n\n) — escreva sempre em bloco único contínuo
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
- Presencial é exclusivo para clientes de Vitória/ES. Para qualquer outro estado ou cidade, o atendimento é sempre online, sem oferecer presencial
- Após o agendamento confirmado, solicitar os documentos relevantes antes de encerrar
- Alertar sobre prazo prescricional de 2 anos em casos trabalhistas quando relevante, sem alarmismo
- Coletar o e-mail do cliente logo após o nome, antes de qualquer outra pergunta — é obrigatório para envio do convite. Se o cliente não tiver, prosseguir normalmente
- Priorizar agendamentos dentro das próximas 48h (2 dias úteis) — nunca recusar por falta de horário imediato, sempre oferecer a data mais próxima disponível
- Após agendamento confirmado, solicitar os documentos relevantes para o caso antes de encerrar a conversa
- Se o cliente informar que mora fora de Vitória/ES, direcionar automaticamente para videochamada sem oferecer presencial
- Quando o cliente confirmar nome completo, área, data, horário, formato e WhatsApp, use a ferramenta schedule_meeting para criar o evento antes de informar ao cliente
- Sempre confirme a data completa com dia, mês e ano antes de chamar a ferramenta — nunca assuma o mês ou ano
- Após agendamento online bem-sucedido, informe o link do Google Meet na mesma mensagem — mas SOMENTE se o meet_link vier preenchido no resultado da ferramenta. Se meet_link for null ou vazio, diga que o link será enviado pelo advogado responsável em breve, pelo WhatsApp
- Se o agendamento falhar, informe que alguém do escritório confirmará o horário manualmente"""


# ---------------------------------------------------------------------------
# Helpers: PostgreSQL conversation memory
# ---------------------------------------------------------------------------

def _init_db(conn) -> None:
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


def _get_chat_history(conn, session_id: str, limit: int = 20) -> list:
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


def _save_turn(conn, session_id: str, user_msg: str, assistant_msg: str) -> None:
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

def _fetch(url: str) -> tuple:
    """Download URL → (bytes, content_type)."""
    r = httpx.get(url, follow_redirects=True, timeout=30)
    ct = r.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
    return r.content, ct


def _normalize_image_mime(mime: str) -> str:
    supported = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    aliases = {"image/jpg": "image/jpeg"}
    return aliases.get(mime, mime) if aliases.get(mime, mime) in supported else "image/jpeg"


def _claude_analyze_image(data: bytes, mime_type: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": _normalize_image_mime(mime_type),
                        "data": base64.b64encode(data).decode(),
                    },
                },
                {
                    "type": "text",
                    "text": "Extraia o conteúdo da imagem e retorne em formato de texto sem caracteres especiais.",
                },
            ],
        }],
    )
    return next(b.text for b in resp.content if b.type == "text")


def _claude_analyze_document(data: bytes) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.b64encode(data).decode(),
                    },
                },
                {
                    "type": "text",
                    "text": "Extraia o conteúdo do documento e retorne em formato de texto sem caracteres especiais.",
                },
            ],
        }],
    )
    return next(b.text for b in resp.content if b.type == "text")


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
# Helpers: AI agent (Júlia via Anthropic SDK with prompt caching)
# ---------------------------------------------------------------------------

def _call_julia(conn, session_id: str, user_message: str) -> tuple:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    history = _get_chat_history(conn, session_id)
    messages = list(history)
    messages.append({"role": "user", "content": user_message})

    _days_pt = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
    _months_pt = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
    _now = datetime.now(timezone(timedelta(hours=-3)))
    today = f"{_days_pt[_now.weekday()]}, {_now.day} de {_months_pt[_now.month-1]} de {_now.year}"
    today_iso = _now.strftime("%Y-%m-%d")

    # For each weekday, find the next occurrence (strictly after today)
    next_occurrences = []
    for day_idx, day_name in enumerate(_days_pt):
        days_ahead = (day_idx - _now.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7  # same weekday = next week
        d = _now + timedelta(days=days_ahead)
        next_occurrences.append(f"  Próxima {day_name}: {d.strftime('%d/%m/%Y')}")
    next_days = "\n".join(next_occurrences)
    system = [
        {
            "type": "text",
            "text": JULIA_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": (
                f"Hoje é {today} ({today_iso}). Próximas 2 semanas para referência:\n{next_days}\n"
                f"Para datas além de 14 dias, calcule a partir de hoje ({today_iso}) somando os dias corretamente.\n"
                f"Ao agendar, sempre confirme a data no formato DD/MM/YYYY antes de chamar a ferramenta.\n"
                f"O número de WhatsApp do cliente nesta conversa é: {session_id}. "
                f"Não peça o WhatsApp — você já o tem. Use-o diretamente ao chamar schedule_meeting.\n"
                f"Após agendar com sucesso, informe que a reunião está confirmada, envie o link do Meet se for online "
                f"e também envie o link do agendamento: https://calendar.app.google/mPeSFcH6gFv5oBSHA\n"
                f"O agendamento já está feito — não diga que alguém entrará em contato para confirmar o horário. "
                f"Informe que o advogado responsável entrará em contato no dia da consulta e algumas horas antes como cortesia."
            ),
        },
    ]

    was_scheduled = False

    while True:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=800,
            system=system,
            tools=[SCHEDULE_MEETING_TOOL],
            messages=messages,
        )

        if resp.stop_reason == "end_turn":
            ai_text = next(b.text for b in resp.content if b.type == "text")
            _save_turn(conn, session_id, user_message, ai_text)
            return ai_text, was_scheduled

        if resp.stop_reason == "tool_use":
            tool_use_block = next(b for b in resp.content if b.type == "tool_use")
            args = tool_use_block.input

            result = _schedule_meeting(
                client_name=args["client_name"],
                area=args["area"],
                subarea=args["subarea"],
                date=args["date"],
                meeting_time=args["time"],
                meeting_format=args["format"],
                client_whatsapp=args["client_whatsapp"],
                case_summary=args["case_summary"],
                qualification_notes=args["qualification_notes"],
                client_email=args.get("client_email", ""),
                client_city=args.get("client_city", ""),
                documents_requested=args.get("documents_requested", ""),
            )

            if result.get("success"):
                was_scheduled = True

            messages.append({"role": "assistant", "content": resp.content})
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_block.id,
                    "content": json.dumps(result),
                }],
            })
            continue

        # Fallback: unexpected stop_reason
        ai_text = next((b.text for b in resp.content if b.type == "text"), "")
        if ai_text:
            _save_turn(conn, session_id, user_message, ai_text)
        return ai_text, was_scheduled


# ---------------------------------------------------------------------------
# Helpers: send responses back to Chatwoot
# ---------------------------------------------------------------------------

def _send_text(conversation_id: int, session_id: str, text: str) -> None:
    url = os.environ["CHATWOOT_URL"]
    token = os.environ["CHATWOOT_TOKEN"]
    account = os.environ.get("CHATWOOT_ACCOUNT_ID", "1")

    # Strip markdown bold, split on blank lines, 3 s gap between parts (mirrors n8n)
    parts = [p.strip() for p in text.replace("*", "").split("\n\n") if p.strip()]

    with httpx.Client() as http:
        for i, part in enumerate(parts):
            if i > 0:
                time.sleep(3)
            http.post(
                f"{url}/api/v1/accounts/{account}/conversations/{conversation_id}/messages",
                headers={"Content-Type": "application/json", "api_access_token": token},
                json={
                    "content": part,
                    "message_type": "outgoing",
                    "content_type": "text",
                    "private": False,
                    "content_attributes": {"source_id": session_id},
                },
            )


# ---------------------------------------------------------------------------
# Helpers: Google Calendar scheduling
# ---------------------------------------------------------------------------

def _schedule_meeting(
    client_name: str,
    area: str,
    subarea: str,
    date: str,
    meeting_time: str,
    meeting_format: str,
    client_whatsapp: str,
    case_summary: str,
    qualification_notes: str,
    client_email: str = "",
    client_city: str = "",
    documents_requested: str = "",
) -> dict:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from datetime import datetime as _dt

    try:
        creds = Credentials(
            token=None,
            refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
        creds.refresh(Request())

        calendar_id = (
            os.environ["GOOGLE_CALENDAR_TRABALHISTA"]
            if area == "trabalhista"
            else os.environ["GOOGLE_CALENDAR_PREVIDENCIARIO"]
        )
        lawyer = "Dr. Rodolfo Amadeo" if area == "trabalhista" else "Dra. Genaina Vasconcellos"

        start_dt = _dt.fromisoformat(f"{date}T{meeting_time}:00")
        end_dt = start_dt + timedelta(hours=1)

        email_line = f"\n- E-mail: {client_email}" if client_email else ""
        city_line = f"\n- Estado/Cidade: {client_city}" if client_city else ""
        docs_line = documents_requested if documents_requested else "Não informado"
        area_label = "Trabalhista" if area == "trabalhista" else "Previdenciário"
        format_label = "Videochamada" if meeting_format == "online" else "Presencial"

        base_description = (
            f"📋 RESUMO DO ATENDIMENTO — Vasconcellos & Amadeo Advocacia\n\n"
            f"👤 Dados do cliente:\n"
            f"- Nome completo: {client_name}\n"
            f"- WhatsApp: {client_whatsapp}{email_line}{city_line}\n\n"
            f"⚖️ Área do caso: {area_label}\n"
            f"📁 Subárea: {subarea}\n\n"
            f"📝 Resumo do caso:\n{case_summary}\n\n"
            f"✅ Qualificação:\n{qualification_notes}\n\n"
            f"📎 Documentos solicitados: {docs_line}\n\n"
            f"🗓️ Formato da reunião: {format_label}\n"
            f"👨‍⚖️ Advogado responsável: {lawyer}\n\n"
            f"⏰ Atendimento realizado por: Júlia — Assistente Virtual\n"
            f"📌 Canal: WhatsApp"
        )

        tz = "America/Sao_Paulo"
        event = {
            "summary": f"Consulta — {client_name}",
            "description": base_description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": tz},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": tz},
        }
        if client_email:
            event["attendees"] = [{"email": client_email}]

        service = build("calendar", "v3", credentials=creds)

        if meeting_format == "online":
            event["summary"] = f"Atendimento Google Meet-({client_name})"
            event["conferenceData"] = {
                "createRequest": {"requestId": f"{client_whatsapp}-{date}-{meeting_time}"}
            }
            created = service.events().insert(
                calendarId=calendar_id, body=event, conferenceDataVersion=1,
                sendUpdates="all",
            ).execute()
            entry_points = (created.get("conferenceData") or {}).get("entryPoints", [])
            meet_link = next((ep.get("uri") for ep in entry_points if ep.get("entryPointType") == "video"), None)

            # Validate that the meet link is complete (must have a code after the base URL)
            if meet_link and meet_link.rstrip("/") == "https://meet.google.com":
                meet_link = None

            # If first attempt returned no valid link, fetch the event again (API may need a moment)
            if not meet_link:
                import time as _time
                _time.sleep(2)
                fetched = service.events().get(calendarId=calendar_id, eventId=created["id"]).execute()
                entry_points = (fetched.get("conferenceData") or {}).get("entryPoints", [])
                meet_link = next((ep.get("uri") for ep in entry_points if ep.get("entryPointType") == "video"), None)
                if meet_link and meet_link.rstrip("/") == "https://meet.google.com":
                    meet_link = None

            if meet_link:
                meet_code = meet_link.rstrip("/").split("/")[-1]
                service.events().patch(
                    calendarId=calendar_id,
                    eventId=created["id"],
                    body={
                        "summary": f"Atendimento Google Meet-{meet_code}-({client_name})",
                        "description": base_description + f"\n\n🔗 Link do Google Meet: {meet_link}",
                    },
                    sendUpdates="none",
                ).execute()
        else:
            event["summary"] = f"Atendimento Presencial-({client_name})"
            event["location"] = "Av. Nossa Sra. dos Navegantes, 755 - Sala 508 - Enseada do Suá, Vitória - ES"
            created = service.events().insert(
                calendarId=calendar_id, body=event, sendUpdates="all"
            ).execute()
            meet_link = None

        return {
            "success": True,
            "event_link": created.get("htmlLink", ""),
            "meet_link": meet_link,
            "lawyer": lawyer,
            "error": None,
        }
    except Exception as e:
        return {"success": False, "event_link": None, "meet_link": None, "lawyer": None, "error": str(e)}


# ---------------------------------------------------------------------------
# Modal functions
# ---------------------------------------------------------------------------

@app.function(image=image, secrets=secrets, timeout=180)
def process_message(body: dict) -> None:
    import redis as redis_lib
    import psycopg2
    import traceback

    try:
        _process(body, redis_lib, psycopg2)
    except Exception:
        traceback.print_exc()


def _process(body: dict, redis_lib, psycopg2) -> None:
    # Agent Bot payload: message fields are at body root level
    msg_type = body.get("message_type")

    # Only process incoming messages; ignore outgoing/activity to avoid infinite loops
    if msg_type not in (0, "incoming"):
        return

    conversation = body.get("conversation") or {}
    conversation_id = conversation.get("id")

    # Stop only if a human explicitly added the "atendimento_humano" label
    # (avoid blocking on auto-assignment caused by the bot's own API token)
    labels = conversation.get("labels") or []
    if "atendimento_humano" in labels:
        return

    # session_id: prefer WhatsApp source_id, fall back to conversation id
    # An empty session_id would merge all conversations into one history
    session_id = (
        (conversation.get("contact_inbox") or {}).get("source_id")
        or str(conversation_id or "")
        or body.get("source_id", "")
    )

    message_content = body.get("content") or ""
    attachments = body.get("attachments") or []
    first_att = attachments[0] if attachments else {}
    file_type = first_att.get("file_type") or ""
    data_url = first_att.get("data_url") or ""
    msg_id = body.get("source_id") or str(body.get("id") or "")
    msg_timestamp = body.get("created_at") or ""

    # Deduplication: ignore if this exact message was already processed
    r = redis_lib.from_url(os.environ["REDIS_URL"])
    dedup_key = f"dedup:{msg_id}"
    if not r.set(dedup_key, "1", nx=True, ex=60):
        return  # another invocation already claimed this message

    cw_url = os.environ["CHATWOOT_URL"]
    cw_token = os.environ["CHATWOOT_TOKEN"]
    cw_account = os.environ.get("CHATWOOT_ACCOUNT_ID", "1")

    with httpx.Client() as http:
        http.post(
            f"{cw_url}/api/v1/accounts/{cw_account}/conversations/{conversation_id}/toggle_status",
            headers={"api_access_token": cw_token},
            json={"status": "open"},
        )
        http.post(
            f"{cw_url}/api/v1/accounts/{cw_account}/conversations/{conversation_id}/labels",
            headers={"api_access_token": cw_token},
            json={"labels": ["conversando"]},
        )

    # --- Image debounce: coleta imagens e espera 10s desde a última ---
    if file_type == "image":
        img_key = f"images:{session_id}"
        r.rpush(img_key, json.dumps({"data_url": data_url, "id": msg_id, "enqueued_at": time.time()}))
        r.expire(img_key, 300)

        while True:
            raw = r.lrange(img_key, 0, -1)
            if not raw:
                return
            last = json.loads(raw[-1])
            if last["id"] != msg_id:
                return  # imagem mais recente chegou — deixa ela processar
            if time.time() - last["enqueued_at"] >= 10:
                break  # 10s de silêncio confirmado
            time.sleep(2)

        all_imgs = r.lrange(img_key, 0, -1)
        r.delete(img_key)

        img_texts = []
        for raw_img in all_imgs:
            img = json.loads(raw_img)
            if img.get("data_url"):
                try:
                    data, mime = _fetch(img["data_url"])
                    text = _claude_analyze_image(data, mime)
                    if text:
                        img_texts.append(text)
                except Exception:
                    pass

        if not img_texts:
            return

        all_text = "\n\n".join(img_texts)

        conn = psycopg2.connect(os.environ["POSTGRES_URL"])
        try:
            _init_db(conn)
            ai_response, was_scheduled = _call_julia(conn, session_id, all_text)
        finally:
            conn.close()

        _send_text(conversation_id, session_id, ai_response)

        if was_scheduled:
            with httpx.Client() as http:
                http.post(
                    f"{cw_url}/api/v1/accounts/{cw_account}/conversations/{conversation_id}/labels",
                    headers={"api_access_token": cw_token},
                    json={"labels": ["agendado"]},
                )
        return

    text_message = _extract_text(file_type, message_content, data_url)
    if not text_message:
        if file_type == "audio":
            _send_text(conversation_id, session_id, AUDIO_MSG)
        return

    # --- Redis debounce: coleta mensagens rápidas e responde de uma vez ---
    r.rpush(
        session_id,
        json.dumps({"textMessage": text_message, "id": msg_id, "enqueued_at": time.time()}),
    )

    while True:
        raw = r.lrange(session_id, 0, -1)
        if not raw:
            return

        last = json.loads(raw[-1])

        # Mensagem mais recente chegou — deixa ela processar
        if last["id"] != msg_id:
            return

        if time.time() - last["enqueued_at"] >= 2:
            break  # 2s de silêncio confirmado

        time.sleep(0.5)

    # Collect all queued messages and clear the queue
    all_text = "\n".join(json.loads(m)["textMessage"] for m in r.lrange(session_id, 0, -1))
    r.delete(session_id)

    # --- Call Claude and send response ---
    conn = psycopg2.connect(os.environ["POSTGRES_URL"])
    try:
        _init_db(conn)
        ai_response, was_scheduled = _call_julia(conn, session_id, all_text)
    finally:
        conn.close()

    _send_text(conversation_id, session_id, ai_response)

    if was_scheduled:
        with httpx.Client() as http:
            http.post(
                f"{cw_url}/api/v1/accounts/{cw_account}/conversations/{conversation_id}/labels",
                headers={"api_access_token": cw_token},
                json={"labels": ["agendado"]},
            )


@app.function(image=image, secrets=secrets)
@modal.fastapi_endpoint(method="POST")
async def webhook(body: dict):
    """Receives Chatwoot webhook events and processes them asynchronously."""
    await process_message.spawn.aio(body)
    return {"status": "received"}
