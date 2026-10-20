"""消息模型和插件上下文；插件不需要接触 Hook 或 DLL。"""
from __future__ import annotations

import copy
import logging
import threading
import time
from dataclasses import dataclass, field
from contextlib import nullcontext, contextmanager
from types import MappingProxyType
from typing import Mapping, Any

from .text import validate_target, validate_mentions
from .pacing import TextSendDelay
from .sendguard import SendGuard

logger = logging.getLogger("TextSend")


class CapabilityError(NotImplementedError):
    pass


class ReplyClaims:
    """同一条入站消息只允许一个插件回复。

    插件仍可连续发送同一条回复的多个分段（同一 owner 放行），但其他插件
    在观察到 owner 后不会再发第二条回复。这样既保留分段发送，也避免
    group_fun 与 mention_chat 等插件同时处理一条消息时刷屏。
    """

    def __init__(self, *, ttl_seconds=600.0):
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.Lock()
        self._claims = {}

    @staticmethod
    def _key(message):
        # sequence 在原生消息中稳定；received_at 兜底覆盖 sequence=0 的测试/旧消息。
        return (message.session_id, message.sequence, message.chat_id,
                message.sender_id, round(float(message.received_at), 6))

    def _prune(self, now):
        if len(self._claims) < 256:
            return
        expired = [key for key, (_, stamp) in self._claims.items()
                   if now - stamp > self.ttl_seconds]
        for key in expired:
            self._claims.pop(key, None)

    def claim(self, message, owner):
        now = time.monotonic()
        key = self._key(message)
        with self._lock:
            self._prune(now)
            current = self._claims.get(key)
            if current is None:
                self._claims[key] = (owner, now)
                return True
            if current[0] == owner:
                self._claims[key] = (owner, now)
                return True
            return False

    def release(self, message, owner):
        key = self._key(message)
        with self._lock:
            current = self._claims.get(key)
            if current is not None and current[0] == owner:
                self._claims.pop(key, None)


@dataclass(frozen=True)
class Message:
    text: str
    chat_id: str
    sender_id: str = ""
    receiver_id: str = ""
    protocol: int = 11046
    type: int = 1
    is_group: bool = False
    is_self: bool = False
    sequence: int = 0
    session_id: str = ""
    received_at: float = field(default_factory=time.monotonic)
    data: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_native(cls, protocol, data, session_id):
        snapshot = copy.deepcopy(data)
        room = snapshot.get("room_wxid") or ""
        return cls(text=snapshot.get("msg") or snapshot.get("content") or "",
                   chat_id=snapshot.get("chat_id") or room or snapshot.get("from_wxid") or "",
                   sender_id=snapshot.get("sender_id") or snapshot.get("native_sender") or snapshot.get("from_wxid") or "",
                   receiver_id=snapshot.get("native_receiver") or "", protocol=protocol,
                   is_group=bool(room), is_self=bool(snapshot.get("is_self", False)), sequence=snapshot.get("sequence", 0),
                   session_id=session_id, data=MappingProxyType(snapshot))


class ServiceFacade:
    """序列化发送与连接切换，避免卸载正在使用的原生桥接。"""

    def __init__(self, *, text_delay=None, send_guard=None):
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._backend = None
        self._available = False
        self._cancel = threading.Event()
        self.text_delay = TextSendDelay.from_config(text_delay)
        self.send_guard = SendGuard(send_guard)

    def bind(self, backend):
        with self._lock:
            if self._backend is not None:
                raise RuntimeError("已有连接，需先关闭")
            self._backend = backend
            self._available = True
            self._cancel = threading.Event()

    @property
    def connected(self):
        with self._lock:
            return self._backend is not None and self._available

    def send_text(self, to_wxid, content, *, expected_session=None, _mentions=(), delivery_guard=None):
        validate_target(to_wxid)
        if not isinstance(content, str):
            raise ValueError("文本正文必须是字符串")
        encoded = content.encode("utf-8")
        if not encoded or len(encoded) > 1023 or b"\0" in encoded:
            raise ValueError("文本应为 1–1023 个 UTF-8 字节，且不能包含 NUL")
        with self._lock:
            if self._backend is None or not self._available:
                raise RuntimeError("微信尚未连接")
            if expected_session and expected_session != self._backend.session_id:
                raise RuntimeError("消息所属的微信连接已结束，跳过旧会话回复")
            if _mentions and not callable(getattr(self._backend, "send_at_text", None)):
                raise CapabilityError("当前后端尚未开放真实群艾特")
            backend, cancel = self._backend, self._cancel
        # 等待不占用连接锁，状态查询、断线清理和退出可以及时进行。
        # 发送锁覆盖等待及原生发送，多个插件不会同时等待后集中发送。
        with self._send_lock:
            with self._lock:
                self._check_pending(backend, cancel)
            seconds = self.text_delay.seconds_for(content)
            logger.debug("文本发送等待 %.3f 秒（%d 个字符）", seconds, len(content))
            if cancel.wait(seconds):
                raise RuntimeError("微信连接已结束，取消待发送文本")
            # 发送保护：全局间隔、同会话冷却、每分钟上限、重复内容抑制。
            while True:
                guard_delay, duplicate = self.send_guard.delay_for(to_wxid, content)
                if duplicate:
                    logger.info("已抑制 %s 在冷却窗口内的重复消息（%d 字）", to_wxid, len(content))
                    return True
                if guard_delay <= 0:
                    break
                logger.debug("发送保护等待 %.3f 秒（%s）", guard_delay, to_wxid)
                if cancel.wait(guard_delay):
                    raise RuntimeError("微信连接已结束，取消待发送文本")
                with self._lock:
                    self._check_pending(backend, cancel)
            with self._lock:
                self._check_pending(backend, cancel)
                # 在文本节奏等待之后检查记录版本，清空后不发送旧总结。
                with delivery_guard() if delivery_guard is not None else nullcontext():
                    if _mentions:
                        result = backend.send_at_text(to_wxid, content, _mentions)
                    else:
                        result = backend.send_text(to_wxid, content)
            self.send_guard.note_sent(to_wxid, content)
            return result

    def send_at_text(self, room_wxid, content, at_list, *, expected_session=None, display_names=None, delivery_guard=None):
        members = validate_mentions(room_wxid, at_list)
        if not isinstance(content, str) or not content:
            raise ValueError("文本正文必须是非空字符串")
        names = members if display_names is None else display_names
        if not isinstance(names, (list, tuple)) or len(names) != len(members) or any(
                not isinstance(name, str) or not name.strip() or len(name) > 64 or
                any(ch in name for ch in ("\n", "\r", "\0")) for name in names):
            raise ValueError("艾特显示昵称必须与成员列表一一对应，且每个昵称最多 64 字符")
        # 标签用于显示；真实通知始终使用 members 中的微信 ID。
        labels = " ".join("@" + name + "\u2005" for name in names)
        return self.send_text(room_wxid, labels + "\n" + content,
                              expected_session=expected_session, _mentions=members, delivery_guard=delivery_guard)

    def _check_pending(self, backend, cancel):
        if cancel.is_set() or not self._available or self._backend is not backend or self._cancel is not cancel:
            raise RuntimeError("微信连接已结束，取消待发送文本")

    def cancel_pending(self):
        with self._lock:
            self._available = False
            self._cancel.set()

    def send_payload(self, payload, *, expected_session=None):
        if payload.get("type") != 11237:
            raise CapabilityError("当前后端只支持文本发送 11237")
        data = payload["data"]
        return self.send_text(data["to_wxid"], data["content"], expected_session=expected_session)

    def close(self):
        with self._lock:
            self.cancel_pending()
            if self._backend is None:
                return None
            # 清理失败时仍持有此实例，下次可以继续清理，不能重新注入。
            result = self._backend.stop()
            self._backend = None
            return result


class LegacyService:
    def __init__(self, service, session_id):
        self._service, self._session_id = service, session_id

    def send_text(self, to_wxid, content):
        return self._service.send_text(to_wxid, content, expected_session=self._session_id)

    def send_payload(self, payload):
        return self._service.send_payload(payload, expected_session=self._session_id)


class PluginContext:
    def __init__(self, name, options, service, capabilities, *, llm_config_path=None,
                 member_store=None, usage_store=None, claims=None):
        self.name = name
        self.config = MappingProxyType(copy.deepcopy(options))
        self.logger = logging.getLogger(f"Plugin.{name}")
        self.capabilities = frozenset(capabilities)
        self._service = service
        self._llm_config_path = llm_config_path
        self._llm = None
        self._members = member_store
        self._usage_store = usage_store
        self._claims = claims

    def _claim_reply(self, message):
        if self._claims is None:
            return True
        if self._claims.claim(message, self.name):
            return True
        self.logger.info("同一消息已由其他插件回复，跳过重复发送：chat=%s", message.chat_id)
        return False

    @property
    def members(self):
        if self._members is None:
            raise CapabilityError("尚未配置框架成员目录")
        return self._members

    @property
    def llm(self):
        if self._llm is None:
            if self._llm_config_path is None:
                raise ValueError("尚未配置大模型文件")
            from .llm import LLMClient
            self._llm = LLMClient.from_file(
                self._llm_config_path, usage_store=self._usage_store,
                session={"plugin": self.name, "chat_id": ""})
        return self._llm

    def send_text(self, chat_id, text):
        return self._service.send_text(chat_id, text)

    def personality(self, message):
        from .personality import PersonalityState
        return self._members.personality(message.chat_id) if self._members is not None else PersonalityState(message.chat_id)

    def llm_for(self, message, *, personality=None):
        from .personality import PersonalityLLM
        state = personality if personality is not None else self.personality(message)
        return PersonalityLLM(self.llm, state, current=lambda: self._members is None or
                              self._members.personality_is_current(state),
                              session={"plugin": self.name, "chat_id": message.chat_id,
                                       "is_group": bool(message.is_group)})

    def personality_guard(self, state):
        return self._members.personality_delivery_guard(state) if self._members is not None else nullcontext()

    @contextmanager
    def response_guard(self, snapshot, personality):
        with self.members.delivery_guard(snapshot), self.personality_guard(personality):
            yield

    def reply(self, message, text, *, delivery_guard=None):
        if not self._claim_reply(message):
            return None
        try:
            return self._service.send_text(message.chat_id, text, expected_session=message.session_id,
                                           delivery_guard=delivery_guard)
        except BaseException:
            if self._claims is not None:
                self._claims.release(message, self.name)
            raise

    def reply_at(self, message, text, *, display_name=None, delivery_guard=None):
        if not message.is_group:
            raise ValueError("reply_at 仅用于群消息")
        if not self._claim_reply(message):
            return None
        if self._members is not None:
            member = self._members.find_by_wxid(message.sender_id, message.chat_id)
            if member is not None and (display_name is None or member.self_named):
                display_name = member.display_name
            elif display_name is None:
                display_name = self._members.wechat_name(message.sender_id)
        try:
            return self._service.send_at_text(message.chat_id, text, [message.sender_id],
                                              expected_session=message.session_id,
                                              display_names=[display_name] if display_name is not None else None,
                                              delivery_guard=delivery_guard)
        except BaseException:
            if self._claims is not None:
                self._claims.release(message, self.name)
            raise
