# Runtime Architecture

> PM 系统的运行时引擎——基于 OpenCode + worktree/session pool 的异步调度模型。

---

## 1. 引擎分层

```
┌──────────────────────────────────────────────────────────┐
│                    pm CLI                                 │
│  pm init    pm bootstrap    pm status    pm dispatch      │
└────────────┬─────────────────────────────────────────────┘
             │
┌────────────▼─────────────────────────────────────────────┐
│              OpenCode Runtime Engine                      │
│                                                           │
│  ┌─────────────────┐  ┌──────────────────┐               │
│  │ worktree pool   │  │  session manager  │               │
│  │ (N× git wt)     │  │  (lifecycle +     │               │
│  │ prepare/release │  │   idle-watch)     │               │
│  └────────┬────────┘  └────────┬─────────┘               │
│           │                    │                          │
│           ▼                    ▼                          │
│  ┌────────────────────────────────────────┐              │
│  │         Agent Dispatch Layer            │              │
│  │                                         │              │
│  │  pool dispatch wt_N <dev-agent> --task "..." --yes    │
│  │  session dispatch <sid> General / 门禁 / 审查 / 杂务   │
│  │  task(subagent_type="explore"/"WebSearch")             │
│  └────────────────────────────────────────┘              │
└──────────────────────────────────────────────────────────┘
             │
┌────────────▼─────────────────────────────────────────────┐
│              PM Agent (Orchestrator)                      │
│                                                           │
│  Skills → Workflows → Dispatch → Idle-watch → 收口       │
│  Memory (.pm/) → project_tasks.md → devlog               │
└──────────────────────────────────────────────────────────┘
```

## 2. 异步调度

PM 不"等" Agent 完成——派发后 Agent 在独立 worktree 异步工作：

```
PM: "做 P<N>-T<M>" → dispatch <dev-agent> (wt_1)
  → <dev-agent> 在 wt_1 异步工作（读 spec → 写代码 → commit → create PR）
  → <dev-agent> busy → idle
  → idle-watch 通知 PM session
  → PM 自动推进: dispatch 审查 → dispatch QA → 汇报 merge
```

**开发者在流程中的参与点只有三个**：说"下一步"选方向、说"开工"批执行、merge PR。其余全部由 PM 自动调度。

## 3. Worktree Pool 管理

```
idle wt_1  → prepare → checkout feat_xxx → busy
  → 开发 session 启动 → busy
  → 开发 idle → 审查 session 启动 → busy
  → 审查 idle → QA session 启动 → busy
  → QA idle → PR merged → release → idle
```

会话复用：审查和 QA 复用同一个 worktree 和 session，无需重新 prepare。
资源泄漏防护：任何时候操作 worktree 后必须显式 release。idle-watch 是兜底。

## 4. 最简模式（无 Pool）

```
main worktree
  → PM + General（main session）
  → 开发者在自己的 IDE 中手动开发
  → PM 仍调度 Spec 拆解、Batch 编排、门禁审视
  → 审查/测试由开发者手动执行或 CI 自动跑
```

最简模式下仍提供 I/S/N/F/L/M/R 等核心工作流，但依赖 pool 的 Workflow（标准 7 步 D(evelop)、T(oolchain) PR 路径）不可用。PM 角色从"全自动调度器"退化为"结构化引导者"。

## 5. 迭代自愈

PM 在开发迭代中自动检测缺失能力并补全：

| 检测点 | 缺失 | PM 自动动作 |
| --- | --- | --- |
| Workflow N | `task_specs/` 为空 | 自动派 General 拆解 spec |
| Workflow N | 审查 Agent 未定义 | 从模板渲染 → 提示开发者确认后创建 |
| Workflow N | 缺少 `frontend_spec.md` 但 Phase 涉及前端 | 提示开发者先编写 |
| Workflow S | 健康检查项过时 | 自动更新 status.py 配置 |
| Workflow F | `devlog` 表不存在 | 自动创建空表 |
| 首次 dispatch | worktree pool 未初始化 | 检测 → 提示 `pool init --size 10` |
| Phase 收口 | 下一 Phase spec 未拆 | 自动触发 Spec 拆解 |

### 设计原则

- **默认最小，按需生长**：新项目从最简模式（PM + General）开始
- **检测优于询问**：PM 主动检测缺失，不给开发者开问题清单
- **自动优于手动**：能自动创建的一律自动创建
- **危险操作必确认**：创建 Agent 定义、修改 CI、启用安全边界先征询

### Bootstrap vs 自愈

`pm bootstrap` 生成初始骨架（.pm/ + .opencode/ + project_tasks.md），但不追求"一次性完美"——只创建最小可用集。剩余能力由迭代自愈按需补全。bootstrap 解决"从零到一"，自愈解决"从一到全"。
