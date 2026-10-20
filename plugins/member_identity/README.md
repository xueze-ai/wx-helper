# 本群称呼与成员目录

在已启用的群里，由成员本人发送 `/绑定 昵称`，即可将这个称呼与消息里的真实作者 ID 联系起来。

绑定只影响当前群。同一个人在其他群可以使用不同称呼，未绑定的群使用从微信联系人缓存取得的原始名字。绑定名用于本群的艾特显示和按人查询；再次绑定会更新首选称呼，旧称呼仍可在本群作为别名查询。该功能不修改微信资料。

`members_config.json` 中的 `groups` 指定支持哪些群，成员列表可以为空。不同机器人账号、不同群的数据分别保存。实际绑定、已知身份和当天文本存放在 `.runtime/member_data.sqlite3`，重启保留。

框架先保存新消息，再分发给插件。每天北京时间 0 点清理旧日文本，`/清空` 清除当前群文本；绑定称呼仍保留。

开发接口包括 `ctx.members.resolve(name, room)`、`display_name(wxid, room)`、`history(name, room)`、`today_snapshot(name, room)` 和 `today_group_snapshot(room)`。查询身份始终使用成员 ID，不能根据重名猜测是谁。
