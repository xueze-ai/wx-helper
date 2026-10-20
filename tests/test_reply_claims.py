"""同一入站消息的回复归属：只允许一个插件回复，允许该插件分段发送。"""
import unittest

from wxbot_framework.sdk import Message, PluginContext, ReplyClaims


class FakeService:
    def __init__(self):
        self.sent = []

    def send_text(self, chat_id, text, *, expected_session=None, delivery_guard=None):
        self.sent.append((chat_id, text))
        return True

    def send_at_text(self, chat_id, text, mentions, *, expected_session=None,
                     display_names=None, delivery_guard=None):
        self.sent.append((chat_id, text))
        return True


def message(sequence=1, text="你好"):
    return Message(text=text, chat_id="123@chatroom", sender_id="wxid_friend",
                   is_group=True, sequence=sequence, session_id="session-1")


class ReplyClaimsTests(unittest.TestCase):
    def setUp(self):
        self.claims = ReplyClaims()
        self.service = FakeService()
        self.first = PluginContext("group_fun", {}, self.service, set(), claims=self.claims)
        self.second = PluginContext("mention_chat", {}, self.service, set(), claims=self.claims)

    def test_only_first_plugin_replies(self):
        incoming = message()
        self.assertTrue(self.first.reply(incoming, "菜单"))
        self.assertIsNone(self.second.reply(incoming, "你好呀"))
        self.assertEqual(self.service.sent, [("123@chatroom", "菜单")])

    def test_same_plugin_can_send_multiple_parts(self):
        incoming = message()
        self.first.reply(incoming, "第一段")
        self.first.reply(incoming, "第二段")
        self.assertIsNone(self.second.reply(incoming, "重复回复"))
        self.assertEqual(len(self.service.sent), 2)

    def test_different_messages_are_independent(self):
        self.first.reply(message(sequence=1), "第一条")
        self.second.reply(message(sequence=2), "第二条")
        self.assertEqual(len(self.service.sent), 2)

    def test_failed_send_releases_claim(self):
        class BrokenService(FakeService):
            def send_text(self, *args, **kwargs):
                raise RuntimeError("连接已断开")

        broken = PluginContext("broken", {}, BrokenService(), set(), claims=self.claims)
        with self.assertRaises(RuntimeError):
            broken.reply(message(sequence=3), "失败")
        self.second.reply(message(sequence=3), "我来回复")
        self.assertEqual(len(self.service.sent), 1)

    def test_claim_matching_uses_message_identity(self):
        first = ReplyClaims()
        incoming = message(sequence=7)
        self.assertTrue(first.claim(incoming, "a"))
        self.assertFalse(first.claim(incoming, "b"))
        self.assertTrue(first.claim(incoming, "a"))

    def test_at_reply_also_uses_claim(self):
        incoming = message(sequence=9)
        self.first.reply_at(incoming, "群回复")
        self.assertIsNone(self.second.reply_at(incoming, "另一个群回复"))
        self.assertEqual(len(self.service.sent), 1)


if __name__ == "__main__":
    unittest.main()
