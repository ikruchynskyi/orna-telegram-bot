"""Run with: python3 -m unittest -q test_model_usage"""
import importlib
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from llm import ollama_client
import telegram_bot
from bot import usage_stats


class ModelUsageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name in ("_llm_calls", "_llm_input_tokens", "_llm_output_tokens"):
            p = patch.object(usage_stats, name, Counter())
            p.start()
            self.addCleanup(p.stop)
        for module, name, value in (
            (usage_stats, "_STORE_PATH", Path(self.temp.name) / "usage.json"),
            (usage_stats, "_since", usage_stats._since),
            (ollama_client, "_cloud_model_override", None),
            (ollama_client, "_cloud_down_until", 0.0),
        ):
            p = patch.object(module, name, value)
            p.start()
            self.addCleanup(p.stop)

    def transport(self, handler):
        client = httpx.AsyncClient
        return patch.object(
            ollama_client.httpx, "AsyncClient",
            side_effect=lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
        )

    async def test_billing_fallback_override_and_tokens(self):
        for status in (402, 403):
            with self.subTest(status=status):
                requests = []
                ollama_client.set_cloud_model("chosen:cloud")

                def handle(request):
                    payload = json.loads(request.content)
                    requests.append((request.url.host, payload["model"]))
                    if request.url.host == "ollama.com":
                        return httpx.Response(status, json={"error": "insufficient credits"})
                    return httpx.Response(200, json={
                        "message": {"content": '{"action":"finish"}'},
                        "prompt_eval_count": 12, "eval_count": 7,
                    })

                with self.transport(handle):
                    result = await ollama_client.chat_json_with_fallback(
                        "env:cloud", "http://localhost:11434", "env:local", [])
                    self.assertEqual(result["action"], "finish")
                    self.assertTrue(ollama_client.cloud_is_parked())
                    await ollama_client.chat_json_with_fallback(
                        "env:cloud", "http://localhost:11434", "env:local", [])
                self.assertEqual(requests, [
                    ("ollama.com", "chosen:cloud"),
                    ("localhost", "env:local"), ("localhost", "env:local"),
                ])
        data = usage_stats.snapshot()
        self.assertEqual(data["llm_input_tokens"]["env:local (local)"], 48)
        self.assertEqual(data["llm_output_tokens"]["env:local (local)"], 28)
        self.assertNotIn("chosen:cloud (cloud)", data["llm_input_tokens"])

    async def test_busy_cloud_falls_back_without_parking(self):
        """429 = the account's concurrent-request cap is full: retried, then local
        for THIS call only. It must not park the cloud for everyone (ollama_client,
        OllamaBusy)."""
        requests = []
        ollama_client.set_cloud_model("chosen:cloud")

        def handle(request):
            requests.append((request.url.host, json.loads(request.content)["model"]))
            if request.url.host == "ollama.com":
                return httpx.Response(429, json={"error": "too many concurrent requests"})
            return httpx.Response(200, json={"message": {"content": '{"action":"finish"}'},
                                             "prompt_eval_count": 1, "eval_count": 1})

        with self.transport(handle), patch.object(ollama_client, "_BUSY_RETRY_DELAYS", (0, 0)):
            result = await ollama_client.chat_json_with_fallback(
                "env:cloud", "http://localhost:11434", "env:local", [])
        self.assertEqual(result["action"], "finish")
        self.assertFalse(ollama_client.cloud_is_parked())
        self.assertEqual(requests, [("ollama.com", "chosen:cloud")] * 3 + [("localhost", "env:local")])

    async def test_tokens_count_even_when_content_is_unusable(self):
        def handle(request):
            return httpx.Response(200, json={
                "message": {"content": "not JSON"},
                "prompt_eval_count": 20, "eval_count": 5,
            })

        with self.transport(handle), self.assertRaises(ollama_client.OllamaError):
            await ollama_client.chat_json("https://ollama.com", "bad:cloud", [])
        self.assertEqual(usage_stats.snapshot()["llm_input_tokens"]["bad:cloud (cloud)"], 20)

    def test_legacy_store_persistence_and_reset(self):
        usage_stats._STORE_PATH.write_text(json.dumps({"llm_calls": {"old (cloud)": 3}}))
        usage_stats._load()
        self.assertEqual(usage_stats.snapshot()["llm_calls"]["old (cloud)"], 3)
        self.assertEqual(usage_stats.snapshot()["llm_input_tokens"], {})
        usage_stats.record_llm_tokens("new", "cloud", 10, 0)
        usage_stats._llm_input_tokens.clear()
        usage_stats._llm_output_tokens.clear()
        usage_stats._load()
        self.assertEqual(usage_stats.snapshot()["llm_input_tokens"]["new (cloud)"], 10)
        self.assertEqual(usage_stats.snapshot()["llm_output_tokens"]["new (cloud)"], 0)
        with patch.object(usage_stats, "_commands", Counter()), \
                patch.object(usage_stats, "_orna_tools", Counter()), \
                patch.object(usage_stats, "_user_commands", {}), \
                patch.object(usage_stats, "_user_log", {}):
            usage_stats.reset()
        self.assertEqual(usage_stats.snapshot()["llm_input_tokens"], {})
        self.assertEqual(json.loads(usage_stats._STORE_PATH.read_text())["llm_output_tokens"], {})

    async def test_admin_only_model_command_and_stats(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=123))
        context = SimpleNamespace(args=["chosen:cloud"])
        for allowlist in ({456}, set()):
            with patch.object(telegram_bot, "GO_ALLOWED_USER_IDS", allowlist):
                await telegram_bot.handle_model(update, context)
        message.reply_text.assert_not_awaited()
        self.assertEqual(ollama_client.get_cloud_model("env:cloud"), "env:cloud")
        with patch.object(telegram_bot, "GO_ALLOWED_USER_IDS", {123}):
            await telegram_bot.handle_model(update, context)
            self.assertEqual(ollama_client.get_cloud_model("env:cloud"), "chosen:cloud")
            context.args = ["bad model"]
            await telegram_bot.handle_model(update, context)
            self.assertEqual(ollama_client.get_cloud_model("env:cloud"), "chosen:cloud")
            usage_stats.record_llm_call("chosen:cloud", "cloud")
            usage_stats.record_llm_tokens("chosen:cloud", "cloud", 100, 30)
            context.args = []
            await telegram_bot.handle_stats(update, context)
        text = "\n".join(call.args[0] for call in message.reply_text.await_args_list)
        self.assertIn("chosen:cloud (cloud)", text)
        self.assertIn("вхід 100, вихід 30", text)

    def test_override_is_not_persisted_on_reload(self):
        ollama_client.set_cloud_model("chosen:cloud")
        importlib.reload(ollama_client)
        self.assertEqual(ollama_client.get_cloud_model("env:cloud"), "env:cloud")


if __name__ == "__main__":
    unittest.main()
