# ATRI

ATRI QQ 群聊机器人：记录允许群的聊天消息，先计算接话意愿，再结合人设和最近 50 条聊天记录生成回复。支持 @、引用机器人、名字提及、续聊以及普通群聊参与；模型可以选择保持安静。

## 项目架构

```text
QQ / OneBot → 规则评分 → 模型判断接话意愿 → 人设 + 聊天记录 → 生成回复 → QQ 回执
```

- `onebot.py`：OneBot v11 反向 WebSocket 接入与发送回执。
- `bot.py`：消息过滤、按群排队、意愿判断与回复流程。
- `willingness.py`：接话评分、冷却、续聊和连续等待退避。
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

默认使用 `reply.mode = "willingness"`；旧配置没有 `[reply]` 时也使用该模式。改为 `at_only` 可恢复原来的仅真正 @ 回复。修改配置后重启服务生效。

`reply.frequency` 越大，越容易评估普通群聊；设为 0 时只评估真正 @ 或引用本机器人已确认发送消息的内容。`reply.threshold` 是判断模型的意愿阈值，默认 60。判断后决定接话时共调用模型两次，可用 `reply.judgment_model` 指定同一接口上的快速判断模型。

每次规则分数、分项和模型判断记录在 `data/groups/<群号>/messages.jsonl` 的 `kind="willingness"` 行中，不会进入聊天上下文。详情、算法参数和 MaiBot 参考位置见 [回复意愿设计](docs/reply-willingness.md)。

连接状态可通过 `http://127.0.0.1:28080/healthz` 查看。

## 测试

```sh
uv run python -m unittest discover -s tests -v
```

测试使用模拟群消息和本地模拟模型接口，不连接真实 QQ 或外部模型。

## 许可证

[MIT License](LICENSE)。
