---
name: pm-workflow-bugfix
description: PM 专用：Workflow B(ugfix) 快速 Bugfix 闭环。委派 @explore/@general 定位 → 创建 per-fix worktree + session pipeline → Daedalus→Themis→Argus→QA 串行 → 收口合并 → Windows 同步。
---

# pm-workflow-bugfix

PM 专用 skill——快速 Bugfix 闭环。入口：委派 agent 定位 + 创建 fix worktree + session pipeline。

## 触发规则

| 触发器                                           | 行为         |
|--------------------------------------------------|--------------|
| 描述问题现象（哪个页面、什么错误、什么操作触发） | 启动本工作流 |
| 说 `修一下` / `查一下` / `fix`         | 启动本工作流 |
| Live Monitor 等页面出现异常状态                  | 启动本工作流 |

## 语言要求

| 输出类型             | 语言         |
|----------------------|--------------|
| devlog / 委派 prompt | **简体中文** |
| SKILL 注释           | **简体中文** |

## 闭环流程

```text
① 用户（人工）发现问题并描述
  ↓
② PM 委派 @explore / @general 定位根因（读代码 + 搜索相关调用链）
  ↓
③ PM 创建 per-fix worktree + session pipeline（见 docs/workflow_worktree_sessions_async.md）
  ↓
④ PM 派发 Daedalus 实现 → Themis review → Argus fix → QA verify（串行接力）
  ↓
⑤ PM merge PR → 清理 worktree + session
  ↓
⑥ 同步 Windows 环境（Workflow D Path 2）
  ↓
⑦ 用户验证
```

## 步骤 ②：定位（委派）

PM 不读代码。将问题现象 + 相关页面/组件名委派给 @explore（代码搜索）或 @general（综合分析）：

> 示例 prompt：`搜索 Live Monitor 页面 SSE 连接断开后的重连逻辑，定位为什么 autorecovery 不触发。关注 apps/web/src/components/panels/LiveMonitor.tsx 和 apps/api/routers/events.py。`

agent 返回根因假设 + 证据（file:line + 调用链）后 PM 进入步骤 ③。

## 步骤 ③：创建 Fix Worktree + Session Pipeline

PM 按 `docs/workflow_worktree_sessions_async.md` §4 执行：

1. `git fetch origin main && git worktree add wt_fix_<slug> origin/main -b fix-<slug>` — 创建 worktree
2. seed DuckDB mock 数据
3. `POST /session` ×N 创建 Daedalus / Themis / Argus / QA session，全部指向同一 worktree
4. `POST /watch` 注册到 sidecar

## 步骤 ④：管道接力

按照 OC5 管道串行派发：

| Stage | Agent    | 操作                          |
|-------|----------|-------------------------------|
| 1     | Daedalus | 实现 fix → commit → 报告完成  |
| 2     | Themis   | 代码审查 → 直接回复 PM |
| 3     | Argus    | 修 findings → commit          |
| 4     | QA       | 验证 + 补测 → QC 报告         |

PM 通过 `curl POST /prompt_async` 异步派发，通过 `curl GET /status` 轮询 idle 后接力。

## 步骤 ⑤：收口

- PM 汇报 PR 状态（CI / Themis / QA 结论），等开发者显式确认
- 确认后 `gh pr merge <PR#> --rebase --delete-branch` (OC2.6)
- `git worktree remove` + `DELETE /session` 清理

## 步骤 ⑥：同步 Windows + 验证

执行 Windows 代码同步（`ssh wangc@120.53.123.68 -p 19238 "git -C C:\\Users\\wangc\\xidi-minimal fetch origin main && git -C C:\\Users\\wangc\\xidi-minimal reset --hard origin/main"`），用户验证。

## 关键边界

- 简单前端 fix（不改 schema/manifest/API）→ 步骤 ③–④ 可简化为仅 Daedalus 或 Morpheus，跳过 Themis/QA
- 涉及 `tool_runtime/` / `qmt_gateway/` / 安全敏感代码 → 完整四阶段管道（Daedalus→Themis→Argus→QA）
- 用户决定是否需要 Themis review；不涉及上述敏感区域的小修小补可跳过
- **PM 不直接委派 agent 写代码**——通过 session API 派发，agent 在 worktree 中自检后开始
- 复杂重构不在此流程——走 Workflow N(ext) 建任务 → OC5 Feature 管道

## 管道规则

| 场景                                             | 简化                                    |
|--------------------------------------------------|-----------------------------------------|
| 简单前端 fix（不改 schema/manifest/API）         | 跳过 Themis/QA，仅 Daedalus 或 Morpheus |
| 涉及 `tool_runtime/` / `qmt_gateway/` / 安全敏感 | 完整四阶段（Daedalus→Themis→Argus→QA）  |

详细调度流程见 `docs/workflow_worktree_sessions_async.md`。
