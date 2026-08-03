---
name: pm-workflow-toolchain
description: PM 专用：Workflow T(oolchain) 工具链轻量闭环。PM 分析 → General dispatch 定位 → PM 核验 → General dispatch 修复+自验+合入（≤30行 cherry-pick / >30行 PR）。适用 scripts/ .opencode/ 下 .py .mjs .js 工具代码。
---

# pm-workflow-toolchain

PM 专用 skill——工具链轻量闭环。权威源：[`docs/development_workflow.md`](../../../docs/development_workflow.md) §7。

## 触发规则

| 触发器 | 行为 |
|--------|------|
| `scripts/` 或 `.opencode/` 下 `.py` / `.mjs` / `.js` 文件需改动 | 启动本工作流 |
| 用户说 `改一下 session-worktree-mgr.py` / `修一下 status.py` 等 | 启动本工作流 |
| 工具链 bug 或功能增强 | 启动本工作流 |

## 闭环流程

```text
① 用户（或 PM 自检）发现问题
   ↓
② PM 简要分析 → 现象 + 根因假设 + 涉及文件
   ↓
③ dispatch General（main session）定位 → 读代码 + 搜索调用链 → 返回根因 + 方案 + 改动量预估
   ↓
④ PM 核验 General 定位结果 → 向用户报告根因 + 修复方案 + 风险
   ↓  (用户提出新问题 → 回到②)
   ↓  (用户确认)
⑤ dispatch General（同一 session）修复 + 自验 + 合入
   ├── ≤30 行 → cherry-pick 到 iter → push
   └── >30 行 → push 分支 → gh pr create（PR 方向：fix 分支 → iter）
   ↓
⑥ 用户验证 / merge PR
   ↓
⑦ 完成
```

## Step ③ — General 定位（dispatch 1/2）

```
定位 <bug/问题>：
① 验证根因 — <现象描述>。读涉及文件代码，确认问题点。
② 输出：根因证据（file:line）+ 修复方案 + 涉及文件 + 改动量预估。

背景：<问题描述>
涉及文件：<file:line>
```

## Step ⑤ — General 修复 + 自验 + 合入（dispatch 2/2）

**前置门禁**：dispatch 前确保 iter 干净（`git status --porcelain` 无输出）。PM 域文件先 commit + push。

```
按以下工作流执行（承接上次定位结论）：

① 修复：切分支 fix-<slug> → 直接改代码。
② 自验：ruff check + mypy + 相关 pytest，逐项输出实际结果。
③ 合入：
  - ≤30 行 → cherry-pick 到 iter → push iter → 删 fix 分支
  - >30 行 → push 分支 → gh pr create（PR 方向：fix 分支 → iter）

回报：合入方式 + 改动文件清单 + 自验结果。
方案：<已确认的方案>
```

## 核心约束

- **General 一人完成**：定位→修复→自验→合入，不引入其他 agent
- **session 复用**：dispatch 1 和 dispatch 2 用同一 General main session
- **改动量阈值**：≤30 行 cherry-pick 到 iter · >30 行 PR（fix 分支 → iter）
- **合入目标**：所有 main agent 产出只推 iter，定期 iter→main PR 同步（OC2.6）
- **PM 不写代码**：所有代码修改由 General 执行

## 适用 vs 不适用

| 适用 | 不适用 |
|------|--------|
| `scripts/session-worktree-mgr.py` | `src/` `tests/` `database/`（走标准 7 步） |
| `.opencode/` 下 `.py` `.mjs` `.js` | `.opencode/` 下 `*.md`（走 `pm-workflow-doc-refactor`） |
| 工具链 bug ≤500 行 | `.pm/` `docs/` `*.md`（PM 直推 iter，OC2.7） |
| 工具链 bug ≤500 行 | 工具链改动 >500 行（升格标准 7 步） |
