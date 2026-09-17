# 历史检索与工具接口

消息完整存档和模型默认上下文分开：JSONL 保留已记录的消息，默认上下文仍只取最近一小时。Planner 需要证据时先查询，再决定行动；Replyer 接收实际结果并写正文。旧 willingness 模式在回复阶段调用工具，意愿评分不使用工具。

## 调用流程

当前 Planner 的循环见 [Planner 与 Replyer](planner.md)：每步一个查询或行动，工具预算跨等待/重规划保持。达到查询上限时只保留行动工具。下面是仍保留的旧回复路径；工具契约、权限和结果格式共用。

```text
规则与意愿判断决定接话
  → 人设 + 当前日程 + 近一小时聊天 + 工具说明/schema
  → 模型返回正文，或返回 assistant.tool_calls
  → 校验参数 → 执行本群查询 → role=tool 返回 JSON
  → 模型继续检索或生成最终正文
  → 睡眠检查 → 发送 → 确认成功后写入聊天历史
```

使用 Chat Completions 原生的 function calling。工具定义通过 `tools` 参数发送；每个调用用 `tool_call_id` 与结果对应，支持一次返回多个调用，实际顺序执行。不会解析自然语言中的“我要调用某工具”，也不把工具前言当成最终回复。格式参考 [DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)。

保留同一轮模型返回的 `reasoning_content` 供后续工具请求回传，以兼容 [DeepSeek 思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。该字段不写聊天存档、不打印，也不跨用户轮次保存。下一条用户消息的上下文仍从普通聊天历史重新构造。

## 内置检索工具

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `search_chat_history` | `query`、`user_id`、`since`、`until`、`limit`、`offset` | 按消息时间从新到旧搜索用户消息及已确认发出的机器人回复 |
| `get_chat_context` | 必填 `record_id`，可选 `before`、`after` | 展开命中消息前后的聊天，按存档顺序排列 |
| `search_event_logs` | 必填 `message_id`，可选 `kind`、`limit`、`offset` | 按触发消息号或发送成功的回复消息号，查对应处理记录 |

`search_chat_history`：关键词按空格拆分，所有词都需在同一消息中按字面命中，英文忽略大小写。没有中文分词、正则或语义匹配；未命中可以换更短的关键词。空关键词时必须给发言人或时间条件。`user_id` 是 QQ 号。默认搜索所有已存档时间，`since` / `until` 使用带时区的 ISO 8601，包含两端边界；未来消息排除。默认 `limit=10`，最大 20；`offset` 默认 0，最大 1000。

达到最大分页偏移后，结果仍可标记 `has_more=true`，但 `next_offset=null`、`meta.pagination_limited=true`；此时应缩小时间范围或增加关键词，而不是继续增加 offset。两个分页工具都使用此约定。

用户消息采用 OneBot 时间，无效时回退到落盘时间；机器人回复采用确认发送成功的时间。当前触发消息不会命中自身。结果保留身份、时间、消息号及原文片段，单条正文最多 1200 字符，搜索片段围绕首个关键词。`text_offset` / `text_length` / `text_truncated` 说明正文是否只是片段。

`get_chat_context`：`record_id` 如 `L42`，表示当前群 JSONL 的第 42 行。默认前后各 3 条，分别最多 10 条，只计算可读取的聊天，跳过评分、失败发送等记录。截断时保留锚点消息，裁去周围记录；消息顺序仍为存档顺序，可能与原始发送时间不同。不能通过此接口读任意文件。记录编号在只追加的存档中稳定，重写或手工调整行次后会失效。

`search_event_logs`：只返回 `willingness`、`delivery`、`schedule`、`tool` 的白名单字段；默认 20 条，最多 50 条，按落盘顺序排列。包含状态、阶段、理由、分项、调用号等，不返回未发送的回复正文、工具原始结果、认证信息或全局 `atri.log`。这些记录用于回答“某条消息为何没回”等过程问题，不能当作群友说过的话。此工具对当前允许群内所有发言者开放，不区分管理员。

示例调用参数：

```json
{"query":"冰淇淋","user_id":"123456","since":"2026-09-01T00:00:00+08:00","limit":5}
```

## 通用契约

`tools.py` 不依赖 QQ 传输或模型供应商，分为五个对象：

| 对象 | 职责 |
| --- | --- |
| `ToolSpec` | 工具名称、模型可读说明、JSON Schema、异步 handler |
| `ToolContext` | 应用注入的群号、发言人、机器人号、消息 key、当前时间，以及受限存档读取、存活检查和审计能力 |
| `ToolResult` | 统一的结果或错误，负责返回合法且有长度上限的 JSON |
| `ToolRegistry` | 注册、导出模型工具定义、参数校验、超时和异常转换 |
| `ToolSession` | 一条回复的调用预算、完全相同调用的缓存、运行日志和持久化审计 |

handler 签名为 `async def handler(context: ToolContext, arguments: dict) -> ToolResult`。模型仅决定工具名和参数；群号、文件位置、审计入口等由 Bot 注入，不能用参数覆盖。`archive` 遵循 `ArchiveReader` 协议，后续可替换成数据库查询实现，保持模型工具接口不变。

每个工具参数 schema 根节点必须为 `object`，并设置 `additionalProperties=false`；注册时校验 schema，执行前使用 `jsonschema` 校验类型、必填字段和范围。未注册工具、未知字段、无效 JSON 均不进入 handler。不要把任意代码执行、文件路径或群号选择作为检索参数。

所有工具结果使用相同外壳：

```json
{
  "version": 1,
  "ok": true,
  "data": {
    "items": [{"record_id":"L42","message_id":"1001","role":"user","user_id":"123456","time":"2026-09-10T18:20:00+08:00","text":"我喜欢香草冰淇淋"}],
    "matched_total": 1,
    "offset": 0,
    "has_more": false,
    "next_offset": null,
    "order": "newest_first"
  },
  "error": null,
  "meta": {"source":"messages.jsonl","partial":false,"truncated":false,"scanned_lines":200,"skipped_lines":0}
}
```

`version=1` 是工具结果契约版本。成功时 `data` 是对象，失败时 `ok=false`、`data=null`、`error={"code":"...","message":"..."}`；handler 可抛 `ToolError` 表示已知、可向模型解释的失败，其他异常只返回通用错误，避免泄露内部路径。取消和睡眠拦截直接终止当前处理，不伪装成工具成功。

列表数据统一放入 `data.items`，可用 `data.anchor` 指定上下文查询应保留的记录编号。结果超过字符上限时按整条裁去，标记 `meta.truncated`；分页接口同步调整 `next_offset`。单条也容纳不下或非列表结果过长时返回 `result_too_large`，不会硬截 JSON 字符串。

常见错误码：`unknown_tool`、`invalid_arguments`、`empty_query`、`invalid_time`、`invalid_time_range`、`record_not_found`、`tool_timeout`、`tool_failed`、`call_limit`、`result_too_large`。成功但 `items=[]` 表示本次查询未命中；错误或 `partial=true` 不等于没有记录。

## 图片理解工具

`vision.enabled=true` 时另外注册 `inspect_image(image_id, question?)`，需要 `tools.enabled=true`。`image_id` 从消息的 `images` 字段读取，如 `img_12345_1` 表示消息 12345 的第一张图片；`question` 可指定关注点，最多 500 字符。

主进程为本轮创建 `ImageAccess` 并放入 `ToolContext.images`，只包含当前群、当前消息及一小时窗口内的图片。工具只能选择编号，没有群号、路径或下载链接参数。历史窗口外的图片即使在旧聊天检索中出现，也不能直接通过图片工具读取。

工具通过相同模型客户端单独请求视觉模型，返回 `data.image_id`、`data.observation`、原始/处理尺寸、是否缩放、是否只读首帧；随后聊天模型结合观察生成正文。图片处理与群权限由主进程提供，无 MCP 进程。

通用 `ToolSpec` 新增可选 `timeout` 字段，必须在 (0,120] 秒内，未指定时使用 `tools.timeout`。这是应用注册工具时的设置，模型不能修改。看图工具使用 `vision.timeout`（默认 45 秒）涵盖下载、解码和视觉模型请求；其他工具仍使用默认 5 秒。模型请求仍受 `llm.timeout` 限制，以先到达的超时为准。

视觉调用继承当前消息的睡眠检查和日志上下文；进程关闭会取消异步请求，已经在线程中开始的本地图片解码可能短暂继续，但不会再提交模型或发送回复。`ImageReader.inspect(image_id, question) -> dict` 是可替换的内部接口，将来切换视觉服务时可保留工具契约。

错误码包括 `image_not_available`、`image_url_unavailable`、`image_source_not_allowed`、`image_download_failed`、`image_download_timeout`、`image_too_large`、`invalid_image`、`unsupported_image`、`vision_model_failed`；工具总超时仍返回 `tool_timeout`。模型失败信息不包含 API 响应体或凭据。配置、格式限制和测试入口见 [README 的图片理解说明](../README.md#图片理解)。

## 链接阅读与 MCP

`links.enabled=true` 时注册 `read_link(url, question?)` 和 `read_document(document_id, question? | chunk_ids? | cursor?)`，通过统一的 `MCPManager.call` 连接配置中的只读服务。MCP 工具不直接全部暴露给模型：连接层检查服务和工具白名单，链接适配层负责 URL 平台识别、字幕来源与错误分类、按最终 JSON 大小分页和群隔离存储。文档整理启用时，长文先返回中立概览和目录，具体问题再返回原文块；短文直接返回正文。完整契约及部署见 [链接阅读与 MCP 接入](link-tools.md)。

来源读取默认限时 45 秒，云端转写启用时另限 300 秒（含音轨下载），文档整理/定位合计另限 60 秒；工具总限时为启用阶段之和，默认 405 秒，超时直接失败，不自动重试，仍共享查询次数/轮数预算。实际概览/定位模型请求单独占用模型并发槽位，网络和工具等待不占用。原文位于 `data.text` 或 `data.passages`，概览位于 `data.overview`。`has_more/next_cursor` 仅描述当前 `view` 的分页；`meta.overview_complete` 仅指概览覆盖已获取文本，`meta.partial/truncated` 描述来源不完整或存储截断。结果不要求包含 `data.items`。

## 添加工具

无需修改模型的 HTTP 代码或工具循环。实现 handler 和 schema，在 Bot 的 `tool_registry` 初始化后注册即可，例如在模块中写：

```python
from atri_bot.tools import ToolSpec, ToolResult

async def get_example(context, arguments):
    return ToolResult(True, data={"value": "示例值"})

def register_example(registry):
    registry.register(ToolSpec(
        name="get_example",
        description="读取本地示例值。",
        parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        handler=get_example,
    ))
```

再在 `Bot.__init__` 中调用 `register_example(self.tool_registry)`。这是代码注册接口，没有动态加载目录、热重载或远程插件安装。当前内置工具均只读；将来添加写操作时应为它单独定义访问权限和行为约束，不能把模型提供的参数当作授权。

其他调用入口也可复用 `registry.execute(name, json_arguments, context, config)`；需要调用次数限制、缓存和审计时使用 `ToolSession.execute(call)`。工具执行函数应保持异步，阻塞的文件或数据库读取应卸载到线程并支持取消。

## 预算、存储和日志

配置在 `[tools]`：默认启用，`max_rounds=2`、`max_calls=4`、`timeout=5`、`max_result_chars=8000`。允许范围分别为 1–5 轮、1–16 次、(0,30] 秒、1000–32000 字符。配置修改需要重启。

一轮可含多个工具调用，顺序执行；所有尝试（含参数错误、未知工具和缓存命中）计入次数。相同名称和完全相同参数字符串在一条回复内复用结果，无自动技术重试。超过次数的批次剩余调用会收到 `call_limit`，仍为每个合法调用 ID 补齐一条结果。达到轮数或次数上限后，下一次模型请求使用 `tool_choice=none` 生成正文。服务端仍返回工具调用时以 `tool_round_limit` 失败，不会继续循环或发送工具前言。

每次执行前后以及模型 HTTP 前检查睡眠。跨午夜不再调用下一工具、追加模型请求或发送回复。已在执行的文件扫描是只读操作；夜间检查失败后结果不传回模型。

JSONL 不做迁移或重复存储。检索使用 `rb` 打开，读取开始时固定文件大小，避免追着不断增长的文件扫描；不调用会修复文件的恢复读取器。坏行、未完成末行及超过 1 MiB 的单行跳过，返回 `partial=true` 和计数，保持磁盘字节不变。扫描在线程中运行，超时或取消时通过事件通知线程停止。

查询目前是 O(文件字节数) 扫描，排序只保留本页所需的候选。较大存档可能在 5 秒内查不完；此时可以后续把 `ArchiveReader` 换为 SQLite 索引，工具名、参数和返回结构可继续复用。`record_id`、分页和查询结果不承诺在人工改写存档后保持稳定。

工具调用审计写在本群 JSONL 的 `kind="tool"` 中，只存调用号、工具名、状态、条数、错误码、耗时和缓存/截断标记，不重复存工具结果或原始参数。它不进入聊天上下文。`atri.tools` 使用橙色，继承 `g=群号 m=消息号 u=用户号`；可设置 `[logging.modules] tools="DEBUG"` 查看参数字段校验和执行过程。

工具回复长度受限，但原有一小时聊天上下文仍没有总 token 预算；本功能没有消除高消息量下的 context window 风险。长消息以片段返回、上下文也可能截断，模型必须承认证据的范围，不把旧记录当成当前活动。

## 验证

运行 `uv run python -m unittest discover -s tests -v`。新增测试使用临时 JSONL 和本地模拟 HTTP，覆盖旧消息检索、原始时间/乱序、群隔离、未发送正文排除、关键词/分页、上下文、处理记录投影、坏行只读、参数校验、结果上限、复用注册、超时取消、调用预算、工具协议往返及午夜中断。

`atri test-api` 仍是原有四项连接/意愿/人设测试，不会执行存档工具，也不能据此证明供应商的工具调用兼容性。`atri test-vision` 单独测试真实图片模型接口；真实群聊中的工具选择和回答质量仍需要启动后观察。自动测试不连接外部模型或真实 QQ。
