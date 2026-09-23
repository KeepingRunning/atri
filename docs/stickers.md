# 表情包补充

表情与语音现由同一个辅助表达规划器选择。文字成功发送后，每轮最多单独补一张图或一条语音，也可以不补充。完整的配置、软频率、取消、回执与日志说明见 [表情与语音辅助表达](supplements.md)。

## 图片素材与配置

```toml
[stickers]
enabled = true
catalog = "data/sticker_review/catalog.json"
search_limit = 6
max_image_bytes = 8388608
```

要求 `reply.mode="planner"`、`tools.enabled=true`。图片路径相对于 `catalog.json`；待复核素材不参与自动选择。描述生成见 [表情包预处理](sticker-preprocessing.md)。

共享频率、近期窗口和截止时间移至 `[supplements]`。兼容旧配置：未提供 `[supplements]` 时，会读取旧 `[stickers]` 的同名字段；一旦提供新节，以新节为准。关闭语音后，统一规划器仍可以正常选择表情。

## 图片编码与部署

发送前检查图片存在性、SHA-256 和解码结果，动画 WebP 转成多帧 GIF，静态 WebP 转成 PNG，转换有有界内存缓存，原素材保持不变。

图片以 Base64 内容交给 NapCat，不需要跨容器图片挂载。部署时保留整个 `data/sticker_review/`，包括目录和相对图片文件；发行包不包含个人挑选的图片。

旧版图片与正文合并记录仍能读取。新发图片始终是独立消息；只有成功回执才以 `[表情 ID：画面描述]` 写入已发送历史，与父文字共同计为一轮。
