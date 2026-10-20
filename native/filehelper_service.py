"""微信 4.1.15.13 原生文本服务；测试范围仅为文件传输助手。"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import logging
import threading
from pathlib import Path

import pefile

from run_selftest import DLL, TEXT_DLL, K, check, loaded_bridges, modules, remote_call, run

logger = logging.getLogger("NativeFileHelper")
K.GetExitCodeProcess.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]


class ReceivedMessage(c.Structure):
    _fields_ = [("size", c.c_uint32), ("present", c.c_uint32),
                ("type", c.c_uint32), ("thread_id", c.c_uint32),
                ("dropped", c.c_uint32), ("reserved", c.c_uint32),
                ("sequence", c.c_uint64), ("sender", c.c_char * 128),
                ("receiver", c.c_char * 128), ("text", c.c_char * 1024)]


assert c.sizeof(ReceivedMessage) == 1312


class ReceivedTextMessage(ReceivedMessage):
    _fields_ = [("mention_source", c.c_char * 4096)]


assert c.sizeof(ReceivedTextMessage) == 5408


class ReceiveChannel:
    def __init__(self, pid, *, dll=DLL, poll_export=b"WxPollFileHelperMessage", message_type=ReceivedMessage):
        self.message_type = message_type
        self.process = None
        self.buffer = None
        self.pending = False
        loaded_modules = modules(pid)
        loaded = loaded_modules.get(dll.name.lower())
        if not loaded or Path(loaded[1]).resolve() != dll.resolve():
            raise RuntimeError("原生文本桥接模块未加载")
        pe = pefile.PE(str(dll))
        self.address = loaded[0] + next(item.address for item in pe.DIRECTORY_ENTRY_EXPORT.symbols
                                       if item.name == poll_export)
        self.core_base = loaded_modules["weixin.dll"][0]
        self.process = check(K.OpenProcess(0x43a, False, pid), "打开接收通道")
        try:
            self.buffer = check(K.VirtualAllocEx(self.process, None, c.sizeof(self.message_type),
                                                 0x3000, 0x04), "分配接收缓冲区")
        except Exception:
            self.close()
            raise

    def poll(self):
        if self.pending:
            raise RuntimeError("上次接收线程超时，不能继续复用缓冲区")
        try:
            code = remote_call(self.process, self.address, self.buffer)
        except TimeoutError:
            self.pending = True
            raise
        if code:
            raise RuntimeError(f"接收队列读取失败：{code}")
        message = self.message_type()
        read = c.c_size_t()
        check(K.ReadProcessMemory(self.process, self.buffer, c.byref(message), c.sizeof(message),
                                  c.byref(read)), "读取原生消息副本")
        if read.value != c.sizeof(message) or message.size != c.sizeof(message):
            raise RuntimeError("接收结构大小不匹配")
        if message.dropped:
            raise RuntimeError(f"接收队列已溢出，丢弃了 {message.dropped} 条消息")
        return message if message.present else None

    def read_memory(self, address, size):
        buffer = c.create_string_buffer(size)
        read = c.c_size_t()
        check(K.ReadProcessMemory(self.process, address, buffer, size, c.byref(read)), "读取当前登录账号")
        if read.value != size:
            raise ValueError("账号字段读取不完整")
        return buffer.raw

    def close(self):
        if self.process:
            # 超时线程可能仍持有参数，保留缓冲区直到客户端退出。
            if self.buffer and not self.pending:
                K.VirtualFreeEx(self.process, self.buffer, 0, 0x8000)
            K.CloseHandle(self.process)
        self.process = self.buffer = None

    def process_exited(self):
        if not self.process:
            return False
        code = w.DWORD()
        check(K.GetExitCodeProcess(self.process, c.byref(code)), "检查微信进程状态")
        return code.value != 259


class NativeFileHelperService:
    """提供原项目 register_msg_handler / send_text 的基础接口。"""

    _dll = DLL
    _poll_export = b"WxPollFileHelperMessage"
    _message_type = ReceivedMessage
    _run = staticmethod(run)

    def __init__(self, *, pid=None, preserve_reports=True):
        self.pid = pid
        self.preserve_reports = preserve_reports
        self.msg_handlers = []
        self.last_result = None
        self.last_error = None
        self._channel = None
        self._thread = None
        self._stop = threading.Event()
        self._calls = threading.RLock()
        self._owns_bridge = False

    def register_msg_handler(self, handler):
        self.msg_handlers.append(handler)

    @staticmethod
    def _app_root(path):
        """从 native/build_text/*.dll 反推程序目录；非标准目录返回 None。"""
        try:
            resolved = Path(path).resolve()
            if resolved.parents[1].name.lower() != "native":
                return None
            return resolved.parents[2]
        except (IndexError, OSError):
            return None

    @staticmethod
    def _other_bot_running(root):
        """另一个目录的机器人是否仍持有实例锁。"""
        lock = root / ".runtime/bot.lock"
        if not lock.exists():
            return False
        try:
            from wxbot_framework.locking import InstanceLock
            with InstanceLock(lock):
                return False
        except RuntimeError:
            return True
        except OSError:
            return False

    def _cleanup_loaded(self):
        """清理微信进程中残留的桥接 DLL；不碰仍在运行的其他机器人。"""
        entries = loaded_bridges(self.pid)
        if not entries:
            return []
        current = self._app_root(self._dll)
        results = []
        for entry in entries:
            owner = self._app_root(entry["path"])
            if owner is not None and owner != current and self._other_bot_running(owner):
                logger.warning("桥接 DLL 正被另一个机器人使用：%s；请先在那个控制台安全停止", owner)
                continue
            bridge = TEXT_DLL if entry["name"].lower() == TEXT_DLL.name.lower() else DLL
            try:
                result = run("--remove", pid=entry["pid"], bridge_path=bridge)
            except Exception as error:
                logger.warning("清理残留桥接失败：PID=%s 错误=%s", entry["pid"], error)
                continue
            results.append(result)
            logger.info("已清理残留桥接：PID=%s DLL=%s", entry["pid"], entry["name"])
        return results

    def start(self):
        if self._owns_bridge:
            raise RuntimeError("服务已经启动")
        self._stop.clear()
        self.last_error = None
        try:
            armed = self._run("--listen", pid=self.pid)
        except RuntimeError as error:
            if "桥接 DLL 已加载" not in str(error):
                raise
            logger.warning("检测到上一次未卸载的桥接 DLL，正在自动清理后重试")
            self._cleanup_loaded()
            try:
                armed = self._run("--listen", pid=self.pid)
            except RuntimeError as retry_error:
                raise RuntimeError(
                    f"{retry_error}；请点击控制台的“清理残留桥接”，或重启微信后再试") from error
        self.pid = armed["pid"]
        self._owns_bridge = True
        if armed["code"]:
            self.stop()
            raise RuntimeError(f"接收 Hook 启动失败：{armed['code']}")
        try:
            self._channel = ReceiveChannel(self.pid, dll=self._dll, poll_export=self._poll_export,
                                           message_type=self._message_type)
            self._prepare_channel()
            self._thread = threading.Thread(target=self._receive_loop, name="WxFileHelperReceiver", daemon=True)
            self._thread.start()
        except Exception:
            self.stop()
            raise
        return True

    def _receive_loop(self):
        try:
            while not self._stop.is_set():
                with self._calls:
                    self._before_poll()
                    message = self._channel.poll()
                if message is None:
                    self._stop.wait(0.3)
                    continue
                data = self._map_message(message)
                if data is None:
                    continue
                for handler in list(self.msg_handlers):
                    try:
                        handler(self.pid, 11046, data, self)
                    except Exception:
                        logger.exception("Python 消息处理器异常")
        except Exception as error:
            self.last_error = error
            self._stop.set()
            logger.exception("原生接收服务停止")

    def _prepare_channel(self):
        """子类在开始消息线程前读取必要的当前连接信息。"""

    def _before_poll(self):
        """子类检查当前登录状态；旧文件传输助手后端保持原行为。"""

    def _map_message(self, message):
        sender = message.sender.decode("utf-8")
        receiver = message.receiver.decode("utf-8")
        if "filehelper" not in (sender, receiver) or message.type != 1:
            raise RuntimeError("收到范围外的消息副本")
        text = message.text.decode("utf-8")
        return {"msg": text, "content": text, "from_wxid": "filehelper", "room_wxid": "",
                "native_sender": sender, "native_receiver": receiver,
                "sequence": message.sequence, "native_thread_id": message.thread_id}

    def send_text(self, to_wxid, content):
        if to_wxid != "filehelper":
            raise NotImplementedError("当前验证版只开放文件传输助手")
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("接收服务没有运行")
            self.last_result = self._run("--dispatch", pid=self.pid, text=content, keep_loaded=True,
                                   preserve_report=self.preserve_reports)
            return self.last_result["success"]

    def send_payload(self, payload):
        if payload.get("type") != 11237:
            raise NotImplementedError("当前验证版只支持文本发送 11237")
        data = payload["data"]
        return self.send_text(data["to_wxid"], data["content"])

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=35)
            if self._thread.is_alive():
                raise RuntimeError("接收线程尚未退出，保留 DLL，稍后再尝试停止")
            self._thread = None
        with self._calls:
            process_exited = bool(self._channel and self._channel.process_exited())
            if self._channel:
                self._channel.close()
                self._channel = None
            if self._owns_bridge:
                if process_exited:
                    self._owns_bridge = False
                    return {"code": 0, "pid": self.pid, "process_exited": True, "dll_unloaded": True}
                if not loaded_bridges(self.pid):
                    # 原生 DLL 已被外部清理或微信刚刚退出，不需要再次卸载。
                    self._owns_bridge = False
                    return {"code": 0, "pid": self.pid, "dll_unloaded": True,
                            "already_unloaded": True}
                result = self._run("--remove", pid=self.pid)
                if result["code"] or not result.get("dll_unloaded"):
                    raise RuntimeError(f"桥接模块未卸载：{result}")
                self._owns_bridge = False
                return result
            # 启动失败时 _owns_bridge 仍为 False，但进程里可能残留上一次的 DLL。
            cleaned = self._cleanup_loaded()
            if cleaned:
                self._owns_bridge = False
                return cleaned[-1]

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()
