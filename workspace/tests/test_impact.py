"""电路影响闭包引擎与服务的不变量测试（零第三方依赖）。

覆盖：闭包传播/检查类型/强制节点/边解释、可操作错误（环/缺失/超大/坏拓扑）、
版本化拓扑失效、相同输入幂等、并发单当前版本、人工排除理由与强制冲突、
冻结门禁、审计同时记录自动结论与人工覆盖。
"""
import json
import threading
import unittest

from app.impact import ImpactError, ImpactService
from app.impact_seed import cyclic_topology, seed_demo_topology
from app.service import ECOError, ECOService
from app.store import Store
from app.topology import (CHECK_CDC, CHECK_CONTRACT, CHECK_LEC, CHECK_POLICY,
                          ClosureError, Topology, closure_fingerprint,
                          compute_closure, content_fp)


def service():
    s = Store(":memory:")
    svc = ECOService(s)
    imp = ImpactService(s, svc)
    return s, svc, imp


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.topo = Topology.build(seed_demo_topology())

    def test_origin_closure_and_checks(self):
        r = compute_closure(self.topo, ["l1d"])
        ids = set(r["nodes"])
        # l1d -> async_fifo -> noc -> (l2c 契约, dma)
        self.assertIn("l1d", ids)
        self.assertIn("async_fifo", ids)
        self.assertIn("noc", ids)
        self.assertIn("l2c", ids)
        # 起点带 LEC
        self.assertIn(CHECK_LEC, r["nodes"]["l1d"]["checks"])
        # 跨时钟路径节点带 CDC
        self.assertIn(CHECK_CDC, r["nodes"]["async_fifo"]["checks"])
        # 契约节点带 CONTRACT
        self.assertIn(CHECK_CONTRACT, r["nodes"]["noc"]["checks"])
        self.assertIn(CHECK_CONTRACT, r["nodes"]["l2c"]["checks"])

    def test_every_non_origin_has_explained_incoming_edge(self):
        r = compute_closure(self.topo, ["core"])
        for nid, n in r["nodes"].items():
            if n["depth"] == 0:
                self.assertIsNone(n["incoming_edge"])
            else:
                self.assertIsNotNone(n["incoming_edge"])
                self.assertTrue(n["incoming_edge"]["text"])
                self.assertEqual(n["path"][-1], nid)
        # 边列表数量 = 非起点节点数（每节点一条解释入边）
        self.assertEqual(len(r["edges"]), r["stats"]["total"] - 1)

    def test_mandatory_classification(self):
        r = compute_closure(self.topo, ["l1d"])["nodes"]
        # 起点强制
        self.assertTrue(r["l1d"]["mandatory"])
        # 跨时钟桥与契约对端强制
        self.assertTrue(r["async_fifo"]["mandatory"])
        self.assertTrue(r["noc"]["mandatory"])
        self.assertTrue(r["l2c"]["mandatory"])
        # dma 仅经普通数据流下传，是可排除候选
        self.assertFalse(r["dma"]["mandatory"])
        # 强制理由非空可读
        self.assertTrue(any("CDC" in x or "跨时钟" in x
                            for x in r["async_fifo"]["mandatory_reasons"]))

    def test_policy_forced_node(self):
        # pin_ctrl 被策略标记强制：经 cfg 域路径可达时必须 mandatory
        r = compute_closure(self.topo, ["cfg_bus"])["nodes"]
        self.assertIn("pin_ctrl", r)
        self.assertTrue(r["pin_ctrl"]["mandatory"])
        self.assertTrue(any("策略" in x for x in r["pin_ctrl"]["mandatory_reasons"]))

    def test_policy_edge_forces_target(self):
        # dma →(policy)→ noc：noc 因策略边强制且带 POLICY 检查
        r = compute_closure(self.topo, ["dma"])["nodes"]
        self.assertTrue(r["noc"]["mandatory"])
        self.assertIn(CHECK_POLICY, r["noc"]["checks"])

    def test_edge_explanation_cross_clock(self):
        r = compute_closure(self.topo, ["l1d"])
        edge = next(e for e in r["edges"] if e["source"] == "l1d")
        self.assertTrue(edge["cross_clock"])
        self.assertIn("跨", edge["text"])

    def test_missing_origin(self):
        with self.assertRaises(ClosureError) as cm:
            compute_closure(self.topo, ["ghost_mod"])
        self.assertEqual(cm.exception.code, "ORIGIN_NOT_FOUND")
        self.assertIn("ghost_mod", cm.exception.extra["missing"])

    def test_empty_origin(self):
        with self.assertRaises(ClosureError) as cm:
            compute_closure(self.topo, [])
        self.assertEqual(cm.exception.code, "EMPTY_ORIGIN")

    def test_cycle_detected_with_path(self):
        topo = Topology.build(cyclic_topology())
        with self.assertRaises(ClosureError) as cm:
            compute_closure(topo, ["alu"])
        self.assertEqual(cm.exception.code, "CLOSURE_CYCLE")
        cyc = cm.exception.extra["cycle"]
        self.assertEqual(cyc[0], cyc[-1])
        self.assertGreaterEqual(len(cyc), 4)
        self.assertIn("registered", cm.exception.extra["fix"])

    def test_registered_edge_breaks_cycle(self):
        raw = cyclic_topology()
        for e in raw["edges"]:
            if e.get("name") == "comb_back":
                e["registered"] = True
        r = compute_closure(Topology.build(raw), ["alu"])
        self.assertIn("alu", r["nodes"])

    def test_too_large(self):
        with self.assertRaises(ClosureError) as cm:
            compute_closure(self.topo, ["l1d"], max_nodes=2)
        self.assertEqual(cm.exception.code, "CLOSURE_TOO_LARGE")
        self.assertEqual(cm.exception.extra["max_nodes"], 2)

    def test_topology_validation_aggregates(self):
        bad = {
            "nodes": [{"id": "a"}, {"id": "a"}, {"id": "b", "checks": ["BOGUS"]}],
            "edges": [{"source": "a", "target": "x", "kind": "badkind"}],
            "constraints": [{"id": "c1", "target_nodes": ["y"]}],
        }
        with self.assertRaises(ClosureError) as cm:
            Topology.build(bad)
        self.assertEqual(cm.exception.code, "TOPOLOGY_INVALID")
        codes = [(i["where"], i["problem"]) for i in cm.exception.extra["issues"]]
        self.assertTrue(any("重复" in p for _, p in codes))
        self.assertTrue(any("不存在" in p for _, p in codes))
        self.assertTrue(any("未知边类型" in p for _, p in codes))
        self.assertTrue(any("未知检查类型" in p for _, p in codes))

    def test_bidir_two_node_cycle_is_legal(self):
        raw = {"nodes": [{"id": "a"}, {"id": "b"}],
               "edges": [{"source": "a", "target": "b", "kind": "dataflow"},
                         {"source": "b", "target": "a", "kind": "dataflow"}]}
        r = compute_closure(Topology.build(raw), ["a"])
        self.assertEqual(set(r["nodes"]), {"a", "b"})

    def test_structural_upward_propagation_from_leaf(self):
        # 改叶子 alu：只上溯到例化它的父 core、祖父 soc_top 做集成复验，
        # 不波及兄弟子树（l1i/lsu/l1d），也不沿父的信号边扇出
        r = compute_closure(self.topo, ["alu"])["nodes"]
        self.assertEqual(set(r), {"alu", "core", "soc_top"})
        self.assertTrue(r["core"]["structural"] and not r["core"]["active"])
        self.assertTrue(r["soc_top"]["structural"])
        self.assertFalse(r["core"]["mandatory"])      # 结构上溯节点可人工排除
        # 入边是反向例化边，带集成复验解释
        inc = r["core"]["incoming_edge"]
        self.assertTrue(inc["reverse"])
        self.assertIn("集成", inc["text"])
        self.assertIn("SIM", r["core"]["checks"])

    def test_active_change_still_flows_downstream(self):
        # 改 lsu（有真实信号边）：l1d/async_fifo/noc/l2c 沿 active 信号链下传，
        # 同时父 core/顶层 soc_top 仅作为结构上溯
        r = compute_closure(self.topo, ["lsu"])["nodes"]
        for nid in ("lsu", "l1d", "async_fifo", "noc", "l2c"):
            self.assertIn(nid, r)
        self.assertTrue(r["l1d"]["active"])
        self.assertTrue(r["noc"]["mandatory"])        # CDC + 契约
        self.assertTrue(r["core"]["structural"])      # 父仅集成
        # 结构上溯到 core 后不回流到兄弟 l1i
        self.assertNotIn("l1i", r)

    def test_structural_grandparent_not_reexpand(self):
        # soc_top 即使也例化 cfg_bus，从 alu 上溯到 soc_top 后不得扩散到 cfg 子树
        r = compute_closure(self.topo, ["alu"])["nodes"]
        for nid in ("cfg_bus", "pin_ctrl", "rst_ctrl", "noc", "l2c", "dma"):
            self.assertNotIn(nid, r)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.s, self.svc, self.imp = service()
        self.v = self.imp.upload_topology(seed_demo_topology(), by="sys")["version"]

    def _chg(self, origins=("core",), summary=None):
        return self.imp.create_change(
            "t", "owner", list(origins), summary=summary or {"d": "1"})

    def test_topology_content_idempotent(self):
        d1 = self.imp.upload_topology(seed_demo_topology(), by="x", note="again")
        self.assertTrue(d1["reused"])
        self.assertEqual(d1["version"], self.v)

    def test_create_auto_computes_and_snapshot_binding(self):
        chg = self._chg()
        self.assertIsNotNone(chg["closure"])
        self.assertEqual(chg["closure"]["state"], "CURRENT")
        self.assertEqual(chg["closure"]["topology_version"], self.v)
        self.assertEqual(chg["closure"]["topology_fp"][:8],
                         chg["closure"]["result"] and chg["topology_fp"][:8])

    def test_same_inputs_return_same_snapshot(self):
        chg = self._chg()
        a = chg["closure"]["id"]
        b = self.imp.compute(chg["id"], by="z")
        self.assertTrue(b["reused"])
        self.assertEqual(a, b["id"])

    def test_summary_change_invalidates_and_requires_recompute(self):
        chg = self._chg()
        self.imp.update_change(chg["id"], summary={"d": "2"}, by="o")
        d = self.imp.get_change(chg["id"])
        self.assertIsNone(d["closure"])
        self.assertTrue(d["stale_reason"])
        with self.assertRaises(ImpactError) as cm:
            self.imp.freeze_gate(chg["id"], "o")
        self.assertEqual(cm.exception.code, "CLOSURE_STALE_FOR_FREEZE")
        recomputed = self.imp.compute(chg["id"], by="o")
        self.assertEqual(recomputed["state"], "CURRENT")
        self.imp.freeze_gate(chg["id"], "o")  # 不再抛

    def test_topology_update_invalidates_open_changes(self):
        chg = self._chg(origins=("dma",))
        self.assertEqual(chg["closure"]["state"], "CURRENT")
        new_topo = seed_demo_topology()
        new_topo["nodes"].append({"id": "newmod", "clock_domain": "CLK_CFG"})
        self.imp.upload_topology(new_topo, by="admin", note="v2")
        d = self.imp.get_change(chg["id"])
        self.assertIsNone(d["closure"])
        with self.assertRaises(ImpactError) as cm:
            self.imp.freeze_gate(chg["id"], "o")
        self.assertEqual(cm.exception.code, "CLOSURE_STALE_FOR_FREEZE")
        # 旧快照仍可追溯（不可变）
        old = self.imp.get_snapshot(chg["current_snapshot_id"])
        self.assertEqual(old["state"], "SUPERSEDED")

    def test_concurrent_recompute_single_current(self):
        chg = self._chg()
        cid = chg["id"]
        results, errors = [], []

        def worker(i):
            try:
                self.imp.update_change(cid, summary={"k": i}, by=f"u{i}")
                results.append(self.imp.compute(cid, by=f"u{i}"))
            except Exception as e:  # noqa
                errors.append(e)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        c = self.s.conn
        n = c.execute(
            "SELECT COUNT(*) FROM closure_snapshot WHERE change_id=? AND is_current=1",
            (cid,)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_concurrent_same_input_identical_snapshot(self):
        chg = self._chg()
        cid = chg["id"]
        out = []

        def worker():
            out.append(self.imp.compute(cid, by="u"))

        ts = [threading.Thread(target=worker) for _ in range(12)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual({x["id"] for x in out}, {chg["closure"]["id"]})

    def test_exclude_requires_reason(self):
        chg = self._chg()
        with self.assertRaises(ImpactError) as cm:
            self.imp.exclude_node(chg["id"], "alu", "  ", "o")
        self.assertEqual(cm.exception.code, "REASON_REQUIRED")

    def test_exclude_mandatory_forbidden_and_audited(self):
        chg = self._chg(origins=("l1d",))
        with self.assertRaises(ImpactError) as cm:
            self.imp.exclude_node(chg["id"], "async_fifo", "想跳 CDC", "o")
        self.assertEqual(cm.exception.code, "EXCLUSION_FORBIDDEN")
        self.assertTrue(cm.exception.extra["mandatory_reasons"])
        # 拒绝被持久化（独立事务留痕）
        ev = self.imp.audit(chg["id"])["events"]
        self.assertTrue(any(e["action"] == "exclude.denied"
                            and e["detail"]["node_id"] == "async_fifo" for e in ev))

    def test_origin_cannot_be_excluded(self):
        chg = self._chg()
        with self.assertRaises(ImpactError) as cm:
            self.imp.exclude_node(chg["id"], "core", "起点不验了", "o")
        self.assertEqual(cm.exception.code, "EXCLUSION_FORBIDDEN")

    def test_exclude_optional_then_unexclude(self):
        chg = self._chg()
        view = self.imp.exclude_node(
            chg["id"], "alu", "ALU 仅例化未改, 专项回归覆盖", "wei.dong")
        self.assertIn("alu", view["result"]["excluded_nodes"])
        self.assertNotIn("alu", view["result"]["effective_nodes"])
        view = self.imp.unexclude_node(chg["id"], "alu", "wei.dong")
        self.assertEqual(view["result"]["excluded_nodes"], [])
        self.assertIn("alu", view["result"]["effective_nodes"])

    def test_exclude_node_not_in_closure(self):
        chg = self._chg(origins=("dma",))
        with self.assertRaises(ImpactError) as cm:
            self.imp.exclude_node(chg["id"], "core", "不相关节点", "o")
        self.assertEqual(cm.exception.code, "NODE_NOT_IN_CLOSURE")

    def test_audit_records_auto_and_manual(self):
        chg = self._chg()
        self.imp.exclude_node(chg["id"], "alu", "理由", "o")
        self.imp.unexclude_node(chg["id"], "alu", "o")
        actions = {e["action"] for e in self.imp.audit(chg["id"])["events"]}
        self.assertIn("snapshot.compute", actions)      # 自动结论
        self.assertIn("exclude.add", actions)           # 人工覆盖
        self.assertIn("exclude.revert", actions)        # 人工撤销

    def test_frozen_change_locks_edits_and_exclusions(self):
        chg = self._chg()
        # READY（仅过门禁）后仍允许编辑，编辑后自动退回需重算状态
        self.imp.freeze_gate(chg["id"], "lead")
        self.imp.update_change(chg["id"], summary={"x": 1}, by="o")
        self.assertIsNone(self.imp.get_change(chg["id"])["closure"])
        # 随 ECO 真正冻结后才彻底锁定
        chg2 = self.imp.create_change("锁", "owner", ["core"], {"d": "9"})
        eid = self.svc.create_eco(
            "E", "owner", ["core"],
            {"core": {"netlist": "MODULE core; CELL FF n; ENDMODULE"}})["id"]
        self.svc.freeze_eco(eid, "owner", chg2["id"])
        with self.assertRaises(ImpactError):
            self.imp.update_change(chg2["id"], summary={"x": 2}, by="o")
        with self.assertRaises(ImpactError):
            self.imp.compute(chg2["id"], by="o")
        with self.assertRaises(ImpactError):
            self.imp.exclude_node(chg2["id"], "alu", "r", "o")

    def test_create_change_missing_origin_candidates(self):
        with self.assertRaises(ClosureError) as cm:
            self.imp.create_change("t", "o", ["aluu"])
        self.assertIn("candidates", cm.exception.extra)
        self.assertIn("alu", cm.exception.extra["candidates"]["aluu"])


class EcoGateTests(unittest.TestCase):
    def setUp(self):
        self.s, self.svc, self.imp = service()
        self.imp.upload_topology(seed_demo_topology(), by="sys")
        self.chg = self.imp.create_change(
            "核心变更", "wei.dong", ["core"], {"d": "1"})

    def _eco(self):
        return self.svc.create_eco(
            "E", "wei.dong", ["core"],
            {"core": {"netlist": "MODULE core; CELL FF n; ENDMODULE"}})["id"]

    def test_freeze_with_current_closure_succeeds(self):
        eid = self._eco()
        detail = self.svc.freeze_eco(eid, "wei.dong", self.chg["id"])
        self.assertEqual(detail["status"], "FROZEN")
        # 闭包复验模块并入冻结范围（跨时钟/契约下游也在）
        self.assertIn("noc", detail["affected_modules"])
        self.assertIn("async_fifo", detail["affected_modules"])
        d = self.imp.get_change(self.chg["id"])
        self.assertEqual(d["status"], "FROZEN")
        self.assertEqual(d["eco_id"], eid)

    def test_freeze_without_change_id_keeps_legacy_path(self):
        eid = self._eco()
        detail = self.svc.freeze_eco(eid, "wei.dong")
        self.assertEqual(detail["status"], "FROZEN")

    def test_freeze_rejected_after_topology_update(self):
        new = seed_demo_topology()
        new["nodes"].append({"id": "zzz", "clock_domain": "CLK_CFG"})
        self.imp.upload_topology(new, by="admin", note="v2")
        eid = self._eco()
        with self.assertRaises(ImpactError) as cm:
            self.svc.freeze_eco(eid, "wei.dong", self.chg["id"])
        self.assertEqual(cm.exception.code, "CLOSURE_STALE_FOR_FREEZE")
        # ECO 仍未冻结
        self.assertEqual(self.svc.eco_detail(eid)["status"], "DRAFT")
        # 拒绝双侧留痕
        self.assertTrue(any(e["action"] == "gate.denied"
                            for e in self.imp.audit(self.chg["id"])["events"]))

    def test_freeze_excludes_respected_in_scope(self):
        # alu 为可排除候选：排除后不应进入冻结复验范围
        self.imp.exclude_node(
            self.chg["id"], "alu", "仅例化未改,专项覆盖", "wei.dong")
        eid = self._eco()
        detail = self.svc.freeze_eco(eid, "wei.dong", self.chg["id"])
        self.assertNotIn("alu", detail["affected_modules"])
        # 强制节点仍在
        self.assertIn("async_fifo", detail["affected_modules"])


class FingerprintTests(unittest.TestCase):
    def test_summary_or_topology_changes_fp(self):
        tfp = content_fp(seed_demo_topology())[:16]
        f1 = closure_fingerprint(["core"], tfp, {"a": 1})
        f2 = closure_fingerprint(["core"], tfp, {"a": 2})
        f3 = closure_fingerprint(["alu"], tfp, {"a": 1})
        f4 = closure_fingerprint(["core"], tfp + "x", {"a": 1})
        self.assertEqual(len({f1, f2, f3, f4}), 4)


if __name__ == "__main__":
    unittest.main()
