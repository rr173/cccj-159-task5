/* 电路影响闭包操作页：零依赖原生 JS。 */
let state = { changeId: null, closure: null, change: null, selectedNode: null };

const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = new Error(data.message || res.statusText);
    e.payload = data;
    throw e;
  }
  return data;
};

function toast(msg, ok = false) {
  const box = document.getElementById("toast");
  const d = document.createElement("div");
  d.className = "toast " + (ok ? "ok" : "err");
  d.textContent = msg;
  box.appendChild(d);
  setTimeout(() => d.remove(), 6000);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function loadAll() {
  await Promise.all([loadTopologies(), loadChanges(), loadAudit()]);
  if (state.changeId) await loadChange();
}

async function loadTopologies() {
  const d = await api("/api/impact/topologies");
  const box = document.getElementById("topo-list");
  box.innerHTML = d.topologies.map(t =>
    `<div class="row" style="justify-content:space-between">
       <span>v${t.version} <span class="mut">${esc(t.note || "")}</span></span>
       <span class="mut">${t.content_fp.slice(0, 10)}</span>
     </div>`).join("") || '<div class="mut">（尚无拓扑）</div>';
  const latest = d.topologies[d.topologies.length - 1];
  document.getElementById("topo-info").textContent =
    latest ? `当前拓扑 v${latest.version} · fp ${latest.content_fp.slice(0, 10)}` : "无拓扑";
}

async function uploadTopology() {
  let payload;
  try { payload = JSON.parse(document.getElementById("topo-new").value); }
  catch (e) { return toast("拓扑 JSON 解析失败：" + e.message); }
  try {
    const d = await api("/api/impact/topologies", {
      method: "POST",
      body: { topology: payload, note: document.getElementById("topo-note").value,
              by: "web.designer" },
    });
    toast(d.reused ? "内容未变化，复用既有版本" : `已上传拓扑 v${d.version}；旧闭包已全部失效`, true);
    document.getElementById("topo-new").value = "";
    await loadAll();
  } catch (e) { renderError(e); }
}

async function loadChanges() {
  const d = await api("/api/impact/changes");
  const box = document.getElementById("change-list");
  box.innerHTML = d.changes.map(c => {
    const stale = c.stale && c.status !== "FROZEN";
    return `<div class="card ${c.id === state.changeId ? "active" : ""} ${stale ? "stale" : ""}"
                onclick="selectChange('${c.id}')">
        <div class="row" style="justify-content:space-between;margin:0">
          <b>${esc(c.id)}</b>
          <span class="tag t-${esc(c.status)}">${esc(c.status)}</span>
        </div>
        <div>${esc(c.title)}</div>
        <div class="mut" style="font-size:11px">拓扑 v${c.topology_version}
          ${c.eco_id ? "· ECO " + esc(c.eco_id) : ""}
          ${stale ? "· ⚠ 闭包已过期需重算" : ""}</div>
      </div>`;
  }).join("") || '<div class="mut">（尚无变更）</div>';
}

async function createChange() {
  const title = document.getElementById("c-title").value.trim();
  const owner = document.getElementById("c-owner").value.trim();
  const origins = document.getElementById("c-origins").value.split(",").map(s => s.trim()).filter(Boolean);
  let summary = {};
  const st = document.getElementById("c-summary").value.trim();
  if (st) {
    try { summary = JSON.parse(st); } catch (e) { return toast("变更摘要不是合法 JSON：" + e.message); }
  }
  try {
    const d = await api("/api/impact/changes", {
      method: "POST",
      body: { title, owner, origins, summary },
    });
    state.changeId = d.id;
    toast(`变更 ${d.id} 已创建并自动计算闭包`, true);
    await loadAll();
  } catch (e) { renderError(e); }
}

async function selectChange(cid) {
  state.changeId = cid;
  state.selectedNode = null;
  await Promise.all([loadChange(), loadChanges(), loadAudit()]);
}

async function loadChange() {
  const c = await api(`/api/impact/changes/${state.changeId}`);
  state.change = c;
  state.closure = c.closure;
  document.getElementById("c-title").value = c.title;
  document.getElementById("c-origins").value = c.origins.join(",");
  document.getElementById("c-summary").value = JSON.stringify(c.summary, null, 1);
  renderBanner(c);
  if (!c.closure) {
    document.getElementById("nodes-body").innerHTML = "";
    document.getElementById("edges-box").innerHTML =
      `<div class="ban">⚠ ${esc(c.stale_reason || "尚未计算闭包")}</div>`;
    ["st-total", "st-man", "st-opt", "st-exc", "st-eff"].forEach(id =>
      document.getElementById(id).textContent = "-");
    document.getElementById("snap-tag").innerHTML = "";
    return;
  }
  renderClosure(c);
}

function renderBanner(c) {
  const box = document.getElementById("gate-banner");
  if (c.stale_reason) {
    box.innerHTML = `<div class="ban">⛔ ${esc(c.stale_reason)}。<b>旧结果不得用于冻结或发布。</b>
      <button class="sec" onclick="recompute()">立即重算</button></div>`;
  } else if (c.status === "READY") {
    box.innerHTML = `<div class="okbox">✅ 闭包当前有效，变更已可进入审核冻结。</div>`;
  } else if (c.status === "FROZEN") {
    box.innerHTML = `<div class="okbox">🔒 已随 ECO ${esc(c.eco_id)} 冻结，闭包快照固化。</div>`;
  } else {
    box.innerHTML = `<div class="okbox">✅ 闭包基于最新拓扑 v${c.closure ? c.closure.topology_version : "?"}
      与当前摘要计算，为当前版本。</div>`;
  }
}

function renderClosure(c) {
  const cl = c.closure;
  const r = cl.result;
  document.getElementById("snap-tag").innerHTML =
    `<span class="tag t-${cl.state}">${cl.state}</span>
     <span class="mut mono" style="font-size:11px">${cl.id} · fp ${cl.content_fp}</span>`;
  document.getElementById("bind-info").textContent =
    `绑定拓扑 v${cl.topology_version} / ${cl.topology_fp.slice(0, 10)}`;
  document.getElementById("st-total").textContent = r.stats.total;
  document.getElementById("st-man").textContent = r.stats.mandatory;
  document.getElementById("st-opt").textContent = r.stats.optional;
  document.getElementById("st-exc").textContent = r.stats.excluded;
  document.getElementById("st-eff").textContent = r.stats.effective;

  const excluded = new Set(r.excluded_nodes);
  const rows = Object.values(r.nodes).sort((a, b) => a.depth - b.depth || a.id.localeCompare(b.id));
  document.getElementById("nodes-body").innerHTML = rows.map(n => {
    const isExc = excluded.has(n.id);
    const chks = n.checks.map(k =>
      `<span class="chk ${k}" title="${esc(n.check_labels?.[k] || k)}">${k}</span>`).join("");
    const kindBadge = n.structural
      ? `<span class="tag t-struct" title="仅因例化关系需重新集成，可人工排除">结构上溯</span>`
      : (n.depth === 0 ? `<span class="tag t-origin">修改起点</span>`
                        : `<span class="tag t-active">变更传播</span>`);
    let action;
    if (n.mandatory) {
      action = `<span class="mandatory" title="${esc(n.mandatory_reasons.join("；"))}">🔒 强制</span>`;
    } else if (isExc) {
      action = `<button class="sec" onclick="unexclude('${n.id}')">撤销排除</button>`;
    } else {
      action = `<button class="danger" onclick="askExclude('${n.id}')">排除…</button>`;
    }
    return `<tr class="${isExc ? "excluded" : ""}">
      <td><a onclick="selectNode('${n.id}')" style="cursor:pointer"><b>${esc(n.id)}</b></a>
        <div class="mut" style="font-size:11px">${esc(n.name)}</div>
        <div style="margin-top:2px">${kindBadge}</div></td>
      <td class="mono">${esc(n.clock_domain || "—")}${n.interface ? " 🔌" : ""}</td>
      <td>${n.depth}</td>
      <td>${chks}</td>
      <td>${action}</td></tr>`;
  }).join("");

  document.getElementById("edges-box").innerHTML = r.edges.map(e => {
    const cls = e.reverse ? "structural" : (e.cross_clock ? "cross" : e.kind);
    const badge = e.reverse ? "· ⬆ 结构上溯（集成复验）"
      : `${e.cross_clock ? "· ⚡跨时钟" : ""}${e.registered ? "· 寄存器切割" : ""}`;
    return `<div class="edge ${cls}">
       <div class="mut mono">${esc(e.kind_label)} ${badge}</div>
       ${esc(e.text)}
       <div class="path">${esc(e.source)} → ${esc(e.target)}</div>
     </div>`;
  }).join("") || '<div class="mut">起点无下游传播边。</div>';
  if (state.selectedNode) selectNode(state.selectedNode, true);
}

function selectNode(nid, keep) {
  state.selectedNode = nid;
  const cl = state.closure;
  const n = cl.result.nodes[nid];
  if (!n) return;
  const excluded = new Set(cl.result.excluded_nodes);
  const ex = cl.exclusions.find(x => x.node_id === nid);
  let html = `<div class="row" style="justify-content:space-between">
      <b>${esc(n.id)}</b><span class="mut">${esc(n.name)} · ${esc(n.clock_domain || "无时钟域")}</span></div>
    <div class="row">${n.depth === 0 ? '<span class="tag t-origin">修改起点</span>'
      : (n.structural ? '<span class="tag t-struct">结构上溯·集成复验</span>'
                      : '<span class="tag t-active">变更传播</span>')}</div>
    <div class="mut">传播路径：</div><div class="path mono">${n.path.map(esc).join(" → ")}</div>`;
  if (n.incoming_edge) {
    html += `<div class="edge ${n.incoming_edge.reverse ? "structural"
      : (n.incoming_edge.cross_clock ? "cross" : n.incoming_edge.kind)}">
      ${esc(n.incoming_edge.text)}</div>`;
  }
  html += `<div class="mut" style="margin-top:6px">必做检查：</div><div>${
    n.checks.map(k => `<span class="chk ${k}">${k}</span>`).join("")}</div>`;
  if (n.mandatory) {
    html += `<div class="ban">🔒 不可人工排除：<ul style="margin:4px 0;padding-left:18px">
      ${n.mandatory_reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul></div>`;
  } else if (excluded.has(nid)) {
    html += `<div class="ban" style="background:#3a3320;border-color:#7a6230;color:#ffe2b0">
      已排除（${esc(ex.by)}）：${esc(ex.reason)}
      <button class="sec" onclick="unexclude('${nid}')">撤销排除</button></div>`;
  } else {
    html += `<div class="mut">排除理由（必填）：</div>
      <textarea id="ex-reason" placeholder="如：该模块仅复用未改动接口，专项回归已覆盖"></textarea>
      <div class="row"><button class="danger" onclick="doExclude('${nid}')">确认排除该候选</button></div>`;
  }
  document.getElementById("node-detail").innerHTML = html;
}

async function askExclude(nid) { state.selectedNode = nid; selectNode(nid); }

async function doExclude(nid) {
  const reason = document.getElementById("ex-reason").value;
  try {
    await api(`/api/impact/changes/${state.changeId}/exclude`, {
      method: "POST", body: { node_id: nid, reason, by: "web.designer" },
    });
    toast(`已排除 ${nid}（人工覆盖已记入审计）`, true);
    await loadChange(); await loadAudit();
  } catch (e) { renderError(e); }
}

async function unexclude(nid) {
  try {
    await api(`/api/impact/changes/${state.changeId}/unexclude`, {
      method: "POST", body: { node_id: nid, by: "web.designer" },
    });
    toast(`已恢复 ${nid} 复验`, true);
    await loadChange(); await loadAudit();
  } catch (e) { renderError(e); }
}

async function recompute() {
  if (!state.changeId) return;
  try {
    const d = await api(`/api/impact/changes/${state.changeId}/compute`,
      { method: "POST", body: { by: "web.designer" } });
    toast(d.reused ? "输入未变化，返回原快照" : "已基于当前拓扑/摘要重算，旧版本作废", true);
    await loadChange(); await loadAudit(); await loadChanges();
  } catch (e) { renderError(e); }
}

async function editChange() {
  if (!state.changeId) return;
  const origins = document.getElementById("c-origins").value.split(",").map(s => s.trim()).filter(Boolean);
  let summary = {};
  const st = document.getElementById("c-summary").value.trim();
  if (st) { try { summary = JSON.parse(st); } catch (e) { return toast("摘要不是合法 JSON"); } }
  try {
    await api(`/api/impact/changes/${state.changeId}/edit`, {
      method: "POST",
      body: { origins, summary, title: document.getElementById("c-title").value,
              by: "web.designer" },
    });
    toast("起点/摘要已更新，旧闭包快照已失效，请重算", true);
    await loadAll();
  } catch (e) { renderError(e); }
}

async function markReady() {
  if (!state.changeId) return;
  try {
    await api(`/api/impact/changes/${state.changeId}/ready`,
      { method: "POST", body: { by: "web.designer" } });
    toast("门禁通过：变更可进入审核冻结", true);
    await loadAll();
  } catch (e) { renderError(e); await loadAudit(); }
}

async function loadAudit() {
  const q = state.changeId ? `?change_id=${state.changeId}` : "";
  const d = await api("/api/impact/audit" + q);
  const cls = a => a.includes("deny") || a.includes("denied") ? "deny"
    : a.startsWith("exclude") ? "manual" : "auto";
  document.getElementById("audit-box").innerHTML = d.events.slice(0, 60).map(e =>
    `<div class="audit ${cls(e.action)}">
      <span class="act">${esc(e.action)}</span>
      <span class="mut">${esc(e.at?.slice(11))}</span>
      ${e.actor ? "· " + esc(e.actor) : ""}
      <div class="mut" style="font-size:10.5px">${esc(JSON.stringify(e.detail))}</div>
    </div>`).join("") || '<div class="mut">（无审计事件）</div>';
}

function renderError(e) {
  const p = e.payload || {};
  let extra = "";
  if (p.detail?.issues) {
    extra = "<ul>" + p.detail.issues.map(i =>
      `<li><b>${esc(i.where)}</b>${i.node ? " · " + esc(i.node) : ""}：${esc(i.problem)}
       ${i.fix ? "<br>→ " + esc(i.fix) : ""}</li>`).join("") + "</ul>";
  } else if (p.detail?.cycle) {
    extra = `<div>环路径：<b class="mono">${p.detail.cycle.map(esc).join(" → ")}</b></div>
      <div class="okbox" style="margin-top:6px">→ ${esc(p.detail.fix || "")}</div>`;
  } else if (p.detail?.fix) {
    extra = `<div class="okbox" style="margin-top:6px">→ ${esc(p.detail.fix)}</div>`;
  } else if (p.detail?.missing) {
    extra = `<div>缺失：${p.detail.missing.map(esc).join(", ")}</div>
      <div class="mut" style="margin-top:4px">相近节点：${
        Object.entries(p.detail.candidates || {}).map(([k, v]) =>
          `${esc(k)} → ${v.map(esc).join("/")}`).join("；")}</div>`;
  }
  document.getElementById("err-box").innerHTML =
    `<div class="ban"><b>[${esc(p.error || "ERROR")}]</b> ${esc(e.message)}${extra}</div>`;
  toast(`[${p.error || "ERROR"}] ${e.message}`);
}

loadAll();
