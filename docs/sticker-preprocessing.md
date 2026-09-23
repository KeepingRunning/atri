# 表情包预处理

本文件介绍如何为用户审阅后保留的表情包生成视觉描述。发送工具及群聊中的使用节奏见 [表情包发送](stickers.md)；预处理命令本身不会启动机器人或发送消息。

## 素材和结果

本地素材目录为 `data/sticker_review/`，沿用原来的图片编号。`manifest.json` 是本次处理范围，不会重新下载用户删除的图片。图片原文件不做修改。

- `catalog.json`：图片信息与模型生成的结构化描述；逐张原子保存，记录模型、提示词、处理时间和采样帧。
- `descriptions.html`：带原图的描述预览页，可以搜索编号、情绪、配字和用途，也可以只看模型标记的待复核项。
- `index.html`：原有的图片筛选页。

`catalog.json` 的每个成功条目保留 `id`、相对路径 `file`、来源、文件 SHA-256，以及以下标注：

| 字段 | 用途 |
| --- | --- |
| `title` | 区分相近图片的短名称 |
| `description` | 主体、表情、姿态、关键文字与直观语气 |
| `visible_text` | 清晰可读的主要配字，保留原文 |
| `emotions` | 自然语言情绪或语气标签 |
| `intensity` | 表达强度，1 为含蓄，2 为明显，3 为强烈 |
| `usage` | 模型建议的聊天用途，可包含主动表达、回应、辅助表达或结束话题 |
| `avoid` | 模型建议避免的误用语境，不是程序规则 |
| `animation_summary` | 多帧中看到的动作变化；静图为空 |
| `needs_review` / `review_reason` | 模型或抽查发现的不确定性与原因，不代表其他条目已获人工确认；人工标记另有 `manual_review` 记录 |

失败条目写入顶层 `failures`，不会用文件名猜一段描述冒充识图结果。强制刷新失败时保留此前成功描述，并用 `retained_previous_annotation` 标明；这不代表刷新成功。`completed` 表示当前已有可用标注的数量；完整处理后应等于 `total_stickers`，且 `failures` 应为空。

## 如何生成或续跑

在仓库目录执行，会调用真实视觉 API：

```bash
uv run python scripts/describe_stickers.py
```

复用 `config.toml` 的 `llm.base_url`、`llm.api_key` 及 `vision.model`；不启动 Bot、NapCat、MCP，也不读取或写入群聊历史。当前使用的 `deepseek-flash` 多图输入格式参见 [DeepSeek 图像理解文档](https://api-docs.deepseek.com/zh-cn/guides/vision/)。

可先只处理指定图片：

```bash
uv run python scripts/describe_stickers.py --ids G001,D008,M001,A001 --concurrency 2
```

已有结果在图片 SHA-256、模型和标注参数指纹一致时直接复用；中断后再次运行即可续跑。`--force --ids G001` 可以重新标注指定图片，会再次调用 API。修改参数后仅试跑几个编号时，未选中的旧描述仍保留；每条记录的 `annotation_model` 和 `annotation_signature` 表示自身版本，顶层 `last_run` 记录最近一次运行的参数。

抽查发现具体动作可能误判时，可在素材目录的 `review_notes.json` 中按编号写入复核关注点，例如 `{"A041": "核对手在额头附近的动作是在敬礼还是招手"}`。再次运行只会重新识别受影响的条目。关注点不会直接替换描述，最终描述仍由视觉 API 结合原图生成，并记录在该条目的 `review_note` 中。

静图提供一个画面；动图默认按时间分布选取最多六帧，去掉完全相同的采样画面，在一次请求中按顺序提供。模型会明确收到“同一动图的多个画面”，避免将采样帧当作多张独立素材。采样不保证捕捉每个瞬间，仍可人工查看原动图核对。

默认并发三个请求，单次请求最多等待 90 秒，格式或临时接口错误最多尝试三次。成功返回还必须通过 JSON 字段和类型校验。日志只打印处理进度、编号、标题、错误代码及已有客户端的请求统计，不打印 API Key 或图片 Base64。

## 在聊天中使用

文字成功发送后，统一辅助表达规划器选择单独补图、语音或不补充，以每群已成功的自然发言轮次计算共享软频率目标，每轮最多一种。无消息时主动开场暂不实现；具体配置、行动协议和试聊方法见 [辅助表达](supplements.md)。
