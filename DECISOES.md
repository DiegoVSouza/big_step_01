# Decisões que tomei

## Arquitetura

Escolhi separar o projeto em dois serviços:

- `agent`: recebe o pedido, conversa com o modelo compatível com a API da OpenAI e executa as tools.
- `ticket-system`: representa o sistema externo. Ele consulta clientes e grava chamados no PostgreSQL.

Essa separação deixa claro que o agente não está apenas respondendo texto. Ele realmente chama outro serviço e provoca uma mudança persistente no banco de dados.

O fluxo de uma execução é intencionalmente linear:

1. Validar a entrada com `TicketRequest`, usando Pydantic.
2. Chamar `lookup_customer` para confirmar que o cliente existe.
3. Chamar `create_ticket` com o cliente encontrado.
4. Validar o retorno com `AgentResult` e devolver os IDs, as tools executadas e o consumo da execução.

As duas tools são descritas para o modelo por meio de tool calling, mas o código também verifica a ordem recebida. Assim, o modelo não consegue criar um ticket sem antes consultar o cliente.

As chamadas HTTP para o sistema externo usam timeout configurável e retry com backoff exponencial para timeouts, falhas de rede e respostas 5xx. Erros 4xx não são repetidos porque normalmente indicam uma entrada inválida ou uma regra de negócio rejeitada.

## Validação e segurança

Usei modelos Pydantic na entrada e na saída das APIs. As respostas do sistema externo também são convertidas para modelos antes de serem usadas pelo agente. Isso evita que um JSON inesperado seja tratado como se fosse válido.

O serviço de tickets exige um token interno no header. As chaves ficam em variáveis de ambiente; elas não são colocadas no código, nos testes ou na documentação.

## Medição de custo

Cada execução acumula os tokens informados pelo provedor do modelo. O custo estimado é calculado a partir dos preços configurados para entrada e saída:

```text
(prompt_tokens × input_price + completion_tokens × output_price) / 1.000.000
```

Os preços ficam em `OPENAI_INPUT_USD_PER_1M` e `OPENAI_OUTPUT_USD_PER_1M` porque podem mudar conforme o modelo ou o provedor usado. Se não forem configurados, o agente continua funcionando, mas o custo retornado será zero.

## O que ficou de fora

### Autenticação de usuários finais

Ficou fora porque o exercício avalia tool calling e execução de uma ação externa. O token entre serviços cobre o cenário demonstrado, mas uma aplicação real precisaria de autenticação de usuários, autorização por conta e rotação de credenciais.

### Fila e processamento assíncrono

O endpoint espera a conclusão das duas tools para devolver o resultado. Isso torna a demonstração simples e facilita comprovar o ticket criado. Uma fila seria mais adequada para operações demoradas ou grande volume, mas adicionaria infraestrutura que não é necessária neste exercício.

### Idempotência de criação

O retry foi aplicado às chamadas externas, mas o exemplo ainda não implementa uma chave de idempotência no endpoint de criação. Em produção, essa seria uma prioridade: sem ela, uma falha de rede depois da gravação poderia causar a criação duplicada de um ticket.

### Conversa livre e memória

Não criei um chatbot genérico nem histórico de conversa. A entrada é um comando estruturado para abrir um chamado, porque o objetivo é demonstrar uma operação verificável, com começo, fim e efeito externo observável.

### Managed PostgreSQL

O banco fica em uma máquina PostgreSQL própria, com volume persistente, conforme a restrição do projeto. Isso mantém o deploy alinhado ao requisito do exercício e evita introduzir um serviço gerenciado diferente da arquitetura definida.

## Resultado

A solução prioriza um fluxo pequeno, observável e testável. Ela comprova as partes importantes do exercício — duas tools distintas, ordem obrigatória, validação estruturada, retry com timeout e custo por execução — sem transformar o projeto em uma plataforma maior do que o problema pede.
