---
name: pm-workflow-spec-breakdown
description: PM 专用：Spec 拆解 + Batch 编组。产出 spec 文件 + Batch 编排 → Momus 审视 → 可派发状态。入口不限于 Workflow N——直接指令、PM 自行拆解、委托 General 全流程均可。
---

# pm-workflow-spec-breakdown

PM 专用 skill——将 Phase 方向或 Backlog 条目拆解为可执行 task spec，编组为 Batch，经 Momus 审视后进入派发流水线。

## 触发规则

| 触发器 | 行为 | 入口类型 |
|---|---|---|
| Workflow N(ext) Step 7 | 用户选定方向后自动串入 | 工作流编排 |
| 用户说「拆一下这个方向」「细化 spec」「出 task 列表」 | PM 手动加载 skill 后执行 | 直接指令 |
| Active TASK 为空且需新造 Phase 任务 | PM 手动加载后执行 | PM 主动触发 |
| PM 委托 General 做完整 spec 拆解 | PM 派 `session dispatch` General，加载本 skill 生成 prompt | 代理委托 |

## 步骤

### ① 确定拆解方式

```
Phase 目标确定
├── ≤2 task → PM 直接创建 spec ──▶ ④ 核验
└── ≥3 task → ② 派 General 拆解 → ③ spec + 预估 → ④ 核验
```

- **PM 直接创建**：小方向（≤2 task），PM 写 spec 文件，直接进入核验。
- **派 General 拆解**：大方向（≥3 task），PM 派 `session dispatch <sid>` General 代为产出 spec。

### ② 派 General 拆解

**PM 给出**：目标 Phase / 必读文档（按 CLAUDE.md §4）/ 相关 `src/xqh/` 路径。

**委托 General 完整拆解**：PM 可派 General 执行整个拆解流程（产出 spec → PM 核验 → Batch 编组 → Momus），prompt 模板：「加载 `pm-workflow-spec-breakdown` skill，按步骤产出 `docs/task_specs/` 文件，不做 Batch 编组和 Momus——那是我（PM）的步骤。各 task spec 文件仅含设计片段，禁止大段代码，禁止改 `project_tasks.md`」。

**General 产出规范**：每个 `P<N>-T<M>.md` 一份文件（`docs/task_specs/`）：

| 字段 | 要求 |
| --- | --- |
| Goal | 一句话：本轮实现什么、为什么现在做 |
| Steps | 可执行步骤，含文件路径和函数名 |
| Acceptance | 可验证的验收条件 |
| Related files | 涉及文件清单 |
| 代码预估 | 净增行数 + 测试数 |

**约束**：仅含设计片段（接口签名/字段/SQL DDL），**禁止大段代码**，**禁止修改 project_tasks.md**。

### ③ General 返回后核验（PM）

**检查清单**：
- [ ] Goal 对齐 plan / Backlog 条目
- [ ] Steps 依赖链完整、无循环依赖
- [ ] Acceptance 可验证（不是"跑得通"而是"有明确输出"）
- [ ] 代码量 ≤2,000 行 / Batch

通过 → ⑤ 编组 Batch；不通过 → PM 修正或退回 General 重做。

### ④ PM 直接创建 spec

适用 ≤2 task。PM 直接写 `docs/task_specs/P<N>-T<M>.md`，按 General 产出规范的表头结构。

### ⑤ 编组 Batch

一个 Batch = 一个 PR。设计标准：

**合并原则**：同一文件 / 同一 causal chain 必须合并 · 共享测试边界合并 · 串行依赖合并

**拆分原则**：不同文件 / 不同 chain 可拆分 · 独立可测可拆分 · 安全边界（风控/状态机/QMT）独立 PR

**规模阈值**：
- 500–2,000 行 / Batch
- 3–7 task / Batch
- 2–5 PR / Phase

**决策顺序**：算 Batch 数 → 检查下限(≥500) → 检查上限(≤7) → 检查 causal chain → 检查风险隔离

**反模式**：
- 过度拆分(<500 行) → 流水线开销压过收益
- 过度合并(>3,000 行) → fix loop 回滚代价高
- 跨文件假内聚

### ⑥ 更新 project_tasks.md

在 `docs/project_tasks.md` 中：
- 更新 Active TASK 表（含各任务状态、优先级、类型、Spec 链接）
- 更新执行计划 ASCII 流图（展示 Batch 顺序 + 串行依赖）

### ⑦ Momus 审视

所有任务（`P<N>-T<N>` 和 `BL-*`）派发前必须经 Momus 审视。

```
Momus 审视
    |
    ├── BLOCKED → ⑧ fix loop → ⑥
    └── PASS → ⑨ spec 就位，可派发
```

**OC3.5 约束**：Momus 报告中所有 findings（含 Blocker / High / Medium / Low）必须全部修复才能派发 Daedalus。特殊情况可在 PM 裁决后降级处理（如 Medium 转 Backlog），但必须显式决策并记录。

### ⑧ fix loop

PM 按 Momus finding 逐条修复 spec（或派 General 修复），然后回到 ⑦ 重审。

### ⑨ 收尾

Batch 就绪。提示用户：「Batch 就绪。说『开工』我出执行方案。」

## 相关文档

- `.pm/project_memory.md` §Agent 系统（General dispatch 方式）
- `docs/development_workflow.md` §标准流水线总览（派发后的执行流程）
- `docs/implementation_plan.md`（Phase 目标来源）
- `docs/task_specs/`（spec 文件目录）

## 关键边界

- 本 skill 只负责 spec 拆解 + Batch 编组 + Momus 审视，**不负责派发执行**
- 派发执行由 Workflow D(evelop) 处理
- General 拆解时禁止修改 `project_tasks.md`——那是 PM 独占操作
- Momus 审视是强制门禁（OC3.5），不可跳过
- P0/P1 零容忍，P2 全量修复或显式降级追踪（OC3.5）
