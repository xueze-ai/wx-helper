## 改动说明

<!-- 这个 PR 解决了什么问题？为什么需要这个改动？ -->

## 影响范围

- [ ] 图形界面
- [ ] 插件
- [ ] `wxbot_framework/`
- [ ] `native/` 桥接
- [ ] 打包 / 自动更新
- [ ] 文档 / 测试

## 测试

<!-- 列出执行过的命令和结果 -->

```text
python -B -m unittest discover -s tests -v
python tools/check_publication.py
```

## 隐私检查

- [ ] 没有提交真实 API Key、wxid、群 ID、群名、昵称或聊天正文
- [ ] 没有提交 `.runtime/`、`logs/`、SQLite、CSV 或未脱敏截图
- [ ] 新增配置使用 `*.example.json` 中的占位值
- [ ] 新增日志不会打印密钥、完整请求体或聊天正文

## 其他说明

<!-- 兼容性、迁移步骤、已知限制等 -->
