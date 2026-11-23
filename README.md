# astrbot-plugin-basicz（basicZ）

群聊**基础行为层**，用于 [AstrBot](https://github.com/AstrBotDevs/AstrBot)。原名 `astrbot-plugin-peer`

它决定机器人**开口前后怎么处理、以及部分情况下的开口**，是我这套 QQ 机器人方案里最核心的插件之一；"主动接话要不要发生"由框架层的判官（`group_chat_context.py`）决定，本插件只负责判完之后的人格渲染与输出处理。

> 整套方案（架构 / 部署 / 判官机制）见主仓库：[zincvolt/QQNTbotBUTafter261101](https://github.com/zincvolt/QQNTbotBUTafter261101)

## 功能

- **人格管理**：人格文本存在 `D:\QQNTBOT\data\persona.json`；启动时写入 AstrBot 人格行。文件缺失/损坏时不覆盖现有设置。
- **简单的发言闸门**：`on_waiting_llm_request` 阶段做概率/规则判定，决定这轮要不要调用模型；回复后释放闸门状态。
- **注入防护 / 刷屏拦截**：过滤提示词注入与高频刷屏，避免被带偏或刷屏。
- **群聊格式**：`on_llm_request` 钩子往提示词里加说话人格式说明；具体人设/回答要求由 `persona.json` 提供。
- **读图闸**：判断当前消息是否需要把图发给模型；与 `vision_context` 共用词表。
- **热群暖场**：空闲时在群里发预设暖场话；唯一开关在 access 插件的 `group_flags.warmup`，本插件只按 `water.json` 的空闲时长执行。
- **简单的表情包**：按概率给回复配一张表情（依赖 meme_manager 插件）。
- **输出后处理**：`on_decorating_result` 阶段丢掉模型误输出的空/None 回复，把 `&&表情&&` 标记挪到句末，并套用正则文本替换。


## 配置文件

| 文件 | 用途 |
|---|---|
| `data\persona.json` | 人格文本（可面板编辑） |
| `data\water.json` | 热群空闲时长（开关在 access 的 group_flags） |
| AstrBot 数据目录下的回复配置 | 表情概率、文本替换规则等 |

## 依赖

- AstrBot 4.28+
- 建议配合 `meme_manager`（表情包插件）使用；
- 人格与回复配置 JSON（需要自行补全）

## 许可证

AGPL-3.0-or-later，见 [LICENSE](LICENSE)。
