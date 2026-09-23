# 亚托莉语音素材审阅

本轮整理原作录音，供用户试听、删除不喜欢的片段，再作为文字之外的辅助表达素材。预处理不会启动 Bot，也不会向 QQ 发送消息。

2026-09-23 用户确认当前审阅范围内的 **2,209 条语音全部保留**，选择已记录在 `data/voice_review/selection.json`，没有排除或未审条目。经用户明确授权，2,018 条有文字的语音均已通过配置的 DeepSeek API 生成用途描述，无最终失败条目；日中原文逐条对照原始配对文件，保持原样。

其中 1,905 条可参与自动选择，113 条因含义或语境不确定待复核；另外 191 条纯标点台词保留“字幕无明确台词”说明，暂不自动选用。全部 MP3、WAV 和原始录音均保留。已对抽查发现的 16 条描述补充语境限制或去掉未经音频验证的断言，记录见 `annotation_review.json`。本地 `[voices].enabled` 已开启，发行模板默认关闭；运行时流程见 [辅助表达](supplements.md)，重启 ATRI 后生效。

## 范围与文本依据

- 使用 `../ATRI_dialogue/voice_pairs.jsonl` 中 2,209 条亚托莉单人语音，按剧本语音 ID 精确对应原文件。
- 日文直接保留 `readable_languages.ja.text`，中文直接保留游戏内对应的 `readable_languages.zh-Hans.text`。二者均不重新翻译、不交给描述模型改写。
- 50 条无可靠对应文本的系统音和剧情变体，按用户要求跳过，不做 ASR。
- `ATR_b203_045` 只有剧本引用、缺少实际音频，记录在目录的 `missing_source_audio` 中，不生成替代录音。
- 相同台词的不同录音分别保留，避免丢掉不同的声音表达。角色判断使用 `voice_character=アトリ`，不依赖剧情里“少女”等显示名称。

## 本地产物

默认目录是 `data/voice_review/`，不进入 Git：

| 文件 | 用途 |
| --- | --- |
| `index.html` | 可直接在浏览器打开的试听、搜索与删选页面 |
| `catalog.json` | 日中台词、录音路径、来源、时长和表达描述 |
| `selection.json` | 用户确认的保留结果，可通过页面“导入审阅 JSON”恢复选择 |
| `keep_ids.txt` | 全部 2,209 条保留编号，一行一个 |
| `annotation_review.json` | 基于原台词及必要剧情上下文的人工抽查修订，包含修改前字段、源哈希和修订理由 |
| `audio/*.mp3` | 便于浏览器播放的试听副本，64 kbps MP3 |
| `audio/*.wav` | 兼容播放副本，22.05 kHz 单声道 16-bit PCM，供内嵌预览解码失败时使用 |

原始 Opus 文件保持不变。脚本先核对音频 SHA-256，再转换试听副本；目录同时记录原始哈希与副本哈希。再次执行时，源文件和试听副本都没有变化则复用。

## 描述与不确定性

`title`、`description`、`emotions`、`intensity`、`usage`、`avoid` 用于表达检索，字段与表情包素材保持接近。语音额外保留 `text_ja`、`text_zh`、`duration_seconds`、`text_source`、`translation_source` 和剧本出处。

描述模型只接收原作日中台词、时长和来源类别，不接收音频、群聊记录或运行时用户信息。它根据文字判断表达含义，不能声称听出了实际音高、语速、哭笑声或音色。`intensity` 也只是台词语义强度。

`recommendation` 分为：

- `everyday`：较适合独立使用的日常辅助表达。
- `contextual`：依赖人物、剧情、亲密关系或前后文。
- `review`：字幕不足或表达不明确，需要试听核对。

原作中 191 条日文只含标点，程序直接标注“字幕无明确台词，需试听”，不会调用模型编造发声或情绪。其他含糊的语气词也可以被模型标为待复核。描述完成只表示材料已整理，不表示用户已经认可或允许自动发送。

## 审阅与导出

推荐启动本地审阅服务，在 Chrome / Safari 等外部浏览器打开：

```bash
uv run python scripts/voice_review.py data/voice_review/catalog.json --serve --open-browser
```

默认地址为 `http://127.0.0.1:8766/`，只监听本机；按 Ctrl+C 停止。端口被占用时可指定 `--port`。服务只提供审阅页和目录中列出的音频，支持媒体分段请求，不提供整个仓库的文件。它不会启动 Bot 或调用模型 API。

也可直接打开 `index.html`。VS Code 内置浏览器的本地文件加载路径与普通浏览器不同；如果 MP3 和 PCM WAV 都报 `MediaError.code=4 / Format error`，该错误不足以证明文件损坏，应先使用上述 HTTP 地址在外部浏览器试听。

按编号、日中台词、用途搜索，也可以筛选推荐日常、依赖语境、需试听核对。页面分页加载，一次只播放一段录音。

页面复用一个播放器，切换或离开当前列表时释放媒体资源。MP3 播放失败时会尝试该片段的 WAV 副本，也可以手动点击“兼容播放”。错误提示包含具体编号、格式和浏览器媒体错误信息；可单独打开音频进一步确认。兼容副本只用于试听，不改原作录音。

每条可选保留、排除或恢复未审。选择保存在当前浏览器本地；从 VS Code / 文件预览切换到 HTTP 地址，或更换浏览器、端口时，先在旧页面导出审阅 JSON，再到新页面导入，选择不会自动迁移。JSON 包含 `keep_ids`、`delete_ids`、`unreviewed_ids`。也可以只导出保留编号或待删编号文本。导出和点击排除均不会删除原作文件。

本次通过对话确认的“全部保留”已另行保存为 `selection.json`，不会直接覆盖浏览器中的旧选择；需要在页面同步时导入该文件。重新生成素材目录或审阅页不会改写这个独立的选择文件。

## 重新生成

在 `atri` 目录执行，需要系统已有 `ffmpeg`，不下载本地模型：

```bash
# 只整理原文、中文译文和试听音频，不请求 API
uv run python scripts/prepare_voices.py --prepare-only

# 小样：给前 12 条待标注素材生成描述，使用 config.toml 的 llm 模型
uv run python scripts/prepare_voices.py --limit 12 --concurrency 1

# 处理剩余条目；已有成功结果按指纹复用
uv run python scripts/prepare_voices.py

# 仅重做指定语音的描述，会再次调用文本 API
uv run python scripts/prepare_voices.py --ids ATR_b103_007 --force
```

模型标注会将游戏台词发送到所配置的文本 API，并产生费用。每批结果都校验编号、字段和完整性后保存，失败不会冒充成功。源台词、模型或提示词变化会使相关缓存失效。模型生成结果只写入素材目录，不改运行配置。

只需重新生成审阅页时：

```bash
uv run python scripts/voice_review.py data/voice_review/catalog.json
```

相关离线测试：

```bash
uv run python -m unittest tests.test_voice_preprocessing tests.test_voice_review tests.test_voice_review_server -v
```
