# Workflow System

> PM 工作流体系——12 个 Workflow 的触发、功能和核心逻辑。每个 Workflow 有独立 skill（`.opencode/skills/pm-workflow-*`），skill 是执行时的唯一事实源。

---

## 1. 一览

| ID | 触发 | 功能 |
| --- | --- | --- |
| I(nit) | PM 首次调用 / 找不到 project_tasks.md | 项目初始化：创建 task/devlog/记忆 |
| S(tatus) | `status` | 健康检查 + 漂移报告 |
| N(ext) | `下一步` | 路径选项 → Spec 拆解 → 门禁 |
| B(reakdown) | `拆一下` / `细化 spec` | Spec 拆解 + Batch 编组 |
| D(evelop) | `做 P<N>-T<M>` / `开工` | 标准 7 步特性开发 |
| i(terate) | `修一下` / `fix` / `查一下` | 轻量迭代（bugfix + 小功能，≤200 行） |
| T(oolchain) | 改 `scripts/` `.opencode/` 下 `.py` `.mjs` `.js` | 工具链轻量闭环 |
| F(inish) | `finish` / merge | 收口：devlog + task 迁移 |
| L(eisure) | 情绪 / 闲聊 | 模式切换 |
| M(emo) | `勿忘` | 记忆保存 |
| R(eflection) | `反思` | 审计报告 |
| C(lean) | `重构` / `优化文档` / `review 下` | 文档重构 |

> 字母映射：I(nit) / S(tatus) / N(ext) / B(reakdown-spec) / D(evelop) / i(terate) / T(oolchain) / F(inish) / L(eisure) / M(emo) / R(eflection) / C(lean)。

## 2. 核心链

```
"下一步" → N（路径→拆解→门禁→就绪）
  → "开工" → D（开发→审查→Codex→QA→merge）
  → F（收口）→ 循环
  小改动 → i（explore 定位→PM 核验→General 修复→cherry-pick/PR）
  工具链 → T（General 定位→修复→合入）
```

## 3. 关键 Workflow

**N — Next**：读 memory + tasks → 输出最多 3 条路径 → 用户选 → 拆 spec（B 自动串入）→ 门禁 → "Batch 就绪"

**D — Develop**：标准 7 步（pool prepare → 开发 → 审查 → Codex → QA → merge → release + 收口）。权威源 `docs/development_workflow.md`。

**i — Iterate**：轻量迭代闭环——explore 定位（强制）→ PM 核验 → PM 出方案+用户确认 → General 分支实现+自验+push → PM Windows 验证 → cherry-pick 回合 iter → 清理分支 → 收口。不走标准 7 步。

**T — Toolchain**：PM 分析 → General dispatch 定位 → PM 核验 → General dispatch 修复+自验+合入（≤30 行 cherry-pick / >30 行 PR）。

**F — Finish**：读 tasks → 检测完成项 → 脚本收口（task 迁移 + devlog）→ 审查发现回填 Backlog → memory 同步

**S / L / M / C**：详见各 skill 文件（`.opencode/skills/pm-workflow-*/`）。S 跑健康检查，L 处理模式切换，M 保存记忆，C 文档重构。

### R — Reflection

`反思` 触发。按当前模式（管理/闲聊）执行不同的审计路径。

**管理模式下步骤**：

1. 审计 `project_memory.md` — 约定遵循、Phase 状态、技术决策
2. 审计 `development_log.md` — 近期失败未跟进、决策未记录
3. 工作流触发统计 — Status/Next/Finish/Reflection/Leisure/Check/Memo/Deploy/Bug 各几次；OC5 管道闭环次数；本应触发而未触发的工作流；命名工作流 vs OC5 管道的支配力对比
4. 审计本次会话 bash 命令执行错误 — 失败次数、分类（git / psql / ssh / curl / 工具参数 / 其他）、重复模式和高频根因、改进建议
5. 回顾本 session 对话轮次 — 总轮次数和时长、按阶段分组（阶段名 / 轮次区间 / 主题关键词）、分析模式（占比、反复改同一文件、长时间等 agent 空转）
6. 输出反思报告 — 偏差/漂移、Prompt 遵循、动作复盘、文件读写优化、任务编排、context 传递、agent 行为、OC 约定
7. 输出会话总结 — 本次会话完成了哪些事情和动作
8. 自问可固化工作流 — 候选列表供开发者裁决
9. 落盘 `.pm/reflections/YYYY-MM-DD_HH-MM_session.md`

**闲聊模式下步骤**：审计 user_profile → 审计 chats/INDEX → 输出反思 → 落盘。

**报告模板**含：会话总结 / 偏差漂移 / 工作流触发统计 / 对话轮次统计 / bash 错误审计 / Agent 行为审计 / 文件读写优化 / 可固化工作流 / OC 约定审查。

## 4. 模式切换

PM 有两种模式：管理模式（精确、结构驱动）和闲聊模式（轻松、有温度）。

- 切入闲聊：用户情绪表达 → 自然切入
- 切回管理：用户提 task / Phase / PR / 代码 → 自然切回
- 切回管理前回写闲聊记忆
