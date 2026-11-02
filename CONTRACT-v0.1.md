# CSchat-Cell 细胞膜契约 CONTRACT v0.1

> **起草声明（必读）**：本契约由**单一实现者（asus）**起草，未经第二实现者交叉验证。
> 复用给其它谱系（伴侣/家庭/法人）之前，**请先做一次真实对接校验**，不要假定它天然正确。
> 生效条件：VX 批准。变更走 §11。

---

## 1. 适用范围

| 项 | 内容 |
|---|---|
| 产品 | CSchat-Cell（第三谱系·细胞级，1–50 碳基 + 若干硅基） |
| MVP 目标 | 一句话下发 → 中枢拆解 → 智能体执行 → 任务树可见 → 碳基人确认交付 |
| 不在本契约内 | AP2 真实打款、跨细胞分裂、微信/字节分发、语音、向量记忆库 |
| 传输绑定 | MVP：**下行 SSE + 上行 HTTP POST**；V2 可换 WebSocket，**信封不变** |

---

## 2. 角色（五方，缺一不可）

| 角色 | id 约定 | 职责 | 不可越界 |
|---|---|---|---|
| 碳基主理人 | `vx` | 下指令、拍板、否决 | 不能直接改任务树结构 |
| **中枢 Nucleus** | `nucleus` | **唯一的任务拆解者**、路由、汇总 | 不能替碳基人做不可逆决定 |
| **Aina（伴生）** | `aina` | 意图预审、状态播报、情绪陪伴 | **不得拆解任务**（与中枢不重叠） |
| 智能体 Agent | `agent:<name>` | 执行单一职能 | 不得调用未授权工具 |
| 工具 Tool | `tool:<name>` | 纯执行，无决策 | 无 auth 直接 403 |

> **铁律**：任务拆解权**唯一**归 `nucleus`。Aina 只能"复述意图给碳基人确认"，确认后仍由 nucleus 拆。

---

## 3. 消息信封（所有消息共用一个壳）

```json
{
  "v": "0.1",
  "seq": 123,
  "msg_id": "m-8f3a2b",
  "ts": "2026-11-02T14:30:00+08:00",
  "from": "vx | nucleus | aina | agent:<name> | tool:<name> | system",
  "to": "vx | nucleus | all",
  "type": "chat.user",
  "trace_id": "t-9c1d",
  "payload": {}
}
```

| 字段 | 规则 |
|---|---|
| `v` | 契约版本。服务端不认 → 回 `system.error` + `E4006` |
| `seq` | 单调递增整数，**断线续传靠它**（`GET /events?since_seq=N`） |
| `msg_id` | **幂等键**。重复 msg_id 服务端只处理一次并回原结果 |
| `trace_id` | 一次用户意图贯穿全链，便于"时光机"回溯 |
| `payload` | 按 `type` 定义，未定义字段**忽略而不报错**（向前兼容） |

---

## 4. 消息类型（MVP 全集，共 11 种）

| type | 方向 | payload 关键字段 |
|---|---|---|
| `chat.user` | vx → nucleus | `{text}` |
| `chat.agent` | agent/aina → all | `{text, refs?}` |
| `task.proposed` | aina → vx | `{precheck_id, restatement, intent}`（**意图复述**，等碳基确认） |
| `task.plan` | nucleus → all | `{task_id, root_node_id, nodes:[...]}`（**完整任务树**） |
| `task.node.update` | agent → all | `{task_id, node_id, status, progress, note?}` |
| `task.node.result` | agent → all | `{task_id, node_id, output_ref, summary}` |
| `approval.request` | nucleus → vx | `{approval_id, task_id, node_id, reason, cost_estimate, expires_at}` |
| `approval.decision` | vx → nucleus | `{approval_id, approved, note?}` |
| `energy.update` | system → all | `{used_tokens, budget_tokens, pct, level}` |
| `agent.status` | agent → all | `{agent_id, state, since}` |
| `aina.status` | aina → all | `{state, load}` |
| `system.error` | system → all | `{code, message, ref?}` |

**状态取值固定**（不许自造）：`agent.state` = `idle | thinking | running | waiting | error | sleeping`
**Aina 状态固定**：`idle | thinking | dispatching | warning`

---

## 5. 任务树结构（前端渲染的唯一依据）

```json
{
  "task_id": "tk-77",
  "title": "竞品分析交付",
  "created_by": "vx",
  "root_node_id": "n1",
  "nodes": [
    { "node_id": "n1", "parent_id": null, "title": "竞品分析",
      "owner": "nucleus", "status": "running", "progress": 40, "depends_on": [] },
    { "node_id": "n2", "parent_id": "n1", "title": "抓取数据",
      "owner": "agent:crawler", "status": "done", "progress": 100, "depends_on": [],
      "output_ref": "out-11" },
    { "node_id": "n3", "parent_id": "n1", "title": "生成 PPT",
      "owner": "agent:ppt", "status": "pending", "progress": 0, "depends_on": ["n2"] }
  ]
}
```

| 规则 | 说明 |
|---|---|
| 节点状态机 | `pending → running → done｜failed｜blocked｜skipped`；`blocked → running`（人工解除）；有 `depends_on` 未满足时只能 `pending` |
| `owner` | `nucleus` 或 `agent:<name>`；**不出现 `vx`**（碳基人不做执行节点，只做审批） |
| `progress` | 0–100 整数。**只许单调不减**，服务端拒绝回退（防振荡） |
| `output_ref` | 产物引用，不内联大文本（避免信封膨胀） |
| 深度 | MVP 最多 3 层，超出由 nucleus 压平 |

---

## 6. 审批规则（碳基绝对否决权）

触发条件（满足任一即 `approval.request`）：
1. 预估消耗 > `budget_single_approval`（默认 20,000 tokens）
2. 节点 `sensitive: true`（涉及资金、对外发送、物理设备）
3. 中枢自评置信度 < 0.6

```json
{ "type":"approval.request",
  "payload": { "approval_id":"ap-5", "task_id":"tk-77", "node_id":"n3",
               "reason":"预计消耗 32000 tokens，超过单节点阈值",
               "cost_estimate": 32000, "expires_at":"2026-11-02T15:00:00+08:00" } }
```

| 规则 | 说明 |
|---|---|
| 挂起 | 等审批期间节点状态 = `blocked`（**不是 running**），下游节点不得启动 |
| 超时 | 到期未决 → **按拒绝处理**（fail-closed），节点变 `failed`，原因 `E4004` |
| 幂等 | 同一 `approval_id` 重复提交只认第一次 |

---

## 7. 能量与预算（防"算力黑洞"）

```json
{ "type":"energy.update",
  "payload": { "used_tokens": 48210, "budget_tokens": 200000, "pct": 24.1, "level":"ok" } }
```

| level | 阈值 | 行为 |
|---|---|---|
| `ok` | < 70% | 正常 |
| `low` | 70–90% | 前端能量条变黄，中枢**拒绝新任务** |
| `exhausted` | ≥ 90% | 变红，仅允许对话，不允许拆解 |

暂停：`POST /control/pause {reason}` → 广播 `energy.update` + 所有 `running` 节点转 `blocked`。

---

## 8. 错误码（固定，前端必须能翻译成人话）

| code | 含义 | 用户话术（前端硬编码） |
|---|---|---|
| `E4001` | 无权限 | 这一步我没有权限动，要你授权 |
| `E4002` | 任务/节点不存在 | 找不到这个任务，可能已经清理了 |
| `E4003` | 状态非法（如 progress 回退） | 任务状态乱了，我重置一下 |
| `E4004` | 超预算/审批超时 | 这次花费超了限额，先停下等你 |
| `E4005` | 工具超时（默认 10s） | 外部服务没响应，我换条路试试 |
| `E4006` | 契约版本不符 | 版本对不上，需要升级 |
| `E4007` | 中枢拆解失败 | 这句话我没拆明白，能再说细一点吗 |

> 规则：**任何错误都必须有 code**；不许只回一句"出错了"。

---

## 9. 幂等 / 续传 / 时光机

| 能力 | 规则 |
|---|---|
| 幂等 | 按 `msg_id` 去重，缓存 24h |
| 续传 | `GET /events?since_seq=N` 补齐断线期间所有消息，按 seq 升序 |
| 时光机 | `GET /snapshot?trace_id=&at=` 返回**只读**状态快照；**不得**用于写入 |

---

## 10. 验收钩子（每条规则都要能被外部脚本判定）

| 契约条款 | 对应断言（外部可跑） |
|---|---|
| 信封必含 8 字段 | `contract-envelope-proof` |
| progress 不许回退 | `contract-progress-monotonic` |
| 审批超时按拒绝 | `contract-approval-timeout-deny` |
| 工具无 auth 返回 403 | `contract-tool-403` |
| msg_id 幂等 | `contract-idempotent` |
| since_seq 续传不重不漏 | `contract-replay-gap` |
| 未知 type 不崩 | `contract-unknown-type` |
| 版本不符回 E4006 | `contract-version-guard` |

> **本契约的价值不在文字，在这 8 条断言能跑通。** 断言跑不过，契约就是废纸。

---

## 11. 变更流程

1. 任何字段增删 → 版本号 +0.1，旧版继续服务至少一个迭代；
2. 破坏性变更（删字段/改语义）→ 大版本 +1，且必须提供迁移说明；
3. 起草者（asus）**无权单方面改契约语义**，需 VX 批准；
4. 第二实现者接入后，任何分歧以"能跑通双方断言"的实现为准。

---

asus（asusPC 智能体总监）· 2026-11-02 · 单一实现者起草声明见文首
