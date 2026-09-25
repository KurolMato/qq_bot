# Astra 指令调整记录

核对日期：2026-09-06。

## 阅读依据

- [OpenAI GPT-6 Astra 模型指南](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6-astra)：已读取完整正文，包括行为、提示实践和迁移部分。
- [Eric Provencher：Rethinking skills and prompts for GPT-6 Astra](https://x.com/pvncher/status/2095991462416490862)：已通过浏览器读取原帖完整文章正文，非搜索摘要或第三方整理。

本次采用的原则是精确触发、按需读取、清晰的完成边界和适度验证。模型配置原本已经是 Astra / medium，保留不变。

## 实际检查范围与修改

| 指令来源 | 检查结果与处理 |
| --- | --- |
| `~/.codex/AGENTS.md` | 原为 0 字节。写入简短的中文沟通、完成任务、按需加载与验证约定。 |
| 项目及其父目录 `AGENTS.md` / `AGENTS.override.md` | 未发现现有项目指令。新增根目录 `AGENTS.md`，保留项目结构、环境选择、运行数据保护及笔记本完整同步要求。 |
| `~/.agents/skills/agent-reach/SKILL.md` | 完整读取后改写。原有“Also MUST USE when user mentions any platform or shares any URL”使本地代码任务也可能触发检索。收窄描述，保留六份按需参考；删除强制多平台研究和附带版本检查，加入工具不可用时的替代路径。140 行缩为 33 行。 |
| OpenAI Docs 及 model-migration 参考 | 完整读取。针对官方模型资料的检索路线保留；没有改写系统托管技能。 |
| Skill Creator | 读取当前指导并用于这次技能修改，保留其领域约束和渐进披露原则。 |
| `~/.codex/config.toml` | 检查模型及持久指令配置，当前为 `gpt-6-astra`、`medium`。未发现配置中的额外 instructions 文件入口。未改权限、插件启用情况或模型参数。 |
| `~/.codex/rules/default.rules` | 已读取，内容为命令授权记录，保留。 |
| 应用持久状态 | 仅核对与指令有关的键，未发现另一个可编辑的自定义提示入口。未修改应用内部状态数据库。 |

当前会话的系统／开发者指令由运行环境提供，并非可直接编辑的本地 AGENTS 文件。已检查可见技能目录的触发描述；无关的专业技能未逐一全文审计或重写，不能将本次工作称为所有已安装技能的全面审计。尤其是宠物生成的专用步骤不适合仅因篇幅较长而删除。

## 验证与恢复

- 使用 Skill Creator 的 `quick_validate.py`（Python UTF-8 模式）验证通过，六个参考路径均存在。
- 回读已安装的两个用户级文件；项目差异检查通过。只变更说明与行为指令，不运行机器人业务测试或真实群联调。
- 原全局指令与原 agent-reach 技能保存在本机 `data/astra-instruction-update/backup/`，该目录已排除 Git 同步。恢复时将对应备份复制回原路径即可；新增项目指令可通过版本差异恢复。
- 新任务会加载更新后的配置。当前任务仍带有修改前注入的技能描述，不能声称已清除旧上下文或已验证未来模型行为。
- 项目 `AGENTS.md` 随代码同步；用户级 AGENTS 和技能在用户目录，不随项目 Git 同步。本次没有推送或修改笔记本。
