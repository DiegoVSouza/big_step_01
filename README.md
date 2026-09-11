# Big Step 01 — agente que executa ação externa

O projeto recebe uma ordem estruturada para abrir um chamado. Ele usa tool calling de uma API compatível com OpenAI e executa uma ação persistente em outro serviço: o `ticket-system`. Não há endpoint de conversa livre.

O modelo deve chamar, nesta ordem:

1. `lookup_customer`: consulta o cliente no sistema externo.
2. `create_ticket`: grava de fato o ticket no PostgreSQL do sistema externo.

O contrato de entrada é validado por Pydantic e a resposta também (`AgentResult`). A resposta traz IDs, tools executadas, horário UTC e consumo/custo.

## Rodar localmente

```powershell
cd big_step_01
Copy-Item .env.example .env
# edite .env e informe OPENAI_API_KEY
docker compose up --build
```

Em outro terminal, execute:

```powershell
Invoke-RestMethod http://localhost:8080/runs -Method Post -ContentType 'application/json' -Body '{"customer_email":"ana@example.com","title":"Falha no acesso","description":"A cliente não consegue entrar no portal desde esta manhã.","priority":"high"}'
```

O retorno esperado tem `status: created`, dois nomes em `tools_called` e um `ticket_id` novo. O PostgreSQL local começa com o cliente de demonstração `ana@example.com`.

## Resiliência e custo

- Cada chamada ao sistema externo tem timeout configurável (`TOOL_TIMEOUT_SECONDS`, padrão 4 s).
- Falhas de rede, timeout e 5xx recebem até 3 tentativas (`TOOL_RETRY_ATTEMPTS`) com backoff exponencial de 250 ms, 500 ms e 1 s. Erros 4xx não são repetidos.
- A saída registra os tokens devolvidos pelo provedor e `estimated_cost_usd` por execução. A fórmula é `(prompt_tokens × preço_entrada + completion_tokens × preço_saída) / 1.000.000`.
- Os preços ficam em variáveis de ambiente para acompanhar o modelo escolhido. O padrão é `openai/gpt-oss-20b` na Groq: US$ 0,075/M de entrada e US$ 0,30/M de saída. Atualize-os ao trocar de modelo.

## Testes e checagens

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests
docker compose config
```

## Publicar no Fly (`iad`)

O deploy usa três apps na região `iad`, todos em `shared-cpu-1x` com 256 MB:

- `big-step-01-agent`: API pública, escala para zero quando ociosa.
- `big-step-01-tickets`: sistema externo, escala para zero quando ocioso.
- `big-step-01-postgres`: máquina PostgreSQL própria, com volume persistente de 1 GB; não usa Managed Postgres.

Os nomes dos apps precisam estar livres na organização Fly. Autentique-se primeiro com `fly auth login`, copie `.env.example` para `.env`, preencha `OPENAI_API_KEY` e execute uma única vez:

```powershell
./deploy.ps1
```

O script gera credenciais internas aleatórias para PostgreSQL e para o serviço de tickets, cria volume e apps, aplica secrets e faz o deploy com `--ha=false` para manter uma única máquina por app. Para repetir a publicação, basta executar o mesmo comando. O banco não expõe porta pública; os serviços se comunicam pela rede privada `.internal`. A URL final é `https://big-step-01-agent.fly.dev/runs`.

## Critérios de aceite

- [x] O agente chama pelo menos duas tools distintas (`lookup_customer` e `create_ticket`).
- [x] Entrada e saída estruturadas e validadas com Pydantic.
- [x] Retry com backoff e timeout configurável nas tools externas.
- [x] `AGENTS.md` útil com regras operacionais e de deploy.
- [x] Tokens e custo estimado por execução na saída, com fórmula e preços documentados.

## Configuração pelo `.env`

O Docker Compose lê automaticamente o arquivo `.env` da raiz. Comece copiando o exemplo:

```powershell
Copy-Item .env.example .env
```

Além de `OPENAI_API_KEY`, o arquivo pode definir `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_INPUT_USD_PER_1M`, `OPENAI_OUTPUT_USD_PER_1M`, `TOOL_TIMEOUT_SECONDS`, `TOOL_RETRY_ATTEMPTS`, `POSTGRES_PASSWORD` e `SERVICE_TOKEN`. Os valores ausentes usam os defaults documentados no `.env.example`.

Depois, suba os serviços com:

```powershell
docker compose up --build -d
```

## Interface de teste

Com os containers em execução, sirva a página de teste em outro terminal:

```powershell
docker compose up --build -d
```

Abra http://localhost:5500. A interface envia o formulário para `/runs` e mostra o resultado estruturado, as duas tools executadas, os tokens e o custo estimado.

## Deploy e variáveis

O `deploy.ps1` usa a mesma configuração do `.env`. A prioridade é: parâmetros do script, variáveis do ambiente, valores do `.env` e defaults. A chave do provedor, endpoint, modelo, preços, timeout e retries são enviados ao app do agente. A senha do PostgreSQL e o token interno são enviados aos serviços correspondentes como secrets do Fly.

Para publicar:

```powershell
./deploy.ps1
```

O script não imprime os valores das credenciais. Se `POSTGRES_PASSWORD` ou `SERVICE_TOKEN` não forem definidos, ele gera credenciais internas aleatórias.


