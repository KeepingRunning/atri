# ATRI

ATRI QQ 群聊机器人：记录允许群的聊天消息，仅在被真正 @ 时，将人设与最近 50 条聊天记录交给大模型生成回复，再发送回原群。单纯提到名字或引用消息不会触发回复。

## 项目架构

```text
QQ / OneBot → 检查 @ → 人设 + 聊天记录 → LLM Provider → QQ 回复
```

- `onebot.py`：OneBot v11 反向 WebSocket 接入与发送回执。
- `bot.py`：消息过滤、按群排队、@ 判断与回复流程。
- `context.py` / `storage.py`：构造 conversation，按群保存聊天记录；仅确认发送成功的回复进入历史。
- `model.py`：调用 OpenAI 兼容的 Chat Completions 接口。
- `config.toml` / `personal_info.txt`：运行配置与角色人设。

## 启动方法

需要 Python 3.11+、uv、支持 OneBot v11 的 QQ 客户端（如 NapCat），以及可用的模型接口。

在本项目目录执行（已有 `config.toml` 时跳过复制）：

```sh
uv sync --locked
cp -n config.toml.template config.toml
```

编辑 `config.toml`，填写 `bot.allowed_groups`、`bot.self_id`、`onebot.token`，以及 `llm.base_url`、`llm.model`、`llm.api_key`。按需修改 `personal_info.txt`。所有运行配置均从 TOML 读取；含密钥的 `config.toml` 已被 Git 忽略。

在 QQ 客户端配置 **Universal 反向 WebSocket**：同机部署的默认地址为 `ws://127.0.0.1:28080/onebot/v11/ws`，access token 与 `onebot.token` 一致；跨主机或容器部署时使用客户端可访问的 bot 地址。

```sh
uv run atri check
uv run atri serve
```

连接后，在允许的群中 @ 机器人即可。聊天记录保存在 `data/groups/`，连接状态可通过 `http://127.0.0.1:28080/healthz` 查看。

## 许可证

[MIT License](LICENSE)。
