# Worktree Session 管理架构

本文档描述 `scripts/session-worktree-mgr.py` 的 worktree / session 状态管理体系：架构全景、state 文件系统、核心生命周期、持久化模型、sidecar 集成，以及 overview / rewatch 一致性。

## 1. 架构全景

```text
                        PM session（main worktree）
                        ┌──────────────────────────────┐
                        │  ses_1469...（当前 PM）        │
                        │  ┌──────┬──────┬──────┬────┐  │
                        │  │ Clio │General│Janitor│Momus│── main agents
                        │  └──────┴──────┴──────┴────┘  │
                        └──────────────────────────────┘
                                    │ session dispatch
                                    ▼
           ┌──────────────────────────────────────────────────┐
           │              Pool（~/.worktrees/xidi-minimal）     │
           │                                                  │
           │  wt_1              wt_2              wt_N         │
           │  ┌──────────┐     ┌──────────┐     ┌──────────┐  │
           │  │ Daedalus │     │ Daedalus │     │ Daedalus │  │
           │  │ Themis   │     │ Themis   │     │ Themis   │  │
           │  │ QA       │     │ QA       │     │ QA       │  │
           │  │ Morpheus │     │ Morpheus │     │ Morpheus │  │
           │  └──────────┘     └──────────┘     └──────────┘  │
           │       ▲                                  │        │
           │       │      round-robin                 │        │
           │       └──────────────┼──────────────────┘        │
           │                  prepare                          │
           └──────────────────────────────────────────────────┘
                                   │ watch_session() × 6 call sites
                                   ▼
           ┌──────────────────────────────────────────────────┐
           │  Sidecar（session-status-server.mjs :4107）       │
           │  POST /watch ← 注册                              │
            │  GET /status → 状态查询（idle / busy / unwatch）  │
           │  DELETE /watch/:id ← 注销（无自动调用）           │
           └──────────────────────────────────────────────────┘
```

**三层结构**：
- **Main worktree**：PM session + 4 个 main agent（Clio / General / Momus / Janitor），跨任务复用，`session dispatch` 派发
- **Pool worktrees**：每个 wt_N 有 4 个 agent（Daedalus / Themis / QA / Morpheus），round-robin 抢占；session 由 dispatch 按需创建 + 复用（task_marker 决策树，详见 §3.4）
- **Sidecar**：独立进程追踪 session 状态，供 overview State 列查询

## 2. State 文件系统

### 2.1 物理布局

```
~/.worktrees/xidi-minimal/.state/
├── .pool_rr_index            ← round-robin 指针（wt_1 起）
├── wt_1.state               ← worktree 状态 + agent session ID
├── wt_2.state
├── ...
├── wt_N.state
└── sessions/
    └── <pm_session_id>/
        └── main.state        ← main agent session ID（按 PM session 隔离）
```

### 2.2 `wt_N.state` 字段

| 字段 | 写入时机 | 说明 |
| --- | --- | --- |
| `status` | prepare / release | `idle` 或 `busy` |
| `branch` | prepare / release | 当前 checkout 的分支名 |
| `wt_path` | prepare / release | worktree 物理路径 |
| `base_ref` | release | 基线 commit（通常 `origin/main`） |
| `initialized` | pool init | `"1"` 表示 worktree 已就绪 |
| `updated_at` | 每次 update_state | ISO 时间戳 |
| `task_marker` | prepare（每次新生成 `uuid.uuid4().hex`） | wt 当前任务标识；dispatch 比较该值与 `{agent}_task_marker` 判断任务边界 |
| `Daedalus_session_id` | dispatch auto-create | 当前 session ID（不再由 prepare 预创建，commit `4eedee3`） |
| `Themis_session_id` | dispatch auto-create | 同上 |
| `QA_session_id` | dispatch auto-create | 同上 |
| `Morpheus_session_id` | dispatch auto-create | 同上 |
| `*_session_title` | dispatch auto-create | session 标题（如 `wt_1-Daedalus`） |
| `{agent}_task_marker` | dispatch auto-create 时同步当前 `task_marker` | 该 agent 上次创建 session 时的任务标识；用于下次 dispatch 决策树判定 |

格式为 `key=value`，每行一条。空行和 `#` 开头行为注释。

**兼容性**：state 文件 schema 演进靠 `.get(key, "")` 兼容，旧 state 文件没有 `task_marker` / `{agent}_task_marker` 字段时，dispatch 决策树仍正确（marker 为空串，`"" == ""` 不触发 new-task；走 missing 或 stale 路径）。

### 2.3 `main.state` 字段

| 字段 | 写入时机 | 说明 |
| --- | --- | --- |
| `Clio_session_id` | sessions create | 当前 session ID |
| `General_session_id` | sessions create | 当前 session ID |
| `Janitor_session_id` | sessions create | 当前 session ID |
| `Momus_session_id` | sessions create | 当前 session ID |
| `*_session_title` | sessions create | session 标题 |

按 PM session 隔离：不同 PM 会话的 main agents 互不干扰。代码通过 `config.pm_session_id` 路由到 `sessions/<pm_sid>/main.state`。无 PM session 时回退到 `main.state`（全局）。

## 3. 核心生命周期

### 3.1 总览

```text
pool init ──→ [idle,无 sid] ──→ prepare ──→ [busy,task_marker] ──→ dispatch ──→ Themis/QA ──→ merge
                  ▲                                                            │
                  └──────────────────── release ─────────────────────────────┘
```

### 3.2 pool init：初始化 worktree

1. 循环 `i = 1..pool_size`：
   - `git worktree add --force wt_i <base_ref>`（或 `--force-copy` 从已有 worktree 复制）
   - 标记 `initialized=1`，`status=idle`
   - **不创建 session**（commit `4eedee3`）—— 历史版本会预创建 4 个 agent session，但会造成"未 dispatch 的 unknown session"污染 overview / sidecar /status
2. 全程持 `pool_lock`（mkdir 互斥锁）

**session 创建时机**：dispatch 首次调用某个 agent 时按需 auto-create（详见 §3.4）。

### 3.3 prepare：抢占 worktree + task_marker

1. **持 `pool_lock`**
2. **`find_idle_wt`**：从 `.pool_rr_index` 记录的起始位置开始，round-robin 扫描 status=`idle` + initialized 的 worktree，命中后更新指针到下一个
3. **checkout 任务分支**：`git checkout -b feat_PN_TM`（基于 `base_ref`）
4. **生成 `task_marker`**：`uuid.uuid4().hex` 写入 state（commit `16abebf`）
5. **`update_state`**：`status=busy`，`branch=<任务分支>`，`task_marker=<新 uuid>`
6. 打印 dispatch 命令提示

**关键变更（commit `653b827` + `16abebf`）**：`prepare` 不再调 `ensure_pool_sessions`，**session 创建职责完全下沉到 dispatch**（auto-create on first use）；`pool repair` 也不再预创建 sessions。

**task_marker 设计意图**：dispatch 比较 `state.{agent}_task_marker` 与 `state.task_marker`，不等则触发"new-task"重建。uuid 保证同 branch 重 prepare（release → prepare 同分支）也触发 new-task，因为 uuid 每次新生成。

**并发安全**：`pool_lock` 确保同一时刻只有一个 prepare/release/repair 操作。

### 3.4 dispatch：派发 agent + task_marker 决策树

1. 查 state `{agent}_session_id`，取 `sid`
2. 查 sidecar `/status` 获取目标 session 当前状态（`idle` / `busy` / `unknown`）
3. `--require-no-busy` 模式下，`busy`/`streaming` 状态拒绝派发
4. **决策树**（commit `16abebf`）判断是否需要 archive 旧 sid + 创建新 sid：

   | 顺序 | 条件 | `reason` |
   |------|------|----------|
   | 1 | sid 在 OpenCode 找不到 | `missing` |
   | 2 | `agent_task_marker != wt_task_marker` | `new-task` |
   | 3 | `is_session_stale(ses, 1d)` | `stale (>1d)` |
   | 4 | else | 复用 sid |

5. 若需重建（`--yes` 模式）：
   - `ensure_session(recreate_existing=True)` → archive 旧 sid + 创建新 sid
   - `persist_session` 写 `state.{agent}_session_id`
   - `update_state` 写 `state.{agent}_task_marker = wt_task_marker`（同步 marker，下次同 agent 同任务走复用分支）
6. `POST /session/{sid}/prompt_async` 异步发送 prompt
7. 可选 `--notify-session`：spawn `idle-watch` 后台进程，监听 session busy→idle 后通知 PM

**dispatch 现在会修改 state 文件**（auto-create 路径）——与之前"dispatch 不修改 state"的旧语义相反。

### 3.5 release：释放 worktree

1. **持 `pool_lock`**
2. 检查 worktree 是否 dirty：dirty 时拒绝（除非 `--force`，则 `git reset --hard && git clean -fd`）
3. `git reset --hard <base_ref>` + `git checkout <base_ref>` 回到基线
4. **`cleanup_stale_sessions`**：删除 >1 天的旧 session（OpenCode session + state 条目），回收 storage
5. **`update_state`**：`status=idle`，`branch=""`

**关键**：release 只清理 state 指针和 stale session，不删除还在 1 天内的近期 session。这些 session 作为历史记录保留在 OpenCode 中，overview 可通过 recent filter 查看。

## 4. Session 持久化架构

### 4.1 两类持久化

| 类型 | 函数 | 写入目标 | 写入内容 |
| --- | --- | --- | --- |
| Worktree agent | `persist_session()` | `wt_N.state` | `agent_session_id` + `agent_session_title` |
| Main agent | `persist_main_session()` | `sessions/<pm_sid>/main.state` | 同上 |

**共同行为**：写入 state 后立即调用 `watch_session()` 注册到 sidecar。

### 4.2 ensure_pool_sessions 流程

**已无调用方**——`pool prepare` 在 commit `653b827` 之后、`pool repair` 在 commit `16abebf` 之后都不再调此函数。函数保留供未来可能的 `pool prewarm` 类命令使用。

```
ensure_pool_sessions(wt_id, agents, recreate_always=True):  # currently unused
  for agent in [Daedalus, Themis, QA, Morpheus]:
    ensure_session(agent):
      1. 查 state 文件中的 *_session_id
      2. 如果存在且未过期（≤1天），复用
      3. 如果过期或 recreate_always=True，删除旧 session → 创建新 session
    persist_session(wt_id, agent, session):
      → update_state(wt_id, {agent_session_id, agent_session_title})
      → watch_session(sid)
```

**当前 session 创建的唯一入口**：`cmd_dispatch` 的 auto-create 路径（详见 §3.4 决策树）。`pool repair` 和 `pool init` 都只做 wt 物理 + state 修复/初始化，不再预创建 sessions。

### 4.3 Stale session 清理

```
session 过期判定: time.updated > 1 day (STALE_SESSION_MS_DEFAULT)

release 时: cleanup_stale_sessions() → 删除过期 session 的 OpenCode 记录 + state 条目
dispatch 时（auto-create 路径）: is_session_stale(ses, 1d) → 触发 reason=stale (>1d) 重建
```

`prepare` 不再处理 stale session（commit `653b827`）—— stale 检测完全在 dispatch 决策树里完成（详见 §3.4）。

## 5. Round-Robin 与并发控制

### 5.1 Round-robin

```
.pool_rr_index: 持久化的起始索引（默认 1 = wt_1）

find_idle_wt():
  start = read .pool_rr_index
  for offset in 0..pool_size-1:
    i = ((start - 1 + offset) % pool_size) + 1
    if wt_i.status == "idle" AND wt_i.initialized:
      write .pool_rr_index = (i == pool_size ? 1 : i + 1)
      return wt_i
  fail("no idle initialized worktree")
```

每次 prepare 从上次命中的下一个位置开始扫描，保证 worktree 均匀使用。

### 5.2 Pool lock

```python
@contextmanager
def pool_lock():
    lock_dir = pool_dir / ".grab.lock"
    lock_dir.mkdir()  # mkdir 在 POSIX 上是原子的
    try: yield
    finally: lock_dir.rmdir()
```

基于目录创建的互斥锁——同一池目录下同时只有一个操作持有锁。覆盖 `prepare`、`release`、`pool init`、`pool repair`。

## 6. Sidecar 集成架构

### 6.1 启动与恢复

```text
service start sidecar:
  1. 启动 session-status-server.mjs（Node 进程，监听 :4107）
  2. 等待 health check 通过
  3. rewatch_all_sessions() → 从 state 文件恢复 watch 列表

sidecar 启动时:
  - OPENCODE_SESSION_IDS env var（当前未设置）→ 空 watch 表
  - 仅靠 rewatch_all_sessions 恢复
```

### 6.2 运行时集成点

```text
sessions create ──→ persist_session ──→ watch_session()
prepare         ──→ ensure_pool_sessions ──→ persist_session ──→ watch_session()
watch start     ──→ watch_session()
overview        ──→ collect_overview ──→ watch_session() 所有展示 session
```

**所有路径最终都落到同一入口**：`POST sidecar/watch {"sessionID": "ses_..."}`。

### 6.3 状态消费

```text
overview State 列:
  GET sidecar/status → {sessionID: "idle"|"busy"|"unwatch", ...}
  不在 status 中的 session → 显示 "unwatch"

watch:
  轮询 sidecar/status → 检测 busy→idle 跳变 → 通知 PM
```

### 6.4 僵尸 watch 问题

目前无自动 unwatch 机制。session 一旦注册到 sidecar 就永久追踪，直到：
- 手动 `DELETE /watch/:id`
- Sidecar 进程重启（watch 表清空，靠 rewatch 重建）

这导致 sidecar 的 watched 集合随时间膨胀，远超 overview 实际显示的 session 数。

## 7. Overview / Rewatch 一致性

### 7.1 两套发现逻辑

| | overview | rewatch_all_sessions |
| --- | --- | --- |
| session 来源 | OpenCode 实时 API（`collect_wt_sessions`） | state 文件（`wt_*.state` + `main.state`） |
| PM session | ✅ 显示 | ❌ 不在 state 文件 |
| orphan agent | 可通过 `--show-orphan` 显示 | ❌ 不在 state 文件 |
| 过滤 | `_apply_recent_filter` + `_limit_per_agent` | 仅 `_apply_recent_filter` |
| 清理 | watch 所有展示项 | 只增不减 |

**二者来源不同，结果天然不一致**。要让 status 与 overview 对齐，需要统一发现逻辑（都用 OpenCode API）并增加 unwatch 清理。

### 7.2 默认隐藏 unwatch / unknown + 占位行（commit `63d051c`）

`overview` 默认隐藏 sidecar 状态为 `unwatch` / `unknown` 的 session（避免视觉噪音）。当某个 wt 内的所有 session **全部**被隐藏时，overview 打印一行占位：

```
wt_5  <branch>  <commit>  clean  <Δ>  (无)  ...  (9 unwatch sessions hidden; pass --show-unwatch)
```

占位让 wt 不再"消失"——这是 commit `4eedee3` 之后的关键 UX 改进：全新 wt 的 sessions 都是 unwatch（从未 dispatch 过），默认隐藏后这些 wt 必须靠占位行才能被用户感知。

用户想看完整列表（含 unwatch）：`overview --show-unwatch`。

### 7.4 overview 渲染设计

**busy/streaming 加粗**：终端输出中 busy 和 streaming 状态的 session 加粗显示。目的是让运维一眼识别正在执行任务的 session，快速定位资源占用。

**TTY 检测**：加粗仅在终端输出时生效。管道或 JSON 输出不加粗——保证脚本解析不受 ANSI escape 污染。

### 7.5 Context 字段设计

overview 和 `session show` 的 Context 列显示的是 session **最近一次已完成 LLM 调用**的上下文窗口大小，而非 session 生命周期的累计 tokens。

**为什么不用累计值**：`session.tokens.input` 是历次调用的累加和，只增不减。一个复用了 10 次的老 session 累计值可能 500K+，但实际每次调用的上下文窗口只有 150K——累计值不能反映当前状态。

**为什么取最后一次已完成调用**：session 的最后一条 message 可能正在执行 tool call，此时没有 `step-finish`——取这个值无意义。回溯到最近一条已完成的 assistant message 的上下文窗口，才是当前有效值。

**tool call 进行中显示 0**：这是已知局限——不是 bug，是信息缺失。运维应等 busy→idle 后再查。

以实际运行数据为例（2026-06-12）：

| 指标 | 数量 |
| --- | --- |
| state 文件中的 session ID | 44 |
| rewatch 过滤后（3 天窗口） | 24 |
| overview 默认显示 | 36 |
| sidecar 实际 watch | 184 |

差距来自：① overview 发现 state 文件外的 session（PM、孤儿等）；② 6 个 `watch_session` 调用点长期累积，无清理。

## 8. 异常路径

| 场景 | 处理 |
| --- | --- |
| prepare 时无 idle worktree | `fail("no idle initialized worktree")` |
| release 时 worktree dirty | 拒绝（除非 `--force`：reset + clean） |
| pool dispatch 时 session busy | `--require-no-busy` 拒绝 |
| pool repair 时 worktree 损坏 | `--reset` / `--force-copy` 重建 |
| sidecar 重启 | `rewatch_all_sessions` 从 state 文件恢复 |
| state 文件丢失 | `read_state` 返回 `{}`，视为未初始化 |
| `.pool_rr_index` 丢失 | 重置为 1（wt_1 起扫） |
| pool lock 被占用 | `fail("another pool operation is running")` |

## 10. auto-compact 设计

### 10.1 动机

Main agent session（尤其是 Momus / General）跨多次 spec 拆解和修复循环复用，context 随对话历史不断膨胀。200K+ 后模型推理变慢、token 成本升高、缓存命中率下降。自动 compact 在 session 完成一轮任务后压缩上下文，减少后续派发的冷启动开销。

### 10.2 设计决策

**完整流程**：

```
busy→idle
  │
  ├─ ① [idle-notify:busy->idle] task done     ← 不延迟，立即发
  │
  ├─ fetch_session_context → > 300K?
  │   ├─ No → silent exit
  │   └─ Yes → POST /summarize (deepseek-v4-flash-free, 60s 超时)
  │         ├─ 失败 → [idle-notify:compact-failed] {error}, exit
  │         ├─ OK → POST ping prompt
  │         │   ├─ 失败 → [idle-notify:compact-failed] ping send failed, exit
  │         │   └─ OK → 继续 poll loop, 等 busy→idle
  │         │
  │         └─ busy→idle (ping 完成)
  │               ├─ session 非 idle → [idle-notify:compact-skipped] session reused
  │               └─ session idle → ② [idle-notify:compact-done] {before}K→{after}K
  └─ exit
```

**触发时机：busy→idle，而非 dispatch 前。** dispatch 前 compact 会延迟派发——用户在等待。busy→idle 后 compact 在后台完成，不阻塞下一轮任务。代价是 compact 期间 session 被复用需检测竞态。

**两阶段通知：先报 task done，后报 compact result。** 区分"任务完成了"和"压缩完成了"两个事件。第一条不延迟——PM 立刻知道可以下一步。第二条在 compact 完成后发送。

**ping-then-poll 而非直接读 context。** summarize 是 OpenCode 服务端异步操作，POST 返回 `true` 只表示请求已接受，上下文实际压缩在后续 LLM 调用时生效。compact 后发一条 ping prompt，等 busy→idle 再取 context，此时值才准确。

**独立模型，不污染 session。** compact 用 `deepseek-v4-flash-free`（免费轻量），session 自身模型不变。summarize 端点和 prompt_async 端点是独立的——前者压缩消息历史，后者处理用户 prompt。

**阈值 300K，不参数化。** Phase 1 硬编码足够——低于此值 compact 收益小（summarize 本身也有压缩损耗），高于此值收益明显。Phase 2+ 可按 agent 类型差异化（Momus 阈值低于 Daedalus）。

**竞态防御：compact 后查 session status。** PM 可能在第一条 notify 后立刻 dispatch 新任务到同一 session。compact 完成时 session 已 busy——此时不应发 "compact-done" 误导 PM。改为发 "compact-skipped"。

### 10.3 不触发场景

- **continuous 模式**：用于长期监听，不区分单次任务边界
- **initial-idle**：session 刚创建，无历史 context
- **idle-after-update**：dispatch 后 session 从未 busy（任务极短），context 无变化

### 10.4 与 session 重建的关系

已有 `MAX_MAIN_SESSION_CONTEXT = 200K` 在 `sessions create` 时触发 session 重建（hard delete + 全新 session）。重建 vs compact 的区别：

| | 重建 | compact |
|---|---|---|
| 触发点 | sessions create 时 | busy→idle 后 |
| 方式 | 删除旧 session，建全新 | 压缩消息历史，保留 session |
| 上下文 | 完全丢失 | 保留摘要 |
| 适用 | main agent 长期膨胀 | 所有 session 单次任务后

## 11. 文件索引

| 文件 | 角色 |
| --- | --- |
| `scripts/session-worktree-mgr.py` | 完整实现（L1~3042） |
| `scripts/session-status-server.mjs` | Sidecar 进程（Node，L1~648） |
| 命令操作参考 | 已内置 PM system prompt |
| `docs/development_workflow.md` | 7 步流水线流程 |
| `docs/operational_conventions.md` §OC5 | 约束级规则 |
