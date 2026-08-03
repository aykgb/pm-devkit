---
name: pm-workflow-iterate
description: PM 专用：Workflow i(terate) 轻量迭代闭环——bugfix + 小功能迭代。explore 定位（强制）→ PM 核验 → PM 出方案+用户确认 → 确认 General session（强制 overview）→ General 分支实现+自验+push → PM Windows 分支验证 → PM cherry-pick 回合 iter + 清理分支 → 收口（若涉及任务迁移）。
---

# pm-workflow-iterate

PM 专用 skill——轻量迭代闭环（bugfix + 小功能迭代）。不走标准 7 步流水线（无 Daedalus/Themis/QA/worktree pool），用最轻路径完成"发现→定位→方案→实现→验证→回合"。**PM 不写代码，所有代码修改由 General 执行。cherry-pick 回合 iter 由 PM 执行（Iterate 工作流例外，OC2.5 明确允许）；绝不 cherry-pick 到 main。**

## 触发规则

| 触发器 | 行为 |
|--------|------|
| 说 `修一下` / `查一下` / `fix` | 启动本工作流 |
| 说 `先看下问题` / `看下问题` / `看问题先` / `看问题` | 启动本工作流（先定位，不直接改） |
| Phase 验证中 PATCH/GET/worker 报错 | 启动本工作流 |
| 描述小功能改动（加按钮 / 改样式 / 调整布局 / 小交互优化） | 启动本工作流 |
| 改动量 ≤100 行前端 或 ≤200 行后端 | 启动本工作流 |

## 闭环流程

```text
① 用户描述需求 / 发现问题
   ↓
② PM 委派 explore 只读定位（强制，不可跳过）
   ↓
③ PM 二次核验定位结论（读 explore 定位的关键代码行，验证自洽性）
   ↓
④ PM 出 2-3 个方案 + 利弊分析 + 推荐，与用户讨论确认（强制，不可跳过）
   ↓
⑤-a PM 确认 General session（overview --main，不凭记忆不猜参数）
   ↓
⑤-b PM 委派 General（session dispatch）在 fix 分支复核 + 实现 + 自验 + push
   ↓
⑥ PM 在 Windows 上拉取 fix 分支验证
   ↓
⑦ 验证通过 → PM cherry-pick 回合 iter + 同步 Windows + 清理 fix 分支
   ↓
⑧ PM 加载 pm-workflow-finish skill → pm_finish_task.py 收口

   仅当本次迭代涉及 Active TASK 迁移时才执行步骤 ⑧（如 P<N>-T<M> 从 Active TASK → Recently Completed）。
   纯 bugfix / 小优化（无独立任务编号）跳过此步。
```

## 步骤 ②：explore 定位（强制）

**不可跳过**——即使 PM 已从上下文推断出结论，也必须经 explore 只读确认（防 PM 误判）。

PM 将问题/需求 + 相关文件路径委派给 explore（`task(subagent_type="explore")`）：

```text
问题/需求：{描述，含相关代码段和文件路径}
请定位关键代码位置和交互链。
关注：{相关文件/函数/事件绑定的完整行为}
返回：定位结论 + file:line 证据 + 调用链/事件链。
```

explore 返回后，PM 进入步骤 ③ 核验。

## 步骤 ③：PM 核验

PM 对照 explore 结论，**读其定位的关键代码行**做逻辑自洽检查：

- 结论是否解释了所有观察到的现象？
- 调用链/事件链是否完整（输入→中间环节→输出）？
- explore 指向的 file:line 是否确实支撑其结论？
- explore 结论是否与已知架构约束冲突（CLAUDE.md / OC）？
- 若 explore 结论不完整 → 补充提问或重新委派

**核验通过才进步骤 ④。**

## 步骤 ④：PM 出方案与用户讨论（强制）

**不可跳过**——PM 不得在核验后直接派 General 改代码。必须先出方案，等用户确认。

PM 给出 **2-3 个方案**，每个方案包含：

- **做法**：具体改什么、改哪些文件、改几行
- **利弊**：好处是什么、代价/风险是什么
- **安全影响**：是否触碰风控/状态机/QMT 边界、是否改变现有行为语义

PM 必须给出**明确推荐**，并说明理由。

格式：

```text
# 方案

## 定位结论回顾
{一句话}

## 方案 A：{标题}（推荐 / 可选）
- **做法**：{具体改动}
- **好处**：
- **代价/风险**：
- **安全边界**：

## 方案 B：{标题}
- ...

## 方案 C：{标题}
- ...

我建议选 **方案 X**，因为 {理由}。你定。
```

**用户确认后**才进步骤 ⑤。用户说"换个方案"或"再想想"→ 回到方案讨论。

### 方案设计原则

- 优先根因修复 / 最小改动，不推 workaround 除非用户明确要求
- 改动量与问题严重度匹配——小问题不推大方案
- 每个方案必须是完整可执行的，不是"可能要看看 X"
- 涉及日志级别/事件类型的方案必须说明审计影响
- 方案数量：根因清晰时 2 个，有歧义时 3 个，不编造凑数

## 步骤 ⑤：General 实现

### ⑤-a 确认 General session（强制，不可跳过）

**派发前必须先查——不凭记忆、不猜参数。**

```bash
# 1. 查当前 PM scope 下的 main sessions
python3 scripts/session-worktree-mgr.py overview --main
```

根据输出：
- **有 General 且 idle** → 复用 sid，进 ⑤-b
- **有 General 但 busy/streaming** → 等待，或 `session status <sid>` 确认
- **无 General** → `python3 scripts/session-worktree-mgr.py sessions create --agent General`
- **有但 scope 不对** → 创建新的（旧 session 属其他 PM session，不可复用）

铁律：
- 命令不确定时先 `-h`，不猜参数
- `session`（单）和 `sessions`（批量）不混用
- 确认 sid 属当前 PM scope 后才 dispatch

### ⑤-b 派发

```bash
python3 scripts/session-worktree-mgr.py session dispatch <sid> --agent General --task '
修改 {文件路径}：{需求简述}。

定位结论：{explore 结论摘要 + file:line}
方案：{用户确认的方案}

在 fix_<slug> 分支上工作（从 iter 切出）。自验：ruff + mypy；pytest 相关用例仅建议（General 不跑 pytest，测试由 QA/Janitor 执行）。push 分支。
commit 格式：<type>(<scope>): [General] <description>
' --yes
```

**关键约束**：
- General 一人完成复核→实现→自验（ruff + mypy；pytest 相关用例仅建议）+ push 分支，不引入其他 agent
- 回报：分支名 + commit hash + 改动摘要
- session 复用：同一个 General session 可反复用于多个迭代，减少 session 创建开销
- 改动量 >500 行 → 升格为 Formal Phase Task，走标准流水线

## 步骤 ⑥：PM 在 Windows 上分支验证

PM 通过 `remote-admin` MCP 在 Windows 上拉取 fix 分支并验证：

```text
remote_exec target=windows, script="git fetch origin <branch> && git checkout <branch>"
# 执行验证步骤（重启服务、curl 验证等）
```

验证通过 → 进步骤 ⑦。不通过 → 回到步骤 ⑤。

## 步骤 ⑦：cherry-pick 回合 iter + 清理

1. PM 执行 cherry-pick：

   ```bash
   git checkout iter && git pull && git cherry-pick <commit_hash> && git push
   ```

2. 同步 Windows（走 MCP `remote_exec`）：

   ```text
   remote_exec target=windows, script="git fetch origin iter && git reset --hard origin/iter"
   ```

3. 清理 fix 分支：

   ```bash
   git push origin --delete fix_<slug>
   git branch -D fix_<slug>
   ```

## 步骤 ⑧：收口（条件执行）

**仅当本次迭代涉及 Active TASK 迁移时执行**（如将 `P<N>-T<M>` 从 Active TASK 移至 Recently Completed）。纯 bugfix / 小优化（无独立任务编号）跳过此步。

PM 加载 [`pm-workflow-finish`](../pm-workflow-finish/SKILL.md) skill，执行 `pm_finish_task.py` 同步：
- `project_tasks.md`：Active TASK → Recently Completed 迁移
- `development_log.md`：插入条目
- `project_memory.md`：更新交互历史

**禁止手工 edit project_tasks.md / development_log.md**——统一走 `pm_finish_task.py`。

## PM 写代码边界

PM **不写任何代码**（`.py` / `.sql` / `.js` / `.html` / `.css` 等）。所有代码修改必须走步骤 ②-⑤。

PM 可直接修改的文件（不改代码逻辑）：
- `.pm/` / `.opencode/` / `docs/` 下的 `.md` 文件
- `runtime_config` 值（通过 API PATCH 或 psql）

## 与标准流水线对比

| | 本工作流 | 标准 7 步流水线 |
|---|---|---|
| 定位 | explore（subagent, 快，强制） | — |
| 实现 | General（session dispatch，复用） | Daedalus（pool dispatch） |
| 审查 | 无（PM 核验替代） | Themis |
| 测试 | General 自验（ruff + mypy；pytest 相关用例仅建议，QA/Janitor 执行） | QA |
| worktree | 无（General 在主仓目录 iter 分支切 fix_<slug>） | pool prepare + release |
| 回合 | PM cherry-pick → iter（iter workflow 例外） | PR → 开发者 merge |
| 收口 | pm_finish_task.py（仅任务迁移时） | pool release + pm_finish_task.py |
| 适用范围 | bugfix + 小功能迭代 ≤500 行 | Phase 任务代码开发 |
