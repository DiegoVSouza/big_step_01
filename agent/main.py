import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel, EmailStr, Field

TOOL_TIMEOUT_SECONDS = float(os.getenv("TOOL_TIMEOUT_SECONDS", "4"))
TOOL_RETRY_ATTEMPTS = int(os.getenv("TOOL_RETRY_ATTEMPTS", "3"))
TICKET_API_URL = os.environ["TICKET_API_URL"].rstrip("/")
TICKET_SERVICE_TOKEN = os.environ["TICKET_SERVICE_TOKEN"]


class TicketRequest(BaseModel):
    customer_email: EmailStr
    title: str = Field(min_length=5, max_length=120)
    description: str = Field(min_length=10, max_length=2000)
    priority: Literal["low", "medium", "high"]


class Customer(BaseModel):
    id: UUID
    email: EmailStr
    name: str


class CreatedTicket(BaseModel):
    id: UUID
    customer_id: UUID
    title: str
    priority: Literal["low", "medium", "high"]
    status: str
    created_at: datetime


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0


class AgentResult(BaseModel):
    status: Literal["created"]
    ticket_id: UUID
    customer_id: UUID
    tools_called: list[str] = Field(min_length=2)
    completed_at: datetime
    usage: Usage


class HealthResponse(BaseModel):
    ok: bool
    tool_timeout_seconds: float
    tool_retry_attempts: int


class ToolFailure(Exception):
    pass


async def call_external(method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Call the ticket service with a timeout and bounded exponential backoff."""
    last_error: Exception | None = None
    for attempt in range(TOOL_RETRY_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=TOOL_TIMEOUT_SECONDS) as client:
                response = await client.request(
                    method,
                    f"{TICKET_API_URL}{path}",
                    json=payload,
                    headers={"X-Service-Token": TICKET_SERVICE_TOKEN},
                )
            if response.status_code >= 500:
                raise ToolFailure(f"external system returned {response.status_code}")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as error:
            if 400 <= error.response.status_code < 500:
                raise ToolFailure(
                    f"external system rejected the request: {error.response.status_code}"
                ) from error
            last_error = error
        except (httpx.TimeoutException, httpx.NetworkError, ToolFailure) as error:
            last_error = error

        if attempt < TOOL_RETRY_ATTEMPTS - 1:
            await asyncio.sleep(0.25 * (2**attempt))

    raise ToolFailure(
        f"external tool unavailable after {TOOL_RETRY_ATTEMPTS} attempts: {last_error}"
    )


async def lookup_customer(email: str) -> Customer:
    response = await call_external("POST", "/customers/lookup", {"email": email})
    return Customer.model_validate(response)


async def create_ticket(
    customer_id: str, title: str, description: str, priority: str
) -> CreatedTicket:
    response = await call_external(
        "POST",
        "/tickets",
        {
            "customer_id": customer_id,
            "title": title,
            "description": description,
            "priority": priority,
        },
    )
    return CreatedTicket.model_validate(response)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_customer",
            "description": "Find a customer before creating a ticket.",
            "parameters": {
                "type": "object",
                "properties": {"email": {"type": "string", "format": "email"}},
                "required": ["email"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_ticket",
            "description": "Create an actual support ticket. Call lookup_customer first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "priority": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["customer_id", "title", "description", "priority"],
                "additionalProperties": False,
            },
        },
    },
]


def openai_client() -> OpenAI:
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=os.getenv("OPENAI_BASE_URL"))


def estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    input_price = float(os.getenv("OPENAI_INPUT_USD_PER_1M", "0"))
    output_price = float(os.getenv("OPENAI_OUTPUT_USD_PER_1M", "0"))
    return round((prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000, 8)


async def run_agent(request: TicketRequest) -> AgentResult:
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": "Execute support-ticket operations. Call lookup_customer, then create_ticket. Use tools only.",
        },
        {"role": "user", "content": request.model_dump_json()},
    ]
    called: list[str] = []
    customer: Customer | None = None
    created: CreatedTicket | None = None
    prompt_tokens = completion_tokens = total_tokens = 0
    client = openai_client()

    for _ in range(4):
        response = client.chat.completions.create(
            model=os.environ["OPENAI_MODEL"],
            messages=messages,
            tools=TOOLS,
            tool_choice="required",
        )
        if response.usage:
            prompt_tokens += response.usage.prompt_tokens or 0
            completion_tokens += response.usage.completion_tokens or 0
            total_tokens += response.usage.total_tokens or 0

        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))
        if not message.tool_calls:
            break

        for tool_call in message.tool_calls:
            try:
                arguments = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError as error:
                raise ToolFailure("model returned invalid tool arguments") from error

            expected_tool = "lookup_customer" if customer is None else "create_ticket"
            if tool_call.function.name != expected_tool:
                raise ToolFailure(
                    f"tool order violation: expected {expected_tool}, got {tool_call.function.name}"
                )

            if expected_tool == "lookup_customer":
                customer = await lookup_customer(arguments["email"])
                output: BaseModel = customer
            else:
                if customer is None or str(customer.id) != arguments["customer_id"]:
                    raise ToolFailure("ticket customer does not match the looked-up customer")
                created = await create_ticket(**arguments)
                output = created

            called.append(expected_tool)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": output.model_dump_json(),
                }
            )

        if created:
            break

    if not created or called != ["lookup_customer", "create_ticket"]:
        raise ToolFailure("agent did not complete the mandatory two-tool workflow")

    return AgentResult(
        status="created",
        ticket_id=created.id,
        customer_id=created.customer_id,
        tools_called=called,
        completed_at=datetime.now(timezone.utc),
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimate_cost(prompt_tokens, completion_tokens),
        ),
    )


app = FastAPI(title="Big Step 01 Tool Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5500", "http://127.0.0.1:5500", "https://big-step-01-frontend.fly.dev"],
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        ok=True,
        tool_timeout_seconds=TOOL_TIMEOUT_SECONDS,
        tool_retry_attempts=TOOL_RETRY_ATTEMPTS,
    )


@app.post("/runs", response_model=AgentResult)
async def create_run(body: TicketRequest) -> AgentResult:
    try:
        return await run_agent(body)
    except ToolFailure as error:
        raise HTTPException(status_code=503, detail=str(error)) from error



