---
name: pm-workflow-finish
description: PM 专用：Workflow F(inish) 收口。迁移已完成任务（devlog/project_tasks）+ Themis 发现回填 + project_memory 同步 + 收口后一致性校验。一致性由 status/next 日常漂移检测 + Clio 深度审查覆盖，本 skill 不提供独立自检路径。
---

# pm-workflow-finish

PM 专用 skill——任务收口。收口完成后自动交叉校验一致性。

> 一致性校验的分工：
> - **日常漂移** → Workflow S(tatus) / N(ext) 自然覆盖
> - **深度文档一致性** → Clio review
> - **收口后** → 本 skill 步骤 7 自动执行

## 触发规则

| 触发器 | 行为 |
|--------|------|
| 开发者勾选 TASK / 代码合入 main / Phase 完成 | 启动**任务收口**流程 |

## 语言要求

| 输出类型 | 语言 |
|----------|------|
| 所有输出 | **简体中文** |

## 前置条件

- **devlog 表头**：`docs/development_log.md` 必须包含表头行 `| Datetime | Slug | Summary | PR / Commits |`。脚本会据此定位插入行。
- **separator**：若缺少 `|---|---|` separator 行，脚本自动补充（4 列，stderr 警告）；表头行本身不存在则 exit 1（fail-loud）。

## 步骤

1. Read the TASK file.
2. Detect newly completed items.
3. Ask for evidence only if completion cannot be inferred from files, tests, commits, or clear user notes.
4. **project_tasks.md + devlog 同步** — 执行脚本：
   - `python .opencode/skills/pm-workflow-finish/pm_finish_task.py --task <ID> --summary "<summary>" [--pr <#>] [--devlog] [--slug <slug>]`
   - 若任务有 PR，追加 `--pr <#>`
   - 若需同步 devlog，追加 `--devlog`（slug 默认从 task ID 推导：`BL-PHASE2-SWEEP` → `bl-phase2-sweep`）
   - 若 devlog slug 需自定义，追加 `--slug <slug>`
   - 若任务改变了 Phase/工具/测试数据，追加 `--tools <N>` / `--tests <N>`
   - 可选：`--status-summary "<文本>"` 更新仓库状态摘要行 / `--phase-status "<文本>"` 更新 Phase Status 行
   - **退出码解读**：`exit 0` → 成功（含自动补 separator 警告）；`exit 1` → 失败（Active TASK 找不到 / devlog 写失败 / devlog 表头缺失）。失败时 project_tasks.md **不会被修改**（fail-closed）。
5. **Themis 发现回填检查**：若本次完成的任务有 Themis review：
   - 读取对应 review report，提取 Medium / Low finding
   - 已在 Backlog → 跳过；未在 Backlog → 补入（格式对齐现有 Backlog 表格）
   - 不补入：typo / 单行注释 / <1min 单行 Nit
6. **project_memory.md 同步 + 抽查**：手动交叉比对以下字段，漂移时直接编辑修正（不双边盲改）：
   - **Last document update** 时间戳 → 更新为当前时间
   - **Phase 状态**：memory 中 Phase N 状态行 vs `project_tasks.md` Current Phase Status
   - **Backlog 计数**：memory 中 Backlog 数字 vs `project_tasks.md` Backlog 条目数
   - **Tests 计数**：memory 中 Tests 数字 vs `project_tasks.md` Tests
   - **Interaction History** → 在表格顶部插入一行（日期 + 摘要，保留最近 5 条）
   - Active TASK 为空时，Phase Status 应为 "完成 ✅"；否则检查是否遗漏收口
7. Respond with recognition + summary + next work + quality reminder.

## 关键边界

- **不**直接编辑 project_tasks.md 的 Active TASK 删除/Recently Completed 插入——必须用 `pm_finish_task.py` 脚本。**devlog 允许直接编辑**（手动追加行 / 补齐 separator 等紧急修复），但常规收口仍走脚本 `--devlog`。
- **devlog 推荐用 `--devlog`** — 脚本自动插入，避免手动 edit 的替换/插入风险。自定义 slug 用 `--slug`
- devlog **不**二次抄写到 PROJECT_TODO_COMPLETED.md
- Themis findings 补 Backlog 时格式对齐现有 Backlog 表格
- project_memory.md 同步为**手动**步骤——PM 交叉比对后直接编辑，`pm_finish_task.py` 不覆盖此文件

## 委派风格

回复示例：

> 这一步推进得很稳，尤其是用户先把接口边界定住了，这会减少后面很多返工。我们下一步不贪心，把验证闭环补上就好。
