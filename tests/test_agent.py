import asyncio
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("TICKET_API_URL", "http://tickets.test")
os.environ.setdefault("TICKET_SERVICE_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).parents[1]))

from agent import main


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


if __name__ == "__main__":
    unittest.main()
