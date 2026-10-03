"""影响闭包服务层：版本化拓扑、变更单、分析快照生命周期与冻结门禁。

关键不变量：
1. 快照绑定 (拓扑内容指纹, 修改起点, 变更摘要)：相同输入返回原快照；
   拓扑更新或摘要/起点变更后旧快照不再 CURRENT，禁止用于冻结/发布。
2. 并发重算：同 change 串行化 + 唯一 (change_id, content_fp)，
   提交新快照原子地把旧 CURRENT 置 SUPERSEDED，任何时刻只有一个当前版本。
3. 强制节点（跨时钟/接口契约/策略/修改起点）排除一律拒绝；
   普通候选可以排除但必须填写非空理由。
4. 自动结论（compute）与人工覆盖（exclude/unexclude）分别写审计。
"""
from __future__ import annotations

import json
import threading

from .store import Store, utcnow
from .topology import (CHECK_LABELS, ClosureError, Topology, closure_fingerprint,
                       compute_closure, content_fp)


class ImpactError(ClosureError):
    """服务层错误（与引擎同错误信封形状）。"""


def _row(r):
    return dict(r) if r is not None else None


class ImpactService:
    def __init__(self, store: Store, eco_service=None, default_max_nodes: int = 5000):
        self.s = store
        self.eco = eco_service
        self.default_max_nodes = default_max_nodes
        # 同变更的重算/排除串行化；Store 单写锁兜底跨线程原子性
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, change_id: str) -> threading.Lock:
        with self._locks_guard:
            lk = self._locks.get(change_id)
            if lk is None:
                lk = threading.Lock()
                self._locks[change_id] = lk
            return lk

    def _audit(self, c, action, change_id=None, snapshot_id=None, actor=None, detail=None):
        c.execute(
            "INSERT INTO impact_audit(actor,action,change_id,snapshot_id,at,detail_json)"
            " VALUES(?,?,?,?,?,?)",
            (actor, action, change_id, snapshot_id, utcnow(),
             json.dumps(detail or {}, ensure_ascii=False)))

    # ===== 拓扑版本 =====
    def latest_topology(self, c, topology_id="main"):
        return c.execute(
            "SELECT * FROM topology_version WHERE topology_id=? ORDER BY version DESC LIMIT 1",
            (topology_id,)).fetchone()

    def upload_topology(self, payload: dict, by: str, note: str = "",
                        topology_id: str = "main") -> dict:
        """上传一版拓扑。内容相同则幂等返回既有版本（不新增版本号）。"""
        topo = Topology.build(payload)  # 先校验，坏拓扑不落库
        fp = content_fp(payload)
        with self.s.tx() as c:
            existing = c.execute(
                "SELECT * FROM topology_version WHERE topology_id=? AND content_fp=?",
                (topology_id, fp)).fetchone()
            if existing:
                d = _row(existing)
                d["reused"] = True
                self._audit(c, "topology.upload_reuse", actor=by,
                            detail={"version": d["version"], "content_fp": fp})
                return d
            cur = c.execute(
                "INSERT INTO topology_version(topology_id,content_fp,payload_json,"
                "created_by,created_at,note) VALUES(?,?,?,?,?,?)",
                (topology_id, fp, json.dumps(payload, ensure_ascii=False,
                                             sort_keys=True), by, utcnow(), note))
            version = cur.lastrowid
            # 拓扑更新：所有引用旧版本且尚未冻结的变更，其当前快照立即陈旧。
            # **不推进** impact_change.topology_version 指针——它记录"当前快照所依据
            # 的版本"，保留旧值才能给出准确的 "v1 → v2" 陈旧提示；compute 重算时追赶最新。
            c.execute(
                "UPDATE closure_snapshot SET state='SUPERSEDED', is_current=0,"
                "superseded_at=COALESCE(superseded_at,?) WHERE is_current=1 "
                "AND change_id IN (SELECT id FROM impact_change WHERE status!='FROZEN')",
                (utcnow(),))
            c.execute(
                "UPDATE impact_change SET current_snapshot_id=NULL,updated_at=? "
                "WHERE status!='FROZEN'", (utcnow(),))
            self._audit(c, "topology.new_version", actor=by,
                        detail={"version": version, "content_fp": fp,
                                "nodes": len(topo.nodes), "edges": len(topo.edges),
                                "constraints": len(topo.constraints), "note": note})
            return {"version": version, "topology_id": topology_id, "content_fp": fp,
                    "created_by": by, "created_at": utcnow(), "note": note,
                    "reused": False,
                    "stats": {"nodes": len(topo.nodes), "edges": len(topo.edges),
                              "constraints": len(topo.constraints)}}

    def get_topology(self, version: int) -> dict:
        r = self.s.q_one("SELECT * FROM topology_version WHERE version=?", (version,))
        if not r:
            raise ImpactError("TOPOLOGY_VERSION_NOT_FOUND",
                              f"拓扑版本 v{version} 不存在", 404)
        d = _row(r)
        d["payload"] = json.loads(d.pop("payload_json"))
        return d

    def list_topologies(self, topology_id="main") -> list[dict]:
        rows = self.s.q_all(
            "SELECT version,topology_id,content_fp,created_by,created_at,note "
            "FROM topology_version WHERE topology_id=? ORDER BY version", (topology_id,))
        return [dict(r) for r in rows]

    def _load_topo(self, c, version: int) -> tuple[Topology, str, dict]:
        r = c.execute("SELECT * FROM topology_version WHERE version=?", (version,)).fetchone()
        if not r:
            raise ImpactError("TOPOLOGY_VERSION_NOT_FOUND",
                              f"拓扑版本 v{version} 不存在", 404)
        payload = json.loads(r["payload_json"])
        return Topology.build(payload), r["content_fp"], payload

    # ===== 变更单 =====
    def create_change(self, title: str, owner: str, origins: list[str],
                      summary: dict | None = None, topology_version: int | None = None,
                      max_nodes: int | None = None) -> dict:
        if not title or not owner:
            raise ImpactError("BAD_REQUEST", "title/owner 必填", 400)
        if not origins:
            raise ImpactError("EMPTY_ORIGIN", "至少需要一个修改起点", 400)
        with self.s.tx() as c:
            tv = c.execute(
                "SELECT * FROM topology_version WHERE version=? ",
                (topology_version,)).fetchone() if topology_version else self.latest_topology(c)
            if not tv:
                raise ImpactError("TOPOLOGY_VERSION_NOT_FOUND",
                                  "拓扑不存在，请先上传版本化拓扑", 404)
            topo, tfp, _ = self._load_topo(c, tv["version"])
            missing = sorted({o for o in origins if o not in topo.nodes})
            if missing:
                from .topology import _dist
                hint = {m: sorted(topo.nodes, key=lambda x: _dist(x, m))[:5]
                        for m in missing}
                raise ClosureError(
                    "ORIGIN_NOT_FOUND", f"修改起点不存在: {missing}", 404,
                    {"missing": missing, "candidates": hint,
                     "topology_version": tv["version"],
                     "fix": "检查起点 id，或基于包含这些模块的拓扑版本创建变更"})
            n = c.execute("SELECT COUNT(*) k FROM impact_change").fetchone()["k"]
            cid = f"CHG-{n + 1:03d}"
            now = utcnow()
            c.execute(
                "INSERT INTO impact_change(id,title,owner,topology_version,topology_fp,"
                "origins_json,summary_json,status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,'OPEN',?,?)",
                (cid, title, owner, tv["version"], tfp,
                 json.dumps(sorted(set(origins))),
                 json.dumps(summary or {}, ensure_ascii=False, sort_keys=True), now, now))
            self._audit(c, "change.create", cid, actor=owner,
                        detail={"title": title, "origins": sorted(set(origins)),
                                "topology_version": tv["version"]})
        # 创建即自动计算一次（需求：进入审核前自动计算）；遵守 pin 的拓扑版本
        closure = self.compute(cid, by=owner, max_nodes=max_nodes,
                               topology_version=tv["version"])
        change = self.get_change(cid)
        change["closure"] = closure
        return change

    def _change_dict(self, c, cid: str) -> dict:
        r = c.execute("SELECT * FROM impact_change WHERE id=?", (cid,)).fetchone()
        if not r:
            raise ImpactError("CHANGE_NOT_FOUND", f"变更 {cid} 不存在", 404)
        d = dict(r)
        d["origins"] = json.loads(d.pop("origins_json"))
        d["summary"] = json.loads(d.pop("summary_json"))
        return d

    def get_change(self, cid: str) -> dict:
        with self.s.lock:
            c = self.s.conn
            d = self._change_dict(c, cid)
            snap = self._current_snapshot(c, cid, d["current_snapshot_id"])
            if snap:
                d["closure"] = self._snapshot_view(c, snap)
            else:
                d["closure"] = None
                d["stale_reason"] = self._stale_reason(c, d)
            return d

    def list_changes(self) -> list[dict]:
        rows = self.s.q_all(
            "SELECT id,title,owner,status,topology_version,current_snapshot_id,"
            "eco_id,created_at,updated_at FROM impact_change ORDER BY created_at")
        out = []
        for r in rows:
            d = dict(r)
            d["stale"] = r["current_snapshot_id"] is None
            out.append(d)
        return out

    def update_change(self, cid: str, *, origins=None, summary=None, title=None,
                      by: str = "?") -> dict:
        """编辑起点/摘要：旧快照立即作废，需要重新计算后才能冻结。"""
        with self._lock_for(cid), self.s.tx() as c:
            chg = self._change_dict(c, cid)
            if chg["status"] == "FROZEN":
                raise ImpactError("CHANGE_FROZEN", "已进入审核冻结的变更不可修改", 409)
            latest = self.latest_topology(c)
            new_tv = latest["version"]
            new_origins = sorted(set(origins)) if origins is not None else chg["origins"]
            new_summary = summary if summary is not None else chg["summary"]
            if origins is not None:
                topo, _, _ = self._load_topo(c, new_tv)
                missing = [o for o in new_origins if o not in topo.nodes]
                if missing:
                    raise ClosureError(
                        "ORIGIN_NOT_FOUND", f"修改起点不存在: {missing}", 404,
                        {"missing": missing, "fix": "检查起点 id 或先更新拓扑"})
            c.execute(
                "UPDATE impact_change SET origins_json=?,summary_json=?,title=COALESCE(?,title),"
                "topology_version=?,topology_fp=?,updated_at=? WHERE id=?",
                (json.dumps(new_origins),
                 json.dumps(new_summary, ensure_ascii=False, sort_keys=True),
                 title, new_tv, latest["content_fp"], utcnow(), cid))
            self._invalidate_current(c, cid, "change inputs edited")
            self._audit(c, "change.update", cid, actor=by,
                        detail={"origins": new_origins, "summary_changed": summary is not None,
                                "topology_version": new_tv})
            return self._change_dict(c, cid)

    def _invalidate_current(self, c, cid: str, reason: str):
        c.execute(
            "UPDATE closure_snapshot SET state='SUPERSEDED', is_current=0, superseded_at=? "
            "WHERE change_id=? AND is_current=1", (utcnow(), cid))
        c.execute("UPDATE impact_change SET current_snapshot_id=NULL,updated_at=? WHERE id=?",
                  (utcnow(), cid))
        self._audit(c, "snapshot.invalidate", cid, actor="system", detail={"reason": reason})

    # ===== 闭包计算（自动结论）=====
    def _current_snapshot(self, c, cid, snap_id):
        if not snap_id:
            return None
        return c.execute(
            "SELECT * FROM closure_snapshot WHERE id=? AND change_id=? AND is_current=1",
            (snap_id, cid)).fetchone()

    def _stale_reason(self, c, chg: dict) -> str | None:
        latest = self.latest_topology(c)
        if chg["status"] == "FROZEN":
            return None
        if not latest:
            return "拓扑缺失"
        if latest["version"] != chg["topology_version"]:
            return (f"拓扑已更新（v{chg['topology_version']} → v{latest['version']}），"
                    f"旧闭包结果不得用于冻结/发布，请重新计算")
        if chg["current_snapshot_id"] is None:
            return "起点或变更摘要已变更，需要重新计算影响闭包"
        return None

    def compute(self, cid: str, by: str = "?", max_nodes: int | None = None,
                topology_version: int | None = None, chase_latest: bool = False) -> dict:
        """计算（或幂等取回）当前闭包快照。

        - chase_latest=True（页面/API 手动重算）：先追赶最新拓扑版本再算，
          这样拓扑更新后一次重算即恢复可冻结；
        - topology_version 显式指定（创建时 pin 旧版本）：按指定版本算；
        - 默认：按变更当前绑定版本算。
        相同输入返回原快照（reused=true）；并发提交只保留一个 CURRENT。
        """
        with self._lock_for(cid):
            with self.s.tx() as c:
                chg = self._change_dict(c, cid)
                if chg["status"] == "FROZEN":
                    raise ImpactError("CHANGE_FROZEN", "已冻结变更不可重算", 409)
                if chase_latest:
                    latest = self.latest_topology(c)
                    if latest and latest["version"] != chg["topology_version"]:
                        # 重算前把指针推进到最新拓扑
                        c.execute(
                            "UPDATE impact_change SET topology_version=?,topology_fp=?,"
                            "updated_at=? WHERE id=?",
                            (latest["version"], latest["content_fp"], utcnow(), cid))
                        self._audit(c, "change.retopology", cid, actor=by,
                                    detail={"from_version": chg["topology_version"],
                                            "to_version": latest["version"]})
                        chg = self._change_dict(c, cid)
                if topology_version is not None and topology_version != chg["topology_version"]:
                    tv = c.execute("SELECT * FROM topology_version WHERE version=?",
                                   (topology_version,)).fetchone()
                    if not tv:
                        raise ImpactError("TOPOLOGY_VERSION_NOT_FOUND",
                                          f"拓扑版本 v{topology_version} 不存在", 404)
                    c.execute(
                        "UPDATE impact_change SET topology_version=?,topology_fp=?,"
                        "updated_at=? WHERE id=?",
                        (tv["version"], tv["content_fp"], utcnow(), cid))
                    chg = self._change_dict(c, cid)
                topo, tfp, _ = self._load_topo(c, chg["topology_version"])
                cfp = closure_fingerprint(chg["origins"], tfp, chg["summary"])
                existing = c.execute(
                    "SELECT * FROM closure_snapshot WHERE change_id=? AND content_fp=?",
                    (cid, cfp)).fetchone()
                if existing and existing["is_current"]:
                    view = self._snapshot_view(c, existing)
                    self._audit(c, "snapshot.reuse", cid, existing["id"], actor=by,
                                detail={"content_fp": cfp})
                    view["reused"] = True
                    return view

            # 引擎计算放在事务外（纯 CPU，且可能抛 TOPOLOGY/CYCLE/TOO_LARGE）
            result = compute_closure(
                topo, chg["origins"], max_nodes=max_nodes or self.default_max_nodes)

            with self.s.tx() as c:
                chg = self._change_dict(c, cid)
                # 双重检查：等待锁期间输入可能已被改/拓扑被更新
                cfp2 = closure_fingerprint(chg["origins"], chg["topology_fp"], chg["summary"])
                hit = c.execute(
                    "SELECT * FROM closure_snapshot WHERE change_id=? AND content_fp=?",
                    (cid, cfp2)).fetchone()
                now = utcnow()
                if hit:
                    # 相同输入（拓扑内容指纹一致 → 闭包结果必然相同）：
                    # 无论它当前是 CURRENT 还是曾被作废，都重新提升为唯一当前版本，
                    # 绝不重复插入触发唯一约束。
                    snap_id = hit["id"]
                    c.execute(
                        "UPDATE closure_snapshot SET state='CURRENT',is_current=1,"
                        "superseded_at=NULL WHERE id=?", (snap_id,))
                    c.execute(
                        "UPDATE closure_snapshot SET state='SUPERSEDED',is_current=0,"
                        "superseded_at=? WHERE change_id=? AND id!=?", (now, cid, snap_id))
                    c.execute(
                        "UPDATE impact_change SET current_snapshot_id=?,topology_version=?,"
                        "topology_fp=?,updated_at=? WHERE id=?",
                        (snap_id, chg["topology_version"], chg["topology_fp"], now, cid))
                    view = self._snapshot_view(c, c.execute(
                        "SELECT * FROM closure_snapshot WHERE id=?", (snap_id,)).fetchone())
                    view["reused"] = True
                    self._audit(c, "snapshot.repromote" if not hit["is_current"]
                                else "snapshot.reuse_after_wait", cid, snap_id, actor=by,
                                detail={"content_fp": cfp2})
                    return view
                snap_id = f"CL-{cid}-{cfp2[:10]}"
                c.execute(
                    "INSERT INTO closure_snapshot(id,change_id,content_fp,topology_version,"
                    "topology_fp,origins_json,summary_json,result_json,state,is_current,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,'CURRENT',1,?,?)",
                    (snap_id, cid, cfp2, chg["topology_version"], chg["topology_fp"],
                     json.dumps(chg["origins"]),
                     json.dumps(chg["summary"], ensure_ascii=False, sort_keys=True),
                     json.dumps(result, ensure_ascii=False), by, now))
                # 唯一当前版本：旧 CURRENT 整批作废（含并发中抢先提交者）
                c.execute(
                    "UPDATE closure_snapshot SET state='SUPERSEDED', is_current=0,"
                    "superseded_at=? WHERE change_id=? AND id!=?", (now, cid, snap_id))
                c.execute(
                    "UPDATE impact_change SET current_snapshot_id=?,topology_version=?,"
                    "topology_fp=?,updated_at=? WHERE id=?",
                    (snap_id, chg["topology_version"], chg["topology_fp"], now, cid))
                self._audit(c, "snapshot.compute", cid, snap_id, actor=by, detail={
                    "content_fp": cfp2, "topology_version": chg["topology_version"],
                    "stats": result["stats"],
                    "mandatory_nodes": [nid for nid, v in result["nodes"].items()
                                        if v["mandatory"]],
                })
                return self._snapshot_view(
                    c, c.execute("SELECT * FROM closure_snapshot WHERE id=?",
                                 (snap_id,)).fetchone())

    def _snapshot_view(self, c, row) -> dict:
        d = dict(row)
        d["origins"] = json.loads(d.pop("origins_json"))
        d["summary"] = json.loads(d.pop("summary_json"))
        d["result"] = json.loads(d.pop("result_json"))
        exclusions = c.execute(
            "SELECT * FROM closure_exclusion WHERE snapshot_id=? AND reverted_at IS NULL",
            (d["id"],)).fetchall()
        d["exclusions"] = [
            {"node_id": e["node_id"], "reason": e["reason"], "by": e["by"], "at": e["at"]}
            for e in exclusions]
        excluded = {e["node_id"] for e in exclusions}
        nodes = d["result"]["nodes"]
        remaining = [nid for nid in nodes if nid not in excluded]
        d["result"]["effective_nodes"] = remaining
        d["result"]["excluded_nodes"] = sorted(excluded)
        d["result"]["stats"]["excluded"] = len(excluded)
        d["result"]["stats"]["effective"] = len(remaining)
        d["stale"] = not (d["is_current"] and d["state"] == "CURRENT")
        return d

    def get_snapshot(self, snap_id: str) -> dict:
        with self.s.lock:
            row = self.s.q_one("SELECT * FROM closure_snapshot WHERE id=?", (snap_id,))
            if not row:
                raise ImpactError("SNAPSHOT_NOT_FOUND", f"快照 {snap_id} 不存在", 404)
            return self._snapshot_view(self.s.conn, row)

    # ===== 人工排除（必须理由；强制节点拒绝）=====
    def _require_current(self, c, cid: str):
        chg = self._change_dict(c, cid)
        if chg["status"] == "FROZEN":
            raise ImpactError("CHANGE_FROZEN", "已冻结变更的闭包不可再排除", 409)
        reason = self._stale_reason(c, chg)
        if reason:
            raise ImpactError("SNAPSHOT_STALE", reason, 409,
                              {"fix": "先 POST compute 基于最新拓扑/摘要重算，再做排除"})
        snap = self._current_snapshot(c, cid, chg["current_snapshot_id"])
        if not snap:
            raise ImpactError("NO_CURRENT_SNAPSHOT", "变更尚无当前闭包，请先计算", 409)
        return chg, snap

    def _deny(self, action, cid, snap_id, by, detail: dict):
        """拒绝事件必须留痕：用**独立已提交事务**写审计，避免随业务事务回滚丢失。"""
        with self.s.tx() as c:
            self._audit(c, action, cid, snap_id, actor=by, detail=detail)

    def exclude_node(self, cid: str, node_id: str, reason: str, by: str) -> dict:
        reason = (reason or "").strip()
        if not reason:
            raise ImpactError("REASON_REQUIRED",
                              "人工排除候选项必须填写理由", 400,
                              {"node_id": node_id, "fix": "在 reason 字段写明排除依据"})
        with self._lock_for(cid):
            # 校验（只读事务）：任何拒绝都在独立事务留痕后抛出，绝不因回滚丢失
            with self.s.tx() as c:
                chg, snap = self._require_current(c, cid)
                result = json.loads(snap["result_json"])
                node = result["nodes"].get(node_id)
                if not node:
                    raise ImpactError(
                        "NODE_NOT_IN_CLOSURE", f"{node_id} 不在当前闭包候选中", 404,
                        {"node_id": node_id, "closure_nodes": sorted(result["nodes"]),
                         "fix": "只能排除闭包内节点；拓扑更新后请重算"})
                if node["mandatory"]:
                    deny = {"node_id": node_id, "reason_attempted": reason,
                            "mandatory_reasons": node["mandatory_reasons"]}
            if node["mandatory"]:
                self._deny("exclude.denied", cid, snap["id"], by, deny)
                raise ImpactError(
                    "EXCLUSION_FORBIDDEN",
                    f"强制复验节点不可排除：{node_id}", 409,
                    {"node_id": node_id,
                     "mandatory_reasons": node["mandatory_reasons"],
                     "fix": "跨时钟/接口契约/策略/修改起点节点必须复验，不能排除"})
            # 通过校验：执行人工覆盖（独立写事务）
            with self.s.tx() as c:
                now = utcnow()
                c.execute(
                    "INSERT INTO closure_exclusion(snapshot_id,change_id,node_id,reason,by,at)"
                    " VALUES(?,?,?,?,?,?) ON CONFLICT(snapshot_id,node_id) DO UPDATE SET"
                    " reason=excluded.reason, by=excluded.by, at=excluded.at,"
                    " reverted_at=NULL",
                    (snap["id"], cid, node_id, reason, by, now))
                self._audit(c, "exclude.add", cid, snap["id"], actor=by,
                            detail={"node_id": node_id, "reason": reason,
                                    "auto_checks": node["checks"]})
                return self._snapshot_view(
                    c, c.execute("SELECT * FROM closure_snapshot WHERE id=?",
                                 (snap["id"],)).fetchone())

    def unexclude_node(self, cid: str, node_id: str, by: str) -> dict:
        with self._lock_for(cid), self.s.tx() as c:
            chg, snap = self._require_current(c, cid)
            cur = c.execute(
                "UPDATE closure_exclusion SET reverted_at=? WHERE snapshot_id=? AND node_id=?"
                " AND reverted_at IS NULL", (utcnow(), snap["id"], node_id))
            if cur.rowcount == 0:
                raise ImpactError("EXCLUSION_NOT_FOUND",
                                  f"{node_id} 当前未被排除", 404)
            self._audit(c, "exclude.revert", cid, snap["id"], actor=by,
                        detail={"node_id": node_id})
            return self._snapshot_view(
                c, c.execute("SELECT * FROM closure_snapshot WHERE id=?",
                             (snap["id"],)).fetchone())

    # ===== 冻结门禁（供 ECO freeze 调用 / 页面直接冻结）=====
    def gate_check(self, cid: str) -> dict:
        """冻结/发布前校验：必须存在绑定最新拓扑与当前摘要的 CURRENT 快照。"""
        with self.s.lock:
            c = self.s.conn
            chg = self._change_dict(c, cid)
            stale = self._stale_reason(c, chg)
            if stale:
                raise ImpactError(
                    "CLOSURE_STALE_FOR_FREEZE",
                    f"影响闭包已过期，不得冻结/发布：{stale}", 409,
                    {"fix": "重新计算闭包（拓扑/起点/摘要已变化）"})
            snap = self._current_snapshot(c, cid, chg["current_snapshot_id"])
            if not snap:
                raise ImpactError("NO_CURRENT_SNAPSHOT", "缺少当前影响闭包快照", 409)
            return self._snapshot_view(c, snap)

    def freeze_gate(self, cid: str, by: str) -> dict:
        """操作页直接把变更标记 READY（进入审核）。旧结果一律拦截。"""
        with self._lock_for(cid):
            with self.s.tx() as c:
                chg = self._change_dict(c, cid)
                stale = self._stale_reason(c, chg)
                snap_id = chg["current_snapshot_id"]
                if stale:
                    deny = {"reason": stale}
                else:
                    snap = self._current_snapshot(c, cid, snap_id)
                    if not snap:
                        stale, deny = "缺少当前影响闭包快照", {"reason": "NO_CURRENT_SNAPSHOT"}
                    else:
                        deny = None
            if stale:
                self._deny("gate.denied", cid, snap_id, by, deny)
                raise ImpactError("CLOSURE_STALE_FOR_FREEZE",
                                  f"影响闭包已过期，不得冻结/发布：{stale}", 409)
            with self.s.tx() as c:
                snap = self._current_snapshot(c, cid,
                                              self._change_dict(c, cid)["current_snapshot_id"])
                view = self._snapshot_view(c, snap)
                c.execute("UPDATE impact_change SET status='READY',updated_at=? WHERE id=?",
                          (utcnow(), cid))
                self._audit(c, "gate.ready", cid, snap["id"], actor=by, detail={
                    "content_fp": snap["content_fp"], "topology_version": snap["topology_version"],
                    "effective": view["result"]["stats"]["effective"],
                    "excluded": view["result"]["stats"]["excluded"]})
                view["status"] = "READY"
                return view

    def mark_frozen(self, c, cid: str, eco_id: str, by: str):
        """ECO 冻结成功后调用：固化关联（同一事务内）。"""
        c.execute("UPDATE impact_change SET status='FROZEN',eco_id=?,updated_at=? WHERE id=?",
                  (eco_id, utcnow(), cid))
        self._audit(c, "change.frozen", cid, actor=by, detail={"eco_id": eco_id})

    # ===== 审计 =====
    def audit(self, cid: str | None = None, action: str | None = None,
              limit: int = 200) -> dict:
        q = "SELECT * FROM impact_audit WHERE 1=1"
        args: list = []
        if cid:
            q += " AND change_id=?"
            args.append(cid)
        if action:
            q += " AND action=?"
            args.append(action)
        q += " ORDER BY seq DESC LIMIT ?"
        args.append(limit)
        rows = self.s.q_all(q, args)
        return {"events": [
            {"seq": r["seq"], "actor": r["actor"], "action": r["action"],
             "change_id": r["change_id"], "snapshot_id": r["snapshot_id"],
             "at": r["at"], "detail": json.loads(r["detail_json"] or "{}")}
            for r in rows]}

    def check_catalog(self) -> dict:
        return {"checks": [{"type": k, "label": v} for k, v in CHECK_LABELS.items()]}
