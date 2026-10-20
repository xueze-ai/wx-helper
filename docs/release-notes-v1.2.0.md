# wx-helper v1.2.0 发布说明

- 发布日期：2026-10-20
- 版本：`1.2.0`
- 标签：`v1.2.0`
- 许可证：MIT
- 仓库：<https://github.com/xueze-ai/wx-helper>

这是 wx-helper 的首个 MIT 开源版本。发布包为 **Windows x64 单目录版（onedir）**，压缩包内包含 `wx-helper.exe`、`_internal/`、`plugins/`、`native/` 和文档。解压后请保留整个目录，不要只复制 `wx-helper.exe`。

## 运行环境

| 项目 | 要求 |
| --- | --- |
| 系统 | Windows 10/11 x64；Windows Server 2019/2022 Desktop Experience |
| 微信 | **仅验证 4.1.15.13** |
| 运行方式 | 解压后运行 `wx-helper.exe`，无需安装 Python |
| 不支持 | Linux、Docker、WSL、Windows Server Core、Session 0 服务模式 |

微信升级后，原生桥接可能因版本指纹不匹配而拒绝注入。请先确认微信版本，使用小号测试。

## 安装

1. 下载 `wx-helper-v1.2.0-win64.zip`。
2. 解压到一个**新的空目录**，例如 `<安装目录>\wx-helper`。
3. 运行 `wx-helper.exe`。首次启动会生成空的 `bot_config.json`、`llm_config.json` 和 `members_config.json`。
4. 在“基础设置”页检测并连接本机微信。
5. 在“大模型”页填写你自己的 OpenAI 兼容接口地址、API Key 和模型名。
6. 在“群与成员”页导入群、启用插件，然后在小号群测试 `/菜单` 和一次 @ 回复。

`plugins/` 和 `native/` 必须与 `wx-helper.exe` 保持相对位置；缺少桥接 DLL 或插件目录会导致启动或回复失败。

## 从旧版本升级

1. 停止机器人，确认桥接 DLL 已清理。
2. 备份旧目录中的 `bot_config.json`、`llm_config.json`、`members_config.json`、`.runtime/` 和 `logs/`。
3. 将新版本解压到新目录，不要直接把新的 `_internal/` 覆盖到旧目录。
4. 把备份的配置文件复制到新目录；保留新版本的 `plugins/`、`native/` 和 `_internal/`。
5. 启动后检查微信版本、插件开关和发送保护参数。

## 主要功能

- tkinter 图形控制台：运行状态、基础设置、大模型、群与成员、插件、统计、日志、关于。
- 真正的微信群内 @ 回复，以及好友私聊 AI 回复。
- 群玩法、聊天总结、成员绑定、人格切换、成语接龙、聊天记录等外部插件。
- 大模型调用统计：按“插件 + 会话”聚合消息数、输入/输出/推理/缓存 tokens、使用率、费用和时间。
- 发送保护：全局发送间隔、同会话冷却、每分钟上限、重复内容抑制。
- 同一条入站消息只由一个插件回复，避免重复刷屏。
- 启动后后台检查 GitHub Releases；下载完成后由用户选择立即重启或退出时安装。
- 单目录 EXE 打包；`plugins/` 和 `native/` 保持外部可编辑。

## 修复与改进

- 修复同一条入站消息被多个插件重复回复的问题；`mention_chat` 跳过 `/` 开头的命令。
- 改进桥接 DLL 残留、实例锁和退出清理流程。
- 修复统计页空数据、成本显示和 CSV 导出。
- 补全开源文档、隐私说明、安全策略、贡献指南、行为准则和第三方许可证说明。
- 发布目录检查脚本会拦截本地配置、聊天数据库、日志、构建产物和常见密钥格式。
- 183 个不连接微信的单元测试通过。

## 隐私与安全

- 本项目默认只在本机运行，没有自建遥测、埋点或远程日志服务器。
- 只有启用并触发大模型插件时，才会向你在 `llm_config.json` 中填写的服务商发送必要上下文。
- 发布包不包含真实 API Key、wxid、群 ID、聊天数据库或运行日志；随包截图已脱敏。
- 请勿把 `bot_config.json`、`llm_config.json`、`members_config.json`、`.runtime/` 或 `logs/` 上传到 Issue、网盘或公开仓库。
- 非官方桥接存在微信风控、账号限制或封禁风险；请使用小号，不要在重要账号或生产环境使用。
- MIT 许可证允许商业使用，但请自行确认是否符合微信用户协议、当地法律和大模型服务商条款。

详细说明见仓库中的 [PRIVACY.md](https://github.com/xueze-ai/wx-helper/blob/main/PRIVACY.md)、[SECURITY.md](https://github.com/xueze-ai/wx-helper/blob/main/SECURITY.md) 和 [THIRD_PARTY_NOTICES.md](https://github.com/xueze-ai/wx-helper/blob/main/THIRD_PARTY_NOTICES.md)。

## 发布资产

| 文件 | 说明 |
| --- | --- |
| `wx-helper-v1.2.0-win64.zip` | Windows x64 单目录发布包，约 14.6 MB |
| `wx-helper-v1.2.0-win64.zip.sha256` | SHA-256 校验文件 |

SHA-256：

```text
ffb327a6bf5c13d16e73f61d2ddbc4ec8b62296b2d8f582163a6d8f80999e036
```

Windows PowerShell 校验：

```powershell
Get-FileHash .\wx-helper-v1.2.0-win64.zip -Algorithm SHA256
```

## 已知限制

- 只验证过微信 **4.1.15.13**；其他版本不保证可用。
- 发布包已在无微信登录环境下完成启动冒烟测试；真实微信登录、群内 @ 回复和真实大模型调用需要在你自己的环境验证。
- 自动更新只检查 GitHub Releases，不会上传聊天记录或本地数据库。
- 大模型接口必须兼容 OpenAI 的 `POST /chat/completions`，并返回可解析的 JSON。

## 反馈

- 问题与功能建议：<https://github.com/xueze-ai/wx-helper/issues>
- 私密安全报告：<https://github.com/xueze-ai/wx-helper/security/advisories/new>

提交 Issue 前请删除 API Key、wxid、群 ID、群名、昵称、聊天正文和本地路径。

## 许可证

本项目采用 [MIT License](https://github.com/xueze-ai/wx-helper/blob/main/LICENSE)。第三方组件和桥接 DLL 的许可证见 [THIRD_PARTY_NOTICES.md](https://github.com/xueze-ai/wx-helper/blob/main/THIRD_PARTY_NOTICES.md)。
