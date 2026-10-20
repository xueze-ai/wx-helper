# 安全策略 / Security Policy

## 支持范围

| 项目 | 已验证范围 |
| --- | --- |
| wx-helper | 1.2.x |
| Windows | Windows 10/11 x64；Windows Server 2019/2022 Desktop Experience |
| 微信 | **仅 4.1.15.13** |

其他微信版本、Linux、Docker、WSL、Windows Server Core 和 Session 0 服务模式不在支持范围内。

## 报告安全问题

如果你发现的是**代码安全漏洞**（例如命令注入、路径穿越、更新包校验缺陷、密钥意外写入日志等），请不要在公开 Issue 中贴出利用细节、账号信息或聊天内容。请使用 GitHub 的私密漏洞报告：

<https://github.com/xueze-ai/wx-helper/security/advisories/new>

报告中请尽量提供：

- wx-helper 版本、Windows 版本、微信版本；
- 复现步骤和预期行为；
- 已脱敏的日志片段（删除 wxid、群 ID、昵称、聊天正文、API Key 和本地路径）；
- 影响范围，以及是否可能泄露本机文件或密钥。

我会在确认问题后尽快回复。请给修复留出合理时间，不要在修复发布前公开细节。

## 请不要提交这些内容

- OpenAI 兼容接口的 `api_key`、Authorization 头或完整请求体；
- 机器人 wxid、好友 wxid、群 ID、群名称、成员昵称；
- `.runtime/` 数据库、`logs/` 日志、聊天截图、CSV 导出；
- 含真实路径的崩溃堆栈或更新日志；
- 微信客户端文件、逆向数据库、调试器快照或其他不可再分发材料。

如果密钥已经进入 Git 历史，**仅删除文件并不够**：请立即在服务商控制台吊销并轮换密钥，再使用 `git filter-repo` 等工具清理历史；必要时重建仓库。

## 安全边界

- 本项目通过注入 `text_bridge.dll` 调用本机微信 4.1.15.13 的内部函数，属于非官方桥接。它可能触发微信风控，导致账号被限制或封禁；这不是代码漏洞，也无法由作者保证账号安全。
- 大模型请求会发送到你配置的第三方接口。启用 AI 插件前，请阅读该服务商的隐私政策，并使用独立的测试账号和单独的 API Key。
- 本机配置文件、SQLite 数据库和日志默认是明文保存。请使用 Windows 账户权限、BitLocker 或文件夹访问控制保护它们。
- 发布版更新只从“关于”页配置的 GitHub 仓库下载 Release。请确认仓库地址属于你信任的项目，避免把更新源指向未知仓库。

## 安全使用建议

1. 先用小号测试，不要在重要账号或生产环境直接运行。
2. 不要把 `bot_config.json`、`llm_config.json`、`members_config.json`、`.runtime` 或 `logs` 上传到网盘、Issue 或聊天群。
3. 为机器人单独申请低额度 API Key，并定期轮换。
4. 不需要 AI 功能时，关闭 `mention_chat`、`dm_chat`、`chat_summary`、`idiom_chain` 等调用大模型的插件。
5. 发现账号出现“操作频繁”“当前账号存在安全风险”等提示时，立即停止运行。
