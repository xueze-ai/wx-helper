"""微信机器人可视化控制台（tkinter）。

源码运行：python gui.py
打包运行：wx-helper.exe

界面只负责编辑三个 JSON 配置、启动/停止框架和查看日志；真正的收发仍由
wxbot_framework 完成，和 python main.py 使用同一套逻辑。
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, font as tkfont
from tkinter import messagebox, simpledialog, ttk

from wxbot_framework.config import ConfigStore
from wxbot_framework.locking import InstanceLock
from wxbot_framework.paths import app_root
from wxbot_framework.runtime import BotApplication
from wxbot_framework.usage import UsageStore

APP_NAME = "微信机器人控制台"
APP_TITLE = APP_NAME
APP_VERSION = "1.2.0"
APP_AUTHOR = "薛泽（Xue Ze）"
APP_COPYRIGHT = "Copyright © 2026 薛泽（Xue Ze）"
PROJECT_URL = "https://github.com/xueze-ai/wx-helper"
PROJECT_REPO = "xueze-ai/wx-helper"
DEFAULT_UPDATE_REPO = "xueze-ai/wx-helper"
WECHAT_VERSION = "4.1.15.13"
CONFIG_STEMS = ("bot_config", "members_config", "llm_config")
DEFAULT_MEMBERS = {
    "database_file": ".runtime/member_data.sqlite3",
    "history_enabled": True,
    "daily_clear": True,
    "groups": {},
    "group_labels": {},
}


# ---------------------------------------------------------------------------
# 配置文件与日志
# ---------------------------------------------------------------------------

def ensure_configs(root):
    """首次运行时从 example 模板生成本地配置，不覆盖已有文件。"""
    for stem in CONFIG_STEMS:
        destination = Path(root) / f"{stem}.json"
        if destination.exists():
            continue
        example = Path(root) / f"{stem}.example.json"
        if not example.exists():
            raise FileNotFoundError(f"缺少配置模板：{example.name}")
        shutil.copyfile(example, destination)


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return copy.deepcopy(default)
    except json.JSONDecodeError as error:
        raise ValueError(f"{Path(path).name} 不是合法 JSON：{error}") from error


def _write_temp_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".wxbot_", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    return temporary


def save_json(path, data):
    temporary = _write_temp_json(path, data)
    try:
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_validated_json(path, data, validator):
    """先写临时文件并用真实加载器校验，通过后再原子替换。"""
    temporary = _write_temp_json(path, data)
    try:
        validator(temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class GuiLogHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        try:
            self.sink.put_nowait(self.format(record))
        except Exception:
            pass


def setup_logging(root, level):
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if getattr(handler, "_wxbot_gui", False):
            root_logger.removeHandler(handler)
            handler.close()
    root_logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    directory = Path(root) / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(directory / "bot.log", maxBytes=2 * 1024 * 1024,
                                       backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler._wxbot_gui = True
    root_logger.addHandler(file_handler)
    queue_handler = GuiLogHandler(LOG_QUEUE)
    queue_handler.setFormatter(formatter)
    queue_handler._wxbot_gui = True
    root_logger.addHandler(queue_handler)


LOG_QUEUE = queue.Queue()


# ---------------------------------------------------------------------------
# 机器人线程
# ---------------------------------------------------------------------------

class BotController:
    def __init__(self, root, store):
        self.root = Path(root)
        self.store = store
        self.application = None
        self.thread = None
        self._lock = None
        self.last_error = ""

    @property
    def running(self):
        return bool(self.thread is not None and self.thread.is_alive())

    @property
    def connected(self):
        return bool(self.application is not None and self.application.connected)

    @property
    def needs_cleanup(self):
        """线程已结束，但原生桥接/实例锁尚未清理干净。"""
        app = self.application
        return bool(app is not None and (app.backend is not None or self._lock is not None))

    def start(self):
        if self.running:
            return
        if self.needs_cleanup:
            raise RuntimeError("上一次停止尚未完成，请先点击“清理残留桥接”或重试停止")
        self.store.load()
        lock = InstanceLock(self.root / ".runtime/bot.lock").acquire()
        try:
            application = BotApplication(self.root, self.store)
        except BaseException:
            lock.close()
            raise
        self._lock = lock
        self.application = application
        self.last_error = ""
        self.thread = threading.Thread(target=self._run, args=(application,),
                                       name="BotApplication", daemon=True)
        self.thread.start()

    def _run(self, application):
        try:
            application.run()
        except BaseException as error:
            self.last_error = f"{type(error).__name__}: {error}"
            logging.getLogger("GUI").exception("机器人运行异常结束")
        finally:
            # 清理失败时保留 application 和实例锁，下次“停止/清理”还能重试；
            # 不能丢掉 backend 后重新注入，否则就会残留 DLL。
            if application.backend is None:
                if self.thread is threading.current_thread():
                    self.application = None
                self._release_lock()

    def _release_lock(self):
        lock, self._lock = self._lock, None
        if lock is not None:
            try:
                lock.close()
            except Exception:
                logging.getLogger("GUI").exception("释放实例锁失败")

    def stop(self, timeout=45):
        application = self.application
        if application is not None:
            application.stop_event.set()
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError("机器人仍在停止：可能正在发送消息或清理原生连接，请稍后重试")
        # run() 的 finally 已经尝试过清理；如果 backend 还在，说明清理失败，
        # 这里显式再调用一次 stop() 完成 Quit 并卸载 DLL。
        if application is not None and application.backend is not None:
            application.stop()
        self.thread = None
        self.application = None
        self._release_lock()


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------

class ControlPanel:
    def __init__(self, window):
        self.window = window
        self.root_dir = app_root()
        os.chdir(self.root_dir)
        ensure_configs(self.root_dir)
        self.store = ConfigStore(self.root_dir / "bot_config.json")
        self.bot = self.store.load()
        self.llm_path = self._config_path(self.bot.get("llm_config_file", "llm_config.json"))
        member_name = self.bot.get("members_config_file")
        self.members_path = self._config_path(member_name) if member_name else None
        self.llm = load_json(self.llm_path, {})
        self.members = load_json(self.members_path, DEFAULT_MEMBERS) if self.members_path \
            else copy.deepcopy(DEFAULT_MEMBERS)
        self.prefs = self._load_prefs()
        self.logs = LOG_QUEUE
        self.startup_link = self._startup_link_path()
        self.editor_plugin = None
        self._closing = False
        self.stats_store = UsageStore(self.root_dir / ".runtime/llm_usage.sqlite3")
        self.update_info = None
        self.update_pending = self.prefs.get("pending_update") if isinstance(
            self.prefs.get("pending_update"), dict) else None
        self.update_check_running = False
        self.update_download_running = False
        self.controller = BotController(self.root_dir, self.store)
        setup_logging(self.root_dir, self.bot.get("log_level", "INFO"))
        self._build_window()
        self._refresh_plugin_table()
        self._refresh_status()
        self._load_log_file()
        self._poll_logs()
        self._tick_status()
        self._tick_plugins()
        self._refresh_stats()
        self._tick_stats()
        self.window.after(8000, self._auto_check_updates)

    # -- 路径与偏好 --------------------------------------------------------

    def _config_path(self, name):
        path = Path(name)
        return path if path.is_absolute() else (self.root_dir / path)

    @property
    def prefs_path(self):
        return self.root_dir / ".runtime/gui_prefs.json"

    def _load_prefs(self):
        try:
            data = load_json(self.prefs_path, {"auto_start": False})
            return data if isinstance(data, dict) else {"auto_start": False}
        except Exception:
            return {"auto_start": False}

    def _save_prefs(self):
        try:
            save_json(self.prefs_path, self.prefs)
        except Exception:
            logging.getLogger("GUI").debug("保存界面偏好失败", exc_info=True)

    def _startup_link_path(self):
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return (Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup/wx-helper.lnk")

    # -- 构建界面 ----------------------------------------------------------

    def _build_window(self):
        self.window.title(APP_TITLE)
        self.window.minsize(880, 640)
        self.window.protocol("WM_DELETE_WINDOW", self._on_close)
        self.window.report_callback_exception = self._report_callback_exception
        try:
            style = ttk.Style(self.window)
            if "vista" in style.theme_names():
                style.theme_use("vista")
            style.configure("Treeview", rowheight=26)
            family = "Microsoft YaHei UI"
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                tkfont.nametofont(name).configure(family=family, size=10)
            tkfont.nametofont("TkFixedFont").configure(family="Consolas", size=9)
        except tk.TclError:
            pass
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(self.window)
        self.notebook.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 4))
        self.tab_dashboard = ttk.Frame(self.notebook, padding=14)
        self.tab_basic = ttk.Frame(self.notebook, padding=14)
        self.tab_llm = ttk.Frame(self.notebook, padding=14)
        self.tab_members = ttk.Frame(self.notebook, padding=14)
        self.tab_plugins = ttk.Frame(self.notebook, padding=14)
        self.tab_stats = ttk.Frame(self.notebook, padding=14)
        self.tab_logs = ttk.Frame(self.notebook, padding=14)
        self.tab_about = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(self.tab_dashboard, text="运行状态")
        self.notebook.add(self.tab_basic, text="基础设置")
        self.notebook.add(self.tab_llm, text="大模型")
        self.notebook.add(self.tab_members, text="群与成员")
        self.notebook.add(self.tab_plugins, text="插件")
        self.notebook.add(self.tab_stats, text="统计")
        self.notebook.add(self.tab_logs, text="日志")
        self.notebook.add(self.tab_about, text="关于")

        status_bar = ttk.Frame(self.window, padding=(12, 4))
        status_bar.grid(row=1, column=0, sticky="ew")
        status_bar.columnconfigure(0, weight=1)
        self.var_footer = tk.StringVar(value=f"目录：{self.root_dir}")
        ttk.Label(status_bar, textvariable=self.var_footer, foreground="#666").grid(row=0, column=0, sticky="w")
        ttk.Label(status_bar, text="配置保存后即时生效；连接/账号类修改需重启机器人",
                  foreground="#666").grid(row=0, column=1, sticky="e")

        self._build_dashboard()
        self._build_basic()
        self._build_llm()
        self._build_members()
        self._build_plugins()
        self._build_stats()
        self._build_logs()
        self._build_about()

        width, height = 1000, 760
        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        width, height = min(width, max(880, screen_w - 80)), min(height, max(640, screen_h - 80))
        self.window.geometry(f"{width}x{height}+{(screen_w - width) // 2}+{(screen_h - height) // 2}")

    def _report_callback_exception(self, exc_type, exc_value, exc_traceback):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        logging.getLogger("GUI").error("界面操作异常\n%s", text)
        messagebox.showerror("界面错误", f"{exc_type.__name__}: {exc_value}")

    # -- 运行状态 ----------------------------------------------------------

    def _build_dashboard(self):
        tab = self.tab_dashboard
        tab.columnconfigure(0, weight=1)
        ttk.Label(tab, text="微信机器人", font=("Microsoft YaHei UI", 20, "bold")).grid(
            row=0, column=0, sticky="w")
        ttk.Label(tab, text="在群里艾特机器人或私聊它；配置与命令行版本完全通用。",
                  foreground="#555").grid(row=1, column=0, sticky="w", pady=(2, 14))

        info = ttk.LabelFrame(tab, text="连接状态", padding=14)
        info.grid(row=2, column=0, sticky="nsew")
        info.columnconfigure(1, weight=1)
        self.var_state = tk.StringVar(value="已停止")
        self.var_connected = tk.StringVar(value="—")
        self.var_pid = tk.StringVar(value="—")
        self.var_account = tk.StringVar(value="—")
        self.var_plugins = tk.StringVar(value="—")
        self.var_error = tk.StringVar(value="—")
        rows = (("运行状态", self.var_state), ("微信连接", self.var_connected),
                ("微信进程", self.var_pid), ("机器人账号", self.var_account),
                ("插件", self.var_plugins), ("最近错误", self.var_error))
        for index, (label, variable) in enumerate(rows):
            ttk.Label(info, text=label + "：").grid(row=index, column=0, sticky="nw", pady=3)
            ttk.Label(info, textvariable=variable, wraplength=680).grid(
                row=index, column=1, sticky="nw", pady=3, padx=(8, 0))

        buttons = ttk.Frame(tab)
        buttons.grid(row=3, column=0, sticky="ew", pady=(16, 8))
        self.button_start = ttk.Button(buttons, text="启动机器人", command=self._start_bot)
        self.button_start.pack(side="left")
        self.button_stop = ttk.Button(buttons, text="停止机器人（Quit）", command=self._stop_bot, state="disabled")
        self.button_stop.pack(side="left", padx=8)
        self.button_cleanup = ttk.Button(buttons, text="清理残留桥接", command=self._cleanup_bridge)
        self.button_cleanup.pack(side="left")
        ttk.Button(buttons, text="刷新状态", command=self._refresh_status).pack(side="left", padx=8)
        ttk.Button(buttons, text="重新加载配置", command=self._reload_all).pack(side="left")

        options = ttk.LabelFrame(tab, text="启动选项", padding=14)
        options.grid(row=4, column=0, sticky="ew", pady=(8, 8))
        self.var_auto_start = tk.BooleanVar(value=bool(self.prefs.get("auto_start")))
        ttk.Checkbutton(options, text="打开控制台后自动启动机器人",
                        variable=self.var_auto_start,
                        command=self._save_auto_start).pack(anchor="w")
        self.var_startup = tk.BooleanVar(
            value=bool(self.startup_link and self.startup_link.exists()))
        ttk.Checkbutton(options, text="登录 Windows 后自动打开控制台并启动（创建启动快捷方式）",
                        variable=self.var_startup,
                        command=self._toggle_startup).pack(anchor="w", pady=(6, 0))

        hint = ("使用前请打开微信并登录机器人账号。若提示“拒绝访问”，请右键以管理员身份运行。\n"
                "停止时请使用“停止机器人（Quit）”，等日志出现“框架已停止”再关闭窗口；\n"
                "直接结束进程会在微信里残留桥接 DLL。残留时点“清理残留桥接”即可恢复。")
        ttk.Label(tab, text=hint, foreground="#666", justify="left").grid(
            row=5, column=0, sticky="w", pady=(8, 0))
        tab.rowconfigure(2, weight=1)

    def _refresh_status(self):
        app = self.controller.application
        running = self.controller.running
        pending = self.controller.needs_cleanup
        if running:
            self.var_state.set("运行中")
            self.var_connected.set("已连接微信" if self.controller.connected else "等待微信登录 / 注入桥接…")
            self.var_pid.set(str(app.pid) if app and app.pid else "—")
            active = []
            if app is not None:
                try:
                    active = [row["name"] for row in app.plugins.status() if row["active"]]
                except Exception:
                    active = []
            self.var_plugins.set(f"{len(active)} 个运行中" + (f"：{'、'.join(active)}" if active else ""))
            error = (app.last_connection_error if app is not None else "") or self.controller.last_error
        elif pending:
            self.var_state.set("已停止（待清理）")
            self.var_connected.set("—")
            self.var_pid.set("—")
            self.var_plugins.set("—")
            error = self.controller.last_error or "上次停止没有卸载桥接 DLL，请点“清理残留桥接”"
        else:
            self.var_state.set("已停止")
            self.var_connected.set("—")
            self.var_pid.set("—")
            self.var_plugins.set("—")
            error = self.controller.last_error
        self.var_account.set(self.bot.get("wechat_account") or "自动识别唯一登录账号")
        self.var_error.set(error or "无")
        self.button_start.configure(state="disabled" if running or pending else "normal")
        self.button_stop.configure(state="normal" if running or pending else "disabled")
        self.var_footer.set(f"目录：{self.root_dir}    |    配置：{self.store.path}")

    def _tick_status(self):
        if self._closing:
            return
        self._refresh_status()
        self.window.after(1000, self._tick_status)

    def _tick_plugins(self):
        if self._closing:
            return
        self._refresh_plugin_table()
        self.window.after(3000, self._tick_plugins)

    def _save_auto_start(self):
        self.prefs["auto_start"] = bool(self.var_auto_start.get())
        self._save_prefs()

    # -- 基础设置 ----------------------------------------------------------

    def _build_basic(self):
        tab = self.tab_basic
        for column in (0, 1):
            tab.columnconfigure(column, weight=1, uniform="basic")

        connection = ttk.LabelFrame(tab, text="微信连接", padding=12)
        connection.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))
        connection.columnconfigure(1, weight=1)

        self.var_account_input = tk.StringVar(value=self.bot.get("wechat_account") or "")
        self.var_pid_input = tk.StringVar(value="" if self.bot.get("wechat_pid") is None
                                          else str(self.bot.get("wechat_pid")))
        self.var_backend = tk.StringVar(value=self.bot.get("backend", "native_text"))
        self.var_reconnect = tk.StringVar(value=str(self.bot.get("reconnect_seconds", 5)))

        ttk.Label(connection, text="机器人微信 ID").grid(row=0, column=0, sticky="w", pady=4)
        account_row = ttk.Frame(connection)
        account_row.grid(row=0, column=1, sticky="ew", pady=4, padx=(8, 0))
        account_row.columnconfigure(0, weight=1)
        ttk.Entry(account_row, textvariable=self.var_account_input).grid(row=0, column=0, sticky="ew")
        self.button_detect = ttk.Button(account_row, text="检测", width=7,
                                        command=self._detect_account)
        self.button_detect.grid(row=0, column=1, padx=(6, 0))
        ttk.Label(connection, text="留空时自动选择唯一登录账号；多开必须填写。",
                  foreground="#666", wraplength=380).grid(row=1, column=0, columnspan=2, sticky="w")

        ttk.Label(connection, text="微信进程 PID").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(connection, textvariable=self.var_pid_input).grid(
            row=2, column=1, sticky="ew", pady=4, padx=(8, 0))
        ttk.Label(connection, text="只用于排查多开问题，通常留空。", foreground="#666").grid(
            row=3, column=0, columnspan=2, sticky="w")

        ttk.Label(connection, text="收发后端").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Combobox(connection, textvariable=self.var_backend, state="readonly",
                     values=("native_text", "native_filehelper")).grid(
            row=4, column=1, sticky="ew", pady=4, padx=(8, 0))
        ttk.Label(connection, text="native_text：好友+群聊；native_filehelper：仅文件传输助手。",
                  foreground="#666", wraplength=380).grid(row=5, column=0, columnspan=2, sticky="w")

        ttk.Label(connection, text="重连间隔（秒）").grid(row=6, column=0, sticky="w", pady=4)
        ttk.Spinbox(connection, textvariable=self.var_reconnect, from_=0.1, to=3600,
                    increment=0.5).grid(row=6, column=1, sticky="ew", pady=4, padx=(8, 0))

        runtime = ttk.LabelFrame(tab, text="运行与日志", padding=12)
        runtime.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(0, 8))
        runtime.columnconfigure(1, weight=1)
        self.var_log_level = tk.StringVar(value=self.bot.get("log_level", "INFO"))
        self.var_console = tk.BooleanVar(value=bool(self.bot.get("console", True)))
        self.var_queue = tk.StringVar(value=str(self.bot.get("plugin_queue_size", 128)))

        ttk.Label(runtime, text="日志级别").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(runtime, textvariable=self.var_log_level, state="readonly",
                     values=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")).grid(
            row=0, column=1, sticky="ew", pady=4, padx=(8, 0))
        ttk.Checkbutton(runtime, text="启用命令行控制台命令（仅 python main.py 有效）",
                        variable=self.var_console).grid(row=1, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Label(runtime, text="每个插件队列长度").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Spinbox(runtime, textvariable=self.var_queue, from_=1, to=10000).grid(
            row=2, column=1, sticky="ew", pady=4, padx=(8, 0))
        ttk.Label(runtime, text="队列满时该插件会丢弃新消息，日志里会提示。",
                  foreground="#666", wraplength=380).grid(row=3, column=0, columnspan=2, sticky="w")

        pacing = ttk.LabelFrame(tab, text="发送节奏", padding=12)
        pacing.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(8, 0))
        pacing.columnconfigure(1, weight=1)
        delay = self.bot.get("text_send_delay") or {}
        self.var_delay_enabled = tk.BooleanVar(value=bool(delay.get("enabled", True)))
        self.var_delay_chars = tk.StringVar(value=str(delay.get("characters_per_group", 10)))
        self.var_delay_min = tk.StringVar(value=str(delay.get("min_seconds", 0.1)))
        self.var_delay_max = tk.StringVar(value=str(delay.get("max_seconds", 0.3)))

        ttk.Checkbutton(pacing, text="按文本长度随机等待后发送",
                        variable=self.var_delay_enabled).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(pacing, text="每多少个字符").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Spinbox(pacing, textvariable=self.var_delay_chars, from_=1, to=1000).grid(
            row=1, column=1, sticky="ew", pady=4, padx=(8, 0))
        ttk.Label(pacing, text="最短等待（秒）").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Spinbox(pacing, textvariable=self.var_delay_min, from_=0, to=60,
                    increment=0.05).grid(row=2, column=1, sticky="ew", pady=4, padx=(8, 0))
        ttk.Label(pacing, text="最长等待（秒）").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Spinbox(pacing, textvariable=self.var_delay_max, from_=0, to=60,
                    increment=0.05).grid(row=3, column=1, sticky="ew", pady=4, padx=(8, 0))

        allowlist = ttk.LabelFrame(tab, text="会话白名单", padding=12)
        allowlist.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(8, 0))
        allowlist.columnconfigure(0, weight=1)
        allowlist.rowconfigure(1, weight=1)
        ttk.Label(allowlist, text="每行一个微信 ID 或群 ID；留空表示所有会话。",
                  foreground="#666").grid(row=0, column=0, sticky="w")
        self.text_allowlist = tk.Text(allowlist, height=6, wrap="none", undo=True)
        self.text_allowlist.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        self._set_text(self.text_allowlist, "\n".join(self.bot.get("chat_allowlist", [])))

        guard_cfg = self.bot.get("send_guard") or {}
        self.var_guard_enabled = tk.BooleanVar(value=bool(guard_cfg.get("enabled", True)))
        self.var_guard_interval = tk.StringVar(value=str(guard_cfg.get("min_interval_seconds", 0.8)))
        self.var_guard_chat = tk.StringVar(value=str(guard_cfg.get("per_chat_seconds", 3.0)))
        self.var_guard_minute = tk.StringVar(value=str(guard_cfg.get("max_per_minute", 20)))
        self.var_guard_duplicate = tk.StringVar(value=str(guard_cfg.get("duplicate_window_seconds", 20.0)))
        guard = ttk.LabelFrame(tab, text="发送保护（只能降低风险，不能保证不封号）", padding=12)
        guard.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        for column in range(4):
            guard.columnconfigure(column, weight=1)
        ttk.Checkbutton(guard, text="启用全局发送间隔、同会话冷却和重复内容抑制",
                        variable=self.var_guard_enabled).grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        guard_fields = (
            ("全局最小间隔（秒）", self.var_guard_interval),
            ("同会话冷却（秒）", self.var_guard_chat),
            ("每分钟上限", self.var_guard_minute),
            ("重复内容抑制（秒）", self.var_guard_duplicate),
        )
        for column, (label, variable) in enumerate(guard_fields):
            ttk.Label(guard, text=label).grid(row=1, column=column, sticky="w", padx=(0, 8))
            ttk.Spinbox(guard, textvariable=variable, from_=0, to=600, increment=0.5).grid(
                row=2, column=column, sticky="ew", padx=(0, 8), pady=(2, 0))
        ttk.Label(guard, text="推荐保持开启；账号新、群多或回复频繁时，可把同会话冷却调到 5–15 秒。",
                  foreground="#666").grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))

        save_row = ttk.Frame(tab)
        save_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(save_row, text="保存基础设置", command=self._save_basic).pack(side="left")
        ttk.Label(save_row, text="账号、后端、白名单等修改需要重启机器人后生效。",
                  foreground="#666").pack(side="left", padx=10)

    def _collect_basic(self):
        account = self.var_account_input.get().strip()
        pid_text = self.var_pid_input.get().strip()
        if pid_text:
            try:
                pid = int(pid_text)
            except ValueError:
                raise ValueError("微信进程 PID 必须是正整数，或留空") from None
            if pid <= 0:
                raise ValueError("微信进程 PID 必须是正整数，或留空")
        else:
            pid = None
        delay = {
            "enabled": bool(self.var_delay_enabled.get()),
            "characters_per_group": int(self.var_delay_chars.get()),
            "min_seconds": float(self.var_delay_min.get()),
            "max_seconds": float(self.var_delay_max.get()),
        }
        if delay["min_seconds"] > delay["max_seconds"]:
            raise ValueError("发送节奏的最短等待不能大于最长等待")
        guard = {
            "enabled": bool(self.var_guard_enabled.get()),
            "min_interval_seconds": float(self.var_guard_interval.get()),
            "per_chat_seconds": float(self.var_guard_chat.get()),
            "max_per_minute": int(self.var_guard_minute.get()),
            "duplicate_window_seconds": float(self.var_guard_duplicate.get()),
            "jitter_ratio": float((self.bot.get("send_guard") or {}).get("jitter_ratio", 0.25)),
        }
        from wxbot_framework.sendguard import SendGuardConfig
        SendGuardConfig.from_config(guard)
        allowlist = [line.strip() for line in self.text_allowlist.get("1.0", "end").splitlines()
                     if line.strip()]
        self.bot["wechat_account"] = account or None
        self.bot["wechat_pid"] = pid
        self.bot["backend"] = self.var_backend.get()
        self.bot["reconnect_seconds"] = float(self.var_reconnect.get())
        self.bot["plugin_queue_size"] = int(self.var_queue.get())
        self.bot["log_level"] = self.var_log_level.get()
        self.bot["console"] = bool(self.var_console.get())
        self.bot["text_send_delay"] = delay
        self.bot["send_guard"] = guard
        self.bot["chat_allowlist"] = allowlist
        if self.var_members_enabled.get():
            if not self.bot.get("members_config_file"):
                self.bot["members_config_file"] = "members_config.json"
        else:
            self.bot["members_config_file"] = None

    def _save_basic(self):
        try:
            self._collect_basic()
            self._write_bot()
            logging.getLogger().setLevel(getattr(logging, self.bot["log_level"], logging.INFO))
            self._refresh_status()
            messagebox.showinfo("已保存", "基础设置已保存。连接类和账号类修改需要重启机器人。")
        except Exception as error:
            messagebox.showerror("保存失败", str(error))

    def _detect_account(self):
        self._detect_button_state(False)

        def worker():
            try:
                native = str(self.root_dir / "native")
                if native not in sys.path:
                    sys.path.insert(0, native)
                from run_selftest import wechat_candidates
                from text_service import inspect_account
                pids = wechat_candidates()
                if not pids:
                    raise RuntimeError("没有找到正在运行的微信进程，请先打开并登录微信")
                found = []
                for pid in pids:
                    try:
                        account = inspect_account(pid)
                    except OSError:
                        account = None
                    found.append((pid, account))
                accounts = sorted({account for _, account in found if account})
                message = "检测到：\n" + "\n".join(
                    f"PID {pid}：{account or '尚未登录 / 无法读取'}" for pid, account in found)
                self.window.after(0, lambda: self._detect_done(accounts, message, None))
            except Exception as error:
                self.window.after(0, lambda: self._detect_done([], "", error))

        threading.Thread(target=worker, name="DetectAccount", daemon=True).start()

    def _detect_button_state(self, enabled):
        self.button_detect.configure(state="normal" if enabled else "disabled")

    def _detect_done(self, accounts, message, error):
        self._detect_button_state(True)
        if error is not None:
            hint = str(error)
            if "拒绝访问" in hint or "Access is denied" in hint:
                hint += "\n\n请右键以管理员身份运行控制台后再试。"
            messagebox.showerror("检测失败", hint)
            return
        if not accounts:
            messagebox.showwarning("检测微信账号", message + "\n\n未能读取账号，请确认微信已登录，或尝试以管理员身份运行。")
            return
        if len(accounts) == 1:
            self.var_account_input.set(accounts[0])
            messagebox.showinfo("检测到微信账号", message)
            return
        self._choose("选择机器人账号", accounts, lambda value: self.var_account_input.set(value), message)

    def _choose(self, title, options, apply, extra=""):
        dialog = tk.Toplevel(self.window)
        dialog.title(title)
        dialog.transient(self.window)
        dialog.grab_set()
        dialog.resizable(False, False)
        if extra:
            ttk.Label(dialog, text=extra, justify="left").pack(padx=14, pady=(12, 4), anchor="w")
        listbox = tk.Listbox(dialog, height=min(8, max(3, len(options))), width=50,
                             exportselection=False)
        for option in options:
            listbox.insert("end", option)
        listbox.selection_set(0)
        listbox.pack(padx=14, pady=6, fill="both", expand=True)

        def confirm():
            selection = listbox.curselection()
            if selection:
                apply(options[selection[0]])
            dialog.destroy()

        row = ttk.Frame(dialog)
        row.pack(padx=14, pady=(0, 12), fill="x")
        ttk.Button(row, text="确定", command=confirm).pack(side="right")
        ttk.Button(row, text="取消", command=dialog.destroy).pack(side="right", padx=6)
        listbox.bind("<Double-Button-1>", lambda _event: confirm())

    # -- 大模型 ------------------------------------------------------------

    def _build_llm(self):
        tab = self.tab_llm
        tab.columnconfigure(1, weight=1)
        self.var_base_url = tk.StringVar(value=self.llm.get("base_url", "https://api.deepseek.com"))
        self.var_api_key = tk.StringVar(value=self.llm.get("api_key", ""))
        self.var_model = tk.StringVar(value=self.llm.get("model", "deepseek-flash"))
        self.var_timeout = tk.StringVar(value=str(self.llm.get("timeout_seconds", 20)))
        self.var_max_tokens = tk.StringVar(value=str(self.llm.get("max_tokens", 512)))
        self.var_context_limit = tk.StringVar(value=str(self.llm.get("context_limit", 128000)))
        pricing = self.llm.get("pricing") or {}
        self.var_price_input = tk.StringVar(value=str(pricing.get("input_per_million", 0)))
        self.var_price_output = tk.StringVar(value=str(pricing.get("output_per_million", 0)))
        self.var_price_cache_read = tk.StringVar(value=str(pricing.get("cache_read_per_million", 0)))
        self.var_price_cache_write = tk.StringVar(value=str(pricing.get("cache_write_per_million", 0)))
        self.var_price_currency = tk.StringVar(value=str(pricing.get("currency", "USD")))

        ttk.Label(tab, text="大模型接口", font=("Microsoft YaHei UI", 16, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(tab, text="兼容 OpenAI Chat Completions 的 HTTPS 接口；密钥只保存在本机 JSON 文件中。",
                  foreground="#555").grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 14))

        ttk.Label(tab, text="Base URL").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(tab, textvariable=self.var_base_url).grid(
            row=2, column=1, columnspan=2, sticky="ew", pady=5, padx=(8, 0))

        ttk.Label(tab, text="API Key").grid(row=3, column=0, sticky="w", pady=5)
        self.entry_api_key = ttk.Entry(tab, textvariable=self.var_api_key, show="*")
        self.entry_api_key.grid(row=3, column=1, sticky="ew", pady=5, padx=(8, 0))
        self.var_show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(tab, text="显示", variable=self.var_show_key,
                        command=self._toggle_key_visibility).grid(row=3, column=2, sticky="w", padx=8)

        ttk.Label(tab, text="模型").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Entry(tab, textvariable=self.var_model).grid(
            row=4, column=1, columnspan=2, sticky="ew", pady=5, padx=(8, 0))

        ttk.Label(tab, text="超时（秒）").grid(row=5, column=0, sticky="w", pady=5)
        ttk.Spinbox(tab, textvariable=self.var_timeout, from_=1, to=60).grid(
            row=5, column=1, sticky="ew", pady=5, padx=(8, 0))
        ttk.Label(tab, text="1–60", foreground="#666").grid(
            row=5, column=2, sticky="w", padx=8)

        ttk.Label(tab, text="最大输出 tokens").grid(row=6, column=0, sticky="w", pady=5)
        ttk.Spinbox(tab, textvariable=self.var_max_tokens, from_=128, to=4096).grid(
            row=6, column=1, sticky="ew", pady=5, padx=(8, 0))
        ttk.Label(tab, text="128–4096", foreground="#666").grid(
            row=6, column=2, sticky="w", padx=8)

        ttk.Label(tab, text="上下文限制（tokens）").grid(row=7, column=0, sticky="w", pady=5)
        ttk.Spinbox(tab, textvariable=self.var_context_limit, from_=1000, to=10000000,
                    increment=1000).grid(row=7, column=1, sticky="ew", pady=5, padx=(8, 0))
        ttk.Label(tab, text="用于统计使用率，默认 128000", foreground="#666").grid(
            row=7, column=2, sticky="w", padx=8)

        price_frame = ttk.LabelFrame(tab, text="价格（每百万 tokens；用于统计总成本，未知可填 0）",
                                     padding=8)
        price_frame.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        for column in range(5):
            price_frame.columnconfigure(column, weight=1)
        price_fields = (
            ("输入", self.var_price_input), ("输出", self.var_price_output),
            ("缓存读", self.var_price_cache_read), ("缓存写", self.var_price_cache_write),
            ("币种", self.var_price_currency),
        )
        for column, (label, variable) in enumerate(price_fields):
            ttk.Label(price_frame, text=label).grid(row=0, column=column, sticky="w", padx=(0, 8))
            ttk.Entry(price_frame, textvariable=variable).grid(
                row=1, column=column, sticky="ew", padx=(0, 8), pady=(2, 0))

        actions = ttk.Frame(tab)
        actions.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(16, 8))
        ttk.Button(actions, text="保存大模型设置", command=self._save_llm).pack(side="left")
        ttk.Button(actions, text="测试连接", command=self._test_llm).pack(side="left", padx=8)
        ttk.Button(actions, text="打开配置文件", command=lambda: self._open_path(self.llm_path)).pack(
            side="left")

        self.var_llm_result = tk.StringVar(value="测试会真实请求一次接口，可能产生少量费用。")
        ttk.Label(tab, textvariable=self.var_llm_result, foreground="#555", wraplength=760,
                  justify="left").grid(row=10, column=0, columnspan=3, sticky="w", pady=(6, 0))

        hint = ("DeepSeek 示例：https://api.deepseek.com ，模型 deepseek-chat。\n"
                "阿里云百炼兼容地址形如 https://xxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1。\n"
                "艾特对话、私聊、总结、成语接龙需要填写可用密钥，并在“插件”页启用对应插件。\n"
                "统计页的“使用率”= 最近一次调用 tokens ÷ 上下文限制；“词元总数”是累计值。")
        ttk.Label(tab, text=hint, foreground="#666", justify="left").grid(
            row=11, column=0, columnspan=3, sticky="w", pady=(18, 0))

    def _toggle_key_visibility(self):
        self.entry_api_key.configure(show="" if self.var_show_key.get() else "*")

    def _collect_llm(self):
        data = {
            "base_url": self.var_base_url.get().strip(),
            "api_key": self.var_api_key.get().strip(),
            "model": self.var_model.get().strip(),
            "timeout_seconds": float(self.var_timeout.get()),
            "max_tokens": int(self.var_max_tokens.get()),
            "context_limit": int(self.var_context_limit.get()),
            "pricing": {
                "input_per_million": float(self.var_price_input.get() or 0),
                "output_per_million": float(self.var_price_output.get() or 0),
                "cache_read_per_million": float(self.var_price_cache_read.get() or 0),
                "cache_write_per_million": float(self.var_price_cache_write.get() or 0),
                "currency": self.var_price_currency.get().strip() or "USD",
            },
        }
        return data

    @staticmethod
    def _validate_llm_options(data):
        """即使还没填密钥，也先校验地址、模型和数值范围。"""
        from wxbot_framework.llm import LLMClient
        probe = dict(data)
        if not probe.get("api_key"):
            probe["api_key"] = "placeholder"
        LLMClient(probe)

    def _save_llm(self):
        try:
            data = self._collect_llm()
            self._validate_llm_options(data)
            save_json(self.llm_path, data)
            self.llm = data
            messagebox.showinfo("已保存", "大模型设置已保存；正在运行的插件在重启后使用新配置。")
        except Exception as error:
            messagebox.showerror("保存失败", str(error))

    def _test_llm(self):
        try:
            data = self._collect_llm()
            from wxbot_framework.llm import LLMClient
            client = LLMClient(data, usage_store=self.stats_store,
                               session={"plugin": "控制台测试", "chat_id": ""})
        except Exception as error:
            messagebox.showerror("配置有误", str(error))
            return
        self.var_llm_result.set("正在请求接口…")
        self.window.update_idletasks()

        def worker():
            try:
                result = client.complete_json(
                    "你是接口连通性测试助手。",
                    '只输出 JSON 对象：{"reply":"ok"}')
                text = f"连接成功，模型返回：{json.dumps(result, ensure_ascii=False)}"
                self.window.after(0, lambda: self._llm_test_done(text, None))
            except Exception as error:
                self.window.after(0, lambda: self._llm_test_done("", error))

        threading.Thread(target=worker, name="TestLLM", daemon=True).start()

    def _llm_test_done(self, text, error):
        if error is not None:
            self.var_llm_result.set(f"测试失败：{error}")
            messagebox.showerror("测试失败", str(error))
            return
        self.var_llm_result.set(text)
        messagebox.showinfo("测试成功", text)

    # -- 群与成员 ----------------------------------------------------------

    def _build_members(self):
        tab = self.tab_members
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)
        self.var_members_enabled = tk.BooleanVar(value=self.members_path is not None)
        self.var_history = tk.BooleanVar(value=bool(self.members.get("history_enabled", True)))
        self.var_daily_clear = tk.BooleanVar(value=bool(self.members.get("daily_clear", True)))
        self.var_database = tk.StringVar(value=self.members.get(
            "database_file", ".runtime/member_data.sqlite3"))

        flags = ttk.LabelFrame(tab, text="成员目录", padding=12)
        flags.grid(row=0, column=0, sticky="ew")
        flags.columnconfigure(1, weight=1)
        ttk.Checkbutton(flags, text="启用群成员目录（绑定昵称、聊天记录、总结和艾特上下文需要）",
                        variable=self.var_members_enabled).grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(flags, text="记录群聊文本（history_enabled）",
                        variable=self.var_history).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Checkbutton(flags, text="每天北京时间 0 点清理旧文本（daily_clear）",
                        variable=self.var_daily_clear).grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Label(flags, text="数据库文件").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(flags, textvariable=self.var_database).grid(
            row=2, column=1, sticky="ew", pady=(8, 0), padx=(8, 0))

        add_row = ttk.Frame(tab)
        add_row.grid(row=1, column=0, sticky="ew", pady=(12, 6))
        add_row.columnconfigure(1, weight=1)
        ttk.Label(add_row, text="群 ID").grid(row=0, column=0, sticky="w")
        self.var_new_group = tk.StringVar()
        ttk.Entry(add_row, textvariable=self.var_new_group).grid(
            row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(add_row, text="添加群", command=self._add_group).grid(row=0, column=2)
        ttk.Button(add_row, text="删除选中", command=self._remove_groups).grid(row=0, column=3, padx=6)
        ttk.Button(add_row, text="从机器人导入", command=self._import_groups).grid(row=0, column=4)

        self.group_tree = ttk.Treeview(tab, columns=("group", "label", "members"), show="headings",
                                       selectmode="extended", height=10)
        self.group_tree.heading("group", text="群 ID（以 @chatroom 结尾）")
        self.group_tree.heading("label", text="备注（自己起名）")
        self.group_tree.heading("members", text="预设成员条目")
        self.group_tree.column("group", width=400, anchor="w")
        self.group_tree.column("label", width=240, anchor="w")
        self.group_tree.column("members", width=110, anchor="center", stretch=False)
        self.group_tree.grid(row=3, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.group_tree.yview)
        scroll.grid(row=3, column=1, sticky="ns")
        self.group_tree.configure(yscrollcommand=scroll.set)

        save_row = ttk.Frame(tab)
        save_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(save_row, text="保存群与成员设置", command=self._save_members).pack(side="left")
        ttk.Button(save_row, text="设置群备注", command=self._edit_group_label).pack(side="left", padx=8)
        ttk.Label(save_row, text="成员昵称由群友发送 /绑定 昵称 写入；这里只维护群列表。",
                  foreground="#666").pack(side="left", padx=10)

        hint = ("微信群 ID 本身不包含群名称。点“从机器人导入”会列出最近收到消息的群，并显示\n"
                "最近发言人和消息内容，按内容确认后勾选导入；导入后可选中群点“设置群备注”起名。\n"
                "“预设成员条目”是你在 members_config.json 里为该群预先写的成员资料条数，\n"
                "不是微信群实际人数；0 表示没有预设成员，机器人仍能正常记录和回复。\n"
                "保存后需要重启机器人，群配置和“按艾特回复”才会生效。")
        ttk.Label(tab, text=hint, foreground="#666", justify="left").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._refresh_groups()

    def _refresh_groups(self):
        for item in self.group_tree.get_children():
            self.group_tree.delete(item)
        groups = self.members.get("groups", {})
        if not isinstance(groups, dict):
            return
        labels = self.members.get("group_labels", {})
        if not isinstance(labels, dict):
            labels = {}
        for room in sorted(groups):
            profiles = groups.get(room)
            count = len(profiles) if isinstance(profiles, list) else 0
            self.group_tree.insert("", "end", iid=room,
                                   values=(room, labels.get(room) or "—", count))

    def _add_group(self):
        room = self.var_new_group.get().strip()
        if not room:
            selected = self.group_tree.selection()
            if selected:
                messagebox.showinfo(
                    "添加群",
                    f"{selected[0]} 已经在列表里了。\n\n"
                    "如果要新增其他群，请先在上方输入群 ID；不知道群 ID 时点“从机器人导入”。")
            else:
                messagebox.showinfo(
                    "添加群",
                    "请先输入群 ID，例如：123456789@chatroom。\n"
                    "不知道群 ID 时，点“从机器人导入”。")
            return
        try:
            from wxbot_framework.text import validate_target
            validate_target(room)
            if not room.endswith("@chatroom"):
                raise ValueError("群 ID 必须以 @chatroom 结尾")
        except Exception as error:
            messagebox.showerror(
                "群 ID 不正确",
                f"输入内容：{room}\n\n{error}\n\n群 ID 必须以 @chatroom 结尾。")
            return
        groups = self.members.setdefault("groups", {})
        if not isinstance(groups, dict):
            messagebox.showerror("配置错误", "members_config.json 的 groups 必须是 JSON 对象")
            return
        if room in groups:
            messagebox.showinfo("已存在", "这个群已经在列表里了。")
            return
        groups[room] = []
        self.var_new_group.set("")
        self._refresh_groups()
        self.group_tree.selection_set(room)
        self.group_tree.see(room)

    def _remove_groups(self):
        selected = self.group_tree.selection()
        if not selected:
            messagebox.showinfo("删除群", "请先在列表里选中要删除的群。")
            return
        if not messagebox.askyesno("删除群", f"从配置中删除 {len(selected)} 个群？\n"
                                            "已记录在数据库里的聊天不会立刻删除。"):
            return
        groups = self.members.get("groups", {})
        labels = self.members.get("group_labels")
        for room in selected:
            groups.pop(room, None)
            if isinstance(labels, dict):
                labels.pop(room, None)
        self._refresh_groups()

    def _edit_group_label(self):
        selected = self.group_tree.selection()
        if len(selected) != 1:
            messagebox.showinfo("设置群备注", "请先在列表里选择一个群。")
            return
        room = selected[0]
        labels = self.members.get("group_labels")
        if not isinstance(labels, dict):
            labels = {}
            self.members["group_labels"] = labels
        label = simpledialog.askstring(
            "设置群备注", f"给 {room} 起一个容易识别的备注：",
            initialvalue=labels.get(room, ""), parent=self.window)
        if label is None:
            return
        label = label.strip()
        if label:
            labels[room] = label[:64]
        else:
            labels.pop(room, None)
        self._refresh_groups()
        messagebox.showinfo("已修改", "群备注已修改；点“保存群与成员设置”后生效。")

    def _import_groups(self):
        app = self.controller.application
        if app is None or not self.controller.connected:
            messagebox.showinfo("导入群", "机器人当前未连接微信，无法读取本次会话列表。请先启动机器人并等待连接。")
            return
        try:
            rows = app.backend.chats() if hasattr(app.backend, "chats") else []
        except Exception as error:
            messagebox.showerror("导入失败", str(error))
            return
        groups = self.members.setdefault("groups", {})
        if not isinstance(groups, dict):
            messagebox.showerror("配置错误", "members_config.json 的 groups 必须是 JSON 对象")
            return
        labels = self.members.get("group_labels")
        if not isinstance(labels, dict):
            labels = {}
            self.members["group_labels"] = labels
        candidates = []
        for row in rows:
            room = str(row.get("chat_id") or "")
            if row.get("is_group") and room.endswith("@chatroom"):
                candidates.append((room, row))
        if not candidates:
            messagebox.showinfo(
                "导入群",
                "机器人还没有收到群消息。\n请先在各个群发一条消息（任意内容），再回来点“从机器人导入”。")
            return
        def recency(item):
            try:
                return float(item[1].get("last_time") or 0)
            except (TypeError, ValueError):
                return 0

        candidates.sort(key=recency, reverse=True)

        dialog = tk.Toplevel(self.window)
        dialog.title("从机器人导入群")
        dialog.geometry("900x460")
        dialog.transient(self.window)
        dialog.grab_set()
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        ttk.Label(dialog, text=(
            "微信接口不提供群名称。下面按最近消息列出机器人收到的群：\n"
            "请根据“最近发言人 / 最近消息”确认，再勾选要启用的群。"),
                  justify="left").grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))
        frame = ttk.Frame(dialog)
        frame.grid(row=1, column=0, sticky="nsew", padx=12)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            frame, columns=("group", "label", "sender", "text", "time", "status"),
            show="headings", selectmode="extended")
        headings = {"group": "群 ID", "label": "备注", "sender": "最近发言人",
                    "text": "最近消息", "time": "时间", "status": "状态"}
        widths = {"group": 190, "label": 130, "sender": 110, "text": 260,
                  "time": 90, "status": 80}
        for key in ("group", "label", "sender", "text", "time", "status"):
            tree.heading(key, text=headings[key])
            tree.column(key, width=widths[key], anchor="w", stretch=key == "text")
        tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scroll.set)
        for room, row in candidates:
            text = " ".join(str(row.get("last_text") or "").split())
            if len(text) > 80:
                text = text[:77] + "..."
            sender = str(row.get("last_sender") or "—")
            try:
                stamp = datetime.fromtimestamp(float(row["last_time"])).strftime("%m-%d %H:%M")
            except (KeyError, TypeError, ValueError, OSError, OverflowError):
                stamp = "—"
            status = "已在列表" if room in groups else "待导入"
            tree.insert("", "end", iid=room,
                        values=(room, labels.get(room) or "—", sender,
                                text or "—", stamp, status))
            if room not in groups:
                tree.selection_add(room)

        def select_new():
            tree.selection_set([iid for iid in tree.get_children() if iid not in groups])

        def add_selected():
            selected = [room for room in tree.selection() if room not in groups]
            if not selected:
                messagebox.showinfo("导入群", "没有选中待导入的群。", parent=dialog)
                return
            for room in selected:
                groups[room] = []
            dialog.destroy()
            self._refresh_groups()
            if self._save_members(notify=False):
                messagebox.showinfo(
                    "导入群",
                    f"已导入并保存 {len(selected)} 个群。\n重启机器人后生效。")

        buttons = ttk.Frame(dialog)
        buttons.grid(row=2, column=0, sticky="ew", padx=12, pady=12)
        ttk.Button(buttons, text="全选待导入", command=select_new).pack(side="left")
        ttk.Button(buttons, text="导入选中", command=add_selected).pack(side="left", padx=8)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right")
        dialog.wait_window()

    def _collect_members(self):
        data = copy.deepcopy(self.members)
        data["database_file"] = self.var_database.get().strip() or ".runtime/member_data.sqlite3"
        data["history_enabled"] = bool(self.var_history.get())
        data["daily_clear"] = bool(self.var_daily_clear.get())
        groups = data.get("groups", {})
        if not isinstance(groups, dict):
            raise ValueError("members_config.json 的 groups 必须是 JSON 对象")
        for room in groups:
            if not isinstance(room, str) or not room.endswith("@chatroom"):
                raise ValueError(f"群 ID 必须以 @chatroom 结尾：{room!r}")
        labels = data.get("group_labels")
        if not isinstance(labels, dict):
            labels = {}
        data["group_labels"] = {
            str(room): str(label).strip()[:64]
            for room, label in labels.items()
            if room in groups and str(label).strip()
        }
        return data

    def _save_members(self, notify=True):
        try:
            self._collect_basic()
            data = self._collect_members()
            enabled = bool(self.var_members_enabled.get())
            self.members = data
            if enabled:
                if not self.bot.get("members_config_file"):
                    self.bot["members_config_file"] = "members_config.json"
                self.members_path = self._config_path(self.bot["members_config_file"])
                save_validated_json(self.members_path, data, self._validate_members_file)
            else:
                self.bot["members_config_file"] = None
            self._write_bot()
            self._refresh_status()
            if notify:
                messagebox.showinfo("已保存", "群与成员设置已保存，重启机器人后生效。")
            return True
        except Exception as error:
            messagebox.showerror("保存失败", str(error))
            return False

    def _validate_members_file(self, temporary):
        data = load_json(temporary, {})
        if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
            raise ValueError("成员配置应包含 groups 对象")
        account = self.bot.get("wechat_account")
        if not account:
            return
        from wxbot_framework.members import MemberStore
        MemberStore(temporary, account)

    # -- 插件 --------------------------------------------------------------

    def _build_plugins(self):
        tab = self.tab_plugins
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        tab.rowconfigure(6, weight=1)

        actions = ttk.Frame(tab)
        actions.grid(row=0, column=0, sticky="ew")
        ttk.Button(actions, text="刷新", command=self._refresh_plugin_table).pack(side="left")
        ttk.Button(actions, text="启用", command=lambda: self._set_selected_plugin(True)).pack(
            side="left", padx=6)
        ttk.Button(actions, text="停用", command=lambda: self._set_selected_plugin(False)).pack(
            side="left")
        ttk.Button(actions, text="热重载", command=self._reload_selected_plugin).pack(
            side="left", padx=6)
        ttk.Button(actions, text="编辑选中配置", command=self._edit_selected_plugin).pack(
            side="left")
        ttk.Button(actions, text="打开插件目录",
                   command=lambda: self._open_path(self.root_dir / "plugins")).pack(side="right")

        columns = ("name", "enabled", "active", "handled", "errors", "description")
        self.plugin_tree = ttk.Treeview(tab, columns=columns, show="headings", height=9)
        headings = {"name": "插件", "enabled": "启用", "active": "运行", "handled": "已处理",
                    "errors": "异常", "description": "说明"}
        widths = {"name": 150, "enabled": 60, "active": 70, "handled": 80, "errors": 70,
                  "description": 420}
        for column in columns:
            self.plugin_tree.heading(column, text=headings[column])
            self.plugin_tree.column(column, width=widths[column], anchor="w",
                                    stretch=column == "description")
        self.plugin_tree.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        plugin_scroll = ttk.Scrollbar(tab, orient="vertical", command=self.plugin_tree.yview)
        plugin_scroll.grid(row=2, column=1, sticky="ns", pady=(8, 0))
        self.plugin_tree.configure(yscrollcommand=plugin_scroll.set)
        self.plugin_tree.bind("<Double-Button-1>", lambda _event: self._edit_selected_plugin())

        ttk.Label(tab, text="高级配置（JSON；保存后热重载该插件）",
                  font=("Microsoft YaHei UI", 10, "bold")).grid(row=4, column=0, sticky="w", pady=(12, 2))
        self.var_editor = tk.StringVar(value="未选择插件")
        ttk.Label(tab, textvariable=self.var_editor, foreground="#666").grid(
            row=5, column=0, sticky="w")
        self.text_plugin = tk.Text(tab, height=9, wrap="none", undo=True)
        self.text_plugin.grid(row=6, column=0, sticky="nsew", pady=(4, 0))
        plugin_editor_scroll = ttk.Scrollbar(tab, orient="vertical", command=self.text_plugin.yview)
        plugin_editor_scroll.grid(row=6, column=1, sticky="ns", pady=(4, 0))
        self.text_plugin.configure(yscrollcommand=plugin_editor_scroll.set)

        editor_actions = ttk.Frame(tab)
        editor_actions.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(editor_actions, text="保存并应用", command=self._save_plugin_config).pack(side="left")
        ttk.Button(editor_actions, text="恢复选中配置", command=self._edit_selected_plugin).pack(
            side="left", padx=8)
        ttk.Label(editor_actions, text="enabled 字段也可直接编辑；AI 插件需要先配置大模型。",
                  foreground="#666").pack(side="left", padx=8)

        hint = ("插件列表来自 plugins 目录；停用只影响本机器人，不删除插件文件。\n"
                "修改插件参数后，运行中的机器人会立即热重载该插件；加载失败的原因会显示在“异常”列和日志里。")
        ttk.Label(tab, text=hint, foreground="#666", justify="left").grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def _plugin_specs(self):
        directory = self.root_dir / "plugins"
        result = {}
        if not directory.is_dir():
            return result
        for path in sorted(directory.iterdir()):
            if not path.is_dir() or path.name.startswith("_"):
                continue
            if not ((path / "__init__.py").is_file() or (path / "plugin.py").is_file()):
                continue
            description = ""
            manifest = path / "plugin.json"
            if manifest.exists():
                try:
                    description = str(json.loads(manifest.read_text(encoding="utf-8-sig")).get(
                        "description", ""))
                except Exception:
                    description = "plugin.json 读取失败"
            result[path.name] = description
        return result

    def _refresh_plugin_table(self):
        if not hasattr(self, "plugin_tree"):
            return
        selected = self.plugin_tree.selection()
        selected_name = selected[0] if selected else None
        status = {}
        app = self.controller.application
        if self.controller.running and app is not None:
            try:
                status = {row["name"]: row for row in app.plugins.status()}
            except Exception:
                status = {}
        self.plugin_tree.delete(*self.plugin_tree.get_children())
        specs = self._plugin_specs()
        for name, description in specs.items():
            options = self.bot.get("plugins", {}).get(name, {})
            enabled = options.get("enabled", True)
            state = status.get(name)
            active = "运行" if state and state["active"] else ("停用" if self.controller.running else "—")
            handled = state["handled"] if state else 0
            errors = state["errors"] if state else 0
            self.plugin_tree.insert("", "end", iid=name, values=(
                name, "是" if enabled else "否", active, handled, errors, description))
        if selected_name and self.plugin_tree.exists(selected_name):
            self.plugin_tree.selection_set(selected_name)

    def _selected_plugin(self):
        selected = self.plugin_tree.selection()
        if not selected:
            messagebox.showinfo("插件", "请先在列表里选择一个插件。")
            return None
        return selected[0]

    def _edit_selected_plugin(self):
        name = self._selected_plugin()
        if name is None:
            return
        options = copy.deepcopy(self.bot.get("plugins", {}).get(name, {}))
        self.editor_plugin = name
        self.var_editor.set(f"正在编辑：{name}")
        self._set_text(self.text_plugin, json.dumps(options, ensure_ascii=False, indent=2),
                       editable=True)

    def _save_plugin_config(self):
        if not self.editor_plugin:
            messagebox.showinfo("插件配置", "请先点“编辑选中配置”。")
            return
        try:
            options = json.loads(self.text_plugin.get("1.0", "end").strip() or "{}")
            if not isinstance(options, dict):
                raise ValueError("插件配置必须是 JSON 对象")
            name = self.editor_plugin
            self.bot.setdefault("plugins", {})[name] = options
            self._write_bot()
            self._apply_plugin_state(name, options.get("enabled", True))
            self._refresh_plugin_table()
            messagebox.showinfo("已保存", f"插件 {name} 的配置已保存。")
        except Exception as error:
            messagebox.showerror("保存失败", str(error))

    def _set_selected_plugin(self, enabled):
        name = self._selected_plugin()
        if name is None:
            return
        try:
            options = self.bot.setdefault("plugins", {}).setdefault(name, {})
            options["enabled"] = bool(enabled)
            self._write_bot()
            self._apply_plugin_state(name, enabled)
            self._refresh_plugin_table()
        except Exception as error:
            messagebox.showerror("操作失败", str(error))

    def _apply_plugin_state(self, name, enabled):
        """运行中立即启用/停用/重载；未运行只保存配置。"""
        app = self.controller.application
        if not self.controller.running or app is None:
            return
        if not enabled:
            app.plugins.disable(name)
        elif app.plugins.is_active(name):
            app.plugins.reload(name)
        else:
            app.plugins.enable(name)

    def _reload_selected_plugin(self):
        name = self._selected_plugin()
        if name is None:
            return
        app = self.controller.application
        if not self.controller.running or app is None:
            messagebox.showinfo("热重载", "机器人未运行；保存配置后重新启动即可。")
            return
        try:
            app.plugins.reload(name)
        except Exception as error:
            messagebox.showerror("重载失败", str(error))

    # -- 关于 --------------------------------------------------------------

    def _build_about(self):
        tab = self.tab_about
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        canvas = tk.Canvas(tab, highlightthickness=0)
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas, padding=(0, 0, 12, 0))
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        content.columnconfigure(1, weight=1)

        ttk.Label(content, text=f"关于 {APP_NAME}", font=("Microsoft YaHei UI", 18, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        info_rows = (
            ("软件名称", "wx-helper（微信机器人控制台）"),
            ("版本", f"{APP_VERSION}（Windows x64）"),
            ("作者", APP_AUTHOR),
            ("版权", APP_COPYRIGHT),
            ("适用微信", f"微信 {WECHAT_VERSION}（仅该版本验证过）"),
        )
        for index, (label, value) in enumerate(info_rows, start=1):
            ttk.Label(content, text=f"{label}：", foreground="#555").grid(
                row=index, column=0, sticky="w", pady=2)
            ttk.Label(content, text=value).grid(row=index, column=1, sticky="w", pady=2, padx=(6, 0))
        repo_row = len(info_rows) + 1
        ttk.Label(content, text="项目仓库：", foreground="#555").grid(
            row=repo_row, column=0, sticky="w", pady=2)
        repo_box = ttk.Frame(content)
        repo_box.grid(row=repo_row, column=1, sticky="w", pady=2, padx=(6, 0))
        ttk.Label(repo_box, text="github.com/xueze-ai/wx-helper").pack(side="left")
        ttk.Button(repo_box, text="打开", width=6, command=self._open_project).pack(side="left", padx=8)

        architecture = ttk.LabelFrame(content, text="架构", padding=10)
        architecture.grid(row=repo_row + 1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Label(architecture, text=(
            "Python 3.12 + tkinter 可视化控制台；wxbot_framework 插件框架负责配置、成员、"
            "消息分发与大模型调用；native 文本桥接负责在微信 4.1.15.13 进程内收发文本；"
            "SQLite 保存在 .runtime 目录。插件放在 plugins 目录下，可独立启停和热重载。"),
            wraplength=820, justify="left").pack(anchor="w")

        principle = ttk.LabelFrame(content, text="原则", padding=10)
        principle.grid(row=repo_row + 2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(principle, text="本项目采用 MIT 许可证，按“原样”提供，不附带任何担保。\n"
                                  "请勿用于违法用途，并遵守当地法律和微信用户协议；使用风险自负。\n"
                                  f"仅微信版本 {WECHAT_VERSION}。",
                  foreground="#b00020", justify="left").pack(anchor="w")

        notice = ttk.LabelFrame(content, text="项目说明", padding=10)
        notice.grid(row=repo_row + 3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(notice, text=(
            "这是一个逆向练习项目。使用非官方桥接连接微信存在账号被限制、封禁的风险；"
            "请先用小号测试，不要在重要账号上使用，也不要用于群发、营销等高风险场景。"
            "使用本软件产生的任何后果由使用者自行承担。"),
            wraplength=820, justify="left").pack(anchor="w")

        update_frame = ttk.LabelFrame(content, text="软件更新", padding=10)
        update_frame.grid(row=repo_row + 4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.var_auto_update = tk.BooleanVar(value=bool(self.prefs.get("auto_check_updates", True)))
        ttk.Checkbutton(update_frame, text="启动时自动检查更新",
                        variable=self.var_auto_update,
                        command=self._toggle_auto_update).grid(row=0, column=0, sticky="w")
        ttk.Label(update_frame, text="更新仓库（owner/repo）").grid(
            row=1, column=0, sticky="w", pady=(4, 0))
        self.var_update_repo = tk.StringVar(
            value=str(self.prefs.get("update_repo") or DEFAULT_UPDATE_REPO))
        ttk.Entry(update_frame, textvariable=self.var_update_repo, width=32).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=(4, 0))
        ttk.Label(update_frame, text=f"默认 {DEFAULT_UPDATE_REPO}；仓库改名后在此修改",
                  foreground="#888").grid(row=1, column=2, sticky="w", padx=8, pady=(4, 0))
        ttk.Label(update_frame, text=(
            "打开软件约 8 秒后在后台检查 GitHub Releases；发现新版本自动下载，"
            "下载完成后由你决定是否立即重启更新（也可下次退出时自动安装）。"
            "更新不会覆盖 bot_config.json、llm_config.json、members_config.json、.runtime 和 logs。"),
            wraplength=820, justify="left", foreground="#555").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(4, 6))
        self.var_update_status = tk.StringVar(value="尚未检查更新。")
        ttk.Label(update_frame, textvariable=self.var_update_status, wraplength=820,
                  justify="left").grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 6))
        update_actions = ttk.Frame(update_frame)
        update_actions.grid(row=4, column=0, columnspan=3, sticky="w")
        ttk.Button(update_actions, text="检查更新",
                   command=lambda: self._check_updates(manual=True)).pack(side="left")
        self.btn_update_install = ttk.Button(update_actions, text="立即重启更新",
                                             command=self._install_downloaded_update)
        self.btn_update_install.pack(side="left", padx=8)
        ttk.Button(update_actions, text="打开 Releases 页面",
                   command=self._open_releases).pack(side="left")

        ttk.Label(content, text="统计、群配置和发送保护都只保存在本机，不会上传聊天内容。",
                  foreground="#888").grid(row=repo_row + 5, column=0, columnspan=2,
                                          sticky="w", pady=(10, 0))
        self._refresh_update_controls()

    def _open_project(self):
        try:
            webbrowser.open(PROJECT_URL)
        except Exception:
            messagebox.showinfo("项目仓库", PROJECT_URL)

    def _open_releases(self):
        repo = self.var_update_repo.get().strip() or DEFAULT_UPDATE_REPO
        try:
            webbrowser.open(f"https://github.com/{repo}/releases")
        except Exception:
            messagebox.showinfo("Releases", f"https://github.com/{repo}/releases")

    def _toggle_auto_update(self):
        self.prefs["auto_check_updates"] = bool(self.var_auto_update.get())
        self._save_prefs()

    def _refresh_update_controls(self):
        pending = self.update_pending if isinstance(self.update_pending, dict) else None
        if pending:
            stage = Path(str(pending.get("stage") or ""))
            exe = Path(str(pending.get("exe") or ""))
            if not stage.is_dir() or not exe.exists():
                self.update_pending = None
                self.prefs.pop("pending_update", None)
                self._save_prefs()
                pending = None
        if not hasattr(self, "btn_update_install"):
            return
        if pending:
            version = pending.get("version") or pending.get("tag") or ""
            self.btn_update_install.configure(state="normal")
            self.var_update_status.set(
                f"新版本 {version} 已下载，点“立即重启更新”或退出时自动安装。")
        else:
            self.btn_update_install.configure(state="disabled")
            if not self.var_update_status.get() or self.var_update_status.get() == "尚未检查更新。":
                self.var_update_status.set("尚未检查更新。")

    def _auto_check_updates(self):
        if self._closing or self.update_pending:
            return
        if not self.var_auto_update.get():
            return
        self._check_updates(manual=False)

    def _check_updates(self, *, manual=False):
        if self.update_check_running:
            if manual:
                messagebox.showinfo("检查更新", "正在检查更新，请稍候。")
            return
        repo = self.var_update_repo.get().strip() or DEFAULT_UPDATE_REPO
        if repo.count("/") != 1 or any(not part for part in repo.split("/")):
            self.var_update_status.set("更新仓库格式应为 owner/repo。")
            if manual:
                messagebox.showwarning("仓库格式不正确", self.var_update_status.get())
            return
        self.prefs["update_repo"] = repo
        self._save_prefs()
        self.update_check_running = True
        self._update_manual = bool(manual)
        self.var_update_status.set("正在检查更新…")

        def worker():
            from wxbot_framework import update as update_module
            try:
                info = update_module.check_latest(repo=repo, timeout=12)
                error = None
            except Exception as failure:
                info, error = None, failure
            self.window.after(0, lambda: self._update_check_done(info, error, manual))

        threading.Thread(target=worker, name="CheckUpdate", daemon=True).start()

    def _update_check_done(self, info, error, manual):
        self.update_check_running = False
        if error is not None:
            self.var_update_status.set(f"检查更新失败：{error}")
            if manual:
                messagebox.showwarning("检查更新", str(error))
            return
        from wxbot_framework.update import is_newer
        if not info or not info.get("version") or not is_newer(info["version"], APP_VERSION):
            current = info.get("version") if info else ""
            self.var_update_status.set(
                f"当前已是最新版本（{current or APP_VERSION}）。")
            if manual:
                messagebox.showinfo("检查更新", self.var_update_status.get())
            return
        if not manual and self.prefs.get("skipped_version") == info["version"]:
            self.var_update_status.set(f"已跳过版本 {info['version']}；可随时手动检查更新。")
            return
        self.update_info = info
        self.var_update_status.set(f"发现新版本 {info['tag']}，正在下载…")
        self._download_update(info, manual)

    def _download_update(self, info, manual):
        if self.update_download_running:
            return
        if not getattr(sys, "frozen", False):
            self.var_update_status.set(
                f"发现新版本 {info['tag']}；源码运行请手动更新，不会自动安装。")
            if manual:
                messagebox.showinfo("发现新版本", self.var_update_status.get())
            return
        from wxbot_framework.update import choose_asset
        asset = choose_asset(info.get("assets") or [])
        if asset is None:
            self.var_update_status.set("新版本没有提供 Windows 更新包（.zip 或 .exe）。")
            if manual:
                messagebox.showwarning("没有更新包", self.var_update_status.get())
            return
        self.update_download_running = True
        directory = self.root_dir / ".runtime/updates" / f"v{info['version']}"
        last = [0.0]

        def progress(received, total):
            now = time.monotonic()
            if now - last[0] < 0.4:
                return
            last[0] = now
            if total:
                text = f"正在下载 {asset['name']}：{received / 1048576:.1f}/{total / 1048576:.1f} MB"
            else:
                text = f"正在下载 {asset['name']}：{received / 1048576:.1f} MB"
            self.window.after(0, lambda: self.var_update_status.set(text))

        def worker():
            from wxbot_framework import update as update_module
            try:
                package = update_module.download(asset, directory, progress=progress)
                self.window.after(0, lambda: self.var_update_status.set("下载完成，正在解压…"))
                stage_root, exe = update_module.stage(package, directory / "stage")
                self.window.after(0, lambda: self._update_download_done(
                    info, asset, stage_root, exe, None, manual))
            except Exception as failure:
                self.window.after(0, lambda: self._update_download_done(
                    info, asset, None, None, failure, manual))

        threading.Thread(target=worker, name="DownloadUpdate", daemon=True).start()

    def _update_download_done(self, info, asset, stage_root, exe, error, manual):
        self.update_download_running = False
        if error is not None:
            self.var_update_status.set(f"下载或解压更新失败：{error}")
            if manual:
                messagebox.showerror("更新失败", str(error))
            return
        pending = {
            "version": info.get("version") or info.get("tag"),
            "tag": info.get("tag"),
            "stage": str(stage_root),
            "exe": str(exe),
            "asset": asset.get("name") if asset else "",
            "url": info.get("url") or "",
            "downloaded_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.update_pending = pending
        self.prefs["pending_update"] = pending
        self.prefs.pop("skipped_version", None)
        self._save_prefs()
        self._refresh_update_controls()
        self.var_update_status.set(
            f"新版本 {pending['version']} 已下载。是否现在停止机器人并重启更新？")
        answer = messagebox.askyesnocancel(
            "发现新版本",
            f"发现新版本 {info.get('tag') or pending['version']}（当前 {APP_VERSION}）。\n\n"
            "更新包已下载完成。\n\n"
            "是：立即停止机器人并重启更新\n"
            "否：本次退出时自动安装\n"
            "取消：跳过这个版本")
        if answer is None:
            self.prefs["skipped_version"] = pending["version"]
            self._save_prefs()
            self.var_update_status.set(
                f"已跳过版本 {pending['version']}；可随时手动检查更新。")
        elif answer:
            self._apply_update_now()
        else:
            self.var_update_status.set("将在下次退出时自动安装新版本。")

    def _install_downloaded_update(self):
        if not self.update_pending:
            messagebox.showinfo("没有更新", "当前没有已下载的更新，请先点“检查更新”。")
            return
        self._apply_update_now()

    def _apply_update_now(self):
        if not self.update_pending:
            return
        if not messagebox.askyesno(
                "立即更新",
                "将先安全停止机器人（Quit），然后关闭控制台、替换程序文件并自动重启。\n\n继续吗？"):
            return
        if self.controller.running or self.controller.needs_cleanup:
            self.var_update_status.set("正在停止机器人…")
            self.btn_update_install.configure(state="disabled")

            def worker():
                try:
                    self.controller.stop(timeout=60)
                    error = None
                except Exception as failure:
                    error = failure
                self.window.after(0, lambda: self._finish_update_now(error))

            threading.Thread(target=worker, name="StopForUpdate", daemon=True).start()
        else:
            self._finish_update_now(None)

    def _finish_update_now(self, error):
        if error is not None:
            self.var_update_status.set(f"停止机器人失败：{error}")
            messagebox.showerror("无法更新", f"停止机器人失败：{error}\n\n请先手动停止机器人后重试。")
            self._refresh_update_controls()
            return
        try:
            launched = self._launch_updater(self.update_pending, autostart=True)
        except Exception as failure:
            messagebox.showerror("无法启动更新", str(failure))
            self._refresh_update_controls()
            return
        if not launched:
            messagebox.showinfo("源码运行", "当前是源码运行模式，请手动更新：git pull 或重新下载 Release。")
            return
        self.prefs["pending_update"] = None
        self._save_prefs()
        self._closing = True
        self.window.destroy()

    def _install_pending_update(self):
        if not self.update_pending:
            return False
        try:
            launched = self._launch_updater(self.update_pending, autostart=True)
        except Exception:
            logging.getLogger("GUI").warning("启动更新程序失败", exc_info=True)
            return False
        if launched:
            self.prefs["pending_update"] = None
            self._save_prefs()
        return launched

    def _launch_updater(self, pending, *, autostart=False):
        if not getattr(sys, "frozen", False):
            return False
        pending = dict(pending or {})
        stage = Path(str(pending.get("stage") or ""))
        exe = Path(str(pending.get("exe") or ""))
        if not stage.is_dir() or not exe.exists():
            raise RuntimeError("已下载的更新文件不存在，请重新点“检查更新”。")
        from wxbot_framework.update import write_install_script
        script = write_install_script(self.root_dir / ".runtime/updates/install_update.ps1")
        log_path = self.root_dir / ".runtime/updates/update.log"
        app_dir = Path(sys.executable).parent
        final_exe = app_dir / exe.name
        command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                   "-WaitPid", str(os.getpid()), "-Stage", str(stage), "-AppDir", str(app_dir),
                   "-Exe", str(final_exe), "-Log", str(log_path)]
        if autostart:
            command.append("-Autostart")
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0) |
                 getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
                 getattr(subprocess, "CREATE_NO_WINDOW", 0))
        subprocess.Popen(command, close_fds=True, creationflags=flags)
        return True

    # -- 统计 --------------------------------------------------------------

    STATS_COLUMNS = (
        ("session", "会话", 260, "w"),
        ("messages", "消息数", 80, "center"),
        ("provider", "提供商", 110, "center"),
        ("model", "模型", 130, "center"),
        ("context", "上下文限制", 100, "center"),
        ("total", "词元总数", 100, "center"),
        ("usage", "使用率", 80, "center"),
        ("input", "输入词元", 90, "center"),
        ("output", "输出词元", 90, "center"),
        ("reasoning", "推理词元", 90, "center"),
        ("cache", "缓存词元(读/写)", 140, "center"),
        ("user", "用户消息", 80, "center"),
        ("assistant", "助手消息", 80, "center"),
        ("cost", "总成本", 100, "center"),
        ("created", "创建时间", 150, "center"),
        ("active", "最后活动", 150, "center"),
    )

    def _build_stats(self):
        tab = self.tab_stats
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        actions = ttk.Frame(tab)
        actions.grid(row=0, column=0, sticky="ew")
        ttk.Label(actions, text="大模型调用统计", font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        ttk.Button(actions, text="打开统计数据库", command=self._open_stats_db).pack(side="right")
        ttk.Button(actions, text="清空统计", command=self._clear_stats).pack(side="right", padx=6)
        ttk.Button(actions, text="导出 CSV", command=self._export_stats).pack(side="right")
        ttk.Button(actions, text="刷新", command=self._refresh_stats).pack(side="right", padx=6)

        self.var_stats_summary = tk.StringVar(value="还没有调用记录。")
        ttk.Label(tab, textvariable=self.var_stats_summary, foreground="#555").grid(
            row=1, column=0, sticky="w", pady=(6, 0))

        tree_frame = ttk.Frame(tab)
        tree_frame.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.stats_tree = ttk.Treeview(tree_frame, columns=[item[0] for item in self.STATS_COLUMNS],
                                       show="headings", selectmode="browse")
        for key, title, width, anchor in self.STATS_COLUMNS:
            self.stats_tree.heading(key, text=title)
            self.stats_tree.column(key, width=width, anchor=anchor, stretch=False)
        yscroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.stats_tree.yview)
        xscroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.stats_tree.xview)
        self.stats_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.stats_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        self.stats_tree.tag_configure("high", foreground="#b00020")
        self.stats_tree.tag_configure("mid", foreground="#a15c00")

        hint = ("每条记录来自一次成功的大模型调用，只统计 token、模型和费用，不保存聊天正文。\n"
                "使用率 = 最近一次调用 tokens ÷ 上下文限制；“词元总数”是该会话累计值。\n"
                "总成本按“大模型”页填写的每百万 tokens 价格估算；没有填写价格时显示 0。")
        ttk.Label(tab, text=hint, foreground="#666", justify="left").grid(
            row=3, column=0, sticky="w", pady=(10, 0))

    @staticmethod
    def _format_stats_time(value):
        if not value:
            return "—"
        try:
            moment = datetime.fromisoformat(str(value))
            if moment.tzinfo is not None:
                moment = moment.astimezone()
            return moment.strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _format_money(value, currency):
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            number = 0.0
        return f"{number:.4f} {currency or 'USD'}"

    def _refresh_stats(self):
        try:
            rows = self.stats_store.summary()
            totals = self.stats_store.totals()
        except Exception as error:
            self.var_stats_summary.set(f"读取统计失败：{error}")
            return
        selection = set(self.stats_tree.selection())
        for item in self.stats_tree.get_children():
            self.stats_tree.delete(item)
        labels = self.members.get("group_labels") if isinstance(self.members, dict) else {}
        if not isinstance(labels, dict):
            labels = {}
        for row in rows:
            calls = int(row.get("calls") or 0)
            chat_id = row.get("chat_id") or ""
            if chat_id:
                label = labels.get(chat_id) or chat_id
            else:
                label = "全局"
            session = f"{row.get('plugin') or '未命名'} · {label}"
            context_limit = int(row.get("context_limit") or 0)
            total_tokens = int(row.get("total_tokens") or 0)
            latest_total = int(row.get("latest_total_tokens") or 0)
            usage = (latest_total / context_limit * 100.0) if context_limit > 0 else None
            values = (
                session,
                calls * 2,
                row.get("provider") or "—",
                row.get("model") or "—",
                f"{context_limit:,}" if context_limit else "—",
                f"{total_tokens:,}",
                f"{usage:.1f}%" if usage is not None else "—",
                f"{int(row.get('input_tokens') or 0):,}",
                f"{int(row.get('output_tokens') or 0):,}",
                f"{int(row.get('reasoning_tokens') or 0):,}",
                f"{int(row.get('cache_read_tokens') or 0):,} / {int(row.get('cache_write_tokens') or 0):,}",
                f"{calls:,}",
                f"{calls:,}",
                self._format_money(row.get("total_cost"), row.get("currency")),
                self._format_stats_time(row.get("created_at")),
                self._format_stats_time(row.get("last_active")),
            )
            tag = ""
            if usage is not None and usage >= 85:
                tag = "high"
            elif usage is not None and usage >= 60:
                tag = "mid"
            iid = str(row.get("session"))
            self.stats_tree.insert("", "end", iid=iid, values=values, tags=(tag,) if tag else ())
            if iid in selection:
                self.stats_tree.selection_add(iid)
        self.var_stats_summary.set(
            f"会话 {int(totals.get('sessions') or 0)} 个 · 调用 {int(totals.get('calls') or 0)} 次 · "
            f"输入 {int(totals.get('input_tokens') or 0):,} · 输出 {int(totals.get('output_tokens') or 0):,} · "
            f"总成本 {self._format_money(totals.get('total_cost'), totals.get('currency'))}")

    def _tick_stats(self):
        if self._closing:
            return
        try:
            if self.notebook.select() == str(self.tab_stats):
                self._refresh_stats()
        except tk.TclError:
            return
        self.window.after(5000, self._tick_stats)

    def _clear_stats(self):
        if not messagebox.askyesno("清空统计", "确定要清空全部大模型调用统计吗？此操作不会删除聊天记录，也无法撤销。"):
            return
        try:
            self.stats_store.clear()
        except Exception as error:
            messagebox.showerror("清空失败", str(error))
            return
        self._refresh_stats()

    def _export_stats(self):
        rows = self.stats_store.summary(limit=10000)
        if not rows:
            messagebox.showinfo("没有数据", "当前还没有可导出的调用记录。")
            return
        path = filedialog.asksaveasfilename(
            title="导出统计 CSV", defaultextension=".csv", filetypes=[("CSV 文件", "*.csv")],
            initialfile=f"wx-helper-stats-{datetime.now():%Y%m%d-%H%M%S}.csv")
        if not path:
            return
        headers = ["会话", "插件", "会话 ID", "消息数", "提供商", "模型", "上下文限制", "词元总数",
                   "输入词元", "输出词元", "推理词元", "缓存读词元", "缓存写词元", "用户消息",
                   "助手消息", "总成本", "币种", "创建时间", "最后活动"]
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(headers)
                for row in rows:
                    calls = int(row.get("calls") or 0)
                    writer.writerow([
                        row.get("session"), row.get("plugin"), row.get("chat_id"), calls * 2,
                        row.get("provider"), row.get("model"), row.get("context_limit"),
                        row.get("total_tokens"), row.get("input_tokens"), row.get("output_tokens"),
                        row.get("reasoning_tokens"), row.get("cache_read_tokens"),
                        row.get("cache_write_tokens"), calls, calls, row.get("total_cost"),
                        row.get("currency"), row.get("created_at"), row.get("last_active"),
                    ])
        except OSError as error:
            messagebox.showerror("导出失败", str(error))
            return
        messagebox.showinfo("已导出", f"统计已导出到：\n{path}")

    def _open_stats_db(self):
        self._open_path(self.stats_store.path.parent)

    # -- 日志 --------------------------------------------------------------

    def _build_logs(self):
        tab = self.tab_logs
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        top = ttk.Frame(tab)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.var_autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="自动滚动", variable=self.var_autoscroll).pack(side="left")
        ttk.Button(top, text="清空显示", command=self._clear_log_view).pack(side="left", padx=6)
        ttk.Button(top, text="重新读取日志文件", command=self._load_log_file).pack(side="left")
        ttk.Button(top, text="打开日志目录",
                   command=lambda: self._open_path(self.root_dir / "logs")).pack(side="right")

        self.text_log = tk.Text(tab, wrap="word", state="disabled", height=20)
        self.text_log.grid(row=1, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(tab, orient="vertical", command=self.text_log.yview)
        log_scroll.grid(row=1, column=1, sticky="ns")
        self.text_log.configure(yscrollcommand=log_scroll.set)
        self.text_log.tag_configure("warn", foreground="#9a6700")
        self.text_log.tag_configure("error", foreground="#b42318")

    def _append_log(self, text):
        if not hasattr(self, "text_log"):
            return
        self.text_log.configure(state="normal")
        for line in text.splitlines() or [""]:
            tag = "error" if " ERROR " in line else ("warn" if " WARNING " in line else "")
            self.text_log.insert("end", line + "\n", tag)
        # 只保留最近约 4000 行，避免长时间运行占满内存。
        line_count = int(self.text_log.index("end-1c").split(".")[0])
        if line_count > 4000:
            self.text_log.delete("1.0", f"{line_count - 4000}.0")
        self.text_log.configure(state="disabled")
        if self.var_autoscroll.get():
            self.text_log.see("end")

    def _clear_log_view(self):
        self.text_log.configure(state="normal")
        self.text_log.delete("1.0", "end")
        self.text_log.configure(state="disabled")

    def _load_log_file(self):
        path = self.root_dir / "logs/bot.log"
        if not path.exists():
            self._append_log("（还没有日志文件）")
            return
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
            self._clear_log_view()
            self._append_log("\n".join(lines) or "（日志为空）")
        except OSError as error:
            messagebox.showerror("读取日志失败", str(error))

    def _poll_logs(self):
        if self._closing:
            return
        lines = []
        while True:
            try:
                lines.append(self.logs.get_nowait())
            except queue.Empty:
                break
            except Exception:
                break
        if lines:
            self._append_log("\n".join(lines))
        self.window.after(200, self._poll_logs)

    # -- 保存 / 加载 / 启动 -------------------------------------------------

    def _write_bot(self):
        from wxbot_framework.config import ConfigStore as Store
        save_validated_json(self.store.path, self.bot, lambda temporary: Store(temporary).load())
        self.bot = self.store.load()
        self.llm_path = self._config_path(self.bot.get("llm_config_file", "llm_config.json"))
        member_name = self.bot.get("members_config_file")
        self.members_path = self._config_path(member_name) if member_name else None

    def _save_all_silently(self):
        self._collect_basic()
        self._collect_members()
        self.llm = self._collect_llm()
        self._validate_llm_options(self.llm)
        save_json(self.llm_path, self.llm)
        member_name = self.bot.get("members_config_file")
        self.members_path = self._config_path(member_name) if member_name else None
        if self.members_path is not None:
            save_validated_json(self.members_path, self.members, self._validate_members_file)
        self._write_bot()

    def _start_bot(self):
        if self.controller.running:
            return
        if not (self.root_dir / "plugins").is_dir():
            messagebox.showerror("缺少插件目录", f"未找到：{self.root_dir / 'plugins'}\n"
                                                 "打包版请确认 plugins 文件夹和 EXE 在同一目录。")
            return
        if not (self.root_dir / "native").is_dir():
            messagebox.showerror("缺少原生目录", f"未找到：{self.root_dir / 'native'}\n"
                                                 "打包版请确认 native 文件夹和 EXE 在同一目录。")
            return
        account = self.var_account_input.get().strip()
        if not account:
            if not messagebox.askyesno(
                    "未填写机器人账号",
                    "机器人微信 ID 为空，将自动识别唯一登录的微信账号。\n"
                    "多开微信或启用群成员目录时必须填写。继续吗？"):
                return
        if account == "wxid_example_robot":
            messagebox.showerror("请先配置账号", "请把机器人微信 ID 改成自己的实际微信 ID。")
            return
        try:
            self._save_all_silently()
        except Exception as error:
            messagebox.showerror("配置有误，未启动", str(error))
            return
        try:
            self.controller.start()
        except Exception as error:
            messagebox.showerror("启动失败", str(error))
            self._refresh_status()
            return
        logging.getLogger("GUI").info("已请求启动机器人；等待微信连接")
        self._refresh_status()

    def _stop_bot(self):
        pending = self.controller.needs_cleanup
        if not self.controller.running and not pending:
            return
        if self.controller.running and not messagebox.askyesno(
                "安全停止（Quit）",
                "停止会像控制台输入 quit 一样：先停插件和接收线程，再卸载微信里的桥接 DLL。\n"
                "完成后才能重新启动。现在停止吗？"):
            return
        self.var_state.set("正在安全停止（卸载桥接 DLL）…")
        self.button_stop.configure(state="disabled")
        self.window.update_idletasks()

        def worker():
            try:
                self.controller.stop(timeout=45)
                self.window.after(0, self._stopped_ok)
            except Exception as error:
                self.window.after(0, lambda: self._stopped_error(error))

        threading.Thread(target=worker, name="StopBot", daemon=True).start()

    def _stopped_ok(self):
        logging.getLogger("GUI").info("机器人已安全停止（Quit），桥接 DLL 已卸载")
        self._refresh_status()
        self._refresh_plugin_table()

    def _stopped_error(self, error):
        logging.getLogger("GUI").error("安全停止失败：%s", error)
        messagebox.showerror(
            "安全停止失败",
            f"{error}\n\n桥接 DLL 可能仍在微信里。请点“清理残留桥接”重试；"
            "清理完成前不要结束进程或重启机器人。")
        self._refresh_status()

    def _cleanup_bridge(self):
        """显式清理微信进程中残留的桥接 DLL（相当于异常退出后的 Quit）。"""
        if self.controller.running:
            if not messagebox.askyesno(
                    "清理残留桥接",
                    "机器人正在运行，需要先安全停止（Quit）才能清理桥接 DLL。\n现在停止并清理吗？"):
                return
        if not messagebox.askyesno(
                "清理残留桥接",
                "将扫描微信进程并卸载残留的 text_bridge.dll。\n"
                "请确认没有其他目录的机器人正在使用同一微信；如有，请先在那里停止。\n\n继续吗？"):
            return
        self.button_cleanup.configure(state="disabled")
        self.var_state.set("正在清理残留桥接…")
        self.window.update_idletasks()

        def worker():
            stop_error = None
            try:
                if self.controller.running or self.controller.needs_cleanup:
                    try:
                        self.controller.stop(timeout=45)
                    except Exception as error:
                        stop_error = error
                if self.controller.running:
                    self.window.after(0, lambda: self._cleanup_done(
                        [], None, stop_error or RuntimeError("机器人线程仍在运行，暂不能清理 DLL")))
                    return
                native = str(self.root_dir / "native")
                if native not in sys.path:
                    sys.path.insert(0, native)
                from run_selftest import cleanup_bridges
                rows = cleanup_bridges()
                if self.controller.needs_cleanup:
                    try:
                        self.controller.stop(timeout=45)
                    except Exception as error:
                        stop_error = error
                self.window.after(0, lambda: self._cleanup_done(rows, None, stop_error))
            except Exception as error:
                self.window.after(0, lambda: self._cleanup_done([], error, stop_error))

        threading.Thread(target=worker, name="CleanupBridge", daemon=True).start()

    def _cleanup_done(self, rows, error, stop_error=None):
        self.button_cleanup.configure(state="normal")
        if stop_error is not None:
            rows = list(rows) + [{"pid": "—", "name": "本地清理",
                                  "success": False, "error": str(stop_error)}]
        if error is not None:
            messagebox.showerror("清理失败", str(error))
        elif not rows:
            messagebox.showinfo("清理残留桥接", "没有发现残留的桥接 DLL。")
        else:
            lines = []
            failed = False
            for row in rows:
                ok = bool(row.get("success", row.get("dll_unloaded", False)))
                failed = failed or not ok
                detail = "已卸载" if ok else f"失败：{row.get('error', '模块仍在使用')}"
                lines.append(f"PID {row.get('pid', '—')}  {row.get('name', '')}  {detail}")
            text = "\n".join(lines)
            if failed:
                messagebox.showerror("清理结果", text + "\n\n请关闭正在使用桥接的机器人后重试。")
            else:
                messagebox.showinfo("清理完成", text + "\n\n现在可以重新启动机器人。")
        self._refresh_status()
        self._refresh_plugin_table()

    def _reload_all(self):
        if self.controller.running:
            messagebox.showinfo("重新加载", "请先停止机器人，再重新加载配置。")
            return
        if not messagebox.askyesno("重新加载", "将放弃界面中未保存的修改并重新读取三个配置文件，继续吗？"):
            return
        try:
            self.bot = self.store.load()
            self.llm_path = self._config_path(self.bot.get("llm_config_file", "llm_config.json"))
            member_name = self.bot.get("members_config_file")
            self.members_path = self._config_path(member_name) if member_name else None
            self.llm = load_json(self.llm_path, {})
            self.members = load_json(self.members_path, DEFAULT_MEMBERS) if self.members_path \
                else copy.deepcopy(DEFAULT_MEMBERS)
            self._fill_forms()
            self._refresh_groups()
            self._refresh_plugin_table()
            self._refresh_status()
            logging.getLogger().setLevel(getattr(logging, self.bot.get("log_level", "INFO"), logging.INFO))
            messagebox.showinfo("已重新加载", "配置已重新读取。")
        except Exception as error:
            messagebox.showerror("重新加载失败", str(error))

    def _fill_forms(self):
        self.var_account_input.set(self.bot.get("wechat_account") or "")
        self.var_pid_input.set("" if self.bot.get("wechat_pid") is None else str(self.bot["wechat_pid"]))
        self.var_backend.set(self.bot.get("backend", "native_text"))
        self.var_reconnect.set(str(self.bot.get("reconnect_seconds", 5)))
        self.var_queue.set(str(self.bot.get("plugin_queue_size", 128)))
        self.var_log_level.set(self.bot.get("log_level", "INFO"))
        self.var_console.set(bool(self.bot.get("console", True)))
        delay = self.bot.get("text_send_delay") or {}
        self.var_delay_enabled.set(bool(delay.get("enabled", True)))
        self.var_delay_chars.set(str(delay.get("characters_per_group", 10)))
        self.var_delay_min.set(str(delay.get("min_seconds", 0.1)))
        self.var_delay_max.set(str(delay.get("max_seconds", 0.3)))
        guard = self.bot.get("send_guard") or {}
        self.var_guard_enabled.set(bool(guard.get("enabled", True)))
        self.var_guard_interval.set(str(guard.get("min_interval_seconds", 0.8)))
        self.var_guard_chat.set(str(guard.get("per_chat_seconds", 3.0)))
        self.var_guard_minute.set(str(guard.get("max_per_minute", 20)))
        self.var_guard_duplicate.set(str(guard.get("duplicate_window_seconds", 20.0)))
        self._set_text(self.text_allowlist, "\n".join(self.bot.get("chat_allowlist", [])))

        self.var_base_url.set(self.llm.get("base_url", "https://api.deepseek.com"))
        self.var_api_key.set(self.llm.get("api_key", ""))
        self.var_model.set(self.llm.get("model", "deepseek-flash"))
        self.var_timeout.set(str(self.llm.get("timeout_seconds", 20)))
        self.var_max_tokens.set(str(self.llm.get("max_tokens", 512)))
        self.var_context_limit.set(str(self.llm.get("context_limit", 128000)))
        pricing = self.llm.get("pricing") or {}
        self.var_price_input.set(str(pricing.get("input_per_million", 0)))
        self.var_price_output.set(str(pricing.get("output_per_million", 0)))
        self.var_price_cache_read.set(str(pricing.get("cache_read_per_million", 0)))
        self.var_price_cache_write.set(str(pricing.get("cache_write_per_million", 0)))
        self.var_price_currency.set(str(pricing.get("currency", "USD")))

        self.var_members_enabled.set(self.members_path is not None)
        self.var_history.set(bool(self.members.get("history_enabled", True)))
        self.var_daily_clear.set(bool(self.members.get("daily_clear", True)))
        self.var_database.set(self.members.get("database_file", ".runtime/member_data.sqlite3"))
        self.editor_plugin = None
        self.var_editor.set("未选择插件")
        self._set_text(self.text_plugin, "", editable=True)

    # -- 通用 --------------------------------------------------------------

    def _set_text(self, widget, text, editable=False):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        if not editable:
            widget.configure(state="disabled")

    def _open_path(self, path):
        path = Path(path)
        try:
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except Exception as error:
            messagebox.showerror("打开失败", str(error))

    def _toggle_startup(self):
        link = self._startup_link_path()
        if link is None:
            self.var_startup.set(False)
            messagebox.showerror("开机自启", "无法定位当前用户的启动文件夹。")
            return
        try:
            if self.var_startup.get():
                self._create_startup_shortcut(link)
                messagebox.showinfo("开机自启", f"已创建启动快捷方式：\n{link}\n\n下次登录 Windows 会自动打开并启动机器人。")
            else:
                if link.exists():
                    link.unlink()
                messagebox.showinfo("开机自启", "已取消开机自启。")
        except Exception as error:
            self.var_startup.set(link.exists())
            messagebox.showerror("设置失败", str(error))

    def _create_startup_shortcut(self, link):
        link.parent.mkdir(parents=True, exist_ok=True)
        if getattr(sys, "frozen", False):
            target = Path(sys.executable)
            arguments = "--autostart --minimized"
            workdir = target.parent
        else:
            target = Path(sys.executable)
            if target.name.lower() == "python.exe":
                pythonw = target.with_name("pythonw.exe")
                if pythonw.exists():
                    target = pythonw
            arguments = f'"{self.root_dir / "gui.py"}" --autostart --minimized'
            workdir = self.root_dir

        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"

        script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut(" + quote(link) + ");"
                  "$s.TargetPath=" + quote(target) + ";"
                  "$s.Arguments=" + quote(arguments) + ";"
                  "$s.WorkingDirectory=" + quote(workdir) + ";"
                  "$s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _on_close(self):
        if self.controller.running or self.controller.needs_cleanup:
            if not messagebox.askyesno(
                    "退出",
                    "退出前需要安全停止（Quit）并卸载微信里的桥接 DLL。\n现在停止并退出吗？"):
                return
            try:
                self.controller.stop(timeout=45)
            except Exception as error:
                messagebox.showerror(
                    "无法安全退出",
                    f"{error}\n\n请先点“清理残留桥接”，确认桥接 DLL 已卸载后再关闭窗口。")
                self._refresh_status()
                return
        self._closing = True
        self._save_prefs()
        if self.update_pending:
            self._install_pending_update()
        self.window.destroy()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="微信机器人可视化控制台")
    parser.add_argument("--autostart", action="store_true", help="打开后自动启动机器人")
    parser.add_argument("--minimized", action="store_true", help="启动时最小化到任务栏")
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    window = tk.Tk()
    try:
        panel = ControlPanel(window)
    except Exception as error:
        window.withdraw()
        messagebox.showerror(APP_TITLE, f"启动界面失败：\n{type(error).__name__}: {error}")
        window.destroy()
        return 1
    if args.minimized:
        window.after(200, window.iconify)
    if args.autostart or panel.prefs.get("auto_start"):
        window.after(600, panel._start_bot)
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
