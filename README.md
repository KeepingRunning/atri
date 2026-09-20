# ATRI

ATRI QQ 群聊机器人：记录允许群的聊天消息，每天上海时间 00:00–08:00 睡觉、不回复。新配置默认按群收集一批消息，由 Planner 理解互动并决定回复、查询、等待或旁听；Replyer 再结合人设、当前日常和近一小时的记录写正文。没有被 @ 时也可参与。

## 项目架构

```text
QQ / OneBot → 校验、记录、睡眠拦截 → 按群收集 → 固定聊天快照
                                                    ↓
                           Planner ← 查询历史 / 看图 / 阅读链接的实际结果
                             ↓ 回复       ↓ 等待补充 / 旁听
                           Replyer
                             ↓
                    过时与睡眠检查 → QQ 回执 → 记录已发送正文
```

- `onebot.py`：OneBot v11 反向 WebSocket 接入与发送回执。
- `bot.py` / `group_session.py`：消息过滤、按群合批、等待唤醒、快照过时检查与统一发送。
- `planner.py`：行动定义、参数和来源校验、两次失败重试、规划与查询循环。
- `willingness.py`：保留旧模式的接话评分、续聊和退避，供对比使用。
- `context.py` / `storage.py`：构造 conversation，按群保存聊天记录；仅确认发送成功的回复进入历史。
- `model.py`：调用 OpenAI 兼容的 Chat Completions 接口。
- `tools.py` / `history_tools.py`：通用工具定义、参数校验和执行；查询本群聊天存档与消息处理记录。
- `vision.py`：按需下载和理解群聊图片，作为 `inspect_image` 工具使用。
- `mcp_client.py` / `link_tools.py`：管理外部 MCP 服务，按需读取分享链接、提供概览和原文读取工具。
- `documents.py` / `document_analysis.py`：按群保存原文及块位置，生成中立概览，再按问题定位原文证据。
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

模板及当前配置使用 `reply.mode = "planner"`，详细流程、配置和边界见 [Planner 与 Replyer](docs/planner.md)。默认安静两秒后合批、持续收集最多八秒；普通聊天经过时间、频率和冷却等技术检查便可交给模型，不再用关键词分数筛掉主题。通常一次规划、一次正文生成；查询、等待或重规划会增加请求。

`reply.frequency` 在 Planner 中是参与偏好，0 仍只允许真正 @ 或引用已发送消息；`cooldown_seconds` 控制普通群聊发言后的冷却，`judgment_model` 可指定规划模型。`threshold`、`continuation_seconds` 只影响旧模式。Planner 参数不合法或调用失败时最多重试两次，仍失败就记录本批失败，不发送规划文本。

可改回 `reply.mode = "willingness"` 比较旧流程，或用 `at_only` 保持仅真正 @ 回复。为兼容旧配置，缺少 `[reply]` 时仍为 willingness。修改配置后重启服务生效。以下评分与规则降级说明仅适用于旧 willingness 模式：

`reply.frequency` 越大，越容易评估普通群聊；设为 0 时只评估真正 @ 或引用本机器人已确认发送消息的内容。`reply.threshold` 是判断模型的意愿阈值，默认 60。判断一次成功、决定接话且无需检索时，共调用模型两次；检索后会追加模型请求。可用 `reply.judgment_model` 指定同一接口上的快速判断模型。

判断提示词明确要求仅返回 `score`、`reason` 的 JSON；人设及历史作为独立数据传入。判断失败会再重试两次（共最多三次），重试时重申输出协议。仍失败则用本条消息的规则分对比 `reply.threshold`：默认 ≥60 生成回复，低于 60 等待。例如规则分 70 会回复，45 会等待。单次技术失败不累计等待次数，只按最终决策更新一次状态。降级只决定是否生成回复，聊天正文仍单独调用模型生成。

每次规则分数、分项和模型判断记录在 `data/groups/<群号>/messages.jsonl` 的 `kind="willingness"` 行中，不会进入聊天上下文。详情、算法参数和 MaiBot 参考位置见 [回复意愿设计](docs/reply-willingness.md)。

连接状态可通过 `http://127.0.0.1:28080/healthz` 查看。

允许的群内发送纯文本 `/health` 可检查服务：返回运行时间、当前群排队数、异常群处理任务数，并明确模型 API / MCP 未主动探测。命令不调用模型，使用独立通道，不等待群聊生成，也不受睡眠时段限制；它不能证明外部模型或解析平台可用。

命令及应答仅保存为 `command` / `command_delivery` 运维记录，不进入聊天上下文、聊天检索或发言冷却；重复事件不会重复应答。`/healthz` 同时提供本地状态、运行时间、总排队数与连接状态，群处理任务异常时返回 HTTP 503。

## 自动复读

同一群中，两名不同群友连续发送相同的非空纯文本时，ATRI 自动跟读一次，不调用模型；同一人连续发送不会触发。同一轮后续相同消息也不交给 Planner 或旧回复模型，出现不同正文或非纯文本消息后重新计数。比较的是去除首尾空白后的完整正文，带图片、@ 或引用的消息不参与。

复读在每群串行队列内发送，遵守睡眠拦截，不受普通闲聊的参与频率或冷却影响；发送成功后才进入聊天历史。发送失败或回执不明不会在这一轮再次尝试。状态保存在当前进程内，重启后重新计数。

## 输出标点替换

所有出站正文在统一发送入口转换一次：`。` → `)`，`，` / `,` → `，，，`；英文句点 `.` 保持原样。例如 `好，知道了。OK, thanks.` 发为 `好，，，知道了)OK，，， thanks.`。普通回复、自动复读和命令应答共用此规则，输入消息、人设与工具资料不修改；发送审计和已确认的聊天历史保存实际发出的文字。每个原始逗号只扩展一次，不会把中文逗号重复扩展为九个逗号。

## 聊天历史时间窗口

按实际构造上下文时的时间筛选近一小时历史，包含恰好一小时前的消息；不再取最近 50 条，也不会因消息少而补入更早记录。Planner 快照还受 `planner.max_snapshot_chars`（默认 32000 字符）约束：优先舍弃较旧历史，单条长消息会标注截断；本批消息单列为 `pending`，不重复出现在 `history`。Planner 与 Replyer 使用同一份固定快照。

```toml
[context]
history_seconds = 3600
```

群友消息优先使用 OneBot 的 `timestamp`，缺失、无效或为 0 时使用本地落盘的 `time`；机器人回复使用确认发送成功的 `time`。没有可信时间或时间晚于当前时刻的历史不进入提示词。保留原有记录顺序，并继续按群隔离。

内存历史按时间清理，重启也只恢复窗口内的正文。完整 JSONL、消息去重记录和已发送消息 ID 仍保留；窗口之外的旧记录不自动进入上下文，模型需要时可通过工具检索。排队等待期间跨过一小时边界的历史，会在实际构造上下文时排除。旧 `context.recent_messages` 已停用、会被忽略；已有配置已改为 `history_seconds`，修改配置或升级代码后重启服务生效。

DEBUG 日志的 `[选择历史]`、`[选择意愿历史]` 显示时间窗口、截止时间、过滤数和实际选取数。

## 按需检索聊天存档

聊天继续保存为 `data/groups/<群号>/messages.jsonl`。Planner 在决定回复前可调用三个工具（旧模式在回复阶段调用）：`search_chat_history` 按关键词、QQ 号、时间检索旧消息；`get_chat_context` 展开某条命中的前后文；`search_event_logs` 按消息号查看意愿判断、睡眠和发送状态。检索范围不受一小时限制，只能读取当前群、当前机器人账号的记录。未确认发送的回复正文不会被当成聊天检索出来。

```toml
[tools]
enabled = true
max_rounds = 2
max_calls = 4
timeout = 5
max_result_chars = 8000
```

Planner 每步最多执行一个工具，默认最多两轮查询，随后必须选择最终行动；查询预算在等待和重规划期间保持。旧模式最多两轮查询、累计四次执行，再生成最终正文，意愿评分不提供工具。普通检索工具默认超时为 5 秒，图片工具 45 秒；链接抓取与文档处理分阶段计时，见下文。返回 JSON 最多 8000 字符，截断会明确标记；这不是整个 conversation 的 token 上限。参数错误、未注册工具、超时等以统一 JSON 错误返回，模型可在预算内调整查询。达到查询上限时，Planner 只保留行动工具，继续查询视为协议错误；旧回复路径强制 `tool_choice=none`。不发送中间文本。

接口需要支持 Chat Completions 的 `tools`、`tool_calls`、`tool` 消息和 `tool_choice`；Planner 的行动选择也需要原生工具调用；`tools.enabled=false` 只关闭查询，不能绕过这一接口要求。不支持工具调用的接口需使用旧模式并关闭 tools。工具执行前后及每次追加模型请求前仍检查睡眠。工具中间消息仅存在于本次生成，运行审计作为 `kind="tool"` 落盘，只有最终确认发出的正文进入聊天历史。`tools` 日志为橙色，包含工具名、调用号、校验结果、返回条数、耗时和错误码。

目前使用本地字面关键词检索，没有语义向量匹配；JSONL 以只读流式方式扫描，不阻塞事件循环，超时会通知后台扫描停止。可先问“我之前说喜欢吃什么？”，观察 `[工具调用开始]` / `[工具调用结束]`。接口契约、返回格式、扩展实例及限制见 [历史检索与工具接口](docs/chat-tools.md)。

## 分享链接与 MCP

启用 `[links]` 和 `[mcp]` 后，Planner 可调用 `read_link(url, question?)` 读取公众号、知乎具体文章/回答和 B站视频。默认短文直接给原文，长文先提供概览和目录；`read_document` 可按问题、最多三个块编号或续读游标展开原文。纯文本和 QQ JSON/XML 分享卡片中的 URL 会进入消息的 `links` 字段；收到链接不会自动抓取。资料只作为工具观察传入 Planner / Replyer，不写成聊天历史。

本机代理使用 Fake-IP 时，B站短链接需要开启 `[links].dns_over_https=true`，否则可能在读取字幕前被公网地址校验拦截。本地配置已开启；它与音轨、MCP 的 DNS 开关独立，默认仍不重试或切换解析方式。

`[documents]` 默认启用：按标题、段落和句子切成约 1200 字符的块，保留原文、位置及可对齐的字幕时间戳；超过 4000 字符时生成中立概览，并给每个目录项和主要观点标注原文块。输入超过预算会分批处理再合并，目录必须覆盖全部已取得的块。模型不携带人设或群聊记录；是否接话、如何表达仍交给 Planner / Replyer。概览和原文保存到按机器人账号、群号隔离的 `data/documents/`，默认 24 小时有效，重启可恢复。按问题定位无命中不能证明原文不存在相关内容。

视频原文、时间戳和中立概览另存于 `data/video-sources/`，按机器人＋BV号＋分P保留 24 小时。去掉分享参数后的同一视频、同机器人其他群、以及重启后的请求均可复用；并发请求合并获取，同期不重复下载音频或提交转写。不同群的聊天、文档编号和回复仍独立；新问题可以再次定位原文。缓存满时拒绝新来源，保留未过期的视频。

一次工具结果默认最多 8000 字符，概览、目录和原文都计入；`has_more` 表示当前读取视图还有后续页。取得全文、概览覆盖全文、本轮实际展开的原文是不同的范围，不能因为有完整概览就声称逐段看过原文。长文概览和问题定位会增加真实模型请求，受独立输入、输出、调用次数和总超时预算约束，失败不重试。

接入固定版本的 `website2markdown` 和 `bilibili-mcp`，由本地 stdio 子进程提供服务，需要 Node.js 20+ 和 `npx`。前者默认使用在线解析后端；后者读取信息及字幕。启用 `[asr]` 后，字幕不存在或字幕登录凭据失效时，主进程获取公开 AAC 音轨并调用百炼；网络超时、付费或访问拒绝直接失败。无需本地语音模型，音轨用完删除；字幕或转写都不表示看过视频画面。

配置模板默认关闭；本地配置已添加并启用这两项，重启 ATRI 后生效。MCP 首次使用才启动，调用失败不会阻止其他聊天。链接取源默认最多 45 秒，单次 MCP 请求最多 30 秒；取得文字后的概览和问题定位共用 60 秒文档处理限时。启用自动转写时，音轨获取、上传与转写另共用 `asr.timeout`（默认 300 秒），其中下载还受 60 秒上限约束。`read_link` 总限时是三个阶段之和，默认 405 秒；未启用 ASR 时为 105 秒。`read_document` 默认 60 秒。分页仍受 `tools.max_result_chars` 和共享查询预算限制。等待网页或字幕期间不占模型并发名额。

```sh
# 替换为真实分享链接；长文会调用概览模型，不连接 QQ、不写真实群记录。
uv run atri test-links --url 'https://mp.weixin.qq.com/s/文章ID'
# 完整B站流程；重复运行命中诊断缓存，不再次转写。
uv run atri test-links --url 'https://www.bilibili.com/video/BV1dVRdBpEze/'
# 使用已有转写检查概览和证据定位，不重新下载音频或转写。
uv run atri test-document --document data/diagnostics/BV1dVRdBpEze/transcript.json \
  --url 'https://www.bilibili.com/video/BV1dVRdBpEze/' --question '后半段有哪些补充条件？'
```

文档诊断结果在 `data/diagnostics/document-reading/<document_id>/`，`preview.md` 便于阅读，`result.json` 对应实际工具结果。配置、登录、返回字段、错误分类与缓存限制见 [链接阅读与 MCP 接入](docs/link-tools.md)。`atri check` 只校验配置；外部内容能否读取需用具体链接实测。

百炼云端语音转写可先独立测试：在 `config.toml` 的 `[asr]` 填写百炼 `api_key`，确认 `base_url` 与 Key 的地域一致，运行 `uv run atri test-asr`。默认北京地域、`fun-asr`，使用官方公开短音频，无需本地语音模型或自建 OSS。测试会产生真实 API 调用，依次验证提交、查询和转写正文下载；总超时默认 300 秒，失败不重试。`asr.enabled=true` 将同一套云端 API 接入 B站阅读，本地配置已启用；模板默认关闭且密钥留空。聊天模型仍使用 `[llm]`。具体步骤见 [百炼语音测试](docs/link-tools.md#百炼云端语音转写测试)。

## 图片理解

启用 `[vision]` 后，Planner（旧模式为回复模型）可以调用 `inspect_image` 查看当前群近一小时消息中的图片，识别截图文字、描述照片或理解表情包。每次调用处理一张，图片编号由程序提供，不能通过工具参数指定 URL、文件路径或其他群。收到图片不会自动调用视觉模型：Planner 在技术门控后判断是否有交流价值，再决定是否看图；单独发图也可以选择旁听。

```toml
[vision]
enabled = true
# 沿用 [llm] 的 API 地址、密钥和 thinking；空字符串沿用聊天模型。
model = "deepseek-flash"
timeout = 45
download_timeout = 10
max_image_bytes = 8388608
max_pixels = 20000000
max_output_tokens = 768
allowed_hosts = ["multimedia.nt.qq.com.cn", "gchat.qpic.cn", "c2cpicdw.qpic.cn"]
```

模板默认关闭，启用需要 `tools.enabled=true` 和支持图片输入的模型。本地 `config.toml` 已启用，并为 DeepSeek 官方接口配置 `deepseek-flash` 作为看图模型。模型能力与图片输入格式见 [DeepSeek 官方图像理解文档](https://api-docs.deepseek.com/guides/vision/)。配置修改后需重启 ATRI。

流程是：消息携带图片编号 → 模型选择看图工具 → 从配置允许的图片主机下载 → 本地检查、缩放 → 单独请求视觉模型 → 将观察结果交给聊天模型 → 按人设生成最终回复。意愿判断只接收图片编号和文字，不接收图片内容。图片文字是待分析数据，不能覆盖人设或系统规则。

支持 JPEG、PNG、GIF、WebP；默认限制原文件 8 MiB、2000 万像素。图片去除元数据、透明背景合成为白色、长边最多 2048 像素后以 JPEG 发送；动图仅查看首帧，小字或缩放后的细节可能识别不清。工具总超时默认 45 秒，覆盖普通 `tools.timeout`，仍占用原有调用次数和轮数预算。下载、视觉请求和后续生成均遵守睡眠拦截。

图片链接来自 OneBot 图片段的 `url` 或可下载的 `file`，不会读取任意本地路径，不跟随重定向。链接过期、主机不在 `allowed_hosts`、只有文件哈希而没有下载 URL、文件过大或识别失败时，会返回明确工具错误；当前版本没有额外调用 OneBot `get_image` 刷新链接。

原始消息段仍随聊天记录落盘；下载的图片和处理后的 Base64 只保存在本轮内存中，不额外保存图片文件。同一轮反复查看同图可复用下载结果；下一轮需要时重新下载。工具观察结果仅用于本轮，最终已发送的回复才进入聊天历史。`vision` 日志使用独立紫红色，记录图片编号、下载主机、HTTP 状态、大小、处理尺寸、耗时和识别结果预览；不打印带签名的下载 URL 或 Base64，`preview_chars=0` 可隐藏观察正文。

独立测试不启动机器人、不读取群历史、不发送 QQ 消息，但会调用一次真实模型 API：

```sh
# 自动生成随机双色图片，检查模型是否正确识别左右颜色。
uv run atri test-vision
# 上传自己明确选择的一张本地图片，显示识别结果供人工核对。
uv run atri test-vision --image /path/to/example.png
```

`test-vision` 可在 `vision.enabled=false` 时手动执行；合成图识别正确或指定图片返回有效结果时退出 0，模型/图片失败或合成图答案不符时退出 1，参数、配置或本地文件错误时退出 2，中断为 130。原有 `test-api` 四项测试保持不变。

重启后可在群里同时发送图片和“@亚托莉 帮我看看这张图”，再追问图里的文字或内容。该版本侧重看图和识字，尚未实现图片编辑、生成、视频理解或完整动图分析。

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

Planner 在取得模型槽位、建立快照时读取两小时大日程和当前十分钟小日程，规划与正文使用同一份背景；旧模式在回复 system 提示词中注入。其他十一格、出处和审核记录不传入。日程不主动发消息、不增加意愿分、不单独写入群聊历史。

**默认开启 `schedule.sleep_enabled`，00:00（含）至 08:00（不含）由代码强制禁止回复，包括 @、引用、名字呼叫。** 夜间新消息在入口记录、去重后即结束处理，不排入回复队列、不发给 LLM。聊天的每次模型 HTTP 提交前都会重新检查睡眠，意愿判断跨午夜失败也不再重试或降级。午夜前已经排队或提交给模型的请求若跨午夜，后续处理会丢弃，08:00 后不补发。已经在午夜前提交给 OneBot 的消息，其网络送达或回执可能在午夜后发生。

```toml
[schedule]
enabled = true
sleep_enabled = true
timezone = "Asia/Shanghai"
routines_dir = "resources/daily_routines"
```

没有 `[schedule]` 时也默认启用。`enabled = false` 只关闭清醒时的日程背景，夜间禁回复仍生效。路径相对于配置文件目录，也可用绝对路径。旧的 `schedule.model` 会被忽略，可删除。配置修改后重启服务生效。

临时夜间测试：设置 `schedule.sleep_enabled = false` 后重启，入口、模型请求和 OneBot 发送均不再因睡眠拦截；凌晨日程改用“夜晚/不限”素材，没有候选时自由休息。要恢复作息，改回 `true` 后重启。`enabled` 与此开关相互独立；关闭睡眠不会补发已结束处理的历史消息。

`data/schedule/state.json` 只保存当前窗口和选中的素材 ID 等恢复信息；旧生成式缓存不再使用。删改素材会在下一窗口或重启后重新读取。坏文件会跳过并记录日志；没有合适候选时用“自由休息”，不会临时调用模型补写。后台每 30 秒检查窗口，收到消息时也按实际时钟刷新，不需要等待后台轮询。

本地预览无需 API 密钥、不连接 QQ、不读取群历史、不改正式日程缓存：

```sh
uv run atri test-schedule --at 2026-09-11T18:35:00+08:00
uv run atri test-schedule --at 2026-09-12T00:00:00+08:00
```

预览显示选中日常的十二格及当前提示词背景；省略 `--at` 使用当前上海时间，重复运行预览会独立抽选。详细实现与验证范围见 [日程设计](docs/daily-routines.md)。

## 查看处理日志

默认 `DEBUG` 详细日志，终端按模块配色：接收为青色、队列为蓝色、Planner / willingness 为粉紫色、Replyer 为浅绿色、上下文为紫色、模型为金色、发送为绿色、存储为灰青色。警告和错误额外使用橙色和红色。每条消息的处理日志带 `g=群号 m=消息号 u=用户号`，并发群聊和异步发送回执也能对应到原消息。

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

新流程真实模型试用：`uv run atri test-planner`，用临时群记录和模拟发送器运行四组样本，显示行动与正文，不连接 QQ；详见 [新流程说明](docs/planner.md)。

本地自动测试：

```sh
uv run python -m unittest discover -s tests -v
```

日程测试覆盖时段筛选、十分钟及两小时边界、重启恢复、坏文件降级、午夜/08:00、跨午夜排队与生成、发送前拦截和本地预览无 API 调用。

图片测试使用合成图片和本地模拟 HTTP，覆盖下载限制、格式/尺寸、透明图和动图首帧、群与时间范围、意愿等待不下载、工具往返、下载/模型失败以及跨午夜停止后续调用。

自动测试使用模拟群消息和本地模拟模型接口，不连接真实 QQ 或外部模型；不会自动执行上述真实 API 请求。

## 许可证

[MIT License](LICENSE)。
