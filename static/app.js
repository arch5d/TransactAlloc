/* =====================================================================
   Cloud Management System - dashboard client
   Everything rendered here comes from the live API; nothing is hardcoded.
   ===================================================================== */
"use strict";

const $ = (id) => document.getElementById(id);
const make = (tag, cls, html) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
};
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const num = (n, d = 0) => (n == null || isNaN(n) ? "—" : Number(n).toFixed(d));
const clock = (ts) => {
  const date = new Date(ts);
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toLocaleTimeString([], { hour12: false });
};

let SESSION = localStorage.getItem("cms.user") || null;
let activeAccount = null;      // tab selected in the Account section
let users = [];
let state = null;
let stats = null;
let history = null;
let cycleNodes = new Set();

/* ------------------------------- transport ---------------------------- */
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
}
const post = (p, body) => api(p, {
  method: "POST",
  headers: body ? { "Content-Type": "application/json" } : undefined,
  body: body ? JSON.stringify(body) : undefined,
});

function toast(title, msg, kind = "") {
  const t = make("div", "toast " + kind, `<b>${esc(title)}</b>${esc(msg || "")}`);
  $("toasts").appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; }, 3600);
  setTimeout(() => t.remove(), 4000);
}

/* -------------------------------- login ------------------------------- */
async function initLogin() {
  try {
    users = (await api("/api/users")).users;
  } catch (e) {
    $("loginErr").hidden = false;
    $("loginErr").textContent = "Cannot reach the API: " + e.message;
    return;
  }
  const picker = $("accountPicker");
  picker.innerHTML = "";
  let chosen = SESSION;
  users.forEach((u) => {
    const opt = make("button", "acct-opt" + (u.username === chosen ? " sel" : ""),
      `<span class="avatar">${esc(u.username[0].toUpperCase())}</span>${esc(u.username)}`);
    opt.type = "button";
    opt.onclick = () => {
      picker.querySelectorAll(".acct-opt").forEach((n) => n.classList.remove("sel"));
      opt.classList.add("sel");
      $("loginUser").value = u.username;
    };
    picker.appendChild(opt);
  });
  if (chosen) $("loginUser").value = chosen;
}

async function signIn() {
  const name = ($("loginUser").value || "").trim();
  const err = $("loginErr");
  if (!name) { err.hidden = false; err.textContent = "Pick an account or type a username."; return; }
  if (!users.some((u) => u.username === name)) {
    err.hidden = false; err.textContent = `No account named "${name}".`;
    return;
  }
  err.hidden = true;
  SESSION = name;
  localStorage.setItem("cms.user", name);
  activeAccount = name;
  enterApp();
}

function signOut() {
  SESSION = null;
  localStorage.removeItem("cms.user");
  $("app").hidden = true;
  $("login").style.display = "";
  initLogin();
}

function enterApp() {
  $("login").style.display = "none";
  $("app").hidden = false;
  activeAccount = activeAccount || SESSION;
  refresh();
  refreshUsers();
  refreshStats();
  refreshHistory();
}

/* ------------------------------ navigation ---------------------------- */
function initNav() {
  const links = [...document.querySelectorAll(".nav-link")];
  const obs = new IntersectionObserver((entries) => {
    entries.forEach((en) => {
      if (en.isIntersecting) {
        links.forEach((l) => l.classList.toggle("active",
          l.getAttribute("href") === "#" + en.target.id));
      }
    });
  }, { rootMargin: "-45% 0px -50% 0px" });
  document.querySelectorAll(".section").forEach((s) => obs.observe(s));
}

/* ------------------------------ rendering ----------------------------- */
function fragBadge(v) {
  const cls = v >= 0.5 ? "vhi" : v >= 0.25 ? "hi" : "";
  return `<span class="frag-badge ${cls}">${num(v, 3)}</span>`;
}

function drawStrip(container, blocks, total, big) {
  container.innerHTML = "";
  for (const b of blocks) {
    const seg = make("span", b.state === "FREE" ? "free" : "alloc");
    seg.style.width = ((b.size_mb / total) * 100).toFixed(3) + "%";
    seg.title = `${b.state} · ${b.start_mb}–${b.start_mb + b.size_mb} MB (${b.size_mb} MB)`;
    if (big) seg.style.minWidth = "2px";
    container.appendChild(seg);
  }
}

function renderKPIs() {
  const c = state.counts;
  const running = state.jobs.filter((j) => j.status === "RUNNING");
  const inflight = state.jobs.filter((j) => j.status === "RUNNING" || j.status === "QUEUED");
  const avgWait = inflight.length
    ? inflight.reduce((a, j) => a + j.wait_sec, 0) / inflight.length : 0;
  const peakFrag = Math.max(0, ...state.clusters.map((cl) => cl.frag_index));
  const tp = stats && stats.throughput;
  const perMin = tp && tp.span_sec > 0 ? (tp.done / tp.span_sec) * 60 : 0;
  const events = Array.isArray(state.system_events)
    ? state.system_events.filter((e) => e && typeof e === "object")
    : [];
  const deadlocks = events.filter((e) => e.kind === "DEADLOCK").length;
  const defragRuns = (stats && stats.defrag && stats.defrag.runs) || 0;

  const defs = [
    ["Active jobs", c.running, `${running.length} running`, "k-ok"],
    ["Queue depth", c.queued, "waiting to place", "k-warn"],
    ["Completed", c.completed, `${c.failed} failed`, "k-cyan"],
    ["Throughput", num(perMin, 1), "jobs / min", ""],
    ["Avg wait", num(avgWait, 1) + "s", "in-flight jobs", "k-violet"],
    ["Peak frag", num(peakFrag, 3), `${deadlocks} deadlock evt · ${defragRuns} defrags`, peakFrag >= 0.5 ? "k-danger" : "k-violet"],
  ];
  $("kpis").innerHTML = defs.map(([l, v, s, k]) =>
    `<div class="kpi ${k}"><div class="kpi-label">${l}</div>
     <div class="kpi-value">${v}</div><div class="kpi-sub">${esc(s)}</div></div>`).join("");
}

function renderClusterCards() {
  const wrap = $("clusterCards");
  wrap.innerHTML = "";
  for (const cl of state.clusters) {
    const pc = cl.total_cores ? (cl.allocated_cores / cl.total_cores) * 100 : 0;
    const pr = cl.total_ram_mb ? (cl.allocated_ram_mb / cl.total_ram_mb) * 100 : 0;
    const busy = pc > 80 || pr > 80;
    const card = make("div", "card");
    card.innerHTML = `
      <div class="cluster-head">
        <div><div class="cluster-name">${esc(cl.hostname)}</div>
             <div class="cluster-id">#${cl.machine_id} · ${esc(cl.ip_address)}</div></div>
        <span class="pill ${cl.status === "ONLINE" ? (busy ? "busy" : "online") : "offline"}">
          ${busy && cl.status === "ONLINE" ? "BUSY" : esc(cl.status)}</span>
      </div>
      <div class="metric">
        <div class="metric-row"><span class="k">CPU</span>
          <span class="v">${cl.allocated_cores}/${cl.total_cores} · ${num(pc, 1)}%</span></div>
        <div class="bar"><i class="fill-cpu" style="width:${pc}%"></i></div>
      </div>
      <div class="metric">
        <div class="metric-row"><span class="k">RAM</span>
          <span class="v">${num(cl.allocated_ram_mb / 1024, 1)}/${num(cl.total_ram_mb / 1024, 1)} GB · ${num(pr, 1)}%</span></div>
        <div class="bar"><i class="fill-ram" style="width:${pr}%"></i></div>
      </div>
      <div class="metric">
        <div class="metric-row"><span class="k">Holes</span>
          <span class="v">${cl.holes} · largest ${cl.largest_free_mb} MB</span></div>
        <div class="metric-row"><span class="k">Frag index</span><span class="v">${fragBadge(cl.frag_index)}</span></div>
      </div>
      <div class="strip" data-strip="${cl.machine_id}"></div>
      <div class="strip-legend"><span class="legend-free"><i></i>free</span>
        <span class="legend-alloc"><i></i>allocated</span></div>`;
    wrap.appendChild(card);
    drawStrip(card.querySelector(".strip"), state.maps[String(cl.machine_id)] || [], cl.total_ram_mb, false);
  }
}

function renderAccountTabs() {
  const tabs = $("accountTabs");
  tabs.innerHTML = "";
  const me = SESSION;
  const ordered = [...users].sort((a, b) => (b.username === me) - (a.username === me));
  for (const u of ordered) {
    const t = make("button", "tab" + (u.username === activeAccount ? " active" : ""),
      esc(u.username) + (u.username === me ? " · you" : ""));
    t.onclick = () => { activeAccount = u.username; renderAccountTabs(); renderAccount(); };
    tabs.appendChild(t);
  }
}

function renderAccount() {
  const u = users.find((x) => x.username === activeAccount);
  if (!u) { $("accountCard").innerHTML = `<div class="empty">Select an account.</div>`; return; }
  const cpuPct = u.quota_cpus ? (u.used_cpus / u.quota_cpus) * 100 : 0;
  const ramPct = u.quota_ram_mb ? (u.used_ram_mb / u.quota_ram_mb) * 100 : 0;
  $("accountCard").innerHTML = `
    <div class="acct-ident">
      <span class="avatar">${esc(u.username[0].toUpperCase())}</span>
      <div><h3>${esc(u.username)}</h3><span class="acct-role">${esc(u.role)}</span>
        <div class="muted small" style="margin-top:6px">
          ${u.active_clusters.length ? "active on " + u.active_clusters.map((m) => "#" + m).join(", ") : "not on any cluster"}</div>
      </div>
    </div>
    <div class="gauge">
      <div class="metric-row"><span class="k">CPU quota</span>
        <span class="v">${u.used_cpus}/${u.quota_cpus} cores</span></div>
      <div class="bar"><i class="fill-cpu" style="width:${cpuPct}%"></i></div>
      <div class="metric-row" style="margin-top:8px"><span class="k">RAM quota</span>
        <span class="v">${num(u.used_ram_mb / 1024, 1)}/${num(u.quota_ram_mb / 1024, 1)} GB</span></div>
      <div class="bar"><i class="fill-ram" style="width:${ramPct}%"></i></div>
    </div>
    <div class="gauge">
      <div class="metric-row"><span class="k">Queued</span><span class="v">${u.queued}</span></div>
      <div class="metric-row"><span class="k">Running</span><span class="v">${u.running}</span></div>
      <div class="metric-row"><span class="k">Completed</span><span class="v">${u.completed}</span></div>
      <div class="metric-row"><span class="k">Failed</span><span class="v">${u.failed}</span></div>
    </div>`;
  $("submitAs").textContent = u.username;

  const mine = state.jobs.filter((j) => j.username === u.username).slice(0, 12);
  const box = $("accountJobs");
  box.innerHTML = mine.length ? "" : `<div class="empty">No jobs from this account yet.</div>`;
  for (const j of mine) {
    box.appendChild(make("div", "mini-job", `
      <span class="jid">#${j.job_id}</span>
      <span class="task">${esc(j.task_name)}</span>
      <span class="status ${j.status}">${j.status}</span>
      <span class="meta">${j.req_cpus}c · ${j.req_ram_mb}MB</span>`));
  }
}

function renderClusterDetails() {
  const wrap = $("clusterDetails");
  wrap.innerHTML = "";
  for (const cl of state.clusters) {
    const pc = cl.total_cores ? (cl.allocated_cores / cl.total_cores) * 100 : 0;
    const pr = cl.total_ram_mb ? (cl.allocated_ram_mb / cl.total_ram_mb) * 100 : 0;
    const row = make("div", "cluster-row");
    row.innerHTML = `
      <div>
        <div class="cluster-name">${esc(cl.hostname)} <span class="cluster-id">#${cl.machine_id}</span></div>
        <div class="cluster-id">${esc(cl.ip_address)} · ${esc(cl.status)} · ${num(cl.clock_speed_ghz, 2)} GHz/core</div>
        <div class="strip-legend" style="margin-top:10px"><span class="legend-free"><i></i>free</span>
          <span class="legend-alloc"><i></i>allocated</span></div>
      </div>
      <div>
        <div class="strip" data-strip="${cl.machine_id}"></div>
        <div class="cluster-stats" style="margin-top:14px">
          <span class="k">Cores</span><span class="v">${cl.allocated_cores}/${cl.total_cores} (${num(pc, 1)}%)</span>
          <span class="k">RAM</span><span class="v">${num(cl.allocated_ram_mb / 1024, 1)}/${num(cl.total_ram_mb / 1024, 1)} GB (${num(pr, 1)}%)</span>
          <span class="k">Holes</span><span class="v">${cl.holes}</span>
          <span class="k">Largest free</span><span class="v">${cl.largest_free_mb} MB</span>
        </div>
      </div>
      <div style="text-align:right">
        <div class="metric-row"><span class="k">Frag index</span><span class="v">${fragBadge(cl.frag_index)}</span></div>
        <button class="btn btn-sm" data-defrag="${cl.machine_id}" style="margin-top:10px">Defrag</button>
      </div>`;
    wrap.appendChild(row);
    drawStrip(row.querySelector(".strip"), state.maps[String(cl.machine_id)] || [], cl.total_ram_mb, true);
    row.querySelector("[data-defrag]").onclick = () => doDefrag(cl.machine_id);
  }
}

function renderJobs() {
  const statusF = $("jobStatusFilter").value;
  const userF = $("jobUserFilter").value;
  const tb = $("jobsBody");
  tb.innerHTML = "";
  const rows = state.jobs.filter((j) =>
    (statusF === "ALL" || j.status === statusF) && (userF === "ALL" || j.username === userF));
  $("jobsSub").textContent =
    `${rows.length} of ${state.jobs.length} jobs shown` + (userF !== "ALL" ? ` · account ${userF}` : "");
  if (!rows.length) { tb.appendChild(make("tr", "", `<td colspan="11" class="empty">No jobs match the filter.</td>`)); return; }
  for (const j of rows) {
    tb.appendChild(make("tr", "", `
      <td class="mono">#${j.job_id}</td><td>${esc(j.task_name)}</td><td>${esc(j.username)}</td>
      <td class="mono">${j.req_cpus}</td><td class="mono">${j.req_ram_mb}</td><td class="mono">${j.priority}</td>
      <td><span class="status ${j.status}">${j.status}</span></td>
      <td class="mono">${j.machine_id ?? "—"}</td><td class="mono">${j.remaining_sec}</td>
      <td class="mono">${num(j.wait_sec, 1)}</td><td class="mono">${num(j.turnaround_sec, 1)}</td>`));
  }
}

function renderDeadlock() {
  const wfg = state.wfg || {};
  const edges = Array.isArray(wfg.edges) ? wfg.edges : [];
  const details = Array.isArray(wfg.edge_details) ? wfg.edge_details : [];
  cycleNodes = detectCycle(edges);
  drawWFG($("wfgGraph"), edges, cycleNodes, details);
  $("wfgNote").textContent = edges.length
    ? `${edges.length} wait-for edge(s)` + (cycleNodes.size ? " · cycle present!" : " · acyclic")
    : "No wait-for edges — no deadlock possible right now.";

  const feed = $("eventFeed");
  feed.innerHTML = "";
  const evs = Array.isArray(state.system_events)
    ? state.system_events.filter((e) => e && typeof e === "object").slice(0, 40)
    : [];
  const latestDeadlock = evs.find((e) => e.kind === "DEADLOCK");
  const summary = $("deadlockSummary");
  summary.hidden = !latestDeadlock;
  if (latestDeadlock) {
    summary.innerHTML = `<b>Deadlock recovery</b><span>${esc(latestDeadlock.message)}</span>`;
  }
  if (!evs.length) feed.appendChild(make("div", "empty", "No system events yet."));
  for (const e of evs) {
    const kind = esc(e.kind || "EVENT");
    const severity = esc(e.severity || "INFO");
    const deadlockClass = e.kind === "DEADLOCK" ? " deadlock-event" : "";
    feed.appendChild(make("div", "event sev-" + severity + deadlockClass, `
      <span class="ts">${clock(e.ts)}</span>
      <span class="kind ${kind}">${kind}</span>
      <span>${esc(e.message)}</span>`));
  }
}

function detectCycle(edges) {
  const safeEdges = Array.isArray(edges)
    ? edges.filter((edge) => Array.isArray(edge) && edge.length >= 2
      && edge[0] != null && edge[1] != null)
    : [];
  const adj = {};
  for (const [a, b] of safeEdges) (adj[a] = adj[a] || []).push(b);
  const nodes = new Set(safeEdges.flat());
  const color = {}, stack = new Set();
  let found = new Set();
  const dfs = (n, path) => {
    color[n] = 1; stack.add(n);
    for (const m of (adj[n] || [])) {
      if (color[m] === 1) {
        const i = path.indexOf(m);
        (i >= 0 ? path.slice(i) : [m]).forEach((x) => found.add(x));
      } else if (!color[m]) dfs(m, path.concat(m));
    }
    color[n] = 2; stack.delete(n);
  };
  for (const n of nodes) if (!color[n]) dfs(n, [n]);
  return found;
}

function edgeKey(waiter, holder) {
  return `${waiter}:${holder}`;
}

function edgeLabel(detail) {
  if (!detail) return "wait relationship";
  const resources = [];
  if (detail.requested_cpus != null) resources.push(`${detail.requested_cpus} CPU`);
  if (detail.requested_ram_mb != null) resources.push(`${detail.requested_ram_mb} MB RAM`);
  const cluster = detail.machine_id != null ? `cluster ${detail.machine_id}` : "cluster unknown";
  const reason = detail.reason ? String(detail.reason).split("\n")[0] : "resource unavailable";
  return `${resources.length ? resources.join(" + ") + " on " : ""}${cluster}: ${reason}`;
}

function drawWFG(container, edges, cycle, details = []) {
  container.innerHTML = "";
  const safeEdges = edges.filter((edge) => Array.isArray(edge) && edge.length >= 2);
  const detailMap = new Map(details
    .filter((detail) => detail && detail.waiter != null && detail.holder != null)
    .map((detail) => [edgeKey(detail.waiter, detail.holder), detail]));
  const nodes = [...new Set(safeEdges.flat())];
  if (!nodes.length) { container.appendChild(make("div", "muted small", "Graph is empty.")); return; }
  const W = 420, H = 280, R = Math.min(W, H) / 2 - 46, cx = W / 2, cy = H / 2;
  const pos = {};
  nodes.forEach((n, i) => {
    const a = (2 * Math.PI * i) / nodes.length - Math.PI / 2;
    pos[n] = [cx + R * Math.cos(a), cy + R * Math.sin(a)];
  });
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("width", W); svg.setAttribute("height", H);
  const defs = document.createElementNS(NS, "defs");
  defs.innerHTML =
    `<marker id="ah" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto">
       <path d="M0,0 L7,3 L0,6 Z" fill="#94a3b8"/></marker>
     <marker id="ahr" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto">
       <path d="M0,0 L7,3 L0,6 Z" fill="#ef4444"/></marker>`;
  svg.appendChild(defs);
  for (const [a, b] of safeEdges) {
    const [x1, y1] = pos[a], [x2, y2] = pos[b];
    const inCycle = cycle.has(a) && cycle.has(b);
    const label = edgeLabel(detailMap.get(edgeKey(a, b)));
    const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy) || 1;
    const ux = dx / len, uy = dy / len, rr = 24;
    const line = document.createElementNS(NS, "line");
    line.setAttribute("x1", x1 + ux * rr); line.setAttribute("y1", y1 + uy * rr);
    line.setAttribute("x2", x2 - ux * rr); line.setAttribute("y2", y2 - uy * rr);
    line.setAttribute("stroke", inCycle ? "#ef4444" : "#94a3b8");
    line.setAttribute("stroke-width", inCycle ? 2.6 : 1.6);
    line.setAttribute("marker-end", inCycle ? "url(#ahr)" : "url(#ah)");
    const title = document.createElementNS(NS, "title");
    title.textContent = `Job #${a} waits for Job #${b}: ${label}`;
    line.appendChild(title);
    svg.appendChild(line);
    const mid = document.createElementNS(NS, "text");
    mid.setAttribute("x", (x1 + x2) / 2);
    mid.setAttribute("y", (y1 + y2) / 2 - 5);
    mid.setAttribute("text-anchor", "middle");
    mid.setAttribute("font-size", "9");
    mid.setAttribute("fill", inCycle ? "#a11c1c" : "#64748b");
    mid.textContent = `${a} → ${b}`;
    const labelTitle = document.createElementNS(NS, "title");
    labelTitle.textContent = label;
    mid.appendChild(labelTitle);
    svg.appendChild(mid);
  }
  for (const n of nodes) {
    const [x, y] = pos[n];
    const c = document.createElementNS(NS, "circle");
    c.setAttribute("cx", x); c.setAttribute("cy", y); c.setAttribute("r", 20);
    c.setAttribute("fill", cycle.has(n) ? "#fdecec" : "#eef0ff");
    c.setAttribute("stroke", cycle.has(n) ? "#ef4444" : "#6366f1");
    c.setAttribute("stroke-width", 2);
    svg.appendChild(c);
    const t = document.createElementNS(NS, "text");
    t.setAttribute("x", x); t.setAttribute("y", y + 4); t.setAttribute("text-anchor", "middle");
    t.setAttribute("font-size", "12"); t.setAttribute("font-weight", "700");
    t.setAttribute("fill", cycle.has(n) ? "#a11c1c" : "#3730a3");
    t.textContent = "#" + n;
    svg.appendChild(t);
  }
  container.appendChild(svg);
}

/* ------------------------------- charts ------------------------------- */
function lineChart(container, series, { height = 220, yMax = null } = {}) {
  container.innerHTML = "";
  const pts = series.flatMap((s) => s.points);
  if (!pts.length) { container.appendChild(make("div", "muted small", "No samples yet — start the engine to collect metrics.")); return; }
  const W = 560, H = height, padL = 40, padR = 12, padT = 14, padB = 26;
  const xmin = Math.min(...pts.map((p) => p.x)), xmax = Math.max(...pts.map((p) => p.x));
  const maxY = yMax != null ? yMax : Math.max(1, ...pts.map((p) => p.y)) * 1.15;
  const X = (x) => padL + ((x - xmin) / (xmax - xmin || 1)) * (W - padL - padR);
  const Y = (y) => H - padB - (y / maxY) * (H - padT - padB);
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  for (let i = 0; i <= 4; i++) {
    const y = padT + (i / 4) * (H - padT - padB);
    const g = document.createElementNS(NS, "line");
    g.setAttribute("x1", padL); g.setAttribute("x2", W - padR); g.setAttribute("y1", y); g.setAttribute("y2", y);
    g.setAttribute("stroke", "#eef0f4");
    svg.appendChild(g);
    const lab = document.createElementNS(NS, "text");
    lab.setAttribute("x", padL - 6); lab.setAttribute("y", y + 4); lab.setAttribute("text-anchor", "end");
    lab.setAttribute("font-size", "10"); lab.setAttribute("fill", "#94a3b8");
    lab.textContent = num(maxY * (1 - i / 4), 0);
    svg.appendChild(lab);
  }
  for (const s of series) {
    if (!s.points.length) continue;
    const d = s.points.map((p, i) => `${i ? "L" : "M"}${X(p.x).toFixed(1)},${Y(p.y).toFixed(1)}`).join(" ");
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", d); path.setAttribute("fill", "none");
    path.setAttribute("stroke", s.color); path.setAttribute("stroke-width", 2);
    path.setAttribute("stroke-linejoin", "round");
    svg.appendChild(path);
  }
  container.appendChild(svg);
  const legend = make("div", "strip-legend");
  series.forEach((s) => legend.appendChild(make("span", "", `<i style="background:${s.color}"></i>${esc(s.name)}`)));
  container.appendChild(legend);
}

function renderCharts() {
  const eng = history ? history.engine : [];
  lineChart($("chartQueue"), [
    { name: "queue depth", color: "#f59e0b", points: eng.map((e) => ({ x: e.t, y: e.queue_depth })) },
    { name: "running", color: "#10b981", points: eng.map((e) => ({ x: e.t, y: e.running_jobs })) },
  ]);

  const byHost = {};
  (history ? history.metrics : []).forEach((m) => (byHost[m.hostname] = byHost[m.hostname] || []).push({ x: m.t, y: m.frag_index }));
  const colors = ["#6366f1", "#06b6d4", "#8b5cf6", "#10b981"];
  lineChart($("chartFrag"),
    Object.keys(byHost).map((h, i) => ({ name: h, color: colors[i % colors.length], points: byHost[h] })));
}

function renderStats() {
  if (!stats || !state) return;
  const tb = $("txnBody");
  tb.innerHTML = "";
  (stats.txn || []).forEach((t) => tb.appendChild(make("tr", "", `
    <td>${esc(t.op)}</td><td>${esc(t.outcome)}</td>
    <td class="mono">${t.n}</td><td class="mono">${num(t.avg_ms, 2)}</td><td class="mono">${t.max_ms}</td>`)));
  if (!(stats.txn || []).length) tb.appendChild(make("tr", "", `<td colspan="5" class="empty">No transactions logged.</td>`));

  const ub = $("utilBody");
  ub.innerHTML = "";
  (stats.utilization || []).forEach((u) => ub.appendChild(make("tr", "", `
    <td>${esc(u.hostname)}</td>
    <td class="mono">${num(u.avg_cpu_pct, 1)}</td>
    <td class="mono">${num(u.avg_ram_pct, 1)}</td>
    <td class="mono">${num(u.avg_frag, 3)}</td>`)));
  if (!(stats.utilization || []).length) ub.appendChild(make("tr", "", `<td colspan="4" class="empty">No samples yet.</td>`));

  const dl = $("defragList");
  dl.innerHTML = "";
  if (!state.defrags.length) dl.appendChild(make("div", "empty", "no defragmentation runs yet"));
  state.defrags.forEach((d) => {
    const better = d.frag_after <= d.frag_before;
    dl.appendChild(make("div", "defrag-card", `
      <div class="host">${esc(d.hostname)} <span class="muted small">[${esc(d.triggered_by)}]</span></div>
      <div class="row"><span>frag index</span>
        <span class="${better ? "good" : ""}">${num(d.frag_before, 3)} &rarr; ${num(d.frag_after, 3)}</span></div>
      <div class="row"><span>holes</span><span>${d.holes_before} &rarr; ${d.holes_after}</span></div>
      <div class="row"><span>moved</span><span>${d.blocks_moved} blocks · ${d.mb_moved} MB</span></div>
      <div class="row"><span>duration</span><span>${d.duration_ms} ms</span></div>`));
  });
}

/* ------------------------------ top bar ------------------------------- */
function renderTopbar() {
  $("engineChip").classList.toggle("on", state.engine.running);
  $("engineText").textContent =
    state.engine.running ? `running · ${state.engine.workers}w` : "stopped";
  $("queueChip").textContent = `queue ${state.counts.queued}`;
  $("policyChip").textContent = `${state.policy.mode}/${state.policy.current}`;
  $("policyChip").title = state.policy.reason || "";

  const u = users.find((x) => x.username === SESSION);
  if (u) {
    $("acctAvatar").textContent = u.username[0].toUpperCase();
    $("acctName").textContent = u.username;
    $("acctRole").textContent = `${u.role} · ${u.used_cpus}/${u.quota_cpus}c`;
  }
}

/* ------------------------------- polling ------------------------------ */
async function refresh() {
  try {
    state = await api("/api/state");
    renderTopbar();
    renderKPIs();
    renderClusterCards();
    if (users.length) { renderAccountTabs(); renderAccount(); }
    renderClusterDetails();
    renderJobs();
    renderDeadlock();
    renderStats();
  } catch (e) { console.warn(e); }
}
async function refreshUsers() { try { users = (await api("/api/users")).users; populateUserFilter(); renderTopbar(); if (state) { renderAccountTabs(); renderAccount(); } } catch (e) {} }
async function refreshStats() { try { stats = await api("/api/stats"); renderStats(); renderKPIs(); } catch (e) {} }
async function refreshHistory() { try { history = await api("/api/history?minutes=15"); renderCharts(); } catch (e) {} }

/* ------------------------------- actions ------------------------------ */
async function doDefrag(cluster) {
  try {
    const r = await post("/api/defrag", { cluster });
    const n = (r.results || []).length;
    toast("Defragmentation", `${n} cluster(s) compacted`, "ok");
    refresh(); refreshStats();
  } catch (e) { toast("Defrag failed", e.message, "err"); }
}

function wireActions() {
  $("signin").onclick = signIn;
  $("loginUser").addEventListener("keydown", (e) => { if (e.key === "Enter") signIn(); });
  $("logout").onclick = signOut;

  $("start").onclick = async () => {
    try { await post(`/api/engine/start?workers=4&policy=${$("polMode").value}`); toast("Engine", "started", "ok"); refresh(); }
    catch (e) { toast("Start failed", e.message, "err"); }
  };
  $("stop").onclick = async () => {
    try { await post("/api/engine/stop"); toast("Engine", "stopping", "ok"); refresh(); }
    catch (e) { toast("Stop failed", e.message, "err"); }
  };
  $("polMode").onchange = async () => {
    try { await post("/api/policy", { mode: $("polMode").value }); refresh(); }
    catch (e) { toast("Policy change failed", e.message, "err"); }
  };
  $("reset").onclick = async () => {
    if (!confirm("Reset all jobs and rebuild block maps? The engine will stop.")) return;
    try { await post("/api/reset"); toast("Reset", "clusters cleared", "ok"); refresh(); refreshStats(); refreshHistory(); }
    catch (e) { toast("Reset failed", e.message, "err"); }
  };
  $("batch").onclick = async () => {
    const count = Math.max(1, Math.min(500, +$("batchCount").value || 20));
    try { await post("/api/jobs/batch", { count, seed: 7 }); toast("Batch", `${count} jobs queued`, "ok"); refresh(); }
    catch (e) { toast("Batch failed", e.message, "err"); }
  };
  $("defragAll").onclick = () => doDefrag("all");

  $("jobForm").onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      const r = await post("/api/jobs", {
        user: activeAccount, task: $("jTask").value || "Task",
        cpus: +$("jCpus").value, ram_mb: +$("jRam").value,
        priority: +$("jPri").value, duration_sec: +$("jDur").value,
      });
      toast("Submitted", `job #${r.job_id} as ${activeAccount}`, "ok");
      refresh(); refreshUsers();
    } catch (e) {
      const msg = /quota/i.test(e.message) ? "quota exceeded for " + activeAccount : e.message;
      toast("Submit failed", msg, "err");
    }
  };

  $("jobStatusFilter").onchange = renderJobs;
  $("jobUserFilter").onchange = renderJobs;
  $("dlDemo").onclick = async () => {
    const box = $("demoTranscript");
    box.classList.remove("muted"); box.textContent = "Running…";
    try {
      const d = await post("/api/deadlock-demo");
      box.textContent = d.transcript.join("\n");
      toast("Deadlock demo", "circular wait resolved", "ok");
      refresh(); refreshStats();
    } catch (e) { box.textContent = "Demo failed: " + e.message; toast("Demo failed", e.message, "err"); }
  };
}

function populateUserFilter() {
  const sel = $("jobUserFilter");
  const cur = sel.value;
  sel.innerHTML = `<option value="ALL">All accounts</option>` +
    users.map((u) => `<option value="${esc(u.username)}">${esc(u.username)}</option>`).join("");
  sel.value = cur || "ALL";
}

/* -------------------------------- boot -------------------------------- */
wireActions();
initNav();
initLogin();
setInterval(() => { if (SESSION && state) refresh(); }, 1000);
setInterval(() => { if (SESSION) refreshUsers(); }, 2500);
setInterval(() => { if (SESSION) { refreshStats(); refreshHistory(); } }, 4000);
if (SESSION) enterApp();
