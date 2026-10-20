"""大模型调用统计：按插件/会话累计 tokens、耗时和费用。

统计数据库放在 .runtime/llm_usage.sqlite3，只记录 token 数量、模型、耗时
和费用，不记录聊天正文或 API Key。每条记录对应一次成功的模型调用。
"""
from __future__ import annotations

import math
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

BEIJING = timezone(timedelta(hours=8))

_PROVIDER_NAMES = {
    "deepseek.com": "DeepSeek",
    "openai.com": "OpenAI",
    "moonshot.cn": "Moonshot",
    "bigmodel.cn": "智谱",
    "aliyuncs.com": "阿里云百炼",
    "siliconflow.cn": "硅基流动",
    "volces.com": "火山方舟",
    "qwen.ai": "通义千问",
    "zhipuai.cn": "智谱",
}


def provider_name(base_url):
    """把 base_url 转成界面上可读的提供商名，不发起网络请求。"""
    try:
        host = (urlsplit(str(base_url)).hostname or "").lower()
    except ValueError:
        host = ""
    if not host:
        return "未知"
    for suffix, name in _PROVIDER_NAMES.items():
        if host == suffix or host.endswith("." + suffix):
            return name
    return host


def _non_negative_int(value):
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _non_negative_float(value):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _clean_text(value, limit=200):
    text = str(value or "").strip()
    return text[:limit]


class UsageStore:
    """SQLite 统计存储；每次操作独立连接，方便控制台同时读取。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        try:
            db.row_factory = sqlite3.Row
            yield db
            db.commit()
        finally:
            db.close()

    def _initialize(self):
        with self._lock, self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS llm_calls(
                    id INTEGER PRIMARY KEY,
                    session TEXT NOT NULL,
                    plugin TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    context_limit INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    cost REAL NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    created_at TEXT NOT NULL,
                    last_active TEXT NOT NULL
                )""")
            db.execute("CREATE INDEX IF NOT EXISTS llm_calls_by_session ON llm_calls(session, id)")
            db.execute("CREATE INDEX IF NOT EXISTS llm_calls_by_time ON llm_calls(last_active)")

    def record(self, *, session, plugin="", chat_id="", provider="", model="", context_limit=0,
               input_tokens=0, output_tokens=0, reasoning_tokens=0, cache_read_tokens=0,
               cache_write_tokens=0, total_tokens=0, cost=0.0, currency="USD", created_at=None):
        now = datetime.now(BEIJING).isoformat(timespec="seconds")
        created_at = created_at or now
        total = _non_negative_int(total_tokens) or (_non_negative_int(input_tokens) +
                                                    _non_negative_int(output_tokens))
        row = (str(session)[:200], _clean_text(plugin, 80), _clean_text(chat_id, 200),
               _clean_text(provider, 80), _clean_text(model, 120), _non_negative_int(context_limit),
               _non_negative_int(input_tokens), _non_negative_int(output_tokens),
               _non_negative_int(reasoning_tokens), _non_negative_int(cache_read_tokens),
               _non_negative_int(cache_write_tokens), total, _non_negative_float(cost),
               _clean_text(currency, 12) or "USD", created_at, now)
        with self._lock, self._connect() as db:
            db.execute("""INSERT INTO llm_calls(
                session, plugin, chat_id, provider, model, context_limit,
                input_tokens, output_tokens, reasoning_tokens, cache_read_tokens,
                cache_write_tokens, total_tokens, cost, currency, created_at, last_active)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", row)

    def summary(self, limit=500):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("统计查询 limit 应为 1–10000 的整数")
        with self._connect() as db:
            rows = db.execute("""
                SELECT c.session,
                       c.plugin,
                       c.chat_id,
                       COUNT(*) AS calls,
                       COALESCE(SUM(c.input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(c.output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(c.reasoning_tokens), 0) AS reasoning_tokens,
                       COALESCE(SUM(c.cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(c.cache_write_tokens), 0) AS cache_write_tokens,
                       COALESCE(SUM(c.total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(c.cost), 0) AS total_cost,
                       MIN(c.created_at) AS created_at,
                       MAX(c.last_active) AS last_active,
                       (SELECT provider FROM llm_calls x WHERE x.session=c.session ORDER BY x.id DESC LIMIT 1) AS provider,
                       (SELECT model FROM llm_calls x WHERE x.session=c.session ORDER BY x.id DESC LIMIT 1) AS model,
                       (SELECT context_limit FROM llm_calls x WHERE x.session=c.session ORDER BY x.id DESC LIMIT 1) AS context_limit,
                       (SELECT total_tokens FROM llm_calls x WHERE x.session=c.session ORDER BY x.id DESC LIMIT 1) AS latest_total_tokens,
                       (SELECT currency FROM llm_calls x WHERE x.session=c.session ORDER BY x.id DESC LIMIT 1) AS currency
                FROM llm_calls c
                GROUP BY c.session
                ORDER BY last_active DESC
                LIMIT ?""", (_non_negative_int(limit) or 500,)).fetchall()
        return [dict(row) for row in rows]

    def totals(self):
        with self._connect() as db:
            row = db.execute("""
                SELECT COUNT(DISTINCT session) AS sessions,
                       COUNT(*) AS calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(cost), 0) AS total_cost,
                       (SELECT currency FROM llm_calls ORDER BY id DESC LIMIT 1) AS currency
                FROM llm_calls""").fetchone()
        return dict(row) if row else {"sessions": 0, "calls": 0, "input_tokens": 0, "output_tokens": 0,
                                      "reasoning_tokens": 0, "cache_read_tokens": 0,
                                      "cache_write_tokens": 0, "total_tokens": 0,
                                      "total_cost": 0.0, "currency": "USD"}

    def clear(self):
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM llm_calls")

    def close(self):
        """兼容生命周期调用；当前实现每次操作都独立连接，无需常驻关闭。"""
        return None