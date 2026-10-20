# 参与贡献 / Contributing

感谢你愿意改进 wx-helper。提交代码前，请先阅读 [README](README.md)、[安全策略](SECURITY.md) 和 [隐私说明](PRIVACY.md)。

## 开发环境

- Windows 10/11 x64（原生桥接只支持 Windows；Linux/Docker/WSL 不能运行完整功能）
- Python 3.12 x64
- 微信 4.1.15.13（只有需要联调桥接时才需要）
- 重新编译 `native/` 时需要 Visual Studio Build Tools 的 MSVC x64 开发者命令环境

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

如果只做界面、配置或插件开发，不需要安装 PyInstaller；需要打包时再执行：

```powershell
python -m pip install -r requirements-build.txt
```

## 运行与测试

```powershell
python tools\init_config.py   # 生成空的本地配置（也可以直接启动 gui.py）
python gui.py                 # 图形控制台
python main.py --check        # 检查配置、插件与语法，不连接微信
python -B -m unittest discover -s tests -v
```

测试不连接微信、不调用真实大模型，也不应读取你本机的 `bot_config.json`。测试临时文件放在系统临时目录，不会写入项目 `.runtime`。请在提交前确保全部测试通过。

## 代码结构

```text
gui.py                   tkinter 控制台
main.py                  命令行入口
wxbot_framework/         配置、消息分发、成员目录、LLM、统计、发送保护、更新
plugins/                 可独立启停的外部插件
native/                  C++ 桥接源码、Python 服务、兼容性指纹和 MinHook
tests/                   不依赖微信的单元测试
tools/                   配置初始化、打包和发布前检查
docs/screenshots/        已脱敏的界面截图
```

## 新增或修改插件

1. 复制 `plugins/_template/` 为新的插件目录；
2. 在 `plugin.json` 中填写名称、版本和说明；
3. 实现 `Plugin.on_load` / `on_message`，不要在导入阶段连接网络；
4. 通过 `PluginContext` 的 `reply`、`reply_at`、`send_text` 发送消息，不要绕过发送保护和回复归属；
5. 在 `bot_config.example.json` 中补充默认开关和公开参数；
6. 增加不依赖真实微信和大模型的单元测试；
7. 更新 README 的插件表和必要文档。

## 提交前检查

```powershell
python -B -m unittest discover -s tests -v
python tools\check_publication.py
```

`check_publication.py` 只报告文件路径和问题类型，不会打印密钥原文。检查通过后再提交。

## 隐私与安全红线

**不要提交**以下内容，即使只是测试数据：

- 真实 `api_key`、Authorization 头、`.env` 或私钥；
- 真实 wxid、群 ID、群名称、成员昵称、手机号、邮箱；
- `.runtime/`、`logs/`、SQLite、CSV、聊天截图；
- 含本机绝对路径或用户名的崩溃日志；
- 微信客户端文件、逆向数据库、调试器快照、旧版本 DLL。

截图必须使用测试账号，并遮挡 wxid、群 ID、头像、昵称、聊天正文和本地路径。提交前请逐张检查。

如果发现密钥已经进入 Git 历史，请先轮换密钥，再清理历史；不要只删除当前文件。

## 提交规范

- 一个 Pull Request 尽量只做一件事；
- 说明改动目的、影响范围和测试结果；
- 修复 bug 时附上最小复现步骤；
- 不提交与改动无关的格式化、重命名或生成文件；
- 保持现有的中文界面、日志和配置命名风格；
- 涉及 `wxbot_framework/`、`native/`、更新器或发送保护的改动，请补充对应测试。

## 安全问题

请不要在公开 Issue 中披露可利用细节。按照 [SECURITY.md](SECURITY.md) 使用 GitHub 私密漏洞报告。
