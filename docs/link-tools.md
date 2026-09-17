# 链接阅读与 MCP 接入

ATRI 按需读取群友分享的链接，把取得的原文、全文概览和本轮相关段落作为工具观察交给 Planner 和 Replyer。收到链接不会自动抓取，也不会把网页内容写成群友发言或聊天历史。文档整理不携带人设；是否接话、怎样表达感受，仍由 Planner 和 Replyer 决定。

## 服务与职责

| 服务 | 固定版本 | 当前调用 |
| --- | --- | --- |
| [website2markdown](https://github.com/Digidai/website2markdown) | `@digidai/mcp-website2markdown@0.1.0` | `convert_url`，返回公众号、知乎具体文章或回答的 Markdown |
| [bilibili-mcp](https://github.com/XZXZZX-Ai/bilibili-mcp) | `@xzxzzx/bilibili-mcp@1.14.1` | `get_video_metadata` 和 `get_video_transcript`，读取所选分 P 的信息与字幕 |

两项均为 Apache-2.0 项目，通过 npm 包调用，不复制源码。Python 主进程用官方 `mcp` SDK 连接本地 stdio 子进程。主进程保留权限、调用预算、文档缓存和上下文整理；MCP 服务负责平台请求。后续其他 MCP 可以复用连接管理，再通过相应适配器注册到 `ToolRegistry`。

`website2markdown` 的 MCP 默认请求 `https://md.genedai.me` 在线后端：目标文章 URL 会发送给该服务，正文抓取不全在本机完成。可通过 `WEBSITE2MARKDOWN_API_URL` 换成自行部署的后端。上游是否需要 Token、浏览器渲染以及平台能否访问，取决于后端配置和站点状态；支持某个平台不等于任意链接必然可读。

## 模型接口

| 工具 | 参数 | 返回 |
| --- | --- | --- |
| `read_link` | `url`、可选 `question` | 短文原文；长文概览和目录；有问题时还定位相关原文块 |
| `read_document` | `document_id`，以及可选的 `question`、`chunk_ids` 或 `cursor` | 按问题定位、按编号展开最多三个原文块，或继续当前视图分页 |

`question`、`chunk_ids`、`cursor` 是互斥的读取方式，不混用。问题为 1～2000 字符的文本；块编号由工具给出，不能指定文件路径或其他群。`read_document` 没有指定读取方式时，从原文开头读起。`next_cursor` 由程序签发，绑定文档版本和读取视图。

链接支持 HTTPS 公众号 `/s/文章ID` 或带 `__biz/mid/idx/sn` 的 `/s`；知乎 `/p/文章ID`、`/question/问题ID/answer/回答ID`、`/answer/回答ID`；B站 `/video/BV...` 和 `b23.tv` 分享短链。不读取知乎整道问题的全部回答、专栏列表、B站动态/直播/收藏夹。域名按完整主机名校验，不接受用户信息、显式端口或内网地址。

消息中的纯文本 URL、OneBot JSON 小程序卡片和 XML 分享卡片会提取到 `links` 字段。先提取再裁剪正文，每条最多三个 URL；极端情况下快照预算仍可裁去链接，并标记 `links_truncated`。该步骤不发网络请求。B站短链独立解析，只允许有限次数的 b23.tv → B站视频跳转，不携带 Cookie，并检查实际连接的 DNS 地址。

如果系统 DNS 将 `b23.tv` 解析为代理 Fake-IP（例如 `198.18.x.x`），默认解析会返回 `unsafe_redirect`，此时尚未请求 B站元信息、字幕或百炼。可显式设置 `[links].dns_over_https=true`，通过固定的 Google DoH 取得并校验公网地址；本机配置已开启，模板默认关闭。该开关只负责短链接，不依赖 ASR 是否启用，也不会因失败自动切换解析方式；域名、跳转目标、公网地址和 TLS 校验仍保留。`links` 日志会记录短链阶段、DNS 模式和错误码，方便区分解析失败与后续阅读失败。

调用经过现有 `ToolSession`：参数校验 → 超时/调用预算 → MCP 请求 → 格式规范化 → 长度限制 → `role=tool` 与 `tool:<调用号>` 观察。Planner 和 Replyer 共享同一结果。工具 description/schema 仍走模型 API 的 `tools` 字段，阅读范围与外部资料规则也加入系统提示。

结果沿用 `ToolResult` 外壳。以下是原文分页的字段示例；概览和按块读取会另外提供目录、原文块及对应覆盖信息：

```json
{
  "version": 1,
  "ok": true,
  "data": {
    "document_id": "doc_…",
    "source_url": "https://…",
    "title": "文章标题",
    "text": "本次实际返回的正文片段",
    "range": {"start": 0, "end": 6000, "total_cached_chars": 12000},
    "read_sections": ["article_text"],
    "has_more": true,
    "next_cursor": "由程序生成的续读游标"
  },
  "error": null,
  "meta": {
    "source": "website2markdown",
    "data_source": "article_text",
    "untrusted": true,
    "visuals_read": false,
    "partial": false,
    "truncated": false,
    "warning": null,
    "cached": false,
    "completeness": "as_returned_by_parser"
  }
}
```

`has_more` 只表示**当前视图**还有后续内容，例如尚未返回的目录项或本次所选块的剩余正文；它不代表模型已经看完缓存中的其他原文。`partial` 表示取得的来源内容不完整，例如视频没有字幕，或达到文档存储上限。`completeness` 只承诺后端实际返回的内容，不保证后端已经取得网站完整正文。分页计算最终 JSON 的字符长度，包括转义和元数据；游标指向实际交付的位置，不会因二次裁剪跳过正文。

视频读取保留 `?p=`，缓存未命中时，一次模型工具调用内部执行信息与字幕两次 MCP 请求。字幕保留时间戳，区分人工 `subtitle` 与平台 AI `ai_subtitle`；后者可能有识别错误。`read_sections` / `data_source` 仅描述当前页实际交付的内容：第一页如果只有较长简介，不能声称已经读到后面的字幕。

`read_link` 优先读取字幕。`asr.enabled=true` 时，`SUBTITLE_UNAVAILABLE` 或 `COOKIE_EXPIRED` 会尝试公开音轨，再上传百炼转写；后者标记为“字幕凭据失效”，不声称已确认无字幕。网络超时、限流、访问拒绝、付费或试听限制不进入该分支。转写失败直接返回失败，不能把简介冒充转写成功。关闭云 ASR 时维持原规则：只有明确无字幕可退回简介并标记部分内容。MCP 的本地 ASR 始终关闭。转写来自音频，不代表理解了画面；文章图片也未做 OCR。外部正文中的指令仍是资料。

## 原文、概览与证据块

取得的文字按原有标题、段落和句子边界切分，每块默认约 1200 字符；极长、找不到自然边界的片段才按长度拆分，并标记 `hard_split`。块编号可回查原文，位置保留字符起止范围；字幕或 ASR 句段能可靠对齐时还保留时间戳。分块不改写术语、不删除口头重复，也不把模型猜测的纠错覆盖原文。

默认不超过 4000 字符的短文直接提供原文，不另调模型生成概览；工具结果过大时仍受分页预算约束。长文先生成中立概览，包括总摘要、按内容展开的目录、主要观点及各自引用的块编号。无需预先分类群聊主题，也不加入 ATRI 的角色口吻或最终评价。

提示词建议总摘要不超过 600 字符、每项目录摘要不超过 180 字符、每项要点不超过 240 字符，但保留主体与条件优先。程序硬上限分别为 2000、500、600 字符，目录标题为 80 字符；适度超过建议值不会让完整概览失败，超过硬限则明确失败。不会截断文字来凑数，也不会补造缺失引用或自动重试。输出仍受 `max_output_tokens`、后续输入预算和工具 JSON 总预算限制，目录及原文按实际结果大小分页。

整理模型实际读取全部已取得的文字。单次输入放得下时一次处理；放不下时分批整理，再合并概览，必要时做有界的多层合并。每次请求以 UTF-8 字节数加消息封装开销作为保守 token 上界，计入完整提示词和输入资料，不把字符数当作 token 数。超过输入或调用预算、超时、输出格式错误、目录漏块或引用不存在时，都返回失败，不用前几块冒充全文概览，也不重试。

概览直接描述原文内容，不向聊天模型讲“输入有几批概览”等整理过程。提示词要求保留动作主体、条件、否定、不同作者的观点以及后文修正，区分示例的业务步骤、实现解释和 API 通用约束；不能把示例中的一次校验变成普遍规则，或丢掉标准库、执行环境等前提后扩大结论。转写术语无法确认时应保留上下文和不确定性，不靠外部常识补写。

程序校验 `outline` 合起来覆盖所有输入块，`covered_chunk_ids` 完整且引用有效；`complete=true` 仅表示概览覆盖**已取得的这些文本块**。这种结构校验不证明每句话都忠实，抽查与精读仍需对照原文。即使它为 true，也不能声称原站内容完整、已经看过视频画面，或本轮已经展开所有原文。

长文带 `question` 时，模型依据完整目录和要点选择最多三个适合核对问题的块，再把这些原文交给对话模型；短文能一次完整返回时不额外定位，超出工具结果预算时仍按问题定位原文。目录超预算也分批检查，不只检查开头。精确引语、数字、争议性判断仍需核对返回的原文。空命中只代表这次定位没有找到相关线索，不能证明原文没有相关内容。显式 `chunk_ids` 读取无需再调用定位模型。

返回的 `view` 区分 `overview`（概览及目录）、`passages`（选定原文块）和 `text`（连续正文）。`meta.overview_complete` / `overview_scope="acquired_text"` 标记概览覆盖范围；`meta.raw_read_chars` 和 `raw_read_chunk_ids` 标记实际返回的原文字符数及完整块。`passages` 中的 `complete=false` 表示该块本页未展开完整，时间戳 `time_scope="source_chunk"` 对应原块，不把部分文字误称为完整时间范围。`unread_chunk_ids` 可继续按编号读取，`next_cursor_view` 说明游标续读的视图。

原文和概览作为不同字段存盘。概览缓存绑定文档内容、模型、提示词版本和整理预算；只有版本及全量引用检查均通过才复用。首轮可以了解全貌，下一轮围绕当前交流读原文；工具不会要求 ATRI 每次读链接都向群里发一篇摘要。本版未实现额外的“懒得看”行为。

## 缓存、预算和进程

原文、块位置与完整概览保存到 `data/documents/`，按机器人账号和群号隔离。默认 TTL 24 小时、每群 32 篇、每篇最多 200000 字符；另有全局 256 篇和 8000000 字符上限，达到上限淘汰较旧文档。相同群内同一规范链接可复用，重启后未过期记录仍可恢复；过期或淘汰后需重新 `read_link`。文档编号由内容版本和作用域确定，续读游标绑定具体版本；别的群无法拿编号续读。游标签名只在当前进程有效，重启后可用文档/块编号继续读取，或重新读链接取得新游标。降低原文上限或修改块长后，重新读链接会按新配置建立版本；旧编号直接续读会提示重新读取。

B站来源另外保存在 `data/video-sources/`：机器人账号＋规范 BV 号＋分 P 构成键，分享参数和 `p=1` 别名不产生新任务。只共享视频资料及中立概览，不共享聊天、用户问题或角色回复。组内观察重建为本群文档编号，别群仍不能拿编号续读。成功来源先落盘，再调用概览模型；概览失败不会导致下一次重新转写。并发同视频串行检查缓存，重启也可恢复，TTL 从首次成功取源计算，读取和更新概览都不续期。来源缓存上限 256 篇／8000000 字符，满时返回 `video_cache_full`，不提前淘汰 24 小时内的视频；群内文档句柄被容量淘汰后，重新 `read_link` 可从来源恢复。过期记录在后续缓存操作时清理；不同分 P、不同机器人独立。失败或尚未完成的云任务不作为成功来源缓存。

`tools.max_result_chars` 默认 8000，是每次返回的完整 JSON 上限，概览、目录和相关原文也计入。Planner 默认最多两轮查询，因此通常可先取得概览，再展开相关原文；预算耗尽后应依据已读范围决定行动，不能声称所有原文已读。`documents.max_model_calls` 默认 8，分别限制每次概览和每次定位的内部模型调用次数；同次阅读若两阶段都需要执行，它们各自计数，但共享文档处理总限时。这些都不是整个 conversation 的 token 上限。

超时分为取源和文档处理两个阶段：`links.timeout=45` 秒限制来源抓取阶段，包含排队、短链、MCP 启动以及两个 B站请求；其中 `mcp.startup_timeout=20` 秒限制进程启动与握手，`mcp.request_timeout=30` 秒限制每次 MCP 工具请求。取得文字后，`documents.timeout=60` 秒限制整次文档处理，包括概览和可选问题定位，等待模型并发槽位也计时；不是两个阶段各再加 60 秒。

B站上游包还有独立的 HTTP 总限时，默认只有 10 秒，包含 DNS、连接和完整响应正文。因此即使已收到 HTTP 200，正文没收完也会失败。本地与模板通过 `[mcp.servers.bilibili].env.BILIBILI_REQUEST_TIMEOUT_MS="25000"` 将其设为 25 秒；外层 MCP 30 秒及来源读取限时仍生效，超时后仍立即返回失败，不继续转写。

云 ASR 阶段另由 `asr.timeout=300` 秒限制，覆盖音轨获取、上传、提交、排队、轮询与转写结果下载；音轨获取还受 `asr.download_timeout=60` 秒限制。`read_link` 外层限时是启用阶段之和：`links.timeout + asr.timeout + documents.timeout`，默认 405 秒，包含并发同视频的等待；未启用 ASR 为 105 秒，关闭文档整理则不计该阶段。`read_document` 默认 60 秒。各阶段与总限时以先到者为准，超时不重试、不再换方法。

每个 MCP 服务首次使用时才启动并握手，之后保持连接；服务暂不可用不会阻止普通聊天启动。每服务请求串行、有界排队。启动或请求超时时先把失败结果交回调用者，子进程清理由连接 worker 继续完成，不让模型等待进程退出。本次请求不自动重试；后续独立调用需要时重建连接。Bot 关闭时等待回收子进程。MCP 等待不占模型并发名额，实际 Planner、Replyer、图片理解、文档概览/定位和旧意愿请求共用模型并发限制。睡眠与快照有效性检查保留在工具阶段及每次文档模型请求前后。

链接的总超时、短链超时、MCP 超时及已识别的上游网络超时统一返回 `ok=false`、`data=null`、`error.code="tool_timeout"`、`meta.retryable=false`。即使已获得视频简介，也不会把它作为字幕读取超时后的成功结果或缓存；本轮不继续重试该链接或追加音频转写。若来源文字已完整取得、后续概览阶段失败，可以保留原文存盘，不保存未完成的概览。Planner 提示词包含本轮不重试的规则。此约定针对 ATRI 的工具调用截止；上游 B站包在内部 HTTP 总 deadline 内仍有其自身重试策略，当前没有关闭该策略的配置。

## 配置与使用

需要 Node.js 20+ 和 `npx`。完整配置在 `config.toml.template` 的 `[links]`、`[documents]`、`[mcp]` 与两个 `[mcp.servers.*]` 中；链接和 MCP 模板默认关闭，启用时将 `[tools]`、`[links]`、`[mcp]` 的 `enabled` 都设为 `true`。文档整理默认启用，可按下列配置调整。改配置后重启 ATRI 生效。

```toml
[documents]
enabled = true
chunk_chars = 1200
overview_min_chars = 4000
input_token_budget = 48000
max_output_tokens = 4096
timeout = 60
model = ""
max_model_calls = 8
```

`model` 留空使用聊天模型，接口、密钥和 thinking 沿用 `[llm]`；概览和定位请求分别标为 `document_overview`、`document_select`，不携带聊天记录、人设或其他群资料。`enabled=false` 关闭概览和按问题/块编号定位，使用正文分页；B站来源的 24 小时存盘复用仍可用，群内文档编号仅保存在当前进程。

文章 MCP 通过固定版本的 `npx` 启动，首次使用可能联网下载。B站 `1.14.1` 的 CLI 使用真实路径判断入口，直接走 npx 的 `.bin` 符号链接会提前退出；已实测确认。为避免该问题，在 `atri` 目录执行一次：

```bash
npm install --prefix data/mcp --ignore-scripts --no-audit --no-fund --save-exact @xzxzzx/bilibili-mcp@1.14.1
```

配置使用 `node --import ./scripts/bilibili-dns.mjs data/mcp/node_modules/@xzxzzx/bilibili-mcp/dist/index.js` 启动 stdio 服务；预加载脚本的 DNS 开关默认关闭，启用条件见下文。安装目录在 Git 忽略的 `data/mcp` 内；保留生成的 `package-lock.json` 后，可以用 `npm ci --prefix data/mcp --ignore-scripts` 重建该目录的依赖。两种安装方式均关闭生命周期脚本，不修改上游发布包源码。

B站字幕通常需要有效登录：可使用上游提供的交互式配置，再让 ATRI 与其以相同系统用户运行：

```bash
node data/mcp/node_modules/@xzxzzx/bilibili-mcp/dist/cli.js setup
```

也可以在启动 ATRI 的环境中提供 `BILIBILI_SESSDATA`、`BILIBILI_BILI_JCT`、`BILIBILI_DEDEUSERID`。主进程只传递配置中列出的额外环境变量，不把模型 API key、OneBot token 或所有环境变量一并传给子进程。`env` 配置可覆盖普通设置；Cookie、Token 不放进示例、提交或日志。账号配置、收藏夹、搜索和更新工具不开放给群聊模型。

`atri check` 只校验配置，不启动 MCP 或检验登录。下面的显式测试会读取真实链接，启用文档整理时，长文会调用真实模型生成概览；短文不调用概览模型。测试不连接 QQ、不写入群聊存档，请把 URL 替换为真实分享链接：

```bash
uv run atri test-links --url 'https://mp.weixin.qq.com/s/文章ID'
uv run atri test-links --url 'https://www.bilibili.com/video/BV1xx411c7mD?p=2'
```

输出首份标准工具结果，包含实际阅读范围及可用的概览。退出码 0 表示取得内容（可能仅为简介或部分正文），1 表示工具失败，2 表示配置/参数错误，Ctrl+C 为 130。启用文档整理时，诊断缓存保存在 `data/diagnostics/link-reading/`，与正式群隔离，并受相同 TTL 与容量上限约束；视频诊断来源保存在 `data/diagnostics/video-sources/`，同样保留 24 小时，重复测试可核对不再抓取／转写。

日志分类 `mcp`（蓝色）记录连接、调用、结束、错误码与耗时，`links`（黄色）记录解析来源、缓存字符数和覆盖范围，`documents` 记录概览缓存、分批覆盖、模型调用次数和原文定位；通用 `tools` 日志/审计继续记录每次模型工具调用。日志不包含 Cookie、带参数的原始 URL 或外部全文；第三方 stderr 和原始协议异常不直接转存到 ATRI 日志。

## 用已有转写测试文档阅读

已有转写结果可直接验证文字处理，不必重新下载视频、上传音轨或转写：

```bash
uv run atri test-document \
  --document data/diagnostics/BV1dVRdBpEze/transcript.json \
  --url 'https://www.bilibili.com/video/BV1dVRdBpEze/' \
  --question '视频后半段对前面结论补充了哪些条件？'
```

命令读取指定文件，保留可识别的 ASR 时间戳，复用正式的文档存储、概览及问题定位；首次整理长文和每次按问题定位都会请求 `[llm]`。不配置问题时只展示默认阅读结果。命令不连接 QQ、不读取群历史，也不发群消息；诊断文档使用独立作用域。

`data/diagnostics/document-reading/<document_id>/preview.md` 展示完整缓存概览和本轮实际选中的原文，`result.json` 保存传入模型的标准工具结果。预览有完整目录，不表示单次工具返回也能装下所有目录。它用于检查内容是否覆盖尾段、引用能否回查、原文是否支持概览，它只测试文字阶段；完整 B站取源、转写和缓存命中用 `test-links` 验证。

## 百炼云端语音转写测试

`atri test-asr` 用百炼 `fun-asr` 转写一段官方公开短音频，先验证云端 API。无需本地语音模型、GPU、自建 OSS 或 B站登录。这是独立连通性测试；`asr.enabled=true` 时生产 `read_link` 也使用百炼客户端。B站 MCP 的本地 ASR 仍关闭。

在百炼控制台创建对应地域的 API Key，然后编辑已被 Git 忽略的 `config.toml`：

```toml
[asr]
enabled = true
base_url = "https://dashscope.aliyuncs.com/api/v1"
model = "fun-asr"
api_key = ""
timeout = 300
max_audio_bytes = 134217728
max_audio_seconds = 7200
download_timeout = 60
dns_over_https = false
```

在 `api_key` 的空字符串中填写真实 Key，只保存在自己的本地或服务器配置中；`config.toml.template` 始终留空。与 `[llm]` 的聊天模型配置独立，测试也不要求配置 QQ 或聊天模型。项目从 TOML 读取此配置，不自动读取终端中的 `DASHSCOPE_API_KEY`。

主进程从公开视频所选分 P 获取最低码率 AAC/MP4 音轨，检查 BV/CID、完整时长、实际字节数与公网 DNS；不带 B站 Cookie，不接受内网地址或付费试听音轨。默认音轨上限 128 MiB／两小时。`dns_over_https` 默认关闭；若本机代理返回 Fake-IP，可显式使用固定的 Google DoH 服务并继续校验、固定公网 IP。本地配置已为已发现的 Fake-IP 环境开启；服务器可按实际 DNS 关闭，失败不会自动切换解析方式。

B站 MCP 的 Node 子进程另有独立开关：模板通过 `--import ./scripts/bilibili-dns.mjs` 预加载解析器，`[mcp.servers.bilibili].env` 中 `ATRI_BILIBILI_DOH = "1"` 才启用，仅处理 `api.bilibili.com`，其他域名沿用系统解析。模板默认 `"0"`，本机已开启。这不会修改系统 DNS 或代理设置，也不会在请求失败后换出口重试；请求仍验证原域名的 TLS 证书。

三个开关作用不同：`links.dns_over_https` 用于 `b23.tv` 短链接，`asr.dns_over_https` 用于公开音轨获取，`ATRI_BILIBILI_DOH` 用于 MCP 的 B站 API。只开启后两项不能解决短链接的 Fake-IP 拦截。

音频临时文件无论成功、失败或取消都会删除，密钥只发给配置的百炼 API。上传使用百炼临时 OSS 凭证和精确 Content-Length，不下载本地模型。ATRI 资料缓存的 24 小时与供应商临时文件寿命独立：百炼临时文件最长有效 48 小时；高并发部署宜改用正式 OSS，见[官方临时上传说明](https://help.aliyun.com/zh/model-studio/get-temporary-file-url/)。

北京地域默认地址如上；新加坡地域使用 `https://dashscope-intl.aliyuncs.com/api/v1`。如果使用官方推荐的业务空间专属域名，北京为 `https://<WorkspaceId>.cn-beijing.maas.aliyuncs.com/api/v1`，新加坡为 `https://<WorkspaceId>.ap-southeast-1.maas.aliyuncs.com/api/v1`。Key、业务空间与地域必须对应。录音文件转写使用 `/api/v1/services/audio/asr/transcription`，不能使用聊天的 `/compatible-mode/v1`。参考 [API Key](https://help.aliyun.com/zh/model-studio/get-api-key) 与 [语音转写 HTTP API](https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api)。

```bash
uv run atri test-asr
```

命令会产生真实 API 调用，按供应商规则计费：提交 [官方示例音频](https://dashscope.oss-cn-beijing.aliyuncs.com/samples/audio/paraformer/hello_world_female2.wav)，查询同一任务状态，再下载转写 JSON。只有任务与音频子任务都成功且取得非空正文才判定通过。控制台 `asr` 日志记录各阶段、HTTP 状态、任务 ID 和耗时；不打印 Key，也不写正式机器人日志或群聊存档。下载转写结果时不携带 API Key。

`timeout` 覆盖整次提交、排队、轮询和结果下载。轮询是查询已经提交的任务，并非重复提交；任何失败或超时均结束本次测试，不自动重试。超时后已提交的云端任务可能继续运行，不能把本地超时当作云端取消。退出码 0 为通过、1 为请求或识别失败、2 为配置错误，Ctrl+C 为 130。只有取得真实转写结果才说明当前 Key 与服务可用；离线测试和 `atri check` 无法验证账户权限或额度。

## 验证范围

离线测试覆盖真实本地 stdio 模拟服务的握手、白名单、取消、超时、崩溃重连、强制回收、环境和跨群日志隔离；链接适配测试覆盖两种上游返回格式、分享卡片、分 P、短链、字幕错误分类、分页不丢字、文档隔离/过期/淘汰和读取结果进入 Replyer。文档测试检查原文无损、时间戳、存盘恢复、缓存版本、尾段覆盖、分批合并、目录漏引用失败、按问题定位及无命中；失败和取消不保留不完整概览。模型并发测试使用本地模拟 HTTP，覆盖慢工具不占槽、睡眠与取消、相关新消息使草稿失效。

实际 npm 包的启动与工具枚举只证明协议兼容，不证明公众号/知乎防抓取页面可读或 B站账号有效；这些需要 `test-links` 对具体分享链接验证。
