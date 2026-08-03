# Pipeline System

> 两条开发流水线：标准 7 步（业务代码）与工具链闭环（工具链代码）。

---

## 1. 选择速查

| 改动 | 流程 | worktree | 审查 |
| --- | --- | --- | --- |
| 业务代码 | **标准 7 步** | pool wt | 审查 Agent + Codex bot |
| 工具链代码 | **工具链闭环**（General 一人） | main wt | 跳过 |
| PM 域文档 | PM 直推 iter | main wt | 跳过 |
| Bugfix / 小功能（≤200 行） | **轻量迭代**（explore 定位 → General 分支修复 → cherry-pick/PR） | iter 分支 | 可选 |

> **裁减（OC5.8）**：开发者可按复杂度跳过审查/QA/Codex 步骤。

## 2. 标准 7 步

```
① pool prepare → ② 开发 Agent → ③ 审查 Agent → ③.5 Codex（PR review bot）
  → ④ QA Agent → ⑤ 开发者 merge → ⑥ pool release → ⑦ PM 收口
```

**3 个铁律**：同 wt 串行不 release / 审查 P0 零容忍 / PM 禁止 merge。

详细步骤 + 失败回滚表见项目 `development_workflow.md`。

## 2.5 轻量迭代（Workflow i(terate)）

覆盖 bugfix + 小功能迭代，改动量 ≤200 行。不走标准 7 步，无 Daedalus/Themis/QA/worktree pool。

```
explore 定位（强制）→ PM 核验 → PM 出方案+用户确认
  → General 分支实现+自验+push（iter）
  → PM Windows 分支验证 → cherry-pick 回合 iter → 清理分支 → 收口
```

详见 `pm-workflow-iterate` skill。

## 3. 工具链闭环

General 一人定位→修复→验证→PR。≤30 行 cherry-pick 直推，>30 行 PR。

### Dispatch 1 — 定位

```
定位 <bug/问题>：
① 验证根因 — <现象描述>。读涉及文件代码，确认问题点。
② 输出：根因证据（file:line）+ 修复方案 + 涉及文件 + 改动量预估。
只定位，不改代码。

背景：<问题描述>
涉及文件：<file:line>
```

### Dispatch 2 — 修复 + 自验 + 合入

```
按以下工作流执行（承接上次定位结论，直接改代码）：

① 修复：切分支 fix-<slug> → 直接改代码。
② 自验：执行自验清单，逐项输出实际结果。
③ 合入：pre-commit → commit。
  - ≤30 行 → cherry-pick 到 main → push main → 删 fix 分支
  - >30 行 → push → gh pr create

回报：合入方式 + 改动文件清单 + 自验结果。

方案：<已确认的方案>
涉及文件：<file:line>
```

### General 自验标准

| 检查项 | 说明 |
| --- | --- |
| 工具可运行 | `<entry cmd> -h` 正常 |
| 核心子命令不崩 | 至少跑 3 个高频命令 |
| 相关命令覆盖 | 改动影响的子命令逐一验证 |
| 幂等性 | 同一命令跑两次，不报错不重复创建 |
| 向后兼容 | 旧数据/目录结构不崩溃 |

### 关键边界

- 不修改业务代码——业务代码走标准流水线
- 不影响核心安全——工具链代码不触及业务核心逻辑
- General 修复失败 ≤2 次重试——超过升级为正式 P<N>-T<M> 任务
- 改动量 ≤30 行 → cherry-pick 直推 iter；>30 行 → PR
- PM 禁止 merge——修复完成后 PM 汇报 PR，开发者合并
