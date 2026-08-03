---
name: pm-workflow-develop
description: PM 专用：Workflow D(evelop) 标准 7 步特性开发。pool prepare → 开发 agent(Daedalus/Morpheus) → Themis → 查 Codex → QA → merge → release + 收口。适用 src/ tests/ database/ 业务代码。
---

# pm-workflow-develop

PM 专用 skill——标准 7 步特性开发流水线。权威源：[`docs/development_workflow.md`](../../../docs/development_workflow.md) §1–§6。

## 触发规则

| 触发器 | 行为 |
|--------|------|
| Phase 任务开工（`P<N>-T<M>`） | 启动本工作流 |
| 用户说 `做 P<N>-T<M>` / `开工` | 启动本工作流 |
| 新增业务代码（`src/` `tests/` `database/`） | 启动本工作流 |

## 闭环流程

> **派发前**：确认 spec 已过 Momus 审视（spec-breakdown §⑦）。  

```text
① pool prepare wt_X → checkout feat branch → push task doc 到 main
   ↓
② dispatch <dev-agent> → 开发 + commit + push + create PR → Codex 后台触发
   ↓
③ dispatch Themis（同 wt）→ review → P0/P1 → fix loop（Daedalus→查 Codex→Themis R2）
   ↓
③.5 PM 查 PR Codex bot comments → 合并到 Themis findings
   ↓
④ dispatch QA（同 wt）→ ruff/mypy/pytest
   ↓
⑤ 等开发者 merge PR（OC2.5，PM 禁止合并）
   ↓
⑥ pool release wt_X
   ↓
⑦ PM 收口：加载 pm-workflow-finish → pm_finish_task.py 同步 project_tasks + devlog + project_memory
```

> **收口时**：统一走 `pm_finish_task.py`，禁止手工 edit `project_tasks.md` / `development_log.md`。

## 开发 agent 选择

| 任务类型 | Agent | 派发方式 |
|----------|-------|----------|
| 后端 / 系统 / 集成 | Daedalus | `pool dispatch wt_X Daedalus` |
| 静态前端（HTML / 原生 JS / ECharts） | Morpheus | `pool dispatch wt_X Morpheus` |

## 核心约束

- **同一 wt 串行**：Daedalus/Themis/QA 共享 wt_X，不跨 wt
- **中间不 release**：Step 2~6 期间 wt 持续 busy
- **Themis fix loop**：放行条件 = 0 P0 AND 0 P1（P2 → Backlog）
- **Codex merge**：首次 Themis 后去 PR 页面查 Codex comments，与 Themis findings 合并
- **PM 禁合并**：OC2.5，merge 由开发者执行
- **派发前 push spec**：task doc 变更先 push main，再 prepare worktree
- **派发前 Momus**：BL-\* 和 P<N>-T<N> 均需经 Momus 审视（spec-breakdown §⑦）

## 失败回滚

| 失败点 | 处置 | release？ |
|--------|------|-----------|
| 开发 agent 失败 | explore 排查 | ✅ 不继续时 |
| Themis P0/P1 | fix loop | ❌ |
| QA fail | fix loop | ❌ |
| PR 未 merge | 等开发者 | ❌ |

## 流程裁减

开发者可按 OC5.8 裁减步骤（跳过 Themis / Codex / QA 部分或全部）。PM 照办。

Base directory for this skill: /Users/clark/xidi-minimal/.opencode/skills/pm-workflow-feature
