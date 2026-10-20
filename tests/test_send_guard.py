"""发送保护：间隔、冷却、分钟限流和重复内容抑制。"""
import unittest

from wxbot_framework.sdk import ServiceFacade
from wxbot_framework.sendguard import SendGuard, SendGuardConfig


class MemoryBackend:
    def __init__(self):
        self.session_id = "session"
        self.sent = []

    def send_text(self, chat, text):
        self.sent.append((chat, text))
        return True

    def stop(self):
        return {"dll_unloaded": True}


class SendGuardTests(unittest.TestCase):
    def test_default_is_disabled(self):
        self.assertFalse(SendGuardConfig.from_config(None).enabled)
        self.assertTrue(SendGuardConfig.from_config({}).enabled)

    def test_duplicate_content_is_suppressed_by_facade(self):
        facade = ServiceFacade(text_delay={"enabled": False}, send_guard={
            "enabled": True, "min_interval_seconds": 0, "per_chat_seconds": 0,
            "max_per_minute": 100, "duplicate_window_seconds": 60, "jitter_ratio": 0})
        backend = MemoryBackend()
        facade.bind(backend)
        facade.send_text("room", "你好")
        facade.send_text("room", "你好")
        facade.send_text("room", "别的")
        self.assertEqual(backend.sent, [("room", "你好"), ("room", "别的")])

    def test_disabled_guard_sends_same_content_twice(self):
        facade = ServiceFacade(text_delay={"enabled": False})
        backend = MemoryBackend()
        facade.bind(backend)
        facade.send_text("room", "你好")
        facade.send_text("room", "你好")
        self.assertEqual(len(backend.sent), 2)

    def test_sendguard_reports_waits(self):
        guard = SendGuard({"enabled": True, "min_interval_seconds": 0.05,
                           "per_chat_seconds": 0.5, "max_per_minute": 100,
                           "duplicate_window_seconds": 0, "jitter_ratio": 0})
        guard.note_sent("room", "a")
        delay, duplicate = guard.delay_for("room", "b")
        self.assertFalse(duplicate)
        self.assertGreater(delay, 0.0)
        self.assertLessEqual(delay, 0.6)

    def test_minute_limit_reports_long_wait(self):
        guard = SendGuard({"enabled": True, "min_interval_seconds": 0,
                           "per_chat_seconds": 0, "max_per_minute": 1,
                           "duplicate_window_seconds": 0, "jitter_ratio": 0})
        guard.note_sent("room", "a")
        delay, _ = guard.delay_for("room", "b")
        self.assertGreater(delay, 30.0)


if __name__ == "__main__":
    unittest.main()
