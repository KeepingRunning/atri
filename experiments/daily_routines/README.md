# 从原作整理两小时日常

成品放在 [`resources/daily_routines`](../../resources/daily_routines/README.md)，一份日常一个 JSON，方便直接删除。这里保留离线提取过程和脚本；正式机器人直接读取这批已认可的 JSON，按上海时间每两小时随机选取，不运行这些生成脚本。接入方式见 [日程设计](../../docs/daily-routines.md)。

## 来源与取舍

输入是 [`ATRI_dialogue/dialogue.jsonl`](../../../ATRI_dialogue/dialogue.jsonl) 的 12,184 条记录、35 份脚本。按脚本将完整正文交给模型阅读，未只检索少量旧标签或摘要；优先取简体中文，没有时回退其他已有语言。覆盖记录和初提的 223 个方向在 [candidates.json](candidates.json)。

逐项结合原文核对主体、具体物件和剧情条件后，合并重复方向、排除其他角色独自做的事与不宜复用的剧情；另补充了 5 个有原文行号的方向，见 [manual_candidates.json](manual_candidates.json)。全部 228 个候选都有明确取舍，见 [selection.json](selection.json)：116 个保留、53 个重复、59 个排除。这是本次提取覆盖量，不声称原作只可能整理出这些主题。

同一活动保留不同场景时，应有实际差别，例如“跟水菜萌学汉堡肉”和“自己做给同伴吃”；仍可能有你觉得太相似或不够有趣的条目，留给你决定。

生成输入包含当前完整人设、选中方向、经过核对的改编要求和证据行附近的原文。提示词在 [prompts.py](prompts.py)。保留原作同伴的虚构生活场景，不把群友代入夏生、主人或恋人。少数过去时空、人工岛、角色愿望改编需要满足文件里的前提。

## 文件含义

- `macro`：连续覆盖 0–120 分钟的 2–5 个大阶段。
- `micro`：12 个连续十分钟格，描述当前活动与关注点；不是要求每个小动作持续十分钟。
- `participants` / `preconditions`：参与者和适用条件。
- `adaptation_notes`：两小时扩写、情境迁移等说明。原作没有官方的这份两小时作息。
- `source`：原文脚本、稳定 ID、全局行号、说话人、引文与活动依据。引文由程序从原文件复制，不让模型生成。为便于阅读，文字只去掉外围双引号并将字面量 `\n` 换成空格。
- `source_strength`：`direct` 是活动核心有直接线索；`adjacent` 是需要更明显的场景或意愿迁移。两者都不表示全部细节来自原文。
- `source_contains_spoilers`：出处可能涉及后期剧情。过去回忆等也不能当成当前背景直接拼接。
- `normalization`：如果模型大阶段的时间没有对齐十分钟，依据已经完整的十二格活动分组校正，并保留前后时间。不补写缺失格、不改文字。
- `review`：辅助编辑判断、问题和修订记录。模型可能漏检，也可能给出不合理意见；不是用户验收。`revised` 表示按核实的问题修订，`pass` / `revise` 是辅助模型的建议，不能当作原作设定。`status` 保持 `pending_user_review`。

## 运行

在 `atri` 目录运行：

```sh
# 只校验现有文件并刷新索引，无 API 调用
uv run python experiments/daily_routines/build.py index

# 以下命令使用 config.toml 中的模型，会调用 API
uv run python experiments/daily_routines/build.py extract
uv run python experiments/daily_routines/build.py generate
uv run python experiments/daily_routines/build.py review

# 应用已核实的具体修订要求，也会调用 API、可能改写成品
uv run python experiments/daily_routines/build.py refine

# 限定某些已有条目
uv run python experiments/daily_routines/build.py review --ids b204-01 b205-01
```

提取与生成会缓存同输入的成功结果；生成不会覆盖已存在的 JSON，也不会自动补回已发布后被删掉的文件。删除后运行 `index` 即可刷新分类表。发布记录在忽略提交的 `cache/published.json`，若主动删除这份记录，生成器便无法辨认过去手动删除的文件。

辅助模型逐份结合原文给编辑意见。实际发现它会把正常的生活延展、人物期待误判成错误，还会要求重演原作意外，因此默认 `review` 只写建议，不改日程正文。`--max-revisions 1` 或 `2` 可显式启用自动修订，但本批成品改用 [editorial_fixes.json](editorial_fixes.json) 中 60 份核实过的具体要求定向修订，并直接修正了重点样例与剩余明确问题。少数条目仍有模型提出、尚未采纳的主观问题，保留给你参考。

`refine` 会修改成品，适合继续制作阶段，不应在你亲自修改后随意批量重跑。它只处理有核实意见的文件，尊重已经删除的文件。不同阶段的原始响应、失败原因与耗时在本地忽略提交的 `cache/` 中；同一阶段再次请求可能替换该阶段缓存。

校验结果见 [verification.json](verification.json)。校验涵盖全量脚本的覆盖记录、每个候选的取舍、每份日程的时间与字段、来源行号和原文一致性、编号和完整微日程不重复。它不能证明每个改编细节都像原作或有趣，最终仍以阅读挑选为准。

素材制作和此次接入均未启动真实 QQ 或发送群消息。运行时只按适用时段抽选，将场景、参与者和前提随大日程、当前一格注入提示词；不维护跨窗口剧情状态。历史 `status` 字段是制作记录，不影响用户已认可的整库参与抽选。每天 00:00–08:00 固定睡觉，代码强制拦截回复。
