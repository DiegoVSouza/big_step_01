import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

os.environ.setdefault("TICKET_API_URL", "http://tickets.test")
os.environ.setdefault("TICKET_SERVICE_TOKEN", "test-token")
os.environ.setdefault("OPENAI_MODEL", "test-model")
sys.path.insert(0, str(Path(__file__).parents[1]))

from agent import main


class FakeToolCall:
    def __init__(self, name, arguments):
        self.id = f"call-{name}"
        self.function = SimpleNamespace(name=name, arguments=arguments)


class FakeMessage:
    def __init__(self, tool_calls=None, content=None):
        self.tool_calls = tool_calls or []
        self.content = content

    def model_dump(self, exclude_none=True):
        return {"role": "assistant", "content": self.content, "tool_calls": []}


class FakeCompletion:
    def __init__(self, message):
        self.choices = [SimpleNamespace(message=message)]
        self.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)


class FakeOpenAI:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        return next(self.responses)


class AgentTests(unittest.TestCase):
    def test_cost_is_calculated_from_usage(self):
        old_input = os.environ.get("OPENAI_INPUT_USD_PER_1M")
        old_output = os.environ.get("OPENAI_OUTPUT_USD_PER_1M")
        os.environ["OPENAI_INPUT_USD_PER_1M"] = "1"
        os.environ["OPENAI_OUTPUT_USD_PER_1M"] = "2"
        self.assertEqual(main.estimate_cost(1_000_000, 1_000_000), 3)
        if old_input is None:
            del os.environ["OPENAI_INPUT_USD_PER_1M"]
        else:
            os.environ["OPENAI_INPUT_USD_PER_1M"] = old_input
        if old_output is None:
            del os.environ["OPENAI_OUTPUT_USD_PER_1M"]
        else:
            os.environ["OPENAI_OUTPUT_USD_PER_1M"] = old_output

    def test_retry_uses_bounded_attempts(self):
        original = main.httpx.AsyncClient
        calls = []

        class FailingClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return None
            async def request(self, *args, **kwargs):
                calls.append(1)
                raise main.httpx.TimeoutException("timeout")

        main.httpx.AsyncClient = lambda **kwargs: FailingClient()
        try:
            with self.assertRaises(main.ToolFailure):
                asyncio.run(main.call_external("POST", "/tickets", {}))
        finally:
            main.httpx.AsyncClient = original
        self.assertEqual(len(calls), main.TOOL_RETRY_ATTEMPTS)

    def test_short_confirmation_reuses_email(self):
        from agent.chat import ChatMessage, ChatRequest, asks_for_registered_users, previous_request_with_email, requests_customer_creation

        request = ChatRequest(
            message="cadastra",
            history=[ChatMessage(role="user", content="an1231a@example.com não consegue acessar o portal.")],
        )
        self.assertTrue(requests_customer_creation(request))
        directory_request = ChatRequest(message="quais is clientes que existem")
        self.assertTrue(asks_for_registered_users(directory_request))
        self.assertEqual(previous_request_with_email(request)[1], "an1231a@example.com")

    def test_rejects_tool_out_of_order(self):
        response = FakeCompletion(FakeMessage([
            FakeToolCall("create_ticket", '{"customer_id":"%s","title":"Acesso bloqueado","description":"Problema no portal","priority":"high"}' % uuid4())
        ]))
        with patch.object(main, "openai_client", return_value=FakeOpenAI([response])):
            with self.assertRaisesRegex(main.ToolFailure, "tool order violation"):
                asyncio.run(main.run_agent(main.TicketRequest(
                    customer_email="ana@example.com",
                    title="Acesso bloqueado",
                    description="Cliente não consegue acessar o portal.",
                    priority="high",
                )))

    def test_rejects_ticket_for_different_customer(self):
        customer_id = uuid4()
        lookup_response = FakeCompletion(FakeMessage([
            FakeToolCall("lookup_customer", '{"email":"ana@example.com"}')
        ]))
        create_response = FakeCompletion(FakeMessage([
            FakeToolCall("create_ticket", '{"customer_id":"%s","title":"Acesso bloqueado","description":"Cliente não consegue acessar o portal.","priority":"high"}' % uuid4())
        ]))
        customer = main.Customer(id=customer_id, email="ana@example.com", name="Ana Souza")
        with patch.object(main, "openai_client", return_value=FakeOpenAI([lookup_response, create_response])), patch.object(main, "lookup_customer", new=AsyncMock(return_value=customer)):
            with self.assertRaisesRegex(main.ToolFailure, "customer does not match"):
                asyncio.run(main.run_agent(main.TicketRequest(
                    customer_email="ana@example.com",
                    title="Acesso bloqueado",
                    description="Cliente não consegue acessar o portal.",
                    priority="high",
                )))

    def test_does_not_retry_client_error(self):
        original = main.httpx.AsyncClient
        calls = []

        class RejectedClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return None
            async def request(self, *args, **kwargs):
                calls.append(1)
                return main.httpx.Response(404, request=main.httpx.Request("POST", "http://tickets.test/customers/lookup"))

        main.httpx.AsyncClient = lambda **kwargs: RejectedClient()
        try:
            with self.assertRaisesRegex(main.ToolFailure, "404"):
                asyncio.run(main.call_external("POST", "/customers/lookup", {}))
        finally:
            main.httpx.AsyncClient = original
        self.assertEqual(calls, [1])

    def test_chat_confirmation_runs_four_tools(self):
        from agent.chat import ChatMessage, ChatRequest, run_chat

        customer = main.Customer(id=uuid4(), email="novo@example.com", name="Novo")
        article = main.KnowledgeArticle(id="KB-001", title="Redefinir acesso", summary="Confirme o e-mail.", tags=["acesso"])
        ticket = main.CreatedTicket(
            id=uuid4(), customer_id=customer.id, title=article.title, priority="high", status="open",
            created_at=datetime.now(timezone.utc),
        )
        search_response = main.KnowledgeSearchResponse(articles=[article])
        llm_responses = FakeOpenAI([
            FakeCompletion(FakeMessage([FakeToolCall("search_help_articles", '{"query":"acesso portal"}')])),
            FakeCompletion(FakeMessage([FakeToolCall("lookup_customer", '{"email":"novo@example.com"}')])),
            FakeCompletion(FakeMessage([FakeToolCall("create_customer", '{"email":"novo@example.com","name":"Novo"}')])),
            FakeCompletion(FakeMessage([FakeToolCall("create_ticket", ('{"customer_id":"%s","title":"Redefinir acesso","description":"novo@example.com não consegue acessar o portal e isso é urgente.","priority":"high"}' % customer.id))])), 
        ])
        with patch.object(main, "openai_client", return_value=llm_responses), \
             patch.object(main, "search_help_articles", new=AsyncMock(return_value=search_response)), \
             patch.object(main, "lookup_customer", new=AsyncMock(side_effect=main.ToolFailure("not found", status_code=404))), \
             patch.object(main, "create_customer", new=AsyncMock(return_value=customer)), \
             patch.object(main, "create_ticket", new=AsyncMock(return_value=ticket)):
            result = asyncio.run(run_chat(ChatRequest(
                message="pode cadastrar pra mim",
                history=[ChatMessage(role="user", content="novo@example.com não consegue acessar o portal e isso é urgente.")],
            )))
        self.assertEqual(result.status, "created")
        self.assertEqual(result.tools_called, ["search_help_articles", "lookup_customer", "create_customer", "create_ticket"])

    def test_rejects_invalid_llm_tool_arguments(self):
        response = FakeCompletion(FakeMessage([
            FakeToolCall("lookup_customer", "{email-invalido")
        ]))
        with patch.object(main, "openai_client", return_value=FakeOpenAI([response])):
            with self.assertRaisesRegex(main.ToolFailure, "invalid tool arguments"):
                asyncio.run(main.run_agent(main.TicketRequest(
                    customer_email="ana@example.com",
                    title="Acesso bloqueado",
                    description="Cliente não consegue acessar o portal.",
                    priority="high",
                )))

    def test_guidance_request_still_calls_llm(self):
        from agent.chat import ChatRequest, run_chat

        article = main.KnowledgeArticle(id="KB-001", title="Redefinir acesso", summary="Confirme o e-mail.", tags=["senha"])
        llm = FakeOpenAI([
            FakeCompletion(FakeMessage([FakeToolCall("search_help_articles", '{"query":"senha portal"}')]))
        ])
        with patch.object(main, "openai_client", return_value=llm), \
             patch.object(main, "search_help_articles", new=AsyncMock(return_value=main.KnowledgeSearchResponse(articles=[article]))):
            result = asyncio.run(run_chat(ChatRequest(message="Como resolvo minha senha? Ainda não preciso de ticket.")))
        self.assertEqual(result.tools_called, ["search_help_articles"])
        self.assertEqual(result.usage.total_tokens, 15)

if __name__ == "__main__":
    unittest.main()
