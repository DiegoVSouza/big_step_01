# Regras do projeto

- Código e nomes técnicos em inglês; documentação em português do Brasil.
- O agente é operacional: só conclui depois de executar as duas tools externas na ordem definida.
- Toda entrada e saída HTTP usa modelos Pydantic. Não contorne a validação.
- Falhas transitórias de tools usam timeout e retry com backoff; não faça retry de erros 4xx.
- Nunca registre chaves, tokens ou conteúdo de `.env`.
- Antes de concluir alterações, rode `python -m unittest discover -s tests` e `docker compose config`.
- Fly usa `iad`, VMs mínimas e PostgreSQL em uma máquina própria com volume; não use Managed Postgres.
