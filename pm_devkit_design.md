# PM DevKit Design

> 通用 PM 调度系统——Agent 协作、开发流水线、Spec 拆解、Batch 编排、Workflow 路由、记忆体系。
>
> 位于 `.pm/devkit/`。以 git submodule 管理，项目无关，跨项目复用。

Last updated: 2026-08-03

---

## 架构全景图

```
┌──────────────────────────────────────────────────┐
│                  PM 调度引擎                       │
│                                                    │
│  impl_plan.md ──→ Spec 拆解 ──→ Batch 编组        │
│       │               │              │             │
│       │          General 执行    PM 按标准决策     │
│       ▼               ▼              ▼             │
│  project_tasks.md  ◀── task_specs/  + 执行计划     │
│       │                                            │
│       ▼                                            │
│  ┌─ 开发 Agent ─→ 审查 Agent ─→ QA Agent ─┐      │
│  │         (同 worktree，串行)              │      │
│  └────────── fix 循环 ← ────────────────┘        │
│       │                                            │
│       ▼                                            │
│  开发者 merge PR → PM 收口 → devlog 更新           │
└──────────────────────────────────────────────────┘
```

## 子系统索引

| 子系统 | 文件 | 内容 |
|--------|------|------|
| **Agent 体系** | [specs/agent-system.md](specs/agent-system.md) | 角色定义、两大派发模型、协作协议速查卡 |
| **开发流水线** | [specs/pipeline-system.md](specs/pipeline-system.md) | 标准 7 步、工具链闭环、General dispatch prompt |
| **Spec 拆解 & Batch** | [specs/spec-breakdown.md](specs/spec-breakdown.md) | Spec 拆解 8 步流程、Batch 合并/拆分原则、规模阈值 |
| **Workflow 体系** | [specs/workflow-system.md](specs/workflow-system.md) | I/S/N/B/D/i/T/F/L/M/R/C 12 个 Workflow、核心链、模式切换 |
| **运行时架构** | [specs/runtime-architecture.md](specs/runtime-architecture.md) | OpenCode 引擎分层、异步调度、Worktree Pool、迭代自愈 |
| **Session/Worktree 管理** | [specs/session_worktree_design.md](specs/session_worktree_design.md) | Worktree/Session 状态体系、生命周期、持久化、sidecar 集成 |
| **文件体系 & 约定** | [specs/file-conventions.md](specs/file-conventions.md) | PM 域目录结构、Task 状态机、提交权限、Backlog |

## 模板目录

| 目录 | 内容 |
|------|------|
| [templates/pm/](templates/pm/) | `CLAUDE.md` / `project_memory.md` / `operational_conventions.md` / `persona.md` / `pm.config.yaml` 骨架 |
| [templates/agents/](templates/agents/) | 9 个 Agent 定义骨架：PM / 开发 / 审查 / QA / 门禁 / 文档审查 / 前端 / 杂务 / WebSearch（含 `<agent-name>` 占位符） |

## 运行时

| 目录 | 内容 |
|------|------|
| [skills/](skills/) | 12 个 `pm-workflow-*` skill（PM 工作流引擎，bootstrap 时复制到项目 `.opencode/skills/`）：I(nit) / S(tatus) / N(ext) / B(reakdown) / D(evelop) / i(terate) / T(oolchain) / F(inish) / L(eisure) / M(emo) / R(eflection) / C(lean) |
| [runtime/](runtime/) | `session-worktree-mgr.py` / `session-status-server.mjs` / `check-codex.sh` / `session_worktree_usage.md`（bootstrap 时复制到项目 `scripts/`；`.md` 文档改复制到 `docs/session-worktree-usage.md`，per M2 修复） |
| [scripts/](scripts/) | `pm-bootstrap.py`（bootstrap 入口） |
| [plugins/](plugins/) | `pm-guardian.conf.json` + `pm-guardian.js`（PM 守护插件：PM session 启动/停止 hook + 配置 schema 校验。bootstrap 时按扩展名分发：`.js` → `.opencode/plugins/`，`.json` → `.pm/`） |

## OpenCode 内置 agent 关系

[templates/opencode.json](templates/opencode.json) 含 11 个 agent 配置，其中 3 个**不**在 [templates/agents/](templates/agents/) 模板目录：

| Agent | 类型 | 来源 | 与 PM 工作方式关系 |
|---|---|---|---|
| `WebSearch` | subagent（fire-and-forget）| OpenCode 内置 | PM 派发 subagent 拉取外部信息（市场数据/技术文档）——项目有独立 agent 定义（`.opencode/agents/websearch.md`），仅走 `task()` 路径 |
| `General` | main agent（session dispatch）| OpenCode 内置 | 工具链代码维护（per `file-conventions.md §4`）——始终走 `session dispatch`，不使用 `task()` 路径 |
| `explore` | subagent（main session dispatch）| OpenCode 内置 | 代码理解委派（per `pm.agent.md` Role 段）——仅走 `task()` 路径 |
| `plan` / `build` | subagent | OpenCode 内置 | **`opencode.json` 显式 `disable: true`**（per M3 决议）—— PM 自己 plan、开发 agent 接管 build，**不**用 OpenCode 内置 subagent，避免与 PM 委派模型冲突 |

> **派发路径（OC3.1）**：项目 agent（PM / Momus / Clio / Daedalus / Morpheus / Themis / QA / Janitor / WebSearch）走 `pool dispatch`（worktree agents）或 `session dispatch`（main agents）；OpenCode 内置 subagent（explore / WebSearch）走 `task()`。General 始终走 `session dispatch`（main agent），不使用 `task()` 路径。

## 决策记录

| 目录 | 内容 |
|------|------|
| [adr/](adr/) | ADR-001 bootstrap-vs-self-healing / ADR-002 batch-size-threshold |

## 两种接入路径

| | 手工路径 | 程序化路径 |
|---|---|---|
| **适合** | 首次学习、高度定制 | 快速启动、标准化团队 |
| **命令** | 按 templates/ 清单逐文件复制 | `pm bootstrap --from docs/` |
| **补充** | 运行时自愈在迭代中按需补全 | bootstrap 只建最小可用集 |

## 操作约定速查

| 要点 |
|------|
| 约定优先于 Prompt · 中文沟通 · 先想后动 |
| main 只做文档 · 分支 `feat_P<N>_T<M>` · PM 禁止 merge |
| 业务 Agent 只给 task ID · 审查 P0/P1 零容忍 · 门禁全修 |
| wt 操作后必须 release · 派发前先 push spec · 开发者可裁减流程 |
| 破坏性操作先征询 · 低优发现全量追踪入 Backlog |

> 权威源：`operational_conventions.md` · 完整 OC 速查见 `specs/file-conventions.md`

---

## 变更记录

| Date | Author | Change |
|------|--------|--------|
| 2026-08-03 | PM | v5：agents 模板 9 个 + skills 12 个对齐 canonical；路径 `.pm/design/` → `.pm/devkit/`；派发路径（OC3.1）补充 |
| 2026-06-13 | PM | v4：拆为 6 个 spec 文件 + templates/ + 总纲缩减为索引 |
| 2026-06-13 | PM | v3：加程序化路径、运行时架构、迭代自愈矩阵 |
| 2026-06-13 | PM | v2：引导式结构 + 附录骨架模板 + Core Flow Diagram |
| 2026-06-13 | PM | 首版：Agent 体系 + 流水线 + Spec 拆解 + Batch + Workflow + 文件体系 |
