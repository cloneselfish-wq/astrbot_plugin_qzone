
<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_qzone?name=astrbot_plugin_qzone&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_qzone

_✨ [astrbot](https://github.com/AstrBotDevs/AstrBot) QQ空间对接插件 ✨_  

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-Zhalslar-blue)](https://github.com/Zhalslar)

</div>

## 🤝 介绍

QQ空间对接插件, 可自动发说说、表白墙投稿审核、查看说说、点赞、评论等

## 📦 安装

- 直接在astrbot的插件市场搜索astrbot_plugin_qzone，点击安装，等待完成即可

- 也可以克隆源码到插件文件夹：

```bash
# 克隆仓库到插件目录
cd /AstrBot/data/plugins
git clone https://github.com/Zhalslar/astrbot_plugin_qzone

# 控制台重启AstrBot
```

## ⌨️ 配置

请前往插件配置面板进行配置

说说和系统消息统一使用内置卡片渲染器，不再需要配置渲染器或外部样式目录。卡片渲染未生成图片时，发送原始文本。

### 调度说明

**自动发说说（随机间隔）**

- `publish_interval_min_hours` / `publish_interval_max_hours`：两次发布之间的随机间隔区间，默认 `24` ~ `96` 小时（即 1~4 天）。
- 每次发布完成后，在下一次间隔区间内随机取一个**秒级精度**的时间点，更接近真人随手发动态的节奏。
- 随机出的时刻若落在 `quiet_hours` 夜间免打扰时段内，会重新抽取，确保自动发说说永不打扰静默时段。
- `publish_cron` / `publish_offset` 仅作为**冷启动锚点**：首次启用或没有历史排程时，用它确定第一条说说的时刻（若锚点落在静默时段，则顺延到静默结束）。
- 排程状态保存在插件数据目录的 `publish_state_<平台>.json`，重载插件不会让说说提前发布；若记录的下一次时刻已经过期，则重新随机排程（不会补发）。

**自动评论（固定周期 + 随机偏移）**

- `comment_cron` 表示任务的**基准时间**（Cron 表达式：分 时 日 月 周）。
- `comment_offset` 表示围绕基准时间的随机前后浮动范围（单位**秒**，`±N`）。
- 例如：`comment_cron = 0 8 * * *` 且 `comment_offset = 600`，表示每天 `08:00 ± 10 分钟` 随机执行一次自动评论。
- 将偏移设为 `0` 可关闭浮动，严格按 Cron 时间触发；落在静默时段则改期到静默结束后。

### 记忆素材（群聊 + 自己的经历）

自动发说说每次都会读两份素材，**综合成同一条说说**，而不是二选一：

| 素材 | 作用 |
|---|---|
| 群聊记录（`source.post_max_msg` 条） | 今天大家聊了什么、气氛是什么 |
| `astrbot_plugin_livingmemory` 的近期记忆 | 我自己今天真实经历了什么（如「在 MC 里摔死、挖到第一块铁锭」） |

这样写出来才像「一天下来的总结」——既有群里的热闹，也有她自己做过的事。

- `memory.enable`：总开关，默认开启。探测不到记忆库时自动只用群聊记录。
- `memory.persona`：读哪个人格的记忆，留空表示与平台实例 ID 同名（如「山吹沙绫」）。
  记忆按 `session_id` 前缀区分人格，填错会读不到数据。
- `memory.mc_topic_prob`：每条说说融合记忆素材的概率，默认 `1.0`（每条都融合）。
  调低则偶尔只写群聊话题；群聊记录不受此开关影响，始终参与。
- `memory.lookback_hours` / `memory.max_items`：回看时长与每次取的记忆条数。
- `memory.keywords`：命中任一关键词的记忆才会被当作「自己的经历」
  （默认 `MC` / `Minecraft` / `我的世界` / `苦力怕` / `下界` / `挖矿`）。
- `memory.attach_screenshot`：从 `astrbot_plugin_mc_live` 的截图目录挑一张与记忆时间窗
  （±`screenshot_tolerance_minutes`，默认 10 分钟）吻合的实况图一起发；没有吻合的就纯文字。
- `memory.post_prompt`：记忆素材的使用方式（怎么和群聊话题揉在一起）。
  文体与篇幅仍由 `llm.post_prompt` 决定，两者分工不同。

记忆库以 `mode=ro` **只读**打开，不写、不迁移、不加锁。已消费到的记忆 id 记录在
`memory_state_<平台>.json`，发布成功后才推进，失败不吞素材。

## 🐔 使用说明（QzonePlugin）

### 一、基础说明

- **默认查看的是“好友动态流”**
- **@某人 / @QQ号**：表示查看该用户的 QQ 空间
- **序号从 0 开始**
  - `0` = 最新一条
  - `-1` = 最后一条
- 支持 **范围语法**：`2~5`
- 机器人在需要评论 / 回复时，会 **自动排除自己的评论**


### 二、命令一览表

> 群聊里发命令需先唤醒机器人：用 `wake_prefix` 前缀（默认 `/`，例如 `/记忆说说`）或 @机器人。

| 命令 | 别名 | 权限 | 参数 | 功能说明 |
|----|----|----|----|----|
| 查看访客 | - | ADMIN | - | 查看 QQ 空间最近访客列表 |
| 看说说 | 查看说说 | ALL | `[@用户] [序号/范围]` | 查看说说（自动拉取完整详情） |
| 评说说 | 评论说说、读说说 | ALL | `[@用户] [序号/范围]` | 给说说评论（可配置自动点赞） |
| 赞说说 | - | ALL | `[@用户] [序号/范围]` | 给说说点赞 |
| 发说说 | - | ADMIN | `<文本> [图片]` | 立即发布一条说说 |
| 记忆说说 | 试发记忆 | ADMIN | - | 立即用最近的 MC 记忆生成并发布一条（不动自动排程、跳过概率抽签） |
| 写说说 | 写稿 | ADMIN | `<主题> [图片]` | AI 生成草稿，保存为待审核稿件 |
| 删说说 | - | ADMIN | `<序号>` | 删除自己发布的说说 |
| 回评 | 回复评论 | ALL | `<稿件ID> [评论序号]` | 回复评论（默认回复最后一条非自己评论） |
| 投稿 | - | ALL | `<文本> [图片]` | 向表白墙投稿 |
| 匿名投稿 | - | ALL | `<文本> [图片]` | 匿名投稿到表白墙 |
| 看稿 | 查看稿件 | ADMIN | `[稿件ID]` | 查看稿件（默认最新） |
| 过稿 | 通过稿件、通过投稿 | ADMIN | `<稿件ID>` | 审核并发布稿件 |
| 拒稿 | 拒绝稿件、拒绝投稿 | ADMIN | `<稿件ID> [原因]` | 拒绝稿件 |
| 撤稿 | - | ALL | `<稿件ID>` | 撤回自己投稿的稿件 |

## 三、范围参数使用示例

```text
看说说
看说说 2
看说说 1~3
看说说 @某人
看说说 @某人 0
```

### 效果图

## 💡 TODO

- [x] 发说说
- [x] 校园表白墙功能：投稿、审核投稿
- [x] 点赞说说（接口显示成功，但实测点赞无效）
- [x] 评论说说
- [x] 定时自动发说说、日记
- [x] 定时自动评论、点赞好友的说说
- [x] LLM发说说
- [ ] LLM配图
- [ ] 自动上网冲浪写说说

## 👥 贡献指南

- 🌟 Star 这个项目！（点右上角的星星，感谢支持！）
- 🐛 提交 Issue 报告问题
- 💡 提出新功能建议
- 🔧 提交 Pull Request 改进代码

## 📌 注意事项

- 想第一时间得到反馈的可以来作者的插件反馈群（QQ群）：460973561（不点star不给进）

## 🤝 鸣谢

- 部分代码参考了[CampuxBot项目](https://github.com/idoknow/CampuxBot)，由作者之一的Soulter推荐

- [QQ 空间爬虫之爬取说说](https://kylingit.com/blog/qq-空间爬虫之爬取说说/)
  感谢这篇博客提供的思路。

- [一个QQ空间爬虫项目](https://github.com/wwwpf/QzoneExporter)

- [QQ空间](https://qzone.qq.com/) 网页显示本地数据时使用的样式与布局均来自于QQ空间。
