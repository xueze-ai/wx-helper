"""大模型用量统计：token 解析、费用估算和 SQLite 汇总。"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wxbot_framework.llm import LLMClient
from wxbot_framework.usage import UsageStore, provider_name


class UsageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "usage.sqlite3"
        self.store = UsageStore(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_record_and_summary(self):
        self.store.record(session="mention_chat:room", plugin="mention_chat", chat_id="room",
                          provider="DeepSeek", model="deepseek-chat", context_limit=64000,
                          input_tokens=100, output_tokens=50, total_tokens=150,
                          cost=0.001, currency="USD")
        self.store.record(session="mention_chat:room", plugin="mention_chat", chat_id="room",
                          provider="DeepSeek", model="deepseek-chat", context_limit=64000,
                          input_tokens=200, output_tokens=100, reasoning_tokens=30,
                          cache_read_tokens=20, total_tokens=300, cost=0.002, currency="USD")
        rows = self.store.summary()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["calls"], 2)
        self.assertEqual(row["input_tokens"], 300)
        self.assertEqual(row["output_tokens"], 150)
        self.assertEqual(row["reasoning_tokens"], 30)
        self.assertEqual(row["cache_read_tokens"], 20)
        self.assertEqual(row["total_tokens"], 450)
        self.assertAlmostEqual(row["total_cost"], 0.003)
        self.assertEqual(row["latest_total_tokens"], 300)
        totals = self.store.totals()
        self.assertEqual(totals["sessions"], 1)
        self.assertEqual(totals["calls"], 2)
        self.store.clear()
        self.assertEqual(self.store.totals()["calls"], 0)

    def test_provider_names(self):
        self.assertEqual(provider_name("https://api.deepseek.com/v1"), "DeepSeek")
        self.assertEqual(provider_name("https://llm-x.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"),
                         "阿里云百炼")
        self.assertEqual(provider_name("https://example.com/v1"), "example.com")


class LLMUsageTests(unittest.TestCase):
    def test_client_records_usage_and_cost(self):
        temporary = tempfile.TemporaryDirectory()
        try:
            store = UsageStore(Path(temporary.name) / "usage.sqlite3")
            options = {
                "api_key": "dummy-test-key",
                "model": "deepseek-chat",
                "context_limit": 8000,
                "pricing": {"input_per_million": 1.0, "output_per_million": 2.0,
                            "cache_read_per_million": 0.1, "cache_write_per_million": 0.5,
                            "currency": "USD"},
            }
            client = LLMClient(options, usage_store=store,
                               session={"plugin": "mention_chat", "chat_id": "room@chatroom"})
            response = io.BytesIO(json.dumps({
                "choices": [{"message": {"content": "{\"reply\":\"ok\"}"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500,
                          "prompt_tokens_details": {"cached_tokens": 200},
                          "completion_tokens_details": {"reasoning_tokens": 120}},
            }).encode())
            with patch("wxbot_framework.llm.urlopen", return_value=response):
                self.assertEqual(client.complete_json("system", "user"), {"reply": "ok"})
            row = store.summary()[0]
            self.assertEqual(row["session"], "mention_chat:room@chatroom")
            self.assertEqual(row["input_tokens"], 1000)
            self.assertEqual(row["output_tokens"], 500)
            self.assertEqual(row["reasoning_tokens"], 120)
            self.assertEqual(row["cache_read_tokens"], 200)
            self.assertEqual(row["context_limit"], 8000)
            self.assertAlmostEqual(row["total_cost"], 0.00182, places=6)
        finally:
            temporary.cleanup()

    def test_direct_cost_is_preferred(self):
        temporary = tempfile.TemporaryDirectory()
        try:
            store = UsageStore(Path(temporary.name) / "usage.sqlite3")
            client = LLMClient({"api_key": "dummy-test-key", "model": "m"},
                               usage_store=store, session="test")
            response = io.BytesIO(json.dumps({
                "choices": [{"message": {"content": "{\"reply\":\"ok\"}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                          "cost": 0.25},
            }).encode())
            with patch("wxbot_framework.llm.urlopen", return_value=response):
                client.complete_json("system", "user")
            self.assertAlmostEqual(store.summary()[0]["total_cost"], 0.25)
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
