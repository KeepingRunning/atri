# 表情与语音辅助表达

主 Planner 决定是否回应，Replyer 生成并发送文字。文字取得成功回执后，同一个辅助表达规划器再选择：不补充、单独发一张表情，或单独发一条日语语音。每轮最多补充一种，正文不等待选素材；本版不做主动开场或纯语音替代文字。

## 配置

```toml
[supplements]
target_turns_min = 3
target_turns_max = 5
recent_window = 20
max_age_seconds = 20

[stickers]
enabled = true
catalog = "data/sticker_review/catalog.json"
search_limit = 6
max_image_bytes = 8388608

[voices]
enabled = true
catalog = "data/voice_review/catalog.json"
selection = "data/voice_review/selection.json"
search_limit = 6
target_turns_min = 6
target_turns_max = 10
preferred_max_seconds = 5
max_audio_bytes = 8388608
```

两类素材可以分别开启；任一开启都要求 `reply.mode="planner"`、`tools.enabled=true`。发行模板默认关闭，因为镜像不含个人素材。共享频率和截止时间放在 `[supplements]`；旧配置未写这一节时，从旧 `[stickers]` 的同名字段迁移默认值。

每群按已确认成功的自然文字回应计数。表情与语音合计平均每 3～5 轮补充一次，其中语音平均每 6～10 轮考虑一次，都是模型软目标。没有随机抽签、硬配额或欠额补发。命令、自动复读和补充消息自身不增加自然回复轮次。

## 候选与模型决策

`SupplementPlanner` 复用本轮冻结快照、实际已发正文、人设及本群近期素材状态，用目标消息、正文和主 Planner 的交流理解做本地检索。默认提供至多六张图片和六条语音给一次模型决策，无候选直接结束。

语音检索使用中文译文、描述与正向用途，优先短句；`preferred_max_seconds` 是排序偏好，不是删除长语音的限制。相同日文去除标点和空白后具有相同 `semantic_group`，近期编号和同句组共同降权，同一组只占一个候选位置。

模型看到的语音包含日文原句、中文译文、时长、用途及避用条件。它必须检查字面含义、称呼、对象、关系、剧情事实及承诺，不能仅凭情绪相似选择，不能与刚才正文矛盾。它同时尊重“只要文字”“不要语音”“不要图片”等当前要求。

原生行动 `supplement_media` 只接受：

```json
{"kind": "voice", "asset_id": "ATR_b101_013", "reason": "补充刚才回应里的得意态度"}
```

`kind` 为 `none`、`sticker` 或 `voice`；`none` 必须配 `asset_id=null`，其余编号必须来自对应类型的本轮候选。一次只能调用一个行动，不能传入群号、文件路径、URL 或新正文。协议/模型失败最多重试两次，全部受同一个总截止时间约束。

## 时效与回执

每群只有一个辅助表达任务，不另开两套表情、语音任务，不排队积压。20 秒默认时限从文字确认算到媒体提交前，包含等待模型槽位、重试和读取素材。同群新普通消息、更新的自身回复、睡眠或关闭服务会使未提交任务失效。重复上报、其他群消息和 `/health` 不打断本群任务。

已经提交后继续等待正常回执，后续新消息无法撤回已发生的提交；确认未知时不自动补发。任何一种媒介已有该父消息的尝试记录，都阻止再次尝试另一种，避免出现图片、语音接连补发。

文字与补充有独立的消息 ID、回执和审计 key，共用 `turn_id`，补充带 `parent_message_id`。只有确认发送成功才进入聊天历史、更新素材频率；图片/语音不增加新轮次，也不延长普通回复冷却。晚到回执按原父轮次归属，重启从日志恢复相同状态。

语音历史记录原始日文与中文译文，例如 `[日语语音 ATR_b101_013，2.7s；台词：……；译文：……]`。这只是内部已发送消息标记，QQ 中不会再多发一条中文翻译。模型理由、用途、文件路径及 Base64 不进入聊天历史。

## 语音素材和实际发送

素材准备见 [语音素材审阅](voice-preprocessing.md)。用户当前保留全部 2,209 条；运行时只从 `selection.json` 的保留编号中选择描述已完成、内容可确定的条目。纯标点、标注失败和待复核条目继续留在磁盘，暂不进入自动候选。文字用途标注不等于听过音频，不伪造音色、语速或哭笑声的判断。

`VoiceLibrary.prepare` 校验本地相对路径、文件大小、SHA-256 和 MP3 基础帧结构后，以 Base64 返回一个独立 `record` 消息段。不会现场调用 TTS、ASR 或下载本地模型。NapCat 负责实际 QQ 语音转码；浏览器试听与 QQ 客户端播放分别验证。

部署时迁移 `data/voice_review/catalog.json`、`selection.json` 以及 `audio/*.mp3`。原始 Opus、WAV 兼容试听副本和审阅页面不属于机器人运行时必需文件。ATRI 和 NapCat 通过音频内容交接，不要求两个容器共享音频路径。

## 检查与观察

运行 `uv run atri check` 可查看保留、可用、待标注、待复核和纯标点数量，不调用模型或发送消息。配置及素材在启动时加载，更新后需重启生效。

`supplements` 日志记录候选、选择、取消、过期和回执；模型用途为 `supplement`。`voices` 记录语音库统计与发送准备。`supplement_plan` 审计的 `media_kind`、`asset_id`、`reason` 与独立 `delivery` 通过父消息关联，可由 `search_event_logs` 检索。旧 `sticker_plan` 审计仍可读取。

离线覆盖模型协议、两媒介互斥、语音元数据和文件校验、跨群隔离、提交前取消、提交后回执、未知不补发、迟到回执及重启恢复；测试不连接 QQ。
