"""好友、群聊和文件传输助手的原生文本服务。"""
from __future__ import annotations

import logging
import time
import ctypes as c
from collections import deque

from filehelper_service import NativeFileHelperService, ReceivedTextMessage
from wxbot_framework.mentions import parse_at_members
from run_selftest import TEXT_DLL, K, check, modules, wechat_candidates, run
from wxbot_framework.text import TextRouter, validate_target, validate_mentions
from wxbot_framework.account import read_current_account, read_current_profile, select_account_process, validate_account
from wxbot_framework.contacts import read_contact_name

logger = logging.getLogger("NativeText")


def run_text(mode, **options):
    return run(mode, bridge_path=TEXT_DLL, **options)


def inspect_account(pid):
    """只读核对候选进程的当前账号，不向候选进程注入 DLL。"""
    core = modules(pid).get("weixin.dll")
    if not core:
        return None
    process = check(K.OpenProcess(0x410, False, pid), "只读核对微信账号")
    try:
        def read(address, size):
            buffer = c.create_string_buffer(size)
            count = c.c_size_t()
            check(K.ReadProcessMemory(process, address, buffer, size, c.byref(count)), "只读核对账号字段")
            return buffer.raw[:count.value]
        return read_current_account(read, core[0])
    finally:
        K.CloseHandle(process)


def find_account_process(account=None):
    candidates = []
    for pid in wechat_candidates():
        try:
            candidates.append((pid, inspect_account(pid)))
        except OSError:
            # 枚举后退出的进程不参与选择。
            pass
    return select_account_process(candidates, account)


class NativeTextService(NativeFileHelperService):
    _dll = TEXT_DLL
    _poll_export = b"WxPollTextMessageV2"
    _message_type = ReceivedTextMessage
    _run = staticmethod(run_text)

    def __init__(self, *, account=None, **options):
        super().__init__(**options)
        self.preferred_account = validate_account(account) if account is not None else None
        self.router = TextRouter(include_self_groups=True)
        self.chats = {}
        self._next_account_check = 0
        self._sent_texts = deque(maxlen=64)
        self.mention_messages = 0

    @property
    def self_wxid(self):
        return self.router.self_wxid

    def start(self):
        self.router = TextRouter(include_self_groups=True)
        self.chats = {}
        self._sent_texts.clear()
        if self.pid is None:
            self.pid = find_account_process(self.preferred_account)
        elif self.preferred_account and inspect_account(self.pid) != self.preferred_account:
            # 手动指定 PID 时也先只读核对，避免向其他账号加载 DLL。
            raise RuntimeError("目标进程登录的不是绑定的机器人账号，等待正确账号登录")
        result = super().start()
        logger.info("当前账号已自动识别：%s；好友和群聊文本已就绪", self.self_wxid)
        return result

    def _read_account(self):
        return read_current_account(self._channel.read_memory, self._channel.core_base)

    def _prepare_channel(self):
        account = self._read_account()
        if not account:
            raise RuntimeError("微信尚未完成登录，自动等待当前账号就绪")
        if self.preferred_account and account != self.preferred_account:
            raise RuntimeError("目标进程登录的不是绑定的机器人账号，等待正确账号登录")
        self.router.set_account(account)
        self._next_account_check = time.monotonic() + 1

    def _check_account(self, *, force=False):
        if not force and time.monotonic() < self._next_account_check:
            return
        account = self._read_account()
        if not account or account != self.self_wxid:
            error = RuntimeError("微信已退出登录或当前账号发生变化，重新连接并自动识别")
            self.last_error = error
            self._stop.set()
            raise error
        self._next_account_check = time.monotonic() + 1

    def _before_poll(self):
        self._check_account()

    def _sender_wechat_name(self, wxid):
        if self._channel is None:
            return None
        name = read_contact_name(self._channel.read_memory, self._channel.core_base, wxid,
                                 expected_account=self.self_wxid)
        if name is None and wxid == self.self_wxid:
            profile = read_current_profile(self._channel.read_memory, self._channel.core_base)
            name = profile["name"] if profile and profile["wxid"] == wxid else None
        return name

    def _map_message(self, message):
        if message.type != 1:
            return None
        previous = self.self_wxid
        previous_unparsed = self.router.unparsed_groups
        data = self.router.convert(message.sender.decode("utf-8"), message.receiver.decode("utf-8"),
                                   message.text.decode("utf-8"), sequence=message.sequence,
                                   thread_id=message.thread_id)
        if self.self_wxid and not previous:
            logger.info("当前账号已识别；好友和群聊文本已就绪")
        if data:
            name = self._sender_wechat_name(data["sender_id"])
            if name:
                data["sender_wechat_name"] = name
            source = getattr(message, "mention_source", b"").decode("utf-8", errors="replace")
            data["at_members"] = parse_at_members(source)
            data["is_at_self"] = bool(data["room_wxid"] and self.self_wxid in data["at_members"])
            if data["at_members"]:
                self.mention_messages += 1
            if data["is_self"] and any(chat == data["chat_id"] and text == data["msg"] and
                                       time.monotonic() - sent_at < 120
                                       for chat, text, sent_at in tuple(self._sent_texts)):
                self.router.ignored_self += 1
                data["bot_generated"] = True
                data["dispatch_to_plugins"] = False
            elif data["is_at_self"]:
                data["dispatch_to_plugins"] = True
            if "WXBOT_AT_CHAT_PROBE" in data["msg"]:
                import json
                from pathlib import Path
                path = Path(__file__).resolve().parents[1] / ".runtime/at_receive_probe.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"room": data["chat_id"], "sender": data["sender_id"],
                                            "at_members": data["at_members"], "is_at_self": data["is_at_self"],
                                            "source": source}, ensure_ascii=False, indent=2), encoding="utf-8")
                logger.info("艾特接收探针：at_members=%s is_at_self=%s", data["at_members"], data["is_at_self"])
            entry = self.chats.setdefault(data["chat_id"], {})
            entry.update({"chat_id": data["chat_id"],
                          "is_group": bool(data["room_wxid"])})
            if data["room_wxid"]:
                # 微信接口不提供群名称；保存最近一条消息和发言人，供控制台辨认群。
                entry["last_text"] = " ".join(str(data["msg"]).split())[:120]
                entry["last_sender"] = name or data["sender_id"]
                entry["last_sender_id"] = data["sender_id"]
                entry["last_time"] = time.time()
            if data["msg"].startswith(("WXBOT_", "echo WXBOT_")):
                logger.info("测试消息已接收：chat_id=%s sender_id=%s group=%s marker=%s",
                            data["chat_id"], data["sender_id"], bool(data["room_wxid"]), data["msg"])
        elif self.router.unparsed_groups > previous_unparsed:
            # 第一条失败时提示；后续通过 status 查看累计数。
            if self.router.unparsed_groups == 1:
                logger.warning("群文本作者格式未匹配，已跳过；需要用测试群消息核对实际布局")
        return data

    def send_text(self, to_wxid, content):
        return self._send_text(to_wxid, content)

    def send_at_text(self, room_wxid, content, at_list):
        members = validate_mentions(room_wxid, at_list)
        return self._send_text(room_wxid, content, mentions=members)

    def _send_text(self, to_wxid, content, *, mentions=None):
        validate_target(to_wxid)
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("原生文本服务没有运行")
            self._check_account(force=True)
            self._sent_texts.append((to_wxid, content, time.monotonic()))
            self.last_result = self._run("--dispatch", pid=self.pid, text=content, target=to_wxid,
                                        keep_loaded=True, preserve_report=self.preserve_reports, mentions=mentions)
            if content.startswith("WXBOT_"):
                logger.info("测试文本发送完成：chat_id=%s marker=%s success=%s native_thread=%s",
                            to_wxid, content, self.last_result["success"],
                            self.last_result.get("report", {}).get("thread_id"))
            return self.last_result["success"]
