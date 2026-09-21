# 自动测试

使用标准库 `unittest`，在 `atri` 目录执行。测试使用临时数据目录、模拟模型和本地 HTTP 服务，不连接真实 QQ 或外部模型，也不读取生产聊天记录。

```bash
# 完整回归，与 CI 的发现方式一致
uv run python -m unittest discover -s tests -v

# 只运行一个模块，不需要修改 PYTHONPATH
uv run python -m unittest tests.test_planner -v

# 表情包相关测试：素材、协议、后台补发和发送回执
uv run python -m unittest discover -s tests -p 'test_sticker*.py' -v

# 在模块内按测试名称筛选
uv run python -m unittest tests.test_sticker_supplements -k timeout -v
```

部分测试会监听回环地址上的随机端口，受限环境需要允许本地端口监听。Node.js 可用时运行 B站 MCP 的 DNS 测试；没有 Node.js 时，这部分会明确标记为跳过。

## 文件职责

| 文件 | 覆盖内容 |
| --- | --- |
| `test_bot.py` | 消息入口、基础回复、群隔离和聊天历史 |
| `test_group_session.py` | 合批、等待、快照失效、重新规划和群聊调度 |
| `test_planner.py` | 主 Planner 行动协议、工具预算、校验重试和 HTTP 请求 |
| `test_context.py` | 一小时历史窗口、快照筛选、不可变性和大小预算 |
| `test_storage.py` | JSONL 存储恢复 |
| `test_stickers.py` | 表情包素材、检索和配置 |
| `test_sticker_planner.py` | 补图候选、选择/跳过、协议重试和 HTTP 请求 |
| `test_sticker_supplements.py` | 正文确认后的异步补图、取消、超时、过期和晚到回执 |
| `test_sticker_delivery.py` | 图片发送、可见历史、统计及失败回执 |

其余 `test_*.py` 按对应功能组织，如链接、转写、传输、意愿和部署。新用例优先放进对应功能文件；协议校验与后台调度可以分别运行和定位。

## 共享辅助代码

- `support/factories.py`：固定白天时刻 `daytime()`、OneBot 群消息 `raw()`、原生工具调用 `call()`、Planner 行动 `action()`。
- `support/models.py`：记录正文请求的 `RecordingModel`、意愿判断的 `JudgingModel`、可编排 Planner 结果的 `PlanningModel`。
- `support/stickers.py`：隔离的候选素材、`FakeStickerLibrary`、`StickerModel` 和补图行动 `supplement()`。

测试之间不互相导入 `test_*.py`。只有真正被多个模块使用的辅助代码才放进 `support/`；场景专用的发送器和测试数据留在当前文件。

`PlanningModel.steps` 和 `StickerModel.sticker_steps` 接受行动字典、异常或异步函数。异步函数可以用 `asyncio.Event` 控制模型挂起和释放，以测试并发顺序；不要靠任意延时猜测另一个任务是否已执行。验证退避、截止时间等时间行为时仍使用明确、有上限的等待。

临时目录使用 `self.enterContext(tempfile.TemporaryDirectory())`，Bot 创建后立即用 `self.addAsyncCleanup(self.bot.close, timeout=.1)` 注册关闭；清理按逆序执行，先关闭后台任务，再删除数据目录。这样初始化或测试断言失败时也能清理。HTTP 服务和客户端优先使用异步上下文管理器。

真实模型诊断是单独的 `atri test-api`、`atri test-planner` 等命令，会产生外部请求，不属于这套自动回归。
