"""通过配置接入 OpenAI 兼容的大模型接口，不记录密钥或聊天正文。"""
from __future__ import annotations

import copy
import json
import logging
import math
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .usage import provider_name

logger = logging.getLogger("LLM")


class LLMError(RuntimeError):
    pass


def _first_int(*values):
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            return number
    return 0


class LLMClient:
    def __init__(self, options, *, usage_store=None, session=None):
        if not isinstance(options, dict):
            raise ValueError("大模型配置必须是 JSON 对象")
        base_url = options.get("base_url", "https://api.deepseek.com")
        if not isinstance(base_url, str):
            raise ValueError("大模型 base_url 必须是 HTTPS 地址字符串")
        self.base_url = base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("大模型 base_url 应为不含认证信息的 HTTPS 地址")
        self.model = options.get("model", "deepseek-flash")
        self._api_key = options.get("api_key", "")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("大模型 model 不能为空")
        if not isinstance(self._api_key, str) or not self._api_key.strip():
            raise ValueError("请在大模型配置文件中填写 api_key")
        self.timeout = options.get("timeout_seconds", 20)
        self.max_tokens = options.get("max_tokens", 512)
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or not 1 <= self.timeout <= 60:
            raise ValueError("timeout_seconds 应为 1–60 秒")
        if type(self.max_tokens) is not int or not 128 <= self.max_tokens <= 4096:
            raise ValueError("max_tokens 应为 128–4096 的整数")
        self.context_limit = options.get("context_limit", 128000)
        if type(self.context_limit) is not int or not 1000 <= self.context_limit <= 10_000_000:
            raise ValueError("context_limit 应为 1000–10000000 的整数")
        pricing = options.get("pricing") or {}
        if not isinstance(pricing, dict):
            raise ValueError("pricing 必须是 JSON 对象")
        self.pricing = {}
        for key in ("input_per_million", "output_per_million",
                    "cache_read_per_million", "cache_write_per_million"):
            value = pricing.get(key, 0)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"pricing.{key} 应为非负有限数值")
            self.pricing[key] = float(value)
        currency = pricing.get("currency", "USD")
        if (not isinstance(currency, str) or not currency.strip() or
                len(currency.strip()) > 12 or any(ord(ch) < 32 for ch in currency)):
            raise ValueError("pricing.currency 应为 1–12 个字符的币种名")
        self.pricing["currency"] = currency.strip()
        self.provider = provider_name(self.base_url)
        self.usage_store = usage_store
        self.session = session or {}

    @classmethod
    def from_file(cls, path, *, usage_store=None, session=None):
        return cls(json.loads(Path(path).read_text(encoding="utf-8-sig")),
                   usage_store=usage_store, session=session)

    def with_session(self, session):
        clone = copy.copy(self)
        clone.session = session or {}
        return clone

    def _cost(self, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, usage):
        direct = usage.get("cost", usage.get("total_cost")) if isinstance(usage, dict) else None
        if type(direct) in (int, float) and math.isfinite(direct) and direct >= 0:
            return float(direct)
        p = self.pricing
        # 缓存读取通常包含在 input_tokens 中，按缓存价计入；缓存写入按单独价格计入。
        billable_input = max(0, input_tokens - cache_read_tokens)
        return ((billable_input * p["input_per_million"] +
                 cache_read_tokens * p["cache_read_per_million"] +
                 cache_write_tokens * p["cache_write_per_million"] +
                 output_tokens * p["output_per_million"]) / 1_000_000.0)

    def _record_usage(self, usage, session):
        if self.usage_store is None:
            return
        usage = usage if isinstance(usage, dict) else {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        input_tokens = _first_int(usage.get("prompt_tokens"), usage.get("input_tokens"))
        output_tokens = _first_int(usage.get("completion_tokens"), usage.get("output_tokens"))
        reasoning_tokens = _first_int(completion_details.get("reasoning_tokens"),
                                      usage.get("reasoning_tokens"))
        cache_read = _first_int(prompt_details.get("cached_tokens"),
                                usage.get("prompt_cache_hit_tokens"),
                                usage.get("cache_read_input_tokens"))
        cache_write = _first_int(prompt_details.get("cache_creation_tokens"),
                                 usage.get("cache_creation_input_tokens"),
                                 usage.get("cache_write_tokens"))
        total_tokens = _first_int(usage.get("total_tokens")) or input_tokens + output_tokens
        cost = self._cost(input_tokens, output_tokens, cache_read, cache_write, usage)
        session = session if session is not None else self.session
        if isinstance(session, str):
            plugin, chat_id = session, ""
        elif isinstance(session, dict):
            plugin, chat_id = str(session.get("plugin") or ""), str(session.get("chat_id") or "")
        else:
            plugin, chat_id = "", ""
        key = f"{plugin or '未命名'}:{chat_id or '-'}"
        try:
            self.usage_store.record(
                session=key, plugin=plugin, chat_id=chat_id, provider=self.provider,
                model=self.model, context_limit=self.context_limit,
                input_tokens=input_tokens, output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens, cache_read_tokens=cache_read,
                cache_write_tokens=cache_write, total_tokens=total_tokens,
                cost=cost, currency=self.pricing["currency"])
        except Exception:
            # 统计失败不能影响机器人回复；只写一条不含正文的告警。
            logger.warning("大模型用量统计写入失败", exc_info=True)

    def complete_json(self, system, user, *, session=None):
        payload = {"model": self.model, "messages": [{"role": "system", "content": system},
                                                       {"role": "user", "content": user}],
                   "response_format": {"type": "json_object"}, "max_tokens": self.max_tokens,
                   "thinking": {"type": "disabled"}, "stream": False}
        request = Request(self.base_url + "/chat/completions",
                          data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Authorization": "Bearer " + self._api_key,
                                   "Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise LLMError("大模型响应过大")
            result = json.loads(raw)
            choice = result["choices"][0]
            if choice.get("finish_reason") == "length":
                raise LLMError("大模型响应被截断，请增加 max_tokens")
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise LLMError("大模型返回了空结果")
            data = json.loads(content)
            if not isinstance(data, dict):
                raise LLMError("大模型结果应为 JSON 对象")
            self._record_usage(result.get("usage"), session)
            return data
        except HTTPError as error:
            # 不输出响应正文或请求头，避免服务器错误回显密钥。
            raise LLMError(f"大模型接口 HTTP {error.code}；请核对密钥、余额和模型配置") from None
        except (URLError, TimeoutError, OSError):
            raise LLMError("大模型连接失败或超时，请稍后再试") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("大模型响应格式不正确，请重试") from None