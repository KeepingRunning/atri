# ATRI

ATRI QQ 群聊机器人：记录允许群的聊天消息，每天上海时间 00:00–08:00 睡觉、不回复；其他时段先计算接话意愿，再结合人设、当前日常和最近 50 条聊天记录生成回复。支持 @、引用机器人、名字提及、续聊以及普通群聊参与；模型可以选择保持安静。

## 项目架构

```text
QQ / OneBot → 睡眠拦截 → 规则评分 → 模型判断接话意愿
                                    ↓
        人设 + 本地随机日程 + 聊天记录 → 生成回复 → 发送前睡眠拦截 → QQ 回执
```

- `onebot.py`：OneBot v11 反向 WebSocket 接入与发送回执。
- `bot.py`：消息过滤、按群排队、意愿判断与回复流程。
- `willingness.py`：接话评分、冷却、续聊和连续等待退避。
- `context.py` / `storage.py`：构造 conversation，按群保存聊天记录；仅确认发送成功的回复进入历史。
- `model.py`：调用 OpenAI 兼容的 Chat Completions 接口。
- `schedule.py`：按时段随机选取本地日常、恢复当前选择、控制睡眠时段；素材在 `resources/daily_routines/`。
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

`reply.frequency` 越大，越容易评估普通群聊；设为 0 时只评估真正 @ 或引用本机器人已确认发送消息的内容。`reply.threshold` 是判断模型的意愿阈值，默认 60。判断一次成功并决定接话时共调用模型两次，可用 `reply.judgment_model` 指定同一接口上的快速判断模型。

判断提示词明确要求仅返回 `score`、`reason` 的 JSON；人设及历史作为独立数据传入。判断失败会再重试两次（共最多三次），重试时重申输出协议。仍失败则用本条消息的规则分对比 `reply.threshold`：默认 ≥60 生成回复，低于 60 等待。例如规则分 70 会回复，45 会等待。单次技术失败不累计等待次数，只按最终决策更新一次状态。降级只决定是否生成回复，聊天正文仍单独调用模型生成。

每次规则分数、分项和模型判断记录在 `data/groups/<群号>/messages.jsonl` 的 `kind="willingness"` 行中，不会进入聊天上下文。详情、算法参数和 MaiBot 参考位置见 [回复意愿设计](docs/reply-willingness.md)。

连接状态可通过 `http://127.0.0.1:28080/healthz` 查看。

## 虚构日程与睡眠

日程直接使用已认可的 [116 份两小时日常](resources/daily_routines/README.md)，运行时不调用大模型生成日程。按上海时间自然偶数整点划分窗口，例如 08:00–10:00、10:00–12:00；每个窗口从适合时段的文件中等概率随机选一份，有其他候选时避免与上一份重复。同窗口内所有群共用同一份，重启也恢复当前选择。

| 窗口 | 匹配 `suggested_time_of_day` |
| --- | --- |
| 00:00–08:00 | 固定睡觉，不抽选 |
| 08:00–10:00 | 清晨、上午 |
| 10:00–12:00 | 上午 |
| 12:00–14:00 | 中午 |
| 14:00–16:00、16:00–18:00 | 下午 |
| 18:00–20:00 | 傍晚 |
| 20:00–22:00、22:00–24:00 | 夜晚 |

标为“不限”的素材可进入所有清醒窗口。仅按时间标签选取，不维护跨窗口的地点、伙伴或剧情状态；这些条件作为本窗的虚构场景说明传入。活动安排不等于已经发生的经历。

决定接话、取得模型并发槽位后，将两小时大日程和当前十分钟小日程加入 conversation 的 system 提示词，与原有人设一起使用；其他十一格、出处和审核记录不传入。日程不主动发消息、不增加意愿分、不单独写入群聊历史。

**00:00（含）至 08:00（不含）由代码强制禁止回复，包括 @、引用、名字呼叫。** 夜间新消息在入口记录、去重后即结束处理，不排入回复队列、不发给 LLM。聊天的每次模型 HTTP 提交前都会重新检查睡眠，意愿判断跨午夜失败也不再重试或降级。午夜前已经排队或提交给模型的请求若跨午夜，后续处理会丢弃，08:00 后不补发。已经在午夜前提交给 OneBot 的消息，其网络送达或回执可能在午夜后发生。

```toml
[schedule]
enabled = true
timezone = "Asia/Shanghai"
routines_dir = "resources/daily_routines"
```

没有 `[schedule]` 时也默认启用。`enabled = false` 只关闭清醒时的日程背景，夜间禁回复仍生效。路径相对于配置文件目录，也可用绝对路径。旧的 `schedule.model` 会被忽略，可删除。配置修改后重启服务生效。

`data/schedule/state.json` 只保存当前窗口和选中的素材 ID 等恢复信息；旧生成式缓存不再使用。删改素材会在下一窗口或重启后重新读取。坏文件会跳过并记录日志；没有合适候选时用“自由休息”，不会临时调用模型补写。后台每 30 秒检查窗口，收到消息时也按实际时钟刷新，不需要等待后台轮询。

本地预览无需 API 密钥、不连接 QQ、不读取群历史、不改正式日程缓存：

```sh
uv run atri test-schedule --at 2026-09-11T18:35:00+08:00
uv run atri test-schedule --at 2026-09-12T00:00:00+08:00
```

预览显示选中日常的十二格及当前提示词背景；省略 `--at` 使用当前上海时间，重复运行预览会独立抽选。详细实现与验证范围见 [日程设计](docs/daily-routines.md)。

## 查看处理日志

默认 `DEBUG` 详细日志，终端按模块配色：接收为青色、队列为蓝色、willingness 为粉紫色、上下文为紫色、模型为金色、发送为绿色、存储为灰青色。警告和错误额外使用橙色和红色。每条消息的处理日志带 `g=群号 m=消息号 u=用户号`，并发群聊和异步发送回执也能对应到原消息。

级别栏固定为 5 字符：`|DEBUG|`、`|INFO |`、`|WARN |`、`|ERROR|`、`|CRIT |`，终端和文件日志保持一致。`WARN`、`CRIT` 是显示简称，配置仍使用 `WARNING`、`CRITICAL`。

日志包含接收/过滤/去重、排队耗时、各项意愿计算与公式、冷却和退避剩余时间、上下文数量、模型调用耗时与接口返回的 token 用量、回复预览、发送回执和总耗时。这里展示可观测的计算和模型返回的判断理由。

判断失败可搜索 `[判断重试]`、`[判断重试耗尽]`、`[规则降级]`。可恢复的模型判断错误以结构化日志记录；其他异常保留堆栈，堆栈每一行都带时间、级别、模块、消息标识和对应配色。

日程使用独立青绿色 `schedule` 分类。搜索 `[随机日程选定]`、`[日程恢复]`、`[日程窗口生效]`、`[采用日程背景]`、`[睡眠拦截]` 可追踪抽选到使用、夜间丢弃的过程；DEBUG 显示候选数量、当前小格和关注点。坏文件或无候选会有警告。可在 `[logging.modules]` 设置 `schedule = "DEBUG"`。

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

`test-api` 包含 4 项：最短 `OK` 回复、打招呼时的意愿判断、明确要求安静时的意愿判断、携带人设的聊天回复。通常发起 4 次真实请求；两个判断项目失败时各最多重试两次，合计最多 8 次请求。复用正式模型客户端、上下文和超时配置，显示每项耗时、回复或评分、错误码与最终汇总；判断请求使用 `reply.judgment_model`（留空则沿用 `llm.model`）。测试命令在重试耗尽时仍报告失败，不把规则降级算作 API 测试通过。这是小样本冒烟测试，不代表完整的人设或语义准确率评测。

测试读取 `config.toml` 和人设文件，不要求填写 QQ 配置，不启动服务、不读取真实群历史、不写入群聊记录；运行日志仍按 `[logging]` 配置输出。退出码为 0（全部通过）、1（请求失败或结果不符合预期）、2（配置错误），Ctrl+C 中断为 130。`atri check` 仍仅检查配置，不请求模型。

本地自动测试：

```sh
uv run python -m unittest discover -s tests -v
```

日程测试覆盖时段筛选、十分钟及两小时边界、重启恢复、坏文件降级、午夜/08:00、跨午夜排队与生成、发送前拦截和本地预览无 API 调用。

自动测试使用模拟群消息和本地模拟模型接口，不连接真实 QQ 或外部模型；不会自动执行上述真实 API 请求。

## 许可证

[MIT License](LICENSE)。
