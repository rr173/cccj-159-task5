# API 契约

基础路径 `http://<host>:8080`，全部 JSON。错误响应统一为：

```json
{ "error": "CODE", "message": "可读说明", "detail": { "reason": "..." } }
```

## 状态与原因码

- **ECO 状态**：`DRAFT / FROZEN / QUEUED / REBASE_REQUIRED / RELEASING / RELEASED / MERGED / ABORTED`
- **检查状态**：`PENDING / BLOCKED / PASS / FAIL / SUPERSEDED`（门洁面可能显示 `MISSING`）
- **拒收原因 reason**：
  - `FINGERPRINT_MISMATCH` 结果指纹与冻结输入不符（拿错基线/输入被改）
  - `STALE_SUPERSEDED` run 已因纪元变化作废，结果迟到
  - `ALREADY_SUBMITTED` 同一 run 重复提交
  - `DEPENDENCY_BLOCKED` 依赖前置尚未全部 PASS
  - `ECO_NOT_ACTIVE` ECO 不在 FROZEN 态
  - `BASE_CONSTRAINT_CHANGED` 基础约束更新导致在验单整体作废（系统事件留痕）

### 验证执行租约（Runner 协调层）

与 ECO 服务配套的租约层负责“谁在哪一代次可以提交哪个检查”，**不改变** ECO 侧门禁：
租约凭据有效只代表“有权投递”，结果仍由原 `/results` 同一套裁决落库（指纹 / 纪元 /
依赖 / 重跑 / 签核 / 发布），接管不可能绕过任何一项。

- 租约字段：`state=HELD/COMPLETED/RELEASED/EXPIRED/INVALIDATED`、`gen`（租约代次，
  每次接管/超时重领 +1）、`runner_id`、`acquired_at/expires_at/remaining_seconds`。
- **凭据**：CLAIM/TAKEOVER 返回一次性 `lease_token`（16hex，
  `HMAC-SHA256(server_secret, run_id:gen:runner_id)`），服务端密钥落盘 `lease_meta`，
  库内只存 SHA-256 摘要；完成/续租必须携带 `gen + lease_token`。
- **领取幂等**：相同 `idempotency_key` 重放返回首次授权（HTTP 200、`replayed:true`）；
  幂等键被其他 runner/任务占用 → 409 `IDEMPOTENCY_KEY_REUSED`；并发领取同一任务只有一个
  成功，其余 409 `LEASE_HELD_BY_OTHER`。
- 租约层拒收原因：`STALE_LEASE_GEN`（代次过期：已被接管/重领，旧持有者迟到结果）、
  `LEASE_BAD_CREDENTIAL`（凭据不符/伪造，403）、`LEASE_EXPIRED`（TTL 过期）、
  `LEASE_NOT_EXPIRED`（未到期不可强制接管）、`LEASE_NOT_HELD_BY_RUNNER`（非持有者）、
  `LEASE_ALREADY_COMPLETED` / `LEASE_RELEASED`、`LEASE_NOT_FOUND`、
  `DEPENDENCY_NOT_READY`（依赖前置未 PASS，任务不在待领池）、`NO_CLAIMABLE_TASK`、
  `RUN_SUPERSEDED`；指纹/依赖等 ECO 门禁拒绝时**透传 ECO 原始原因码**
  （`FINGERPRINT_MISMATCH` / `STALE_SUPERSEDED` / `DEPENDENCY_BLOCKED` …）。
- 所有拒绝同时写入租约审计 `lease_event(event=REJECT)`（可按 run/事件/原因查询）
  与 ECO 侧 `rejected_result`。
- 进程重启：未过期租约恢复为 HELD（密钥落盘，旧凭据继续可用），已过期租约回收为
  EXPIRED 可接管；运行期每次租约操作还会惰性对账（超时回收、run 已结束/作废对账）。
- 操作页：`GET /ops`（待领任务、当前持有者与剩余时间、可接管池、接管历史、拒绝审计）。

### 临时代理签核授权

管理员可为两种签核角色（`TIMING_LEAD / METHODOLOGY_LEAD`）配置**临时代理人**及其
生效/失效时间窗，并可随时撤销。服务端在**签核动作发生的那一刻**重新校验授权
（而不是配置时或冻结时）：

- 未生效（当前时刻 < `valid_from`）→ `403 DELEGATION_NOT_STARTED`
- 已过期（当前时刻 ≥ `valid_until`，半开区间）→ `403 DELEGATION_EXPIRED`
- 已撤销（`status=REVOKED`）→ `403 DELEGATION_REVOKED`
- 授权角色与签核角色不符 → `403 DELEGATION_ROLE_MISMATCH`
- 授权代理人与操作人不符 → `403 DELEGATION_USER_MISMATCH`
- 授权记录不存在 → `404 DELEGATION_NOT_FOUND`

所有拒绝写入 `audit_log`（`signoff.denied`）。**同一自然人仍不得完成两个角色签核**，
直签与代理签合并判定（`409 DUAL_ROLE_CONFLICT`）。

代理签核成功后，签核记录固化当时的身份与代理链快照
（`signoff_kind=DELEGATE`、`delegation_id`、`auth_chain`：授权人、被代理负责人、
有效期、动作校验时刻），记录长期保留供审计；ECO 重新冻结（rebase / 合并 / 新基线发布）
仍使旧签核失效（标记 `voided_at`），新纪元须重新签核。

## 端点

### 基线与目录
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/baselines` | 发布基线列表 |
| GET | `/api/baselines/{id}` | 基线完整不可变快照 |
| GET | `/api/baselines/diff?from=&to=` | 结构化差异包 |
| GET | `/api/baselines/diff.zip?from=&to=` | ZIP 差异包（before/after/diff/矩阵/结果/签核）|
| GET | `/api/catalog` | 验证矩阵目录（必需性、依赖、指纹作用域）|
| GET | `/api/constraints` | 当前发布基线与工作约束版本 |
| GET | `/api/workbench` | 工作台聚合视图（含代理配置与当前可签人员）|
| GET | `/api/current-signers` | 每种角色当前可签代理（来源、有效期）|
| GET | `/api/admin/delegations` | 全部代理配置（含待生效/已过期/已撤销及实时状态）|
| GET | `/api/rejected-results` | 全部拒收/作废留痕 |

### ECO
| 方法 | 路径 | Body / 说明 |
|---|---|---|
| POST | `/api/ecos` | `{title, owner, affected_modules[], edits:{m:{netlist}}}` → 201 |
| POST | `/api/ecos/{id}/edits` | `{edits, by}` 仅 DRAFT/REBASE_REQUIRED |
| POST | `/api/ecos/{id}/freeze` | `{by}` 冲突时转 QUEUED，否则冻结+触发矩阵 |
| POST | `/api/ecos/{id}/abort` | `{by}` 释放锁并推动排队单晋升 |
| GET | `/api/ecos` / `/api/ecos/{id}` | 列表（含门禁摘要）/ 详情（含 gate/queue/release）|

### 验证（Runner 协议）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ecos/{id}/checks/{type}/inputs` | 拉冻结输入包（含 expected_fingerprint 供核对）|
| POST | `/api/ecos/{id}/checks/{type}/results` | `{run_id, fingerprint, verdict:PASS|FAIL, result, runner_id}` |
| POST | `/api/ecos/{id}/checks/{type}/rerun` | `{by}` 仅 FAIL 可重跑；旧尝试 SUPERSEDED，新 run 绑当前指纹 |

Runner **必须**对 `inputs` 自行按作用域计算 SHA-256 摘要（紧凑规范 JSON：
键排序、无空白、`ensure_ascii`），服务端不接受缺失/不符指纹。

### 验证执行租约（Runner 协调协议）
| 方法 | 路径 | Body / 说明 |
|---|---|---|
| POST | `/api/leases/claims` | `{runner_id, run_id?, idempotency_key?, ttl_seconds?}`；不给 `run_id` 则原子挑一个依赖已就绪任务；成功 201，幂等重放 200。返回 `{gen, lease_token, expires_at, remaining_seconds, taken_over?, previous_holder?}` |
| POST | `/api/leases/runs/{run_id}/renew` | `{gen, lease_token, runner_id, ttl_seconds?}` 续租；旧代次/过期/非持有者拒绝 |
| POST | `/api/leases/runs/{run_id}/complete` | `{gen, lease_token, fingerprint, verdict, result?, runner_id?}` 代次+凭据校验与 ECO 指纹/依赖门禁**同一事务**裁决；拒绝原因透传 ECO 原码 |
| POST | `/api/leases/runs/{run_id}/takeover` | `{runner_id, ttl_seconds?}` 仅 EXPIRED 可接管；gen+1，旧持有者凭据立即失效 |
| POST | `/api/leases/runs/{run_id}/release` | `{gen, lease_token, runner_id, note?}` 持有者主动释放，任务回到待领池 |
| GET | `/api/leases/board` | 看板：`claimable / held(剩余秒) / takeover_available / takeover_history / recent_rejects / all_leases` |
| GET | `/api/leases/runs/{run_id}` | 单条租约状态与完整事件流 |
| GET | `/api/leases/audit?run_id=&event=&reason=&limit=` | 租约可查询审计（CLAIM/RENEW/COMPLETE/RELEASE/EXPIRE/TAKEOVER/INVALIDATE/REJECT/RECOVER）|

`ttl_seconds` 取值 30~3600，默认 600（可用 `--lease-ttl` 调整）。典型 Runner 时序：

```
POST /claims            → gen=1, lease_token（仅本次返回，客户端保存）
GET  .../inputs → 自算指纹
POST .../complete       → {gen:1, lease_token, fingerprint, verdict}
（任务卡住超过 TTL）其他 runner: POST .../takeover → gen=2, 新 token
旧 runner 用 gen=1+旧 token 迟到提交 → 409 STALE_LEASE_GEN（lease_event 留痕）
```

### 签核与发布
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/ecos/{id}/signoff` | `{role, user, delegation_id?}` 门禁红时 409；同一人双签 409；带 `delegation_id` 时代理未生效/过期/撤销/角色或人不符 403/404 |
| POST | `/api/ecos/{id}/release` | `{by, failpoint?}` 门禁/双签不满足 409 且状态不变；`failpoint=AFTER_PHASE1` 注入崩溃 |
| POST | `/api/ecos/{id}/release/resume` | 从中断点幂等恢复 |

### 临时代理签核（管理员）
| 方法 | 路径 | Body / 说明 |
|---|---|---|
| POST | `/api/admin/delegations` | `{role, delegate_user, valid_from, valid_until, granted_by, principal_user?, note?}`；时间为 UTC ISO-8601。**相同角色+代理人+完全相同时间窗重复提交幂等**：返回既有记录（200，`deduped:true`），不生成重复有效记录；与该代理人该角色另一有效记录时间窗重叠 → 409 `DELEGATION_OVERLAP` |
| POST | `/api/admin/delegations/{id}/revoke` | `{revoked_by?}`（或 `by`）立即撤销；重复撤销幂等 |

签核记录字段：`signoff_kind=DIRECT|DELEGATE`、`delegation_id`、`auth_chain`（代理链快照）、
`voided_at/voided_reason`（被重新冻结等原因失效，记录保留）。
`GET /api/workbench` 额外包含 `delegations`（含实时 `state: PENDING/ACTIVE/EXPIRED/REVOKED`）
与 `current_signers`；工作台页面据此展示**当前可签人员、代理来源与有效期**。

### 冲突与基础约束
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/ecos/{src}/merge-into/{tgt}` | `{by, module_winners:{m:TARGET_WINS|SOURCE_WINS}}`；src 须 QUEUED、tgt 须 FROZEN 且确有未决阻塞；合并后 tgt 纪元+1 整轮重跑 |
| POST | `/api/constraints` | `{delta:{...}, by, note}` 存在 RELEASING 单时 409；否则在验 FROZEN 单全部转 REBASE_REQUIRED |
| POST | `/api/admin/reset` | 重置为种子数据 |

## 电路影响闭包（进入审核前自动复验范围分析）

操作页 `GET /impact`，API 前缀 `/api/impact`。系统接收**版本化拓扑**（模块实例关系
`nodes`、时钟域 `clock_domain`、传播边 `edges`、约束引用 `constraints`），依据
**修改起点**（origins）生成必须复验的模块闭包与每节点的检查类型，并为每条传播边
给出中文解释。设计人员可排除**可选候选**（必须填理由）；跨时钟、接口契约、策略
强制节点和修改起点**不可排除**。

### 拓扑与检查类型

- 节点 `nodes[]`：`id`、`name`、`clock_domain`（可多域 `CLK_A|CLK_B`）、
  `interface`（对外接口边界，强制）、`policy_forced/policy_reason`（策略强制）、
  固有 `checks`。
- 边 `edges[]` 的 `kind`：`instance`（父→子例化）/`dataflow`/`clock`/`reset`/
  `contract`（接口契约）/`policy`（策略）/`constraint`（约束，亦可由 constraints 隐式生成）；
  可选 `crossing`（显式跨时钟）、`registered`（寄存器切割，不判组合环）、`name`、`checks`。
- 传播活性：沿信号/时钟/复位/契约/策略/约束/正向例化边到达的为**变更传播(active)**；
  子模块修改时沿**反向例化边**上溯到的父/祖父为**结构上溯(structural)**——只做集成级
  SIM/TIMING 复验，可人工排除，且不再向下例化或沿父的信号边扇出到无关兄弟子树。
- 检查类型：`SIM/TIMING/CDC/CONTRACT/CONSTRAINT/POLICY/RESET/LEC`（起点带 LEC；
  跨时钟边两端带 CDC 且强制；契约/策略/约束目标强制）。

### 快照绑定与陈旧门禁（关键不变量）

- 快照 `closure_snapshot` 不可变，绑定**拓扑内容指纹 + 起点 + 变更摘要**（`content_fp`）。
- **相同输入返回原快照**（`reused:true`；曾被作废的同输入快照重算时重新提升 repromote）。
- **并发重算只保留一个当前版本**：同 change 串行化 + 唯一 `(change_id,content_fp)`，
  提交时把旧 `CURRENT` 整批置 `SUPERSEDED`，任意时刻 `is_current=1` 恰有一条。
- **拓扑上传新版本或编辑起点/摘要后，旧快照立即失效**（`current_snapshot_id=NULL`，
  `stale_reason` 给出 `v1 → v2` 等可操作原因）；旧结果**不得用于冻结或发布**，
  `POST .../ready` 与带 `change_id` 的 ECO `freeze` 一律 `409 CLOSURE_STALE_FOR_FREEZE`。
- 手动重算默认追赶最新拓扑（`change.retopology` 审计）；可显式 `topology_version`
  基于指定版本，或 `keep_version:true` 保持当前版本。

### 可操作错误

| 原因码 | 含义 | detail |
|---|---|---|
| `TOPOLOGY_INVALID` | 缺失/重复节点、坏边、未知边类型或检查类型（**聚合返回全部问题**） | `issues[]{where,problem,node,fix}` |
| `ORIGIN_NOT_FOUND` | 修改起点不在拓扑 | `missing[]`、`candidates{}`（编辑距离近似）、`fix` |
| `EMPTY_ORIGIN` | 未提供修改起点 | — |
| `CLOSURE_CYCLE` | 前向传播图存在长度≥3 的环 | `cycle[]`（环路径）、`fix`（打断或 `registered:true`）|
| `CLOSURE_TOO_LARGE` | 闭包超 `max_nodes`（默认 5000） | `reached/max_nodes/fix` |
| `REASON_REQUIRED` | 人工排除未填理由（400） | `node_id/fix` |
| `EXCLUSION_FORBIDDEN` | 排除跨时钟/接口契约/策略/起点强制节点（409） | `mandatory_reasons[]/fix` |
| `NODE_NOT_IN_CLOSURE` | 排除了不在当前闭包内的节点（404） | `closure_nodes[]` |
| `CLOSURE_STALE_FOR_FREEZE` | 拓扑/起点/摘要已变，旧闭包不得冻结发布（409） | `fix` |
| `NO_CURRENT_SNAPSHOT` | 尚无当前闭包，需先计算 | — |
| `CHANGE_FROZEN` | 已随 ECO 冻结，不可再改/重算/排除 | — |

### 审计：自动结论 + 人工覆盖

`impact_audit` 追加写，`GET /api/impact/audit?change_id=&action=&limit=` 可查：
`snapshot.compute/reuse/repromote`（自动）、`snapshot.invalidate/change.retopology`
（系统失效）、`exclude.add/exclude.revert`（人工覆盖/撤销）、`exclude.denied/
gate.denied`（强制冲突与门禁拒绝，**用独立已提交事务留痕，不随业务事务回滚**）、
`gate.ready/change.frozen`。

### 端点

| 方法 | 路径 | Body / 说明 |
|---|---|---|
| POST | `/api/impact/topologies` | `{topology:{nodes,edges,constraints}, by, note, topology_id?}`；内容相同幂等复用（200 `reused`），新版本 201 并使未冻结变更闭包失效 |
| GET | `/api/impact/topologies` / `.../{version}` | 版本列表 / 某版本完整拓扑 |
| POST | `/api/impact/changes` | `{title, owner, origins[], summary{}, topology_version?, max_nodes?}` → 201，**创建即自动计算闭包** |
| GET | `/api/impact/changes` / `.../{id}` | 列表（含 `stale`）/ 详情（含 `closure`、`stale_reason`）|
| POST | `/api/impact/changes/{id}/compute` | `{by, max_nodes?, keep_version?, topology_version?}` 重算（默认追赶最新拓扑）|
| POST | `/api/impact/changes/{id}/edit` | `{origins?, summary?, title?, by}` 编辑后旧快照失效 |
| POST | `/api/impact/changes/{id}/exclude` | `{node_id, reason(必填), by}` 排除可选候选 |
| POST | `/api/impact/changes/{id}/unexclude` | `{node_id, by}` 撤销人工排除 |
| POST | `/api/impact/changes/{id}/ready` | 冻结门禁：闭包须当前有效，通过后置 READY |
| GET | `/api/impact/snapshots/{id}` | 不可变快照（含 effective/excluded 节点、传播边解释、排除理由）|
| GET | `/api/impact/audit` | `?change_id=&action=&limit=` 自动结论与人工覆盖审计 |
| GET | `/api/impact/catalog` | 检查类型目录 |
| POST | `/api/ecos/{id}/freeze` | 额外支持 `{change_id}`：先过闭包门禁，冻结复验范围取闭包 effective 节点并集；过期闭包 `409 CLOSURE_STALE_FOR_FREEZE` |

## 独立设计签名封装 API

基础路径同主机，默认端口 `8090`，页面 `GET /envelopes`，API 前缀 `/api/envelope`。

### 密钥与签名者

| 方法 | 路径 | Body / 说明 |
|---|---|---|
| POST | `/api/envelope/keys/generate | `{}` → 一次性返回 `{kid, public_key, private_seed}` |
| POST | `/api/envelope/signers` | `{kid, signer, role, public_key, replaces_kid?}`；轮换会同时写旧 key 的 `REVOKE` 注册表事件 |
| POST | `/api/envelope/signers/{kid}/revoke` | `{reason?, by?}`；撤权后历史签名按 `signed_at` 验真，新签名拒绝 |
| GET | `/api/envelope/signers` | 当前每个 kid 最新注册表事件 |
| GET | `/api/envelope/root` | 注册表根公钥 |
| POST | `/api/envelope/root/rotate` | `{by?}`；新旧根交叉签名 ROTATE 事件，旧根历史可沿根链离线追溯 |

### 会话、构件与分块续传

| 方法 | 路径 | Body / 说明 |
|---|---|---|
| POST | `/api/envelope/sessions` | `{by, required_roles?, parent_envelope_id?, metadata?, session_id?}` |
| GET | `/api/envelope/sessions` / `.../sessions/{id}` | 会话、构件、上传、签名状态 |
| POST | `/api/envelope/sessions/{id}/artifacts` | `{slot, kind, content, detail?}`；kind 为 `netlist/constraints/modules/signoffs` |
| POST | `/api/envelope/sessions/{id}/uploads` | `{slot,kind,chunk_size,total_size}` 初始化分块上传 |
| POST | `/api/envelope/uploads/{id}/chunks/{index}` | 原始 body；可用 `X-Chunk-Hash` 声明 SHA-256。重复块幂等，同序号不同内容 409 |
| POST | `/api/envelope/uploads/{id}/chunks` | JSON：`{index,data_hex,chunk_hash?}`，便于小文件/测试 |
| GET | `/api/envelope/sessions/{sid}/uploads/{id}` | 返回已收块、`missing_indexes`、`received_size`，用于断点续传 |
| POST | `/api/envelope/uploads/{id}/complete` | 校验所有块并拼接为内容寻址 blob，再挂到构件槽位 |
| POST | `/api/envelope/sessions/{id}/prepare` | 计算 `content_hash/envelope_id`、分块叶哈希和 Merkle root；同内容并发只生成一个信封 |

### 签名、封口、发布与验真

| 方法 | 路径 | Body / 说明 |
|---|---|---|
| GET | `/api/envelope/sessions/{id}/challenge/{kid}` | 获取固定签名挑战 `{schema,envelope_id,kid,signed_at}` |
| POST | `/api/envelope/sessions/{id}/signatures` | `{signed_payload:challenge, signature}`；服务端验签时并入 content/merkle 上下文 |
| POST | `/api/envelope/sessions/{id}/seal` | 校验全部必需角色、至少两个自然人、签名和内容哈希 |
| POST | `/api/envelope/sessions/{id}/publish` | `{name,by}`；先写 PUBLISHING，再原子落发布收据，同名不同内容 409 `NAME_CONTENT_CONFLICT` |
| GET | `/api/envelope/envelopes` / `.../{id}` | 信封列表/详情 |
| GET | `/api/envelope/envelopes/{id}/proofs` | 构件、分块、签名全部 Merkle 包含证明 |
| GET | `/api/envelope/envelopes/{id}/verify` | 在线验真内容、注册表、签名、Merkle root |
| GET | `/api/envelope/envelopes/{id}/bundle` | 下载可离线验真 ZIP |
| POST | `/api/envelope/verify-offline` | `{bundle_hex, trusted_root_kid?}` 验证 ZIP 内容 |
| GET | `/api/envelope/lineage?name=或envelope_id=` | 父信封链与发布名 |
| GET | `/api/envelope/publications` / `/audit` | 发布列表与篡改/拒绝审计 |

ZIP 内固定包含 `MANIFEST.json`、`SIGNATURES.json`、`TRUST.json`、`PROOFS.json`
和 `artifacts/*`。离线验真会重算每个 artifact 与分块 SHA-256，校验注册表根签名、
签名发生时密钥是否有效、签名角色/人是否匹配，以及最终 Merkle root；任一项失败返回
`OFFLINE_VERIFICATION_FAILED.detail.reasons`。

## 典型时序（一次合规发布）

```
create → freeze → (Runner: inputs→自算指纹→results) ×依赖图
       → TIMING_LEAD signoff → METHODOLOGY_LEAD signoff(不同人)
       → release(阶段1锁状态 → 阶段2原子写 B-R{n}、推进 trunk)
```

## 典型时序（迟到结果）

```
ECO FROZEN 全绿 → POST /api/constraints 收紧时钟
  → ECO=REBASE_REQUIRED, 旧 run 全 SUPERSEDED, 旧签核失效
迟到 Runner 回传旧 run_id → 409 STALE_SUPERSEDED（rejected_result 留痕）
工程师 freeze(refreeze, epoch=2) → 矩阵重跑 → 双签 → 才能发布
```
