# astrbot-plugin-basicz（basicZ）

群聊**基础行为层**，用于 [AstrBot](https://github.com/AstrBotDevs/AstrBot)。原名 `astrbot-plugin-peer`——最初只做消息后处理，后来逐渐承担起整套群聊行为，故更名为 basicZ（basic = 基础行为，Z = 本项目的代号后缀）。

它决定机器人**要不要开口、用什么人格开口、开口前后怎么处理**，是整套 QQ 机器人方案里最核心的插件之一。**无斜线指令，全是被动行为**；"主动接话要不要发生"由框架层的判官（`group_chat_context.py`）决定，本插件只负责判完之后的人格渲染与输出处理。

> 整套方案（架构 / 部署 / 判官机制）见主仓库：[zincvolt/QQNTbotBUTafter261101](https://github.com/zincvolt/QQNTbotBUTafter261101)

## 功能

- **人格管理**：人格文本存在 `D:\QQNTBOT\data\persona.json`，可在面板编辑而不改源码；启动时写入 AstrBot 人格行。文件缺失/损坏时不覆盖现有设置。
- **发言闸门**：`on_waiting_llm_request` 阶段做概率/规则判定，决定这轮要不要真的调用模型；回复后释放闸门状态。
- **注入防护 / 刷屏拦截**：过滤提示词注入与高频刷屏，避免被带偏或刷屏。
- **群聊格式**：`on_llm_request` 钩子往提示词里加说话人格式说明；具体人设/回答要求都由 `persona.json` 提供，不在代码里重复。
- **读图闸**：判断当前消息是否真的在谈图，决定要不要把图发给模型；与 `vision_context` 共用词表。
- **热群暖场**：空闲时在群里低频发预设暖场话；唯一开关在 access 插件的 `group_flags.warmup`，本插件只按 `water.json` 的空闲时长执行。
- **表情包附带**：按概率给回复配一张表情（依赖 meme_manager 插件）。
- **输出后处理**：`on_decorating_result` 阶段丢掉模型误输出的空/None 回复，把 `&&表情&&` 标记挪到句末，套用文本替换规则。

## 与表情插件的配合

本插件负责把 `&&...&&` 标记整理到回复末尾；"剥掉标记 + 从图库选图发送"由 `meme_manager` 插件完成。两插件通过文本标记协作，各自独立。
如果模型输出了图库里不存在的自造情绪词（如 `&&得意&&`），需要 `meme_manager` 的 `filter_all_tags` 开关做最终兜底清理，否则标记文本会残留成乱码。

## 配置文件

| 文件 | 用途 |
|---|---|
| `D:\QQNTBOT\data\persona.json` | 人格文本（可面板编辑） |
| `data\water.json` | 热群空闲时长（开关在 access 的 group_flags） |
| AstrBot 数据目录下的回复配置 | 表情概率、文本替换规则等 |

## 依赖

- AstrBot 4.28+
- 建议配合 `meme_manager`（表情包插件）使用；不装也能正常聊天，只是没有自动配图
- 人格与回复配置写在磁盘 JSON（运行数据，不随代码发布）

## 许可证

AGPL-3.0-or-later，见 [LICENSE](LICENSE)。
