"""发送保护：全局间隔、同会话冷却、分钟限流和重复内容抑制。

这些措施只能降低被风控注意到的概率，不能保证账号安全；账号安全最终取决于
使用方式、账号历史和微信的风控策略。默认关闭，配置 bot_config.json 的
send_guard 后启用。
"""
from __future__ import annotations

import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class SendGuardConfig:
    enabled: bool = False
    min_interval_seconds: float = 0.8
    per_chat_seconds: float = 3.0
    max_per_minute: int = 20
    duplicate_window_seconds: float = 20.0
    jitter_ratio: float = 0.25

    @classmethod
    def from_config(cls, options=None):
        if options is None:
            return cls()
        if not isinstance(options, dict):
            raise ValueError("send_guard 必须是 JSON 对象")
        result = cls(
            enabled=options.get("enabled", True),
            min_interval_seconds=options.get("min_interval_seconds", 0.8),
            per_chat_seconds=options.get("per_chat_seconds", 3.0),
            max_per_minute=options.get("max_per_minute", 20),
            duplicate_window_seconds=options.get("duplicate_window_seconds", 20.0),
            jitter_ratio=options.get("jitter_ratio", 0.25),
        )
        if type(result.enabled) is not bool:
            raise ValueError("send_guard.enabled 必须是布尔值")
        for name in ("min_interval_seconds", "per_chat_seconds", "duplicate_window_seconds"):
            value = getattr(result, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 600:
                raise ValueError(f"send_guard.{name} 应为 0–600 的有限数值")
        if type(result.max_per_minute) is not int or not 1 <= result.max_per_minute <= 600:
            raise ValueError("send_guard.max_per_minute 应为 1–600 的整数")
        if type(result.jitter_ratio) not in (int, float) or not math.isfinite(result.jitter_ratio) \
                or not 0 <= result.jitter_ratio <= 1:
            raise ValueError("send_guard.jitter_ratio 应为 0–1 的有限数值")
        return result


class SendGuard:
    """记录发送时间，计算下一次发送前需要等待的秒数。"""

    def __init__(self, config=None):
        self.config = config if isinstance(config, SendGuardConfig) else SendGuardConfig.from_config(config)
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._chat_next = {}
        self._minute_window = deque()
        self._recent = {}

    @property
    def enabled(self):
        return self.config.enabled

    def delay_for(self, chat_id, content):
        """返回 (等待秒数, 是否为重复内容)。"""
        if not self.config.enabled:
            return 0.0, False
        now = time.monotonic()
        with self._lock:
            while self._minute_window and now - self._minute_window[0] >= 60:
                self._minute_window.popleft()
            due = max(self._next_at, self._chat_next.get(chat_id, 0.0))
            if len(self._minute_window) >= self.config.max_per_minute:
                due = max(due, self._minute_window[0] + 60)
            duplicate = False
            window = self.config.duplicate_window_seconds
            if window > 0:
                last = self._recent.get((chat_id, content))
                duplicate = last is not None and now - last < window
            return max(0.0, due - now), duplicate

    def note_sent(self, chat_id, content):
        if not self.config.enabled:
            return
        now = time.monotonic()
        with self._lock:
            base = self.config.min_interval_seconds
            if base > 0:
                jitter = self.config.jitter_ratio
                due = base * (1 + random.uniform(-jitter, jitter))
                self._next_at = now + max(0.0, due)
            else:
                self._next_at = now
            self._chat_next[chat_id] = now + self.config.per_chat_seconds
            self._minute_window.append(now)
            if self.config.duplicate_window_seconds > 0:
                self._recent[(chat_id, content)] = now
            self._prune(now)

    def _prune(self, now):
        if len(self._chat_next) > 2000:
            cutoff = now - 3600
            self._chat_next = {key: value for key, value in self._chat_next.items() if value >= cutoff}
        if len(self._recent) > 4000:
            cutoff = now - max(600.0, self.config.duplicate_window_seconds * 4)
            self._recent = {key: value for key, value in self._recent.items() if value >= cutoff}