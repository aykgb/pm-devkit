# File System & Conventions

> PM 系统的文件体系、Task 状态机、提交权限与 Backlog 管理。

---

## 1. PM 域文件体系

```
.pm/                          ← 记忆系统（submodule 内）
  project_memory.md           ← 核心：Agent 派发 · 拆解 · Batch · 交互历史
  persona.md                  ← PM 语气
  user_profile.md             ← 用户画像
  user_behavior.md            ← 行为日志
  chats/ + reflections/       ← 闲聊 + 反思
  devkit/                     ← PM 自身设计（submodule，项目无关）

docs/                         ← 项目文档（OC 源文件也在此）
  operational_conventions.md  ← 操作约定（OC0-OC5，权威源）
  impl_plan.md                ← Phase 拆解（唯一 Source of Truth）
  architecture.md + data_model.md
  development_workflow.md     ← 流水线权威
  project_tasks.md            ← 任务状态索引
  development_log.md          ← 历史单表
  task_specs/ + review_report/

.opencode/                    ← Agent 定义 + Skills
  agents/*.md                 ← N 个 agent 定义
  skills/pm-workflow-*/       ← I/S/N/B/D/i/T/F/L/M/R/C 12 个工作流
```

## 2. 角色矩阵

| 场景 | 读 | 写 |
| --- | --- | --- |
| PM 派发 | memory §Agent 派发 | — |
| PM 拆 spec | memory §Spec 拆解 | 派 General 写 task_specs |
| PM 收口 | — | 收口脚本 --devlog |
| Agent 执行 | tasks + spec + workflow | — |

## 3. Task 状态机

```
Todo → In Progress → Done → Recently Completed
```

| 状态 | 含义 | 触发 |
| --- | --- | --- |
| Todo | spec 就位待派发 | 门禁 PASS |
| In Progress | 流水线进行中 | pool dispatch |
| Done | PR merged 待收口 | 开发者 merge |
| Recently Completed | 收口完成 | 收口脚本 |

任务类型：`P<N>-T<M>`（代码，走门禁+7 步）/ `P<N>-e2e`（集成测试）/ `BL-*`（清扫，不走门禁）。

## 4. 提交与分支

双分支模型：`main` 稳定发布分支（worktree pool 基于 main checkout feature，PR→main）；`iter` 常驻迭代分支（main agents 在此工作，快速修复直接提交 iter，定期 PR→main）。Windows 环境始终同步 `iter`。

| 谁 | 域 | 方式 |
| --- | --- | --- |
| PM | 记忆/文档域 | 直推 iter（OC2.7）；仅文档增删改可上 main |
| 开发 Agent | `src/` `tests/` `db/` | PR → main |
| General | 工具链代码 | fix/feat 分支 → PR → main |
| 任何人 | 破坏性操作 | 先征询 |

- 分支：`feat_P<N>_T<M>_<task>` / `fix-<slug>`
- Commit：`<type>(<scope>): [<agent>] <description>`
- PM 禁止 merge（OC2.5）；仅允许 iter 变基（`iter rebase main`）与 Iterate 工作流 cherry-pick → iter

## 5. Backlog 管理

来源：审查 P2 / 门禁 Med+Low / CI Bot P2 / QA 非阻断。
不进入：typo / 单行 Nit / PM 裁决忽略。
清扫：关/修/留三分类 → 杂务 Agent（Janitor）执行。原则：低优全量追踪（OC3.4）。

## 6. 文档关系图

```
implementation_plan.md         ← Phase 目标（Source of Truth）
        │
        ▼
pm_devkit_design.md            ← 怎么拆、怎么派、怎么跑（总纲）
  ├── agent-system.md          ← Agent 体系
  ├── pipeline-system.md       ← 流水线详细步骤
  ├── spec-breakdown.md        ← Spec 拆解 + Batch 设计
  ├── workflow-system.md       ← Workflow 路由
  └── runtime-architecture.md  ← 运行时引擎 + 自愈

project_tasks.md               ← 当前状态
  └── task_specs/              ← 每个 task 怎么验收

development_log.md             ← 历史
```
