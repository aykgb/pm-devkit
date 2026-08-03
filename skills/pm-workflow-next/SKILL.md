---
name: pm-workflow-next
description: PM 专用：Workflow N(ext) 安排下一步任务。读 plan + TASK + log + ci status → 路径选项 → 用户选方向 → 细化 spec → Momus 审视。
---

# pm-workflow-next

PM 专用 skill——安排下一步任务。`project_tasks.md` 是任务状态索引（表格 + 执行计划 ASCII 流图），详细 spec 在 `docs/task_specs/<task>.md`。

## 触发规则

| 触发器                                        | 行为         |
|-----------------------------------------------|--------------|
| `next` / `下一步` / `@PM 下一步` / `下一阶段` | 启动本工作流 |
| `根据 implementation_plan.md 安排任务`           | 启动本工作流 |
| Active TASK 为空且当前对话需要推进            | 启动本工作流 |

## 语言要求

| 输出类型                 | 语言         |
|--------------------------|--------------|
| TASK 文件 / devlog       | **简体中文** |
| SKILL 注释 / 委派 prompt | **简体中文** |

## 步骤

1. Read `.pm/project_memory.md`（缺失 → [Workflow I](../pm-workflow-init/SKILL.md)）
2. Read `docs/project_tasks.md`（缺失 → Workflow I）
3. **仅当 Active TASK 为空时** Read `docs/implementation_plan.md`；Active TASK 非空则跳过
4. Run `python .opencode/skills/pm-workflow-status/status.py --json`（30s 超时）
5. Identify: Current phase / Active TASK / Backlog / Drift / Highest-leverage next step
6. 按 [Response Format](../../../.opencode/agents/project-manager.md#response-format) 输出报告（含 `# <N>条路` 段，最多 3 条路径，推荐优先）：

   - **Active TASK 非空**：路径选项为「继续推进当前 Batch」「先清扫 Backlog」「其他」
   - **Active TASK 为空**：扫描 Backlog + `implementation_plan.md` 下一 Phase 目标，产出选项。仅一条可行路径 → 直接推荐。

7. 用户选定方向后 → 加载 skill **`pm-workflow-spec-breakdown`** 执行 spec 拆解 + Batch 编组 + Momus 审视。

   核心决策树：
   - 已有 spec 文件 → 直接更新 `project_tasks.md` Active TASK 表 + 执行计划
   - Active TASK 为空 + Backlog 有高优项 → 移到 Active TASK，按需补 spec
   - 需新造 Phase 任务 → skill 自动路由（≤2 PM 直接写 / ≥3 派 General）

9. 提示：「Batch 就绪。说『开工』我出执行方案。」

## 关键边界

- 步骤 6 路径选项在细化 spec 之前——用户选方向后再动工
- 委派只给任务 ID（OC3.2）
- Spec 拆解 / Momus fix loop / Batch 设计的详细规则由 `pm-workflow-spec-breakdown` skill 覆盖
