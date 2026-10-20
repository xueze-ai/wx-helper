"""微信机器人框架的统一入口：python main.py。"""
from __future__ import annotations

import argparse
import json
import logging
import math
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys

from wxbot_framework.config import ConfigStore
from wxbot_framework.locking import InstanceLock
from wxbot_framework.paths import app_root
from wxbot_framework.runtime import BotApplication

ROOT = app_root()


def configure_logging(config):
    directory = ROOT / "logs"
    directory.mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file = RotatingFileHandler(directory / "bot.log", maxBytes=2 * 1024 * 1024,
                               backupCount=3, encoding="utf-8")
    file.setFormatter(formatter)
    logging.basicConfig(level=config.get("log_level", "INFO").upper(), handlers=[console, file], force=True)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "bot_config.json")
    parser.add_argument("--check", action="store_true", help="检查配置、清单及启用插件语法，不注入微信")
    parser.add_argument("--list-plugins", action="store_true", help="列出可用插件，不注入微信")
    parser.add_argument("--cleanup", action="store_true", help="卸载微信进程中残留的桥接 DLL")
    parser.add_argument("--run-seconds", type=float, default=0, help="有限时长检查；默认 0 持续运行")
    args = parser.parse_args()
    if not math.isfinite(args.run_seconds) or args.run_seconds < 0:
        parser.error("运行时长必须是非负有限数值")
    if args.cleanup:
        native = str(ROOT / "native")
        if native not in sys.path:
            sys.path.insert(0, native)
        from run_selftest import cleanup_bridges
        rows = cleanup_bridges()
        print(json.dumps({"cleanup": rows}, ensure_ascii=False), flush=True)
        return 0 if all(row.get("success", False) for row in rows) else 1
    try:
        store = ConfigStore(args.config)
        config = store.load()
        if not (args.check or args.list_plugins) and config.get("wechat_account") == "wxid_example_robot":
            parser.error("请先在本地 bot_config.json 中将 wechat_account 占位符改为自己的机器人账号 ID")
        app = BotApplication(ROOT, store)
        if args.check or args.list_plugins:
            specs = app.plugins.discover()
            if app.plugins.load_errors:
                raise ValueError(str(app.plugins.load_errors))
            for spec in specs.values():
                enabled = config.get("plugins", {}).get(spec.name, {}).get("enabled", True)
                if args.check and enabled:
                    for source in spec.path.parent.rglob("*.py"):
                        compile(source.read_bytes(), str(source), "exec")
                    if spec.name in ("idiom_chain", "chat_summary", "mention_chat"):
                        from wxbot_framework.llm import LLMClient
                        LLMClient.from_file(store.path.parent / config.get("llm_config_file", "llm_config.json"))
                print(json.dumps({"name": spec.name, "enabled": enabled, "api": spec.api,
                                  "description": spec.description,
                                  "missing_capabilities": sorted(spec.requires - app.plugins.capabilities)}, ensure_ascii=False))
            if args.check:
                print("配置与插件语法检查通过")
            return 0
        configure_logging(config)
        with InstanceLock(ROOT / ".runtime/bot.lock"):
            app.run(seconds=args.run_seconds)
        return 0
    except Exception:
        logging.getLogger("Main").exception("启动或运行失败")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
