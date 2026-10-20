"""应用根目录；源码运行和 PyInstaller 打包后的目录保持一致。"""
from __future__ import annotations

import sys
from pathlib import Path


def app_root():
    """返回配置、插件、原生目录和日志所在的根目录。

    源码运行时是项目目录；打包后是 EXE 所在目录，避免把配置和日志写进
    PyInstaller 的临时解包目录（退出即丢失）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
