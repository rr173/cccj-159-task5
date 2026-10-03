"""电路影响闭包引擎（纯函数核心，零第三方依赖）。

输入：版本化拓扑
  - nodes：模块实例（id、类型、时钟域、是否接口边界、策略强制标记）
  - edges：实例化 / 数据流 / 跨时钟 / 接口契约 等有向传播关系
  - constraints：约束引用（时钟、例外路径等，被哪些模块引用）

输出：从一组"修改起点"出发必须复验的模块闭包：
  - 每个节点附带必做检查类型（仿真/CDC/接口契约/约束/时序/策略…）
  - 每条进入节点的传播边带**中文解释**（操作页逐条说明为什么要复验）
  - 强制（mandatory）节点不可人工排除：跨时钟、接口契约、策略规定

可操作错误（聚合返回，不做半吊子结果）：
  TOPOLOGY_INVALID  缺失节点、重复 id、坏边、未知检查类型…
  CLOSURE_CYCLE     有向环（给出环路径，可据此打断环或加 registered 标注）
  CLOSURE_TOO_LARGE 闭包超上限（给出已达规模与上限，可调 max_nodes 重算）
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field

# ---- 检查类型目录（闭包给每个节点推导"复验什么"）----
CHECK_SIM = "SIM"                 # 功能仿真复验
CHECK_TIMING = "TIMING"           # 静态时序复验
CHECK_CDC = "CDC"                 # 跨时钟域检查（同步器/异步 FIFO/亚稳态）
CHECK_CONTRACT = "CONTRACT"       # 接口契约复验（协议/握手/位宽）
CHECK_CONSTRAINT = "CONSTRAINT"   # 约束（SDC：时钟、例外路径）复验
CHECK_POLICY = "POLICY"           # 团队策略/流程强制复验
CHECK_RESET = "RESET"             # 复位域一致性检查
CHECK_LEC = "LEC"                 # 逻辑等价（起点本身：改前改后等价）

CHECK_LABELS = {
    CHECK_SIM: "功能仿真",
    CHECK_TIMING: "静态时序",
    CHECK_CDC: "跨时钟域 CDC",
    CHECK_CONTRACT: "接口契约",
    CHECK_CONSTRAINT: "约束 SDC",
    CHECK_POLICY: "策略强制",
    CHECK_RESET: "复位域",
    CHECK_LEC: "逻辑等价",
}
KNOWN_CHECKS = set(CHECK_LABELS)

# 边类型 -> (语义标签, 该边向目标传播的检查)
EDGE_KINDS = {
    "instance":   ("实例化（父→子，结构展开）", {CHECK_SIM}),
    "dataflow":   ("数据流（信号驱动）", {CHECK_SIM, CHECK_TIMING}),
    "clock":      ("时钟传播", {CHECK_TIMING, CHECK_CONSTRAINT}),
    "reset":      ("复位传播", {CHECK_RESET, CHECK_SIM}),
    "contract":   ("接口契约边界", {CHECK_CONTRACT, CHECK_SIM, CHECK_TIMING}),
    "policy":     ("策略规定传播", {CHECK_POLICY}),
    "constraint": ("约束引用（隐式边）", {CHECK_CONSTRAINT, CHECK_TIMING}),
}

DEFAULT_MAX_NODES = 5000


class ClosureError(Exception):
    """领域错误：code 用于 API 原因码，issues/extra 携带可操作细节。"""

    def __init__(self, code: str, message: str, http_status: int = 422, extra=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.extra = extra or {}


def canon(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode()


def content_fp(obj) -> str:
    return hashlib.sha256(canon(obj)).hexdigest()


@dataclass
class Node:
    id: str
    kind: str = "module"
    name: str = ""
    clock_domain: str | None = None
    interface: bool = False                 # 对外接口边界节点
    policy_forced: bool = False             # 策略规定的强制复验节点
    policy_reason: str = ""
    checks: list[str] = field(default_factory=list)   # 节点固有检查
    meta: dict = field(default_factory=dict)


@dataclass
class Edge:
    source: str
    target: str
    kind: str = "dataflow"
    name: str = ""                          # 端口/信号/实例名
    registered: bool = False                # 寄存器切割：仿真仍传播，但不成环报错
    crossing: bool = False                  # 显式声明的跨时钟边
    bidir: bool = False                     # 无向关系（契约/共享约束）的往返边
    reverse: bool = False                   # 实例化反向边（子→父，结构化上溯）
    checks: list[str] = field(default_factory=list)   # 边级附加检查
    note: str = ""


@dataclass
class Constraint:
    id: str
    ctype: str = "clock"                    # clock / false_path / multicycle / ...
    target_nodes: list[str] = field(default_factory=list)  # 空=应用于全部模块
    name: str = ""


@dataclass
class Topology:
    nodes: dict[str, Node]
    edges: list[Edge]
    constraints: list[Constraint]
    adjacency: dict[str, list[Edge]]

    @classmethod
    def build(cls, raw: dict) -> "Topology":
        """从 API JSON 构造并整体校验。任何问题聚合成一次 TOPOLOGY_INVALID。"""
        issues: list[dict] = []
        nodes: dict[str, Node] = {}
        for i, n in enumerate(raw.get("nodes", [])):
            nid = n.get("id")
            if not nid:
                issues.append({"where": f"nodes[{i}]", "problem": "缺少 id"})
                continue
            if nid in nodes:
                issues.append({"where": f"nodes[{i}]", "node": nid,
                               "problem": "节点 id 重复"})
                continue
            for ch in n.get("checks", []) or []:
                if ch not in KNOWN_CHECKS:
                    issues.append({"where": f"nodes[{i}].checks", "node": nid,
                                   "problem": f"未知检查类型 {ch}",
                                   "known": sorted(KNOWN_CHECKS)})
            nodes[nid] = Node(
                id=nid, kind=n.get("kind", "module"), name=n.get("name", nid),
                clock_domain=(n.get("clock_domain") or None),
                interface=bool(n.get("interface", False)),
                policy_forced=bool(n.get("policy_forced", False)),
                policy_reason=n.get("policy_reason", ""),
                checks=list(n.get("checks", []) or []),
                meta=n.get("meta", {}) or {})

        edges: list[Edge] = []
        for i, e in enumerate(raw.get("edges", []) or []):
            s, t, kind = e.get("source"), e.get("target"), e.get("kind", "dataflow")
            where = f"edges[{i}]"
            if not s or not t:
                issues.append({"where": where, "problem": "source/target 必填"})
                continue
            if kind not in EDGE_KINDS:
                issues.append({"where": where, "edge": [s, t],
                               "problem": f"未知边类型 {kind}",
                               "known": sorted(EDGE_KINDS)})
            for miss in (x for x in (s, t) if x not in nodes):
                issues.append({"where": where, "node": miss,
                               "problem": "边引用了拓扑中不存在的节点",
                               "fix": f"在 nodes 中补充 {miss}，或删除该边"})
            for ch in e.get("checks", []) or []:
                if ch not in KNOWN_CHECKS:
                    issues.append({"where": f"{where}.checks", "edge": [s, t],
                                   "problem": f"未知检查类型 {ch}"})
            edges.append(Edge(
                source=s, target=t, kind=kind, name=e.get("name", ""),
                registered=bool(e.get("registered", False)),
                crossing=bool(e.get("crossing", False)),
                checks=list(e.get("checks", []) or []), note=e.get("note", "")))

        constraints: list[Constraint] = []
        for i, con in enumerate(raw.get("constraints", []) or []):
            cid = con.get("id")
            where = f"constraints[{i}]"
            if not cid:
                issues.append({"where": where, "problem": "缺少 id"})
                continue
            targets = con.get("target_nodes", []) or []
            for miss in (x for x in targets if x not in nodes):
                issues.append({"where": f"{where}.target_nodes", "node": miss,
                               "problem": "约束引用了不存在的模块节点",
                               "fix": f"补充节点 {miss} 或从约束 {cid} 移除引用"})
            constraints.append(Constraint(
                id=cid, ctype=con.get("ctype", "clock"),
                target_nodes=list(targets), name=con.get("name", cid)))

        if issues:
            raise ClosureError(
                "TOPOLOGY_INVALID", f"拓扑校验失败：{len(issues)} 个问题",
                422, {"issues": issues})

        adjacency: dict[str, list[Edge]] = {nid: [] for nid in nodes}
        for e in edges:
            adjacency[e.source].append(e)
        # 实例化反向边（子→父）：子模块被修改时，例化它的每一级父模块必须重新集成。
        # 反向边是"结构化上溯"——只让父模块做集成复验，不再经父模块扇出到无关兄弟子树。
        for e in list(edges):
            if e.kind == "instance":
                adjacency[e.target].append(Edge(
                    source=e.target, target=e.source, kind="instance",
                    name=e.name, reverse=True,
                    note=f"父模块 {e.source} 例化本子模块"))
        # 约束生成隐式传播边：共享同一条约束的模块互相影响（一方变更可能改变
        # SDC 假设，其余引用方必须复验约束与时序）。target_nodes 为空表示全局约束，
        # 退化为从每个节点指向约束"锚点"（第一个节点）而非全连通，避免闭包爆炸；
        # 显式 clock 边已负责时钟结构传播。
        for con in constraints:
            owners = sorted(con.target_nodes)
            if len(owners) >= 2:
                anchor = owners[0]
                for owner in owners[1:]:
                    adjacency[owner].append(Edge(
                        source=owner, target=anchor, kind="constraint",
                        name=con.name, bidir=True,
                        note=f"约束 {con.id}（{con.ctype}）"))
                    adjacency[anchor].append(Edge(
                        source=anchor, target=owner, kind="constraint",
                        name=con.name, bidir=True,
                        note=f"约束 {con.id}（{con.ctype}）"))
        # 稳定遍历顺序：按 (target, kind, name)
        for lst in adjacency.values():
            lst.sort(key=lambda x: (x.target, x.kind, x.name))
        return cls(nodes=nodes, edges=edges, constraints=constraints,
                   adjacency=adjacency)

    # ---- 对外解释 ----
    @staticmethod
    def edge_kind_label(kind: str) -> str:
        return EDGE_KINDS.get(kind, (kind,))[0]

    def explain_edge(self, e: Edge) -> dict:
        """为单条传播边生成操作页展示用的中文解释。"""
        sn, tn = self.nodes.get(e.source), self.nodes.get(e.target)
        s_cd = sn.clock_domain if sn else None
        t_cd = tn.clock_domain if tn else None
        # 结构化上溯（反向例化边）即使父子异域也不是信号跨域，不算 CDC
        cross = (not e.reverse) and (e.crossing or _domains_cross(s_cd, t_cd))
        s_name = sn.name if sn else e.source
        t_name = tn.name if tn else e.target
        via = f"（{e.name}）" if e.name else ""
        if e.kind == "instance":
            if e.reverse:
                text = (f"{t_name} 例化了被修改的子模块 {s_name}{via}：子模块改动会进入"
                        f"父模块的结构与连线集成，需重新做集成级功能与时序复验"
                        f"（不连带父模块的其他子树）。")
            else:
                text = (f"{s_name} 例化 {t_name}{via}：容器/父模块改动会展开到子模块，"
                        f"需对 {t_name} 侧做功能复验。")
        elif e.kind == "dataflow":
            if cross:
                text = (f"信号 {e.name or '?'} 从 {s_name}（{s_cd}）流入 {t_name}（{t_cd}），"
                        f"跨越时钟域：下游必须复验同步/亚稳态（CDC）与功能、时序。")
            else:
                text = (f"信号 {e.name or '?'} 由 {s_name} 驱动 {t_name}{via}："
                        f"上游改动沿数据流下传，下游功能与时序需复验。")
        elif e.kind == "clock":
            text = (f"{s_name} 的时钟结构影响 {t_name}{via}：时钟改动需重验时序并核对约束。")
        elif e.kind == "reset":
            text = (f"{s_name} 的复位逻辑传播至 {t_name}{via}：需复验复位域一致性与功能。")
        elif e.kind == "contract":
            text = (f"{s_name} 与 {t_name} 之间存在接口契约 {e.name or ''}："
                    f"一侧改动即破坏协议/握手/位宽约定，两侧均强制复验契约。")
        elif e.kind == "policy":
            text = (f"按团队策略 {e.name or ''}，{s_name} 的变更必须连带复验 {t_name}。")
        else:  # constraint
            text = (f"{s_name} 与 {t_name} 共享约束 {e.name or e.note or ''}："
                    f"一方变更可能改变 SDC 假设，需复验约束与时序。")
        return {
            "source": e.source, "target": e.target, "kind": e.kind,
            "kind_label": self.edge_kind_label(e.kind), "name": e.name,
            "cross_clock": cross, "registered": e.registered, "reverse": e.reverse,
            "source_clock": s_cd, "target_clock": t_cd, "text": text,
        }


def _domains_cross(a: str | None, b: str | None) -> bool:
    """两个时钟域是否跨域。节点可声明多域（'CLK_CORE|CLK_BUS'），有交集即同域。"""
    if not a or not b:
        return False
    da = {x.strip() for x in a.split("|") if x.strip()}
    db = {x.strip() for x in b.split("|") if x.strip()}
    return not (da & db)


def _detect_cycle_on_edges(topo: Topology, edges: list[Edge],
                           roots: list[str]) -> list[str] | None:
    """在实际会被遍历的边构成的子图上做三色 DFS，返回第一条长度≥3 的环。

    registered=True 的边视为寄存器切割（时序边界），不参与组合环判定；
    长度 2 的直接往返（成对反向边/双向契约）是合法耦合，不当作环。
    """
    adj: dict[str, list[Edge]] = {n: [] for n in topo.nodes}
    for e in edges:
        if e.registered and e.kind != "contract":
            continue
        adj[e.source].append(e)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in topo.nodes}
    stack: list[str] = []

    def dfs(u: str, prev: str | None) -> list[str] | None:
        color[u] = GRAY
        stack.append(u)
        for e in adj[u]:
            v = e.target
            if color[v] == GRAY:
                if v == prev:        # 长度 2 往返合法
                    continue
                i = stack.index(v)
                return stack[i:] + [v]
            if color[v] == WHITE:
                ring = dfs(v, u)
                if ring:
                    return ring
        color[u] = BLACK
        stack.pop()
        return None

    for u in sorted(set(roots)):
        if color[u] == WHITE:
            ring = dfs(u, None)
            if ring:
                return ring
    return None


def _reachable_raw(topo: Topology, roots: list[str]) -> tuple[set[str], list[Edge]]:
    """沿**前向**变更传播边（正向例化/信号/时钟/复位/契约/策略/约束）收集可达子图。

    不包含反向例化（结构化上溯）边——层次包含不是信号流，"下传到后代再上溯到
    祖先"不构成真实组合环。用于在活性剪枝前做环检测。
    """
    seen: set[str] = set()
    edges: list[Edge] = []
    dq: deque[str] = deque(sorted(set(roots)))
    while dq:
        u = dq.popleft()
        if u in seen:
            continue
        seen.add(u)
        for e in topo.adjacency[u]:
            if e.reverse:
                continue
            edges.append(e)
            dq.append(e.target)
    return seen, edges


# 结构化上溯（反向例化边）向父模块传播的检查：集成级功能与时序
REVERSE_INSTANCE_CHECKS = {CHECK_SIM, CHECK_TIMING}


def compute_closure(topo: Topology, origins: list[str], *,
                    max_nodes: int = DEFAULT_MAX_NODES) -> dict:
    """从修改起点计算复验闭包。

    传播活性（active/passive）：
      - active = 真正被改动的节点（起点，或沿信号/时钟/复位/契约/策略/约束/正向
        例化等"变更传播边"到达的节点）；
      - passive = 仅因"例化了被改子模块"而需重新集成的父模块（沿反向例化边上溯）。
      passive 节点只继续沿反向例化边向上找祖父，不再向下例化、也不沿自身信号边
      扇出，避免一个叶子改动波及整棵无关子树。

    返回：
      nodes: {node_id: {depth, active, path[], incoming_edge, checks, mandatory, …}}
      edges: 闭包内实际使用的传播边解释（BFS 生成树，每节点一条入边）
      stats: 规模与强制数量
    """
    missing = sorted({o for o in origins if o not in topo.nodes})
    if missing:
        hint = {}
        for m in missing:
            near = sorted(topo.nodes, key=lambda x: _dist(x, m))[:5]
            hint[m] = near
        raise ClosureError(
            "ORIGIN_NOT_FOUND", f"修改起点不存在: {missing}", 404,
            {"missing": missing, "candidates": hint,
             "fix": "检查起点模块 id 是否拼写错误，或先更新拓扑版本"})
    if not origins:
        raise ClosureError("EMPTY_ORIGIN", "至少需要一个修改起点", 400)

    # ---- 环检测在底层可达子图上做（先于 active/passive 剪枝）----
    reachable, raw_edges = _reachable_raw(topo, sorted(set(origins)))
    ring = _detect_cycle_on_edges(topo, raw_edges, sorted(reachable))
    if ring:
        raise ClosureError(
            "CLOSURE_CYCLE", f"传播图存在环：{' -> '.join(ring)}", 422,
            {"cycle": ring,
             "fix": "打断环（拆模块/改方向），或在寄存器边界的边上标记 registered=true"})

    # ---- 活性工作列表遍历：每个节点记录首次到达深度/入边/活性 ----
    depth: dict[str, int] = {}
    parent_edge: dict[str, Edge] = {}
    active: dict[str, bool] = {}
    traversed: list[Edge] = []
    q: deque[str] = deque()
    for o in sorted(set(origins)):
        if o not in depth:
            depth[o] = 0
            active[o] = True
            q.append(o)

    def can_traverse(e: Edge, src_active: bool) -> bool:
        # 反向例化边（结构化上溯）对 active/passive 都可沿其继续向上
        if e.reverse:
            return True
        # 其余"变更传播边"只有 active 节点可沿其传播
        return src_active

    while q:
        u = q.popleft()
        if len(depth) > max_nodes:
            raise ClosureError(
                "CLOSURE_TOO_LARGE",
                f"闭包规模 {len(depth)} 超过上限 {max_nodes}", 422,
                {"reached": len(depth), "max_nodes": max_nodes,
                 "fix": "收窄修改起点，或用更大的 max_nodes 重新计算"})
        for e in topo.adjacency[u]:
            if not can_traverse(e, active[u]):
                continue
            v = e.target
            if v not in depth:
                depth[v] = depth[u] + 1
                parent_edge[v] = e
                active[v] = active[u] and not e.reverse
                traversed.append(e)
                q.append(v)
            elif not active[v] and active[u] and not e.reverse:
                # passive 节点被一条变更传播边"升级"为 active：需要重新展开其传播边
                active[v] = True
                if v not in parent_edge or parent_edge[v].reverse:
                    parent_edge[v] = e
                q.append(v)

    # ---- 汇总每个节点的检查项与强制理由 ----
    nodes_out: dict[str, dict] = {}
    mandatory_reasons: dict[str, list[str]] = {}

    def mark_mandatory(nid: str, reason: str):
        mandatory_reasons.setdefault(nid, [])
        if reason not in mandatory_reasons[nid]:
            mandatory_reasons[nid].append(reason)

    for o in set(origins):
        mark_mandatory(o, "修改起点本身不可排除（必须复验）")

    for e in traversed:
        if e.reverse:
            continue  # 结构化上溯不产生强制节点（父模块集成复验可人工排除）
        s_cd = topo.nodes[e.source].clock_domain
        t_cd = topo.nodes[e.target].clock_domain
        cross = e.crossing or _domains_cross(s_cd, t_cd)
        if cross:
            mark_mandatory(e.source, f"跨时钟域边的源端（{s_cd}→{t_cd}）")
            mark_mandatory(e.target, f"跨时钟域边的目的端（{s_cd}→{t_cd}）：CDC 不可跳过")
        if e.kind == "contract":
            mark_mandatory(e.source, "接口契约边界对端")
            mark_mandatory(e.target, "接口契约节点：契约复验强制")
        if e.kind == "policy":
            mark_mandatory(e.target, f"策略强制（{e.name or '团队策略'}）")
        if e.kind == "constraint":
            mark_mandatory(e.target, f"共享约束 {e.name}：约束节点强制复验")
    for nid, n in topo.nodes.items():
        if nid not in depth:
            continue
        if n.interface:
            mark_mandatory(nid, "对外接口边界节点不可排除")
        if n.policy_forced:
            mark_mandatory(nid, n.policy_reason or "节点被策略标记为强制复验")

    for nid, d in sorted(depth.items(), key=lambda kv: (kv[1], kv[0])):
        n = topo.nodes[nid]
        checks = set(n.checks)
        if d == 0:
            checks.add(CHECK_LEC)
        inc = parent_edge.get(nid)
        if inc:
            if inc.reverse:
                checks |= REVERSE_INSTANCE_CHECKS
            else:
                checks |= set(EDGE_KINDS[inc.kind][1])
                s_cd = topo.nodes[inc.source].clock_domain
                if inc.crossing or _domains_cross(s_cd, n.clock_domain):
                    checks.add(CHECK_CDC)
            checks |= set(inc.checks)
        nodes_out[nid] = {
            "id": nid, "name": n.name, "kind": n.kind,
            "clock_domain": n.clock_domain, "interface": n.interface,
            "depth": d, "active": active[nid],
            "structural": not active[nid],
            "path": _build_path(nid, parent_edge, set(origins)),
            "incoming_edge": topo.explain_edge(inc) if inc else None,
            "checks": sorted(checks),
            "check_labels": {c: CHECK_LABELS[c] for c in sorted(checks)},
            "mandatory": nid in mandatory_reasons,
            "mandatory_reasons": mandatory_reasons.get(nid, []),
        }

    edges_out = [topo.explain_edge(parent_edge[nid])
                 for nid in sorted(depth, key=lambda x: (depth[x], x))
                 if nid in parent_edge]
    return {
        "nodes": nodes_out,
        "edges": edges_out,
        "stats": {
            "total": len(nodes_out),
            "mandatory": sum(1 for v in nodes_out.values() if v["mandatory"]),
            "optional": sum(1 for v in nodes_out.values() if not v["mandatory"]),
            "active": sum(1 for v in nodes_out.values() if v["active"]),
            "structural": sum(1 for v in nodes_out.values() if v["structural"]),
            "max_depth": max((v["depth"] for v in nodes_out.values()), default=0),
            "edge_count": len(edges_out),
        },
    }


def _build_path(nid: str, parent_edge: dict, origins: set) -> list[str]:
    if nid in origins:
        return [nid]
    path = [nid]
    cur = nid
    guard = 0
    while cur not in origins and guard < 100000:
        e = parent_edge.get(cur)
        if not e:
            break
        cur = e.source
        path.append(cur)
        guard += 1
    path.reverse()
    return path


def _dist(a: str, b: str) -> int:
    """简单编辑距离，用于缺失节点的候选提示。"""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    dp = list(range(lb + 1))
    for i in range(1, la + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, lb + 1):
            cur = dp[j]
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1,
                        prev + (a[i - 1] != b[j - 1]))
            prev = cur
    return dp[lb]


def closure_fingerprint(origins: list[str], topology_fp: str,
                        change_summary: dict) -> str:
    """快照内容指纹：拓扑版本内容 + 起点 + 变更摘要，任一改变必产生新快照。"""
    return content_fp({
        "topology_fp": topology_fp,
        "origins": sorted(origins),
        "change_summary": change_summary,
    })[:16]
