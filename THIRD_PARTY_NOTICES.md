# 第三方组件与许可证说明

wx-helper 本体采用 [MIT License](LICENSE)。下面列出仓库中直接包含或发布版运行时用到的第三方组件；各组件的版权和许可证归其各自作者所有。

## 1. 仓库中直接包含的组件

### MinHook 1.3.4

- 用途：`native/` 中的 x64 API Hook 库。
- 位置：`native/vendor/minhook-1.3.4/`。
- 许可证：BSD 2-Clause。
- 版权：Copyright (C) 2009-2017 Tsuda Kageyu；其中 Hacker Disassembler Engine 部分 Copyright (c) 2008-2009 Vyacheslav Patkov。
- 完整许可与作者信息：
  - `native/vendor/minhook-1.3.4/LICENSE.txt`
  - `native/vendor/minhook-1.3.4/AUTHORS.txt`
  - `native/vendor/minhook-1.3.4/src/hde/` 中的原始版权声明

`native/build_text/text_bridge.dll` 和 `native/build/selftest_bridge.dll` 由本项目桥接源码与上述 MinHook 组件编译生成。发布这些 DLL 时请保留本文件和 MinHook 的许可证。

## 2. 运行或构建依赖（未复制源码到本仓库）

| 组件 | 用途 | 许可证 | 说明 |
| --- | --- | --- | --- |
| Python 3.12 | 运行时 | PSF License 2.0 | Windows 官方发行版还包含 Tcl/Tk、OpenSSL 等组件，分别遵循其许可证 |
| Tcl/Tk | tkinter 图形界面 | BSD 风格许可证 | 随 Python Windows 发行版提供 |
| SQLite | 本地统计与成员数据库 | Public Domain | Python 标准库 `sqlite3` 自带 |
| pefile | 读取 PE 文件信息、辅助版本适配 | MIT | `requirements.txt` 中的唯一运行依赖 |
| PyInstaller | 构建 Windows EXE | GPL-2.0-or-later，含 bootloader 特殊例外 | 仅构建时需要；其输出不因此被 GPL 覆盖。见 <https://pyinstaller.org/en/stable/license.html> |

本项目没有把 Python、Tcl/Tk、SQLite、pefile 或 PyInstaller 的源码复制进仓库。通过 PyInstaller 生成的 EXE 会包含 Python 运行时和上述部分组件，分发时请同时保留本说明，并遵守对应许可证。

## 3. 不包含的材料

本仓库**不包含**：

- 微信客户端程序、微信核心 DLL 或腾讯的源代码；
- 逆向数据库、调试器快照、内存 dump、探针可执行文件；
- 真实用户账号、群聊、聊天记录、API Key 或日志；
- 任何旧版本或来源不明的桥接 DLL。

`native/` 只保留重新编译所需的桥接源码、兼容性指纹、Python 服务代码、MinHook 源码与许可证，以及两个由这些源码构建的运行时 DLL。

## 4. 商标与关联声明

“微信 / WeChat”是腾讯控股有限公司的商标。本项目是独立的第三方学习研究项目，与腾讯官方没有任何隶属、授权、赞助或背书关系。

使用本项目即表示你理解：非官方桥接可能违反微信用户协议，并可能导致账号被限制或封禁。请在遵守当地法律和服务条款的前提下，自行承担使用风险。
