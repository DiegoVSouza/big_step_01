import json
import os
import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=30)


class ChatUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0


class Article(BaseModel):
    id: str
    title: str
    summary: str
    tags: list[str]


class ProcessStep(BaseModel):
    key: str
    label: str
    status: Literal["done", "waiting"]
    detail: str


class ChatResponse(BaseModel):
    status: Literal["needs_input", "created"]
    message: str
    tools_called: list[str] = Field(default_factory=list)
    articles: list[Article] = Field(default_factory=list)
    process: list[ProcessStep] = Field(default_factory=list)
    ticket_id: UUID | None = None
    customer_id: UUID | None = None
    usage: ChatUsage


class SearchArguments(BaseModel):
    query: str = Field(min_length=3, max_length=200)


class LookupArguments(BaseModel):
    email: EmailStr


class CreateCustomerArguments(BaseModel):
    email: EmailStr
    name: str = Field(min_length=2, max_length=120)


class CreateArguments(BaseModel):
    customer_id: UUID
    title: str = Field(min_length=5, max_length=120)
    description: str = Field(min_length=10, max_length=2000)
    priority: Literal["low", "medium", "high"]


CHAT_SYSTEM_PROMPT = """
Você é um agente de suporte que opera um sistema real de tickets.
Entenda a mensagem e o histórico e decida qual tool usar. Para problemas, pesquise
artigos internos relacionados usando search_help_articles. Se houver e-mail, consulte
o cliente com lookup_customer. Só depois de encontrar o cliente use create_ticket.
Se o e-mail não existir, use create_customer apenas quando o usuário pedir explicitamente
para cadastrar; depois continue com create_ticket. Para perguntas sobre clientes
cadastrados, responda diretamente sem criar ticket nem pedir e-mail.
Extraia um título curto e a prioridade. Considere urgente, bloqueado ou indisponibilidade
como high; nos outros casos use medium. Se o usuário disser que não precisa de ticket,
que quer apenas orientação ou que não quer abrir chamado, pesquise os artigos e não
consulte cliente nem crie ticket. Depois de criar o ticket, responda em português com o identificador.
"""


GUIDANCE_ONLY_MARKERS = (
    "não precisa de ticket", "nao precisa de ticket", "ainda não preciso de ticket",
    "ainda nao preciso de ticket", "não preciso de ticket", "nao preciso de ticket",
    "só quero orientação", "so quero orientacao", "apenas orientação", "apenas orientacao",
    "não abra ticket", "nao abra ticket", "sem abrir ticket", "não quero ticket",
    "nao quero ticket", "não preciso abrir chamado", "nao preciso abrir chamado",
)


def requests_guidance_only(request: ChatRequest) -> bool:
    text = request.message.lower()
    return any(marker in text for marker in GUIDANCE_ONLY_MARKERS)


def asks_for_registered_users(request: ChatRequest) -> bool:
    text = request.message.lower()
    normalized = re.sub(r"[^a-záéíóúãõçü ]", " ", text)
    asks_who = bool(re.search(r"\b(quais|quem)\b", normalized))
    mentions_people = bool(re.search(r"\b(clientes?|usuários?|usuarios?)\b", normalized))
    mentions_directory = bool(re.search(r"\b(existem|existe|cadastrados?|cadastradas?|lista|cadastro)\b", normalized))
    return asks_who and mentions_people and mentions_directory


def requests_customer_creation(request: ChatRequest) -> bool:
    text = request.message.lower()
    markers = (
        "pode cadastrar", "pode criar", "cadastre", "cadastra", "cadastro",
        "cadastrar o cliente", "crie o cadastro", "cria o cadastro", "registre o cliente",
        "não está cadastrado", "nao esta cadastrado",
    )
    return any(marker in text for marker in markers)


def name_from_email(email: str) -> str:
    local_part = email.split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ")
    return " ".join(part.capitalize() for part in local_part.split()) or "Cliente"


def previous_request_with_email(request: ChatRequest) -> tuple[str, str] | None:
    email_pattern = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
    for item in reversed(request.history):
        if item.role != "user":
            continue
        match = email_pattern.search(item.content)
        if match:
            return item.content, match.group(0)
    return None


def request_email(request: ChatRequest) -> str | None:
    email_pattern = re.compile(r"[^\s@]+@[\s@]+\.[^\s@]+")
    match = email_pattern.search(request.message)
    if match:
        return match.group(0).rstrip(".,;!?")
    previous = previous_request_with_email(request)
    return previous[1].rstrip(".,;!?") if previous else None

def article_text(articles: list[Article]) -> str:
    if not articles:
        return ""
    return "\n\nOrientações encontradas:\n" + "\n".join(
        f"• {article.title}: {article.summary}" for article in articles
    )


def _usage(prompt_tokens: int, completion_tokens: int, total_tokens: int, estimate: float) -> ChatUsage:
    return ChatUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=estimate,
    )


async def run_chat(request: ChatRequest) -> ChatResponse:
    from .main import (
        TOOLS,
        ToolFailure,
        create_customer,
        create_ticket,
        estimate_cost,
        lookup_customer,
        openai_client,
        search_help_articles,
    )

    messages: list[dict[str, Any]] = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
    messages.extend(item.model_dump() for item in request.history)
    pending_context = previous_request_with_email(request) if requests_customer_creation(request) else None
    current_message = request.message
    if pending_context:
        current_message = (
            f"{request.message}\n\nContexto da solicitação anterior: {pending_context[0]}\n"
            f"E-mail relacionado: {pending_context[1]}"
        )
    messages.append({"role": "user", "content": current_message})

    called: list[str] = []
    articles: list[Article] = []
    customer = None
    ticket = None
    researched = False
    customer_missing = False
    guidance_only = requests_guidance_only(request)
    informational_only = asks_for_registered_users(request)
    process = [ProcessStep(key="understand", label="Entender mensagem", status="done", detail="Mensagem recebida pelo agente.")]
    prompt_tokens = completion_tokens = total_tokens = 0
    client = openai_client()

    for _ in range(6):
        expected_name = (
            "search_help_articles" if not researched
            else "create_customer" if customer_missing
            else "lookup_customer" if customer is None
            else "create_ticket"
        )
        available_tools = TOOLS
        tool_choice = "auto"
        response = client.chat.completions.create(
            model=os.environ["OPENAI_MODEL"],
            messages=messages,
            tools=available_tools,
            tool_choice=tool_choice,
        )
        if response.usage:
            prompt_tokens += response.usage.prompt_tokens or 0
            completion_tokens += response.usage.completion_tokens or 0
            total_tokens += response.usage.total_tokens or 0

        assistant_message = response.choices[0].message
        messages.append(assistant_message.model_dump(exclude_none=True))
        if not assistant_message.tool_calls:
            has_email = bool(re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", request.message)) or any(
                bool(re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", item.content)) for item in request.history
            )
            if informational_only:
                reply = assistant_message.content or "Não encontrei uma resposta para essa consulta."
                return ChatResponse(
                    status="needs_input", message=reply + article_text(articles), tools_called=called,
                    articles=articles, process=process,
                    usage=_usage(prompt_tokens, completion_tokens, total_tokens, estimate_cost(prompt_tokens, completion_tokens)),
                )
            if guidance_only and researched:
                reply = assistant_message.content or "Encontrei orientações úteis."
                return ChatResponse(
                    status="needs_input", message=reply + article_text(articles), tools_called=called,
                    articles=articles, process=process,
                    usage=_usage(prompt_tokens, completion_tokens, total_tokens, estimate_cost(prompt_tokens, completion_tokens)),
                )
            expected = (
                "search_help_articles" if not researched
                else "create_customer" if customer_missing
                else "lookup_customer" if customer is None
                else "create_ticket"
            )
            if expected == "lookup_customer" and not has_email:
                reply = (
                    f"Problema informado: {request.message.strip()}\n\n"
                    "Para abrir o chamado, preciso do e-mail do cliente associado ao problema."
                )
                process.append(ProcessStep(key="collect", label="Pedir informação", status="waiting", detail="Falta o e-mail do cliente."))
                return ChatResponse(
                    status="needs_input", message=reply + article_text(articles), tools_called=called,
                    articles=articles, process=process,
                    usage=_usage(prompt_tokens, completion_tokens, total_tokens, estimate_cost(prompt_tokens, completion_tokens)),
                )
            if not researched:
                output = await search_help_articles(request.message)
                articles = [Article.model_validate(item.model_dump()) for item in output.articles]
                researched = True
                called.append("search_help_articles")
                process.append(ProcessStep(key="research", label="Pesquisar orientação", status="done", detail=f"{len(articles)} artigo(s) encontrado(s)."))
                messages.append({"role": "system", "content": f"Resultado de search_help_articles: {output.model_dump_json()}"})
                continue

            if expected == "lookup_customer" and has_email:
                email = request_email(request)
                if email is not None:
                    try:
                        customer = await lookup_customer(email)
                        called.append("lookup_customer")
                        process.append(ProcessStep(key="lookup", label="Consultar cliente", status="done", detail=f"Cliente {customer.name} confirmado."))
                        messages.append({"role": "system", "content": f"Resultado de lookup_customer: {customer.model_dump_json()}"})
                        continue
                    except ToolFailure as error:
                        if error.status_code != 404 or not requests_customer_creation(request):
                            raise
                        customer = await create_customer(email, name_from_email(email))
                        called.extend(["lookup_customer", "create_customer"])
                        process.append(ProcessStep(key="lookup", label="Consultar cliente", status="done", detail="E-mail não encontrado no cadastro."))
                        process.append(ProcessStep(key="register", label="Cadastrar cliente", status="done", detail=f"Cadastro de {customer.name} criado."))
                        messages.append({"role": "system", "content": f"Resultado de create_customer: {customer.model_dump_json()}"})
                        continue

            if expected == "create_ticket" and customer is not None:
                description = request.message.strip()
                title = articles[0].title if articles else "Solicitação de suporte"
                priority = "high" if any(word in description.lower() for word in ("urgente", "bloqueado", "indisponível", "indisponivel")) else "medium"
                ticket = await create_ticket(str(customer.id), title, description, priority)
                called.append("create_ticket")
                process.append(ProcessStep(key="create", label="Criar ticket", status="done", detail=f"Ticket {ticket.id} criado."))
                return ChatResponse(
                    status="created", message=f"Pronto. Criei o ticket {ticket.id}.", tools_called=called,
                    articles=articles, process=process, ticket_id=ticket.id, customer_id=ticket.customer_id,
                    usage=_usage(prompt_tokens, completion_tokens, total_tokens, estimate_cost(prompt_tokens, completion_tokens)),
                )

            messages.append({
                "role": "system",
                "content": f"Você respondeu sem chamar uma tool. Para esta etapa, use somente {expected}. Não responda em texto ainda.",
            })
            continue
        invalid_tool = False
        for tool_call in assistant_message.tool_calls:
            name = tool_call.function.name
            try:
                arguments = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError as error:
                raise ToolFailure("model returned invalid tool arguments") from error

            expected = (
                "search_help_articles" if not researched
                else "create_customer" if customer_missing
                else "lookup_customer" if customer is None
                else "create_ticket"
            )
            if name != expected:
                invalid_tool = True
                messages.append({
                    "role": "system",
                    "content": f"A tool {name} não foi executada. A ordem é obrigatória. Use agora somente {expected} e não execute outra tool nesta etapa.",
                })
                break

            if name == "search_help_articles":
                parsed = SearchArguments.model_validate(arguments)
                source_text = pending_context[0] if pending_context else request.message
                search_query = re.sub(r"[^\s@]+@[^\s@]+\.[^\s@]+", "", source_text).strip()
                output = await search_help_articles(search_query or parsed.query)
                articles = [Article.model_validate(item.model_dump()) for item in output.articles]
                researched = True
                process.append(ProcessStep(key="research", label="Pesquisar orientação", status="done", detail=f"{len(articles)} artigo(s) encontrado(s)."))
                has_email = bool(re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", request.message)) or any(
                    bool(re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", item.content)) for item in request.history
                )
                if not has_email and not guidance_only and not informational_only:
                    process.append(ProcessStep(key="collect", label="Pedir informação", status="waiting", detail="Falta o e-mail do cliente."))
                    return ChatResponse(
                        status="needs_input",
                        message=f"Problema informado: {request.message.strip()}\n\nPara abrir o chamado, preciso do e-mail do cliente associado ao problema." + article_text(articles),
                        tools_called=called + [name], articles=articles, process=process,
                        usage=_usage(prompt_tokens, completion_tokens, total_tokens, estimate_cost(prompt_tokens, completion_tokens)),
                    )
                if guidance_only:
                    return ChatResponse(
                        status="needs_input",
                        message="Encontrei orientações úteis. Como você pediu apenas a pesquisa, não vou abrir um ticket." + article_text(articles),
                        tools_called=called + [name], articles=articles, process=process,
                        usage=_usage(prompt_tokens, completion_tokens, total_tokens, estimate_cost(prompt_tokens, completion_tokens)),
                    )
            elif name == "lookup_customer":
                parsed = LookupArguments.model_validate(arguments)
                try:
                    customer = await lookup_customer(str(parsed.email))
                    output = customer
                    process.append(ProcessStep(key="lookup", label="Consultar cliente", status="done", detail=f"Cliente {customer.name} confirmado."))
                except ToolFailure as error:
                    if error.status_code != 404:
                        raise
                    if not requests_customer_creation(request):
                        process.append(ProcessStep(key="lookup", label="Consultar cliente", status="waiting", detail="E-mail não encontrado no cadastro."))
                        return ChatResponse(
                            status="needs_input",
                            message=f"Não encontrei {parsed.email} no cadastro de demonstração. Se quiser, diga: pode cadastrar este cliente.",
                            tools_called=called, articles=articles, process=process,
                            usage=_usage(prompt_tokens, completion_tokens, total_tokens, estimate_cost(prompt_tokens, completion_tokens)),
                        )
                    customer_missing = True
                    process.append(ProcessStep(key="lookup", label="Consultar cliente", status="done", detail="E-mail não encontrado no cadastro."))
                    output = {"status": "not_found", "email": str(parsed.email)}
            elif name == "create_customer":
                if not customer_missing or not requests_customer_creation(request):
                    raise ToolFailure("customer creation requires an explicit confirmation")
                parsed = CreateCustomerArguments.model_validate(arguments)
                customer = await create_customer(str(parsed.email), parsed.name)
                customer_missing = False
                process.append(ProcessStep(key="register", label="Cadastrar cliente", status="done", detail=f"Cadastro de {customer.name} criado."))
                output = customer
            else:
                parsed = CreateArguments.model_validate(arguments)
                if customer is None or parsed.customer_id != customer.id:
                    raise ToolFailure("ticket customer does not match the looked-up customer")
                ticket = await create_ticket(str(parsed.customer_id), parsed.title, parsed.description, parsed.priority)
                process.append(ProcessStep(key="create", label="Criar ticket", status="done", detail=f"Ticket {ticket.id} criado."))
                output = ticket

            if name not in called:
                called.append(name)
            content = output.model_dump_json() if isinstance(output, BaseModel) else json.dumps(output)
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": content})

        if invalid_tool:
            continue

        if ticket:
            return ChatResponse(
                status="created", message=f"Pronto. Criei o ticket {ticket.id}.", tools_called=called,
                articles=articles, process=process, ticket_id=ticket.id, customer_id=ticket.customer_id,
                usage=_usage(prompt_tokens, completion_tokens, total_tokens, estimate_cost(prompt_tokens, completion_tokens)),
            )

    raise ToolFailure("chat did not complete the ticket workflow")
