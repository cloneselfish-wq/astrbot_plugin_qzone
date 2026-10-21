# Changelog

## v3.3.1

Bug Fixes:

* 修复：**定时评论 / 回复没有注入人格**。`generate_comment` / `generate_reply` 调用
  `_build_request_context` 时漏传 `persona_id`，而定时任务不带事件上下文，
  `_get_persona_context` 直接返回空，system prompt 里只剩下 `llm.comment_prompt`，
  评论口吻因此完全由帖子内容和提示词决定（实测同一人格既能写出温柔附和，也能写出尖锐吐槽，
  表现为「说说像自己、评论像另一个 Bot」）。
  现在空间生成任务统一由 `LLMAction._resolve_persona_id()` 锁定**平台实例对应的人格**：
  显式 `platform_id` → 事件平台 ID → `default_platform_id()`，平台实例 ID 与人格 ID 同名即可命中；
  按 ID 取人格失败时才退回会话解析，仍失败会打 WARNING 便于排查。
* `comment_prompt` / `reply_prompt` 的默认值去掉「犀利」等性格词，改为「用你平时的语气……」。
  人格设定已单独注入，任务提示词里再写性格词会和人设打架。
  **升级后请手动把已有配置改成新值**（AstrBot 不会覆盖已存在的配置项）。

## v3.3.0

New Features:

* 自动发说说支持读取 `astrbot_plugin_livingmemory` 的记忆库，并**与群聊记录融合**成同一条说说：
  群聊决定「今天大家聊了什么」，记忆决定「我自己真实经历了什么」，不再二选一。
  以只读方式访问 SQLite，按人格（`session_id` 前缀）+ 关键词 + 时间窗筛选；
  默认每条都融合（`memory.mc_topic_prob=1.0`），读取失败或无新记忆时自动只用群聊记录。
* 支持给记忆主题的说说配一张 MC 实况截图：从 `astrbot_plugin_mc_live/shots/incoming` 中挑选
  mtime 落在记忆时间窗 ±10 分钟内的图片，找不到就纯文字。
* 记忆消费水位持久化到 `memory_state_<平台>.json`，仅在发布成功后推进，失败不丢素材。
* 生成说说时按平台 ID 加载人格设定（定时任务没有事件上下文，原先取不到人格，容易写成旁观者口吻）。
  文体仍由 `llm.post_prompt` 决定，`memory.post_prompt` 只负责说明记忆素材该怎么融入。
* 新增管理命令「记忆说说」（别名「试发记忆」）：立即结合群聊记录与近期记忆生成并发布一条，跳过概率抽签、
  不检查静默时段、不改动下次自动发布的排程，用于随时查看实际效果。
* 修复：记忆模式不拉群聊历史时 `contexts` 为空，messages 只剩 `system`，
  智谱 glm 会返回 `400 code 1214 messages 参数非法`。现在空上下文时自动补一条中性的 user 消息。

## v3.2.0

New Features:

* 自动发说说改为「随机间隔」调度：每次发布后随机 24~96 小时（精确到秒），不再固定钟点，节奏更接近真人。
  间隔区间由 `trigger.publish_interval_min_hours` / `trigger.publish_interval_max_hours` 配置，最大值设为 `0` 可关闭自动发说说。
* 自动发说说接入夜间免打扰：随机出的时刻落在静默时段会重新抽取；冷启动锚点落在静默则顺延到静默结束后（带随机抖动）。
* 发布排程持久化到插件数据目录的 `publish_state_<平台>.json`，插件重载后沿用未到期的下一次发布时刻，不会提前发；过期记录则重新随机排程（不补发）。
* `trigger.publish_cron` / `trigger.publish_offset` 改为仅作冷启动锚点使用。

Bug Fixes:

* 修复负偏移提前触发时重复排程同一 cron 基准时间，导致同一周期连环发说说（表现为一晚连发多条、每条内容都不同）。
* 修复调度日志把偏移秒数误标为「分钟」的问题。

## v3.1.3

* 统一使用内置卡片渲染器，Sender 直接创建并调用，移除工厂、协议与 pillowmd 回退分支。
* 移除旧渲染器配置、依赖及默认样式资源，显式依赖 Pillow，保留无图片时的纯文本发送。

## v3.1.2

Bug Fixes:

* 将内置卡片渲染器的头像与资源缓存移至插件数据目录（`data_dir / cache`），防止被 AstrBot 定时清理误删。
* 增加缓存 TTL 校验与过期自动清理机制（默认 1 天）。

## v3.1.1

Bug Fixes:

* 支持本地文件路径和 `file://` URI 图片上传与卡片渲染。
* 优化带图发说说失败时的错误提示与响应解析。

## v3.1.0

New Features:

* 新增前端pages，可在Astrbot仪表盘直接查看、评论、点赞自己或好友的说说。
* 添加可插拔的消息渲染抽象层，支持新的内置 Qzone 卡片渲染器以及现有的 pillowmd 渲染器。
* 扩展帖子模型和数据库模式，以在渲染卡片时存储和使用用户头像 URL。
* 将评论、图片和转发内容渲染为富卡片风格的图片，支持表情符号（emoji），并包括头像与媒体的缓存及资源获取。

Bug Fixes:

* 在 HTTP 请求期间处理更多 Qzone 图片过期错误码，以触发重新登录。

Enhancements:

* 重构发送逻辑，将消息分段的创建与发送委托给新的渲染器抽象层，从而减少重复代码。
* 改进 Qzone 解析逻辑，以从动态中抓取头像 URL 并传播至帖子模型。
* 在渲染失败时增加优雅的回退机制，返回纯文本而不是图片。

Build:

* 添加 `apilmoji` 作为依赖，用于在内置卡片渲染器中支持识别 emoji 的文本渲染。

## v3.0.4

Bug 修复：

- 在自动评论调度器中使用专用的评论偏移配置，而不是发布偏移配置。
- 确保自动评论过滤能够正确检测已有的自我评论，同时利用内存中的帖子数据和之前保存的帖子记录，防止在同一条 Feed 上重复评论。

改进：

- 在为自动评论查询 Feed 时，更早排除自己的帖子和已评论的帖子，以减少不必要的处理。
- 在自动评论任务中为每条帖子添加错误处理和结构化日志记录，这样单条评论失败不会中断剩余帖子的处理。

## v3.0.3

- Refactor: 重构自动发说说、自动评论的定时调度逻辑，改为以 `cron` 触发点作为基准时间，并通过 `publish_offset_minutes` / `comment_offset_minutes` 在基准时间前后随机浮动（`±N` 分钟）；偏移为 `0` 时严格按 `cron` 触发。

## v3.0.2

- Fix: 修复 QZone API 返回空字符串时 `json5.loads()` 抛出 `ValueError: Empty strings are not legal JSON5` 导致插件异常中断的问题。
- Improve: 新增接口响应兜底解析，针对空响应、无 JSON 片段、JSON 解析失败返回可处理的错误结果，避免直接抛异常。
- Improve: `QzoneParser.parse_response` 在解析结果非 `dict` 时，改为返回统一错误对象，不再抛出 `RuntimeError`，调用方错误处理路径更一致。
- Fix: 修复 `403` 被误判为“登录失效”并重复重登的问题；仅在 `401` 或接口明确登录失效（`code = -3000`）时触发重登。
- Improve: 查询说说失败时细化错误提示，区分“无权限查看”“登录状态失效”“接口响应异常”“暂无可见说说”等场景。
- Refactor: 新增 `core/qzone/constants.py`，集中维护错误码、错误消息、HTTP 状态及内部元数据键，降低多处散落导致的不一致风险。
- Refactor: 将 HTTP 状态注入到内部元数据 `__qzone_internal__.http_status`，并在 `ApiResponse.data` 中剥离内部字段，避免与业务字段冲突。
- Improve: 统一错误消息常量为中文（如“响应内容为空”“权限不足”），更符合插件用户使用场景。
- Behavior: 保持无参数调用默认行为为查询最新一条说说（`pos=0, num=1`）。
