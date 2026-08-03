---
name: pm-workflow-status
description: PM 专用：Workflow S(tatus) 报告项目状态。`status.py` 是唯一执行入口。
---

# pm-workflow-status

PM 专用 skill——项目状态查询。`project_tasks.md` 是任务状态索引（表格 + 执行计划 ASCII 流图），详细 spec 在 `docs/task_specs/`。`status.py`（同目录）是唯一执行入口，跑 9 项健康度检查并按 [Response Format](../../../.opencode/agents/project-manager.md#response-format) 合成报告。失败快速失败，迭代修复。

## 触发与语言

| 触发器 | 行为 | 语言 |
|--------|------|------|
| `status` / `report` / `状态` / 开发者说"看看项目" | 跑 status.py + 合成报告 | 简体中文 |

## 步骤

1. Read `.pm/project_memory.md`（缺失 → [Workflow I](../pm-workflow-init/SKILL.md)）
2. Read `docs/project_tasks.md`（缺失 → Workflow I）
3. 跑 status.py 拿 9 项健康度检查（退出码 0=pass 1=fail 2=argerr）；若 `worktree_clean` = warn，用 `git status --porcelain` 定位 main repo 的 dirty 文件：

   ```bash
   python .opencode/skills/pm-workflow-status/status.py [--json | --quiet] --pm-session-id <PM_CURRENT_SESSION_ID>
   ```

4. 从 `docs/project_tasks.md` 手工采集项目指标：Active TASK 表行数 / Recently Completed 表行数 / Python 源码行数 / 测试文件数
5. 识别漂移：对照 `project_memory.md`、`project_tasks.md`、status.py 结果三方数据，检查 Backlog 计数、Recently Completed 完整性、文档声称值与实测值的偏差
6. 按 Response Format 输出报告

## 关键边界

- **status.py 是唯一入口** — 不引入 fallback
- **project_tasks.md 是任务状态索引**（表格 + 执行计划 ASCII 流图），**详细 spec（Goal/Steps/Acceptance/Notes）在 `docs/task_specs/`**——status 不需读 task_specs，Workflow N 负责创建/更新
- **不读 plan 文档**（plan 是 [Workflow N](../pm-workflow-next/SKILL.md) 的输入）
- **不主动修复漂移**（[OC1.4](../../../docs/operational_conventions.md)）
- **不要和 Workflow N 混淆**（本工作流无建议、无选项、无下一步计划）
- **不要输出 `# <N>条路` 段**

## 项目体检清单（V1 阶段）

9 项检查与 `status.py` ALL_CHECKS 1:1 对应（同 `name` 字段）。status.py 是事实源。

| `name` | V1 早期期望 | 状态映射 |
|--------|-------------|----------|
| `worktree_clean` | 0 dirty | pass=0 / warn=≥1 \| 针对 main repo，`git status --porcelain` 定位 |
| `branch` | `main` | pass=main / warn=其他 |
| `untracked_files` | 0 | pass=0 / warn=≥1 |
| `python_source` | > 0（V1 早期可 0）| pass=>0 / warn=0 |
| `test_files` | ≥ Active TASK Type=Test 数 | pass=有 / warn=无 |
| `db_migration` | ≥ 1 | pass=≥1 / fail=0 |
| `runtime_config_defaults` | 静态 SQL 文本检查三段默认值（`trading_enabled=false` / `dry_run_mode=true` / `order_submit_enabled=false`）| pass=全 ✔ / fail=任一 ✘ |
| `pm_agent_alignment` | 命中 `CLAUDE.md 对齐声明` | pass=含 / fail=不含 |
| `pm_session_active` | pm-session-info.json 的 `current_session_id` 匹配传入的 `--pm-session-id`（不传则跳过比对） | pass=match / fail=mismatch (=guard blocked) / warn=未传入 |

**全为 pass / warn / skip 时可省略 `## CI健康度总览` 整段；任一 fail 时整段必出。** 状态语义：`pass` 期望匹配 / `warn` 不阻断但需关注 / `fail` 阻断 / `skip` 检查无法执行。

## 输出章节顺序

1. `# Current phase` → 2. `# Active TASK`（表格）→ 3. `# Backlog And Defer` → 4. `# CI健康度总览` → 5. `# 发现的漂移/偏差/回归`

## 不输出

- ❌ `# <N>条路` 段
- ❌ 任何"下一步建议"（[Workflow N](../pm-workflow-next/SKILL.md)）
- ❌ 任何 TASK 完成判定（[Workflow F](../pm-workflow-finish/SKILL.md)）
- ❌ 任何"反思 / 模式切换"内容（[Workflow R](../pm-workflow-reflection/SKILL.md) / [Workflow L](../pm-workflow-leisure/SKILL.md)）
