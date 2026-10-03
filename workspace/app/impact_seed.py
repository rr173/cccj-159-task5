"""演示用版本化拓扑：一个小型 SoC 子系统。

时钟域：
  CLK_CORE（核心域）/ CLK_BUS（总线域）/ CLK_CFG（低速配置域）
覆盖素材：实例化层次、同域数据流、跨时钟数据流（强制 CDC）、接口契约边、
策略强制节点、约束引用。图是无环的（跨域数据路径经 async_fifo 寄存器切割）。
"""
from __future__ import annotations


def seed_demo_topology() -> dict:
    nodes = [
        # 顶层与核心域
        {"id": "soc_top", "kind": "hier", "name": "SoC 顶层", "clock_domain": "CLK_CORE"},
        {"id": "core", "kind": "module", "name": "CPU 核心", "clock_domain": "CLK_CORE"},
        {"id": "alu", "kind": "module", "name": "ALU 运算单元", "clock_domain": "CLK_CORE",
         "checks": ["TIMING"]},
        {"id": "lsu", "kind": "module", "name": "访存单元 LSU", "clock_domain": "CLK_CORE"},
        {"id": "l1i", "kind": "module", "name": "L1 ICache", "clock_domain": "CLK_CORE"},
        {"id": "l1d", "kind": "module", "name": "L1 DCache", "clock_domain": "CLK_CORE"},
        # 跨域桥梁
        {"id": "async_fifo", "kind": "module", "name": "异步 FIFO（跨域桥）",
         "clock_domain": "CLK_CORE|CLK_BUS", "interface": True,
         "checks": ["CDC", "TIMING"]},
        # 总线域
        {"id": "noc", "kind": "module", "name": "片上网络 NoC", "clock_domain": "CLK_BUS"},
        {"id": "l2c", "kind": "module", "name": "L2 Cache 控制器", "clock_domain": "CLK_BUS"},
        {"id": "dma", "kind": "module", "name": "DMA 引擎", "clock_domain": "CLK_BUS"},
        # 配置域（对外接口 + 策略强制）
        {"id": "cfg_bus", "kind": "module", "name": "配置总线 APB", "clock_domain": "CLK_CFG",
         "interface": True},
        {"id": "pin_ctrl", "kind": "module", "name": "引脚/IO 控制", "clock_domain": "CLK_CFG",
         "interface": True,
         "policy_forced": True, "policy_reason": "芯片出厂策略：IO 环形复用必须随任何配置变更复验"},
        {"id": "rst_ctrl", "kind": "module", "name": "复位控制器",
         "clock_domain": "CLK_CFG", "checks": ["RESET"]},
    ]
    edges = [
        {"source": "soc_top", "target": "core", "kind": "instance", "name": "u_core"},
        {"source": "soc_top", "target": "noc", "kind": "instance", "name": "u_noc"},
        {"source": "soc_top", "target": "cfg_bus", "kind": "instance", "name": "u_cfg"},
        {"source": "core", "target": "alu", "kind": "instance", "name": "u_alu"},
        {"source": "core", "target": "lsu", "kind": "instance", "name": "u_lsu"},
        {"source": "lsu", "target": "l1d", "kind": "dataflow", "name": "ld_st_req"},
        {"source": "core", "target": "l1i", "kind": "dataflow", "name": "fetch"},
        {"source": "l1d", "target": "async_fifo", "kind": "dataflow",
         "name": "wdata", "crossing": True},
        {"source": "async_fifo", "target": "noc", "kind": "dataflow",
         "name": "bus_tx", "crossing": True, "registered": True},
        {"source": "noc", "target": "l2c", "kind": "dataflow", "name": "req_chn"},
        {"source": "noc", "target": "dma", "kind": "dataflow", "name": "dma_req"},
        {"source": "dma", "target": "l2c", "kind": "dataflow", "name": "fill"},
        {"source": "cfg_bus", "target": "core", "kind": "dataflow",
         "name": "cfg_regs", "crossing": True, "registered": True},
        {"source": "cfg_bus", "target": "pin_ctrl", "kind": "dataflow", "name": "io_mux"},
        {"source": "rst_ctrl", "target": "cfg_bus", "kind": "reset", "name": "por_n"},
        # 接口契约：CPU↔桥、NoC↔L2 两侧协议必须一起复验
        {"source": "async_fifo", "target": "noc", "kind": "contract",
         "name": "AXI-Stream CDC 握手契约"},
        {"source": "noc", "target": "l2c", "kind": "contract",
         "name": "AXI4 主从契约"},
        # 策略：任何 DMA 改动都必须复验 NoC（带宽/QoS 策略）
        {"source": "dma", "target": "noc", "kind": "policy",
         "name": "NoC QoS 回归策略 v3"},
    ]
    constraints = [
        {"id": "sdc_clk_core", "ctype": "create_clock",
         "name": "CLK_CORE 主时钟 1.2ns", "target_nodes": ["core"]},
        {"id": "sdc_clk_bus", "ctype": "create_clock",
         "name": "CLK_BUS 总线时钟 2.0ns", "target_nodes": ["noc"]},
        {"id": "sdc_fp_cfg", "ctype": "false_path",
         "name": "配置口静态配置例外路径", "target_nodes": ["cfg_bus"]},
    ]
    return {"schema": "circuit-topology/1", "nodes": nodes, "edges": edges,
            "constraints": constraints}


def cyclic_topology() -> dict:
    """反例：组合环 A→B→C→A，用于演示 CLOSURE_CYCLE 可操作错误。"""
    base = seed_demo_topology()
    base["edges"].extend([
        {"source": "alu", "target": "lsu", "kind": "dataflow", "name": "comb_fwd"},
        {"source": "lsu", "target": "l1d", "kind": "dataflow", "name": "comb_mid"},
        {"source": "l1d", "target": "alu", "kind": "dataflow", "name": "comb_back"},
    ])
    return base
