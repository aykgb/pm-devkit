# Spec Breakdown & Batch Design

> Phase 目标 → task spec 拆解 → Batch 编组的标准流程与决策规则。

---

## 1. Spec 拆解流程

```
Phase 目标（impl_plan.md §N）
  → ≤2 task: PM 直接建 spec
  → ≥3 task: PM 派 General 拆解（给 impl_plan §N + 必读文档清单）
  → General 读源码 grep 验证 → 产出 task_specs/P<N>-T<M>.md + 代码预估
  → PM 核验 spec 逻辑自洽性
  → PM 编组 Batch（按 §2 标准）
  → Spec 门禁审视（所有任务强制，含 BL-*）
  → Fix loop → PASS → spec 就位，可派发
```

> 委托 General 完整拆解时 prompt：「加载 `pm-workflow-spec-breakdown` skill，按步骤产出 `docs/task_specs/` 文件，不做 Batch 编组和门禁——那是我（PM）的步骤。各 task spec 文件仅含设计片段，禁止大段代码，禁止改 `project_tasks.md`」。

### PM 派 General 拆解

PM 不读代码，不口述 spec 细节。给出：
- 目标 Phase（`implementation_plan.md` §N）
- 必读文档清单（按项目文档约定）
- 现有源码路径（General 自行 grep 验证行号/接口/函数名）

### General 产出规范

每个 `task_specs/P<N>-T<M>.md`：

| 字段 | 要求 |
| --- | --- |
| Goal | 一句话：实现什么、为什么现在做 |
| Steps | 可执行步骤，含文件路径和函数名（General 从源码验证） |
| Acceptance | 可验证条件（测试通过 / lint 全绿 / 具体行为断言） |
| Related files | 涉及文件清单 |
| 行号快照 | 时间戳 + "实施时用 grep 找位置" |
| 代码预估 | 净增行数 + 测试数 |

### PM 核验（不读代码）

- Goal 与 impl_plan.md 对齐
- Steps 依赖链完整（T<N> 产出 → T<N+1> 消费）
- Acceptance 可验证（非口号）
- 代码量在 Batch 阈值内

### Spec 门禁 + Fix Loop

- 触发：**所有任务**（`P<N>-T<N>` 和 `BL-*`）派发前必须经 Spec 门禁审视（OC3.5 强制门禁）
- 入口不限于 Workflow N——直接指令、PM 自行拆解、委托 General 全流程均可
- Fix loop：Blocker + High 全修 → 重审 → 循环直到 PASS
- **OC3.5 约束**：门禁报告中所有 findings（含 Blocker / High / Medium / Low）必须全部修复才能派发实现。特殊情况可在 PM 裁决后降级处理（如 Medium 转 Backlog），但必须显式决策并记录
- 简单批次（≤3 任务、纯文档、纯清扫）可跳过 R2/R3 轮

---

## 2. Batch 设计标准

> 一个 Batch = 一个 PR。

### 合并原则

| 优先级 | 原则 | 判断 |
| --- | --- | --- |
| 1 | 同文件 / 同 causal chain | 逻辑首尾相连 → 必须合并 |
| 2 | 共享测试边界 | 同组 fixture/mock → 合并 |
| 3 | 串行依赖 | T<N+1> 依赖 T<N> 接口 → 合并 |

### 拆分原则

| 优先级 | 原则 | 判断 |
| --- | --- | --- |
| 1 | 不同文件 / 不同 chain | 完全不交叠 → 可拆分 |
| 2 | 独立可测 | 可独立验证 → 可拆分 |
| 3 | 风险隔离 | 安全边界/核心逻辑 → 独立 PR |

### 阈值

| 指标 | 上限 | 理由 |
| --- | --- | --- |
| 单 Batch 行数 | ~2,000 | 超过审查疲劳，检出率降 |
| Batch task 数 | 3–7 | 与 Task Selection 对齐 |
| Phase PR 数 | 2–5 | 过多流水线开销；过少 fix loop 代价高 |

### 反模式

- **过度拆分**：5+ PR 每个 <500 行 → 流水线开销主导
- **过度合并**：1 PR >3,000 行 → fix loop 爆炸半径大
- **跨文件假内聚**：因"同属一个 Phase"合并不相关文件 → 审查遗漏

> 裁决权归开发者。
