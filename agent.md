# ATRI 开发约定

## 项目说明

ATRI 是 Python QQ 群聊机器人，使用 aiohttp 和 OneBot v11。
默认消息处理流程：接收并记录 → 按群合批 → 固定聊天快照 → Planner 选择行动 → Replyer 写正文 → 发送回执。旧 willingness 模式保留用于对比。

关键文件：

- src/atri_bot/bot.py：消息处理与调度。
- src/atri_bot/group_session.py：合批、等待唤醒、快照失效与重新规划。
- src/atri_bot/planner.py：原生行动调用、协议校验及查询循环。
- src/atri_bot/willingness.py：接话规则、冷却和退避。
- src/atri_bot/context.py：人设、群聊历史和判断提示词。
- src/atri_bot/model.py：大模型 API 调用。
- personal_info.txt：ATRI 的运行时人设。
- docs/reply-willingness.md：接话意愿设计说明。
- docs/planner.md：当前 Planner 与 Replyer 设计、预算与试用。

## 常用命令

以下命令在 atri 目录执行：

- 安装依赖：uv sync --locked
- 检查配置：uv run atri check
- 自动测试：uv run python -m unittest discover -s tests -v
- 真实模型测试：uv run atri test-api
- 新流程真实模型测试：uv run atri test-planner（模拟群聊、临时存储，不连接 QQ）。

test-api 会调用真实模型；用于模型连接、配置或兼容性验证。
普通自动测试使用本地模拟接口。

## 开发约定

- 使用 Python 3.11+，保持现有异步代码风格。
- 优先复用现有模块，避免为小功能增加复杂框架。
- 运行配置从 TOML 读取。
- 新增配置项时，同步更新配置加载、模板和使用说明。
- 日志使用 logging.getLogger("atri.模块名")。
- 关键处理步骤记录结果、耗时和错误原因。
- 不把 API key、OneBot token 写进代码、文档或日志。

## 业务约束

- 不同群的聊天上下文必须隔离。
- 只有确认发送成功的回复才能进入聊天历史。
- Planner 模式的代码只做睡眠、过期、频率、冷却等技术门控；参与兴趣、等待与旁听由模型决定，不能加入预设主题的关键词门槛。
- Planner 与 Replyer 共享固定快照；规划交接和工具观察不作为已确认聊天写入历史。
- 模型调用失败必须明确记录，不能记为成功。
- 测试消息不能写入真实群聊历史。
- 启动后台和发送真实 QQ 消息应符合用户当前任务的授权范围。

## 协作方式

- 使用中文解释，先说明结果，再补充必要细节。
- 修改前阅读相关代码，保留用户已有改动。
- 常规、可逆且已授权的工作直接推进，避免反复确认。
- 根据修改内容运行相应测试。
- 完成后说明改了什么、验证了什么、还有哪些限制。

写的时候尽量具体、稳定、能执行。例如“使用现有日志模块记录请求耗时”比“代码要高
质量”更有用。临时待办、完整聊天记录、大段角色台词可以放到各自的文档里，通过路
径引用；ATRI 的角色设定仍放在 personal_info.txt。

以后如果希望所有项目都遵守“使用中文”等偏好，可以放进 ~/.codex/AGENTS.md。项目
内还可以按子目录补充规则，加载链中更靠近当前工作目录的规则优先。读取规则

保存后，建议从 atri 目录重新启动 Codex，并让它“列出当前加载的 AGENTS.md 文件和
主要规则”，确认读取正确。
