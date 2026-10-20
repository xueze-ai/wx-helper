"""账号自动识别、登录切换和启动前准备的验证，不发送微信消息。"""
from __future__ import annotations

from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import Mock, patch

from wxbot_framework.account import (APP_GLOBAL_RVA, APP_VTABLE_RVA, CONTEXT_VTABLE_RVA,
                                     APP_CONTEXT_GETTER_RVA, IDENTITY_GETTER_RVA, read_current_account,
                                     select_account_process, read_current_profile)
from wxbot_framework.text import TextRouter

BASE, APP, CONTEXT, HEAP = 0x100000000, 0x300000000, 0x400000000, 0x500000000
SELF = "wxid_framework_current"


class AccountMemory:
    def __init__(self, account=SELF):
        self.regions = {}
        self.add(BASE + APP_GLOBAL_RVA, 8)
        self.add(APP, 0x80)
        self.add(BASE + APP_VTABLE_RVA, 0x80)
        self.add(CONTEXT, 0x100)
        self.add(BASE + CONTEXT_VTABLE_RVA, 0x20)
        self.add(BASE + IDENTITY_GETTER_RVA, 5)
        self.add(HEAP, 128)
        self.write(BASE + APP_GLOBAL_RVA, struct.pack("<Q", APP))
        self.write(APP, struct.pack("<Q", BASE + APP_VTABLE_RVA))
        self.write(APP + 0x68, struct.pack("<Q", CONTEXT))
        self.write(BASE + APP_VTABLE_RVA + 0x60, struct.pack("<Q", BASE + APP_CONTEXT_GETTER_RVA))
        self.write(CONTEXT, struct.pack("<Q", BASE + CONTEXT_VTABLE_RVA))
        self.write(BASE + CONTEXT_VTABLE_RVA + 0x18, struct.pack("<Q", BASE + IDENTITY_GETTER_RVA))
        self.write(BASE + IDENTITY_GETTER_RVA, b"\x48\x8d\x41\x48\xc3")
        self.set_account(account)
        # 微信号和账号 ID 是不同字段，不能把旁边的别名当成自身 ID。
        alias = b"alias_name"
        self.write(CONTEXT + 0x68, alias.ljust(16, b"\0") + struct.pack("<QQ", len(alias), 15))

    def add(self, address, size):
        self.regions[address] = bytearray(size)

    def region(self, address, size):
        for start, data in self.regions.items():
            if start <= address and address + size <= start + len(data):
                return data, address - start
        raise OSError("模拟账号对象已释放")

    def write(self, address, value):
        region, offset = self.region(address, len(value))
        region[offset:offset + len(value)] = value

    def read(self, address, size):
        region, offset = self.region(address, size)
        return bytes(region[offset:offset + size])

    def set_account(self, account):
        body = account.encode("ascii")
        if len(body) <= 15:
            header = body.ljust(16, b"\0") + struct.pack("<QQ", len(body), 15)
        else:
            self.write(HEAP, body)
            header = struct.pack("<QQQQ", HEAP, 0, len(body), 127)
        self.write(CONTEXT + 0x48, header)


class AccountTests(unittest.TestCase):
    def test_current_profile_reads_only_id_and_wechat_name(self):
        ram = AccountMemory()
        name = '测试号🌸'.encode('utf-8')
        ram.write(CONTEXT + 0x88, name.ljust(16, b'\0') + struct.pack('<QQ', len(name), 15))
        self.assertEqual(read_current_profile(ram.read, BASE), {'wxid': SELF, 'name': '测试号🌸'})
        ram.write(CONTEXT + 0x88, b'\0' * 16 + struct.pack('<QQ', 0, 15))
        self.assertIsNone(read_current_profile(ram.read, BASE))

    def test_current_profile_change_during_snapshot_is_rejected(self):
        ram = AccountMemory()
        ram.write(CONTEXT + 0x88, b'DemoUser'.ljust(16, b'\0') + struct.pack('<QQ', 5, 15))
        changed = False
        def changing(address, size):
            nonlocal changed
            value = ram.read(address, size)
            if address == CONTEXT + 0x88 and size == 32 and not changed:
                changed = True
                ram.set_account('wxid_different')
            return value
        self.assertIsNone(read_current_profile(changing, BASE))

    def test_bound_account_is_selected_regardless_of_pid_or_enumeration_order(self):
        for candidates in ([(123, "wxid_personal"), (456, SELF)],
                           [(789, SELF), (123, "wxid_personal")]):
            expected = next(pid for pid, account in candidates if account == SELF)
            self.assertEqual(select_account_process(candidates, SELF), expected)

    def test_bound_account_does_not_fall_back_to_personal_account(self):
        with self.assertRaisesRegex(RuntimeError, "等待绑定"):
            select_account_process([(123, "wxid_personal"), (456, None)], SELF)
        with self.assertRaisesRegex(RuntimeError, "等待唯一"):
            select_account_process([(123, SELF), (456, SELF)], SELF)

    def test_unbound_selection_uses_unique_logged_account_and_rejects_ambiguity(self):
        self.assertEqual(select_account_process([(123, None), (456, SELF)]), 456)
        with self.assertRaisesRegex(RuntimeError, "多个微信账号"):
            select_account_process([(123, SELF), (456, "wxid_personal")])
        with self.assertRaisesRegex(RuntimeError, "等待微信"):
            select_account_process([(123, None)])

    def test_live_layout_reads_id_not_alias_for_inline_and_heap_strings(self):
        for account in (SELF, "old_username"):
            ram = AccountMemory(account)
            self.assertEqual(read_current_account(ram.read, BASE), account)

    def test_not_logged_in_missing_context_and_unexpected_layout_are_not_cached(self):
        for address, value in ((CONTEXT + 0x48, b"\0" * 16 + struct.pack("<QQ", 0, 15)),
                               (APP + 0x68, b"\0" * 8),
                               (CONTEXT, struct.pack("<Q", BASE + 0x1234)),
                               (BASE + IDENTITY_GETTER_RVA, b"\0" * 5)):
            ram = AccountMemory()
            ram.write(address, value)
            self.assertIsNone(read_current_account(ram.read, BASE))

    def test_malformed_or_non_account_strings_are_rejected(self):
        for account in ("filehelper", "12345@chatroom", "gh_public", "weixin"):
            ram = AccountMemory(account)
            self.assertIsNone(read_current_account(ram.read, BASE))
        ram = AccountMemory()
        ram.write(CONTEXT + 0x48 + 16, struct.pack("<QQ", 128, 127))
        self.assertIsNone(read_current_account(ram.read, BASE))

    def test_account_string_changes_during_snapshot_are_retried(self):
        ram = AccountMemory()
        reads = 0

        def changing_read(address, size):
            nonlocal reads
            if address == CONTEXT + 0x48 and size == 32:
                reads += 1
                if reads == 2:
                    ram.set_account("wxid_new_account")
            return ram.read(address, size)

        self.assertIsNone(read_current_account(changing_read, BASE))
        self.assertEqual(read_current_account(ram.read, BASE), "wxid_new_account")

    def test_login_context_changes_during_snapshot_are_retried(self):
        ram = AccountMemory()
        reads = 0

        def changing_read(address, size):
            nonlocal reads
            if address == APP + 0x68:
                reads += 1
                if reads == 2:
                    ram.write(APP + 0x68, b"\0" * 8)
            return ram.read(address, size)

        self.assertIsNone(read_current_account(changing_read, BASE))

    def test_first_private_message_works_without_filehelper_initialization(self):
        router = TextRouter()
        router.set_account(read_current_account(AccountMemory().read, BASE))
        data = router.convert("wxid_friend", SELF, "echo 开机直接用")
        self.assertEqual(data["chat_id"], "wxid_friend")
        self.assertEqual(data["msg"], "echo 开机直接用")
        self.assertEqual(router.unidentified, 0)

    @unittest.skipUnless(sys.platform == "win32", "本机原生服务检查")
    def test_service_prepares_identity_before_receive_thread(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "native"))
        from text_service import NativeTextService
        service = NativeTextService()
        service._read_account = Mock(return_value=SELF)
        service._prepare_channel()
        self.assertEqual(service.self_wxid, SELF)
        self.assertIsNone(service._thread)
        service._read_account.return_value = None
        with self.assertRaisesRegex(RuntimeError, "自动等待"):
            service._prepare_channel()

    @unittest.skipUnless(sys.platform == "win32", "本机原生服务检查")
    def test_explicit_wrong_pid_is_rejected_before_loading_bridge(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "native"))
        from text_service import NativeTextService
        service = NativeTextService(pid=123, account=SELF)
        service._run = Mock()
        with patch("text_service.inspect_account", return_value="wxid_personal"):
            with self.assertRaisesRegex(RuntimeError, "不是绑定"):
                service.start()
        service._run.assert_not_called()
        self.assertFalse(service._owns_bridge)

    @unittest.skipUnless(sys.platform == "win32", "本机原生服务检查")
    def test_explicit_pid_cannot_override_bound_account(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "native"))
        from text_service import NativeTextService
        service = NativeTextService(pid=123, account=SELF)
        service._read_account = Mock(return_value="wxid_personal")
        with self.assertRaisesRegex(RuntimeError, "不是绑定"):
            service._prepare_channel()
        self.assertEqual(service.self_wxid, "")

    @unittest.skipUnless(sys.platform == "win32", "本机原生服务检查")
    def test_send_rechecks_account_and_rejects_old_login_before_native_call(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "native"))
        from text_service import NativeTextService
        service = NativeTextService()
        service.router.set_account(SELF)
        service._owns_bridge = True
        service._read_account = Mock(return_value="wxid_different_account")
        service._run = Mock()
        with self.assertRaisesRegex(RuntimeError, "当前账号发生变化"):
            service.send_text("wxid_friend", "不能通过新账号发送旧回复")
        service._run.assert_not_called()
        self.assertTrue(service._stop.is_set())
        self.assertIsNotNone(service.last_error)


if __name__ == "__main__":
    unittest.main()
