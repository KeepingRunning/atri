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

DeepSeek 官方接口可使用 `llm.base_url = "https://api.deepseek.com"`、`llm.model = "deepseek-v4-flash"`，并设置 `llm.thinking = "disabled"` 关闭深度思考，适配短回复和意愿评分。该设置同时作用于回复与判断请求；其他供应商默认留空，不发送此参数。参数说明见 [DeepSeek 官方文档](https://api-docs.deepseek.com/guides/thinking_mode/)。

在 QQ 客户端配置 **Universal 反向 WebSocket**：同机部署的默认地址为 `ws://127.0.0.1:28080/onebot/v11/ws`，access token 与 `onebot.token` 一致；跨主机或容器部署时使用客户端可访问的 bot 地址。

```sh
uv run atri check
uv run atri serve
```

默认使用 `reply.mode = "willingness"`；旧配置没有 `[reply]` 时也使用该模式。改为 `at_only` 可恢复原来的仅真正 @ 回复。修改配置后重启服务生效。

`reply.frequency` 越大，越容易评估普通群聊；设为 0 时只评估真正 @ 或引用本机器人已确认发送消息的内容。`reply.threshold` 是判断模型的意愿阈值，默认 60。判断后决定接话时共调用模型两次，可用 `reply.judgment_model` 指定同一接口上的快速判断模型。

每次规则分数、分项和模型判断记录在 `data/groups/<群号>/messages.jsonl` 的 `kind="willingness"` 行中，不会进入聊天上下文。详情、算法参数和 MaiBot 参考位置见 [回复意愿设计](docs/reply-willingness.md)。

连接状态可通过 `http://127.0.0.1:28080/healthz` 查看。

## 查看处理日志

默认 `DEBUG` 详细日志，终端按模块配色：接收为青色、队列为蓝色、willingness 为粉紫色、上下文为紫色、模型为金色、发送为绿色、存储为灰青色。警告和错误额外使用橙色和红色。每条消息的处理日志带 `g=群号 m=消息号 u=用户号`，并发群聊和异步发送回执也能对应到原消息。

日志包含接收/过滤/去重、排队耗时、各项意愿计算与公式、冷却和退避剩余时间、上下文数量、模型调用耗时与接口返回的 token 用量、回复预览、发送回执和总耗时。这里展示可观测的计算和模型返回的判断理由。

```sh
# 只预览完整日志效果：本地模拟模型和 OneBot，不读取真实配置、不连接 QQ。
uv run python scripts/demo_logs.py

# 启动实际服务时查看彩色日志；若重定向后还想保留颜色，使用 --log-color always。
uv run atri --log-level DEBUG --log-color auto serve

# 后台日志默认另存为无色文件，可按消息号搜索；单文件 10 MiB、最多 3 份备份。
tail -f data/logs/atri.log
```

`[logging]` 支持级别、颜色、文件路径、轮转大小和正文预览长度；`preview_chars=0` 可隐藏收发正文。`color="auto"` 在终端启用颜色，重定向时自动关闭，支持 `NO_COLOR`；`always` 强制保留 ANSI 颜色，`never` 关闭颜色。API key、OneBot token 不会写入运行日志，模型请求不打印认证头或完整请求体。

可以单独提高或降低模块的详细程度，例如：

```toml
[logging]
level = "INFO"
color = "auto"

[logging.modules]
willingness = "DEBUG"
storage = "WARNING"
"plugins.weather" = "DEBUG"
```

以后插件使用 `logging.getLogger("atri.plugins.插件名")` 即可获得独立分类和稳定配色；在消息处理任务内调用会自动继承消息追踪信息。详细 JSONL 聊天记录仍保存在原来的 `data/groups/` 下。

## 测试

实测当前配置的大模型 API：

```sh
uv run atri test-api
# 指定其他配置；只显示测试结果和错误日志：
uv run atri --config config.local.toml --log-level ERROR test-api
```

`test-api` 会发起 4 次真实模型请求：最短 `OK` 回复、打招呼时的意愿判断、明确要求安静时的意愿判断、携带人设的聊天回复。复用正式模型客户端、上下文和超时配置，显示每项耗时、回复或评分、错误码与最终汇总；判断请求使用 `reply.judgment_model`（留空则沿用 `llm.model`）。这是小样本冒烟测试，不代表完整的人设或语义准确率评测。

测试读取 `config.toml` 和人设文件，不要求填写 QQ 配置，不启动服务、不读取真实群历史、不写入群聊记录；运行日志仍按 `[logging]` 配置输出。退出码为 0（全部通过）、1（请求失败或结果不符合预期）、2（配置错误），Ctrl+C 中断为 130。`atri check` 仍仅检查配置，不请求模型。

本地自动测试：

```sh
uv run python -m unittest discover -s tests -v
```

自动测试使用模拟群消息和本地模拟模型接口，不连接真实 QQ 或外部模型；不会自动执行上述真实 API 请求。

## 许可证

[MIT License](LICENSE)。
