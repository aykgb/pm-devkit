---
name: pm-workflow-init
description: PM 专用：Workflow I(nit) 项目初始化。优先使用 `.pm/devkit/scripts/pm-bootstrap.py`，降级为手工创建。
---

# pm-workflow-init

PM 专用 skill——项目首次初始化。基于 `.pm/devkit/pm_devkit_design.md` 定义的最小文件集合。

## 触发规则

| 触发器 | 行为 |
|--------|------|
| PM 首次在仓库中被调用 | 判断 `docs/implementation_plan.md` 是否存在 |
| Workflow S/N/F 找不到 `project_tasks.md` 或 `project_memory` | 降级初始化 |

## 语言

所有输出简体中文。

---

## 步骤

### 1. 前置检查

- `docs/implementation_plan.md` 是否存在
- `.pm/devkit/` submodule 是否存在
- 两者都存在 → **路径 A**（运行 bootstrap 脚本）
- 仅 plan 存在 → **路径 B**（手工创建）
- plan 不存在 → **路径 C**（降级初始化）

---

## 路径 A：Bootstrap（推荐 — 有 submodule）

### 2. 运行 bootstrap 脚本

```bash
python .pm/devkit/scripts/pm-bootstrap.py --from docs/
```

脚本自动：

- 读 `implementation_plan.md` → 推断项目名和当前 Phase
- 读 `pm.config.yaml`（如存在）→ 获取 Agent 名称
- 从 `.pm/devkit/templates/` 复制骨架到 `.pm/` + `.opencode/agents/`
- 创建 `docs/project_tasks.md`（含 Phase 信息）
- 创建 `docs/development_log.md`（空表）
- 创建 `pm.config.yaml`（首次）
- 创建 `.pm/chats/INDEX.md` + `docs/task_specs/` 目录

### 3. 输出 Bootstrap 报告

脚本输出哪些文件已创建、哪些已存在跳过。

**手动补充**：Agent 定义文件中的项目特定边界（安全规则、禁止行为）需开发者手动编辑。

---

## 路径 B：手工创建（无 submodule，有 plan）

按 Bootstrap 清单逐目录扫描，区分"已存在"和"缺失"：

```
.pm/
  project_memory.md          ✅/❌
  persona.md                 ✅/❌
  user_profile.md            ✅/❌
  user_behavior.md           ✅/❌

docs/
  operational_conventions.md ✅/❌

.opencode/agents/
  project-manager.md         ✅/❌
  <dev-agent>.md             ✅/❌
  <review-agent>.md          ✅/❌
  <qa-agent>.md              ✅/❌

docs/
  implementation_plan.md     ✅（已验证）
  architecture.md            ✅/❌
  data_model.md              ✅/❌
  development_workflow.md    ✅/❌
  project_tasks.md           ✅/❌
  development_log.md         ✅/❌
```

### 3. 创建缺失文件

按以下优先级创建：

#### 必须创建（PM 域，不覆盖已有）

| 文件 | 来源 | 创建方式 |
|------|------|---------|
| `.pm/project_memory.md` | `.pm/devkit/templates/pm/project_memory.md` | PM 直接写（填入项目名 + 当前 Phase） |
| `.pm/persona.md` | 模板项目 `persona.md` | PM 直接写（含管理模式 + 闲聊模式） |
| `.pm/user_profile.md` | 空模板（头部 + 分类占位） | PM 直接写 |
| `.pm/user_behavior.md` | 空模板（统计初始化为 0） | PM 直接写 |
| `.pm/chats/INDEX.md` | 空索引表 | PM 直接写 |
| `.pm/` `reflections/` 目录 | — | `mkdir -p` |
| `docs/operational_conventions.md` | `.pm/devkit/templates/pm/operational_conventions.md` | PM 直接写 |

`persona.md` 最小内容：

```markdown
# PM Persona

## Management Mode
（项目管理者：精准、结构驱动、结果导向）

## Casual Chat Mode
（轻松、有温度、有态度）

## Mode Switching
- 切入闲聊：情绪/话题偏离 → 自然切入
- 切回管理：task/Phase/PR → 自然切回
- 切回前回写闲聊记忆
```

#### 条件创建（如缺失且开发者未预置）

| 文件 | 条件 | 创建方式 |
|------|------|---------|
| `docs/development_workflow.md` | 模板项目有此文件 | 提示开发者：从模板复制或自行编写 |
| `docs/architecture.md` | — | 提示开发者：**需手动编写**（项目特有） |
| `docs/data_model.md` | — | 提示开发者：**需手动编写**（项目特有） |
| `.opencode/agents/<agent>.md` | — | 提示开发者：**需手动定义**（项目特有 Agent 名 + 边界） |

#### 必须创建（项目级）

| 文件 | 方式 |
|------|------|
| `docs/project_tasks.md` | PM 读 `implementation_plan.md` → 生成 Current Phase + Active TASK 表 + 空 Backlog |
| `docs/development_log.md` | PM 创建空表格（Date / Slug / Summary / PR） |
| `docs/task_specs/` 目录 | `mkdir -p` |

### 4. 生成首批任务

读 `implementation_plan.md`，找到最早未完成的 Phase：

- 将 Phase 的第一个 Batch 写入 Active TASK 表
- 如 Phase 已有 spec → 引用 `task_specs/` 路径
- 如 Phase 无 spec → 标记 Todo，等 Workflow N 触发 Spec 拆解

### 5. 输出 Bootstrap 报告

```markdown
## PM 系统初始化完成

### 已创建

| 文件 | 说明 |
|------|------|
| `.pm/project_memory.md` | Agent 系统 · 工作流（拆解/流水线/收口） · 工具操作经验 |
| `.pm/persona.md` | PM 双模式语气定义 |
| `.pm/user_profile.md` | 用户画像（空） |
| `.pm/user_behavior.md` | 行为日志（空） |
| `docs/operational_conventions.md` | OC0-OC5 操作约定 |
| `docs/project_tasks.md` | Phase N — Active TASK（第一批） |
| `docs/development_log.md` | 开发历史（空表） |

### 需手动创建

| 文件 | 原因 |
|------|------|
| `.opencode/agents/<dev-agent>.md` | 项目特有 Agent 名和边界 |
| `.opencode/agents/<review-agent>.md` | 同上 |
| `.opencode/agents/<qa-agent>.md` | 同上 |
| `docs/architecture.md` | 项目特有架构 |
| `docs/data_model.md` | 项目特有数据模型 |

### 当前状态

**Phase N — <name>**（首批任务已就位）
- Active TASK：<N> 个
- 下一步：开发者说 `下一步` → PM 启动 Workflow N
```

---

## 路径 C：降级（无 plan）

1. 不虚构完整计划
2. 搜索 README / docs / architecture / CLAUDE.md 获取上下文
3. 仅创建 PM 记忆域骨架（`.pm/` 文件 + `docs/development_log.md`）——这些与 plan 无关
4. 不创建 `project_tasks.md`（无 plan 无法拆任务）
5. 明确告知：

```markdown
## PM 记忆系统已初始化（降级模式）

未找到 `docs/implementation_plan.md`。已创建 PM 记忆域骨架文件，但**无法生成任务列表**。

### 下一步

1. 创建 `docs/implementation_plan.md`（Phase 拆解 + 验收标准）
2. 创建 `docs/architecture.md`（系统架构）
3. 完成后说 `下一步` → PM 启动标准初始化
```

---

## 关键边界

- **不覆盖已存在文件**——任何 `✅` 标记的文件原样保留
- **不虚构 Agent 定义**——Agent 名和边界是项目级设计决策，只提示开发者创建
- **不做 Spec 拆解**——那是 Workflow N 的职责。Init 只创建骨架和一个空的 task_specs 目录
- **不生成 architecture.md / data_model.md**——项目特有，开发者手动编写
