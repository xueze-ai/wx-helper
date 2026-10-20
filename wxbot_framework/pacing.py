"""文本发送前的可配置随机等待；仅控制节奏，不保证账号风控结果。"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class TextSendDelay:
    enabled: bool = True
    characters_per_group: int = 10
    min_seconds: float = 0.1
    max_seconds: float = 0.3

    @classmethod
    def from_config(cls, options=None):
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise ValueError("text_send_delay 必须是 JSON 对象")
        result = cls(enabled=options.get("enabled", True),
                     characters_per_group=options.get("characters_per_group", 10),
                     min_seconds=options.get("min_seconds", 0.1),
                     max_seconds=options.get("max_seconds", 0.3))
        if type(result.enabled) is not bool:
            raise ValueError("text_send_delay.enabled 必须是布尔值")
        if type(result.characters_per_group) is not int or not 1 <= result.characters_per_group <= 10000:
            raise ValueError("text_send_delay.characters_per_group 应为 1–10000 的整数")
        for value in (result.min_seconds, result.max_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 60:
                raise ValueError("text_send_delay 的等待秒数应为 0–60 的有限数值")
        if result.min_seconds > result.max_seconds:
            raise ValueError("text_send_delay.min_seconds 不能大于 max_seconds")
        return result

    def seconds_for(self, text):
        if not self.enabled:
            return 0.0
        # 按 Unicode 字符计数；不足一组向上取整，每组独立抽取等待时间。
        groups = (len(text) + self.characters_per_group - 1) // self.characters_per_group
        return sum(random.uniform(self.min_seconds, self.max_seconds) for _ in range(groups))
