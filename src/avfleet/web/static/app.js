"use strict";
// Renders /api/status. All device-supplied text goes through textContent (never innerHTML).

const REFRESH_MS = 15000;
let eventFilter = "all";
let lastData = null;

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
};

function ago(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function sparkline(values) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", "0 0 100 28");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  const pts = values.map((v, i) => [i, v]).filter(([, v]) => v !== null);
  svg.setAttribute("aria-label", pts.length ? "24 hour response time trend" : "no response time data yet");
  if (pts.length < 2) return svg;
  const max = Math.max(...pts.map(([, v]) => v)) || 1;
  const step = 100 / (values.length - 1);
  let d = "";
  let prev = -2;
  for (const [i, v] of pts) {
    const x = (i * step).toFixed(1);
    const y = (26 - (v / max) * 24).toFixed(1);
    d += (i === prev + 1 ? "L" : "M") + x + " " + y + " ";
    prev = i;
  }
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", d.trim());
  svg.appendChild(path);
  return svg;
}

const METRIC_LABELS = {
  power: (v) => `power ${v}`, input: (v) => `input ${v}`, clients: (v) => `${v} clients`,
  poe_watts: (v) => `PoE ${v} W`, ports_up: null, ports_total: null, model: (v) => v,
  cpu_pct: (v) => `cpu ${Math.round(v)}%`, mem_pct: (v) => `mem ${Math.round(v)}%`,
  upgrade_available: () => "update available", volume: (v) => `vol ${v}`,
  core_cpu: (v) => `core cpu ${Math.round(v)}%`,
  firmware: (v) => `firmware ${v}`, version: (v) => `firmware ${v}`,
  firmware_package: (v) => `package ${v}`,
};

function chips(dev) {
  const wrap = el("div", "chips");
  const m = dev.metrics || {};
  if (m.ports_total) wrap.appendChild(el("span", "chip", `${m.ports_up}/${m.ports_total} ports`));
  for (const [k, v] of Object.entries(m)) {
    const f = METRIC_LABELS[k];
    if (!f || v === null || v === undefined || v === "") continue;
    if (k === "poe_watts" && !v) continue; // no PoE draw (or no PoE ports): nothing worth showing
    wrap.appendChild(el("span", "chip", f(v)));
  }
  if (dev.self_healing) wrap.appendChild(el("span", "chip heal", `self-healing: ${dev.self_healing}`));
  return wrap;
}

function tile(dev) {
  const t = el("article", "tile");
  t.dataset.status = dev.status;
  if (dev.maintenance) t.dataset.maintenance = "1";
  t.setAttribute("aria-label", `${dev.name}: ${dev.maintenance ? "in maintenance" : dev.status}`);
  const head = el("div", "row");
  head.append(el("div", "name", dev.name), el("div", "pill", dev.maintenance ? "maintenance" : dev.status));
  t.appendChild(head);
  t.appendChild(el("div", "detail", dev.detail));
  const stats = el("div", "stats");
  const stat = (k, v, sub) => {
    const s = el("div", "stat");
    const val = el("div", "v", v);
    s.append(el("div", "k", k), val);
    if (sub) s.appendChild(el("div", "typ", sub));
    return s;
  };
  stats.append(
    stat("response", dev.latency_ms === null ? "—" : `${Math.round(dev.latency_ms)} ms`,
         dev.latency_typical_ms ? `typically ${Math.round(dev.latency_typical_ms)} ms` : null),
    stat("24h uptime", dev.uptime_24h === null ? "—" : `${dev.uptime_24h}%`),
    stat(dev.status === "up" ? "up for" : "for", ago(dev.since)),
  );
  t.appendChild(stats);
  t.appendChild(sparkline(dev.latency_24h || []));
  t.appendChild(chips(dev));
  if (dev.maintenance) {
    const until = new Date(dev.maintenance.until * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const why = dev.maintenance.reason ? ` · ${dev.maintenance.reason}` : "";
    t.appendChild(el("div", "notice maint", `In maintenance until ${until}${why}. Alerts and self-healing paused.`));
  } else if (dev.metrics && dev.metrics.notice) {
    t.appendChild(el("div", "notice warn", dev.metrics.notice));
  }
  if (lastData && lastData.controls && lastData.controls.maintenance) t.appendChild(maintControls(dev));
  return t;
}

// --- maintenance controls --------------------------------------------------------------
let openPicker = null; // device id whose duration picker is open (survives the 15 s re-render)
const DURATIONS = [[15, "15 min"], [30, "30 min"], [60, "1 hour"], [120, "2 hours"]];

function storedPin() { try { return localStorage.getItem("avfleet-pin") || ""; } catch (e) { return ""; } }
function savePin(p) { try { localStorage.setItem("avfleet-pin", p); } catch (e) { /* private mode */ } }

async function postControl(path, payload, retried) {
  const headers = { "Content-Type": "application/json" };
  const pin = storedPin();
  if (pin) headers["X-AVFleet-PIN"] = pin;
  const res = await fetch(path, { method: "POST", headers, body: JSON.stringify(payload) });
  if (res.status === 401 && !retried) {
    const entered = window.prompt("Dashboard PIN");
    if (entered === null) return false;
    savePin(entered.trim());
    return postControl(path, payload, true);
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).error || msg; } catch (e) { /* not JSON */ }
    window.alert(`Couldn't change maintenance: ${msg}`);
    return false;
  }
  return true;
}

function maintControls(dev) {
  const row = el("div", "controls");
  const btn = (label, cls, onClick) => {
    const b = el("button", cls, label);
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
  };
  if (dev.maintenance) {
    row.appendChild(btn("End maintenance", "ctl end", async () => {
      if (await postControl("/api/maintenance/end", { device: dev.id })) refresh();
    }));
  } else if (openPicker === dev.id) {
    row.appendChild(el("span", "ctl-label", "Maintenance for"));
    for (const [mins, label] of DURATIONS) {
      row.appendChild(btn(label, "ctl dur", async () => {
        openPicker = null;
        if (await postControl("/api/maintenance", { device: dev.id, minutes: mins })) refresh(); else render();
      }));
    }
    row.appendChild(btn("Cancel", "ctl cancel", () => { openPicker = null; render(); }));
  } else {
    row.appendChild(btn("Maintenance", "ctl start", () => { openPicker = dev.id; render(); }));
  }
  return row;
}

function render() {
  if (!lastData) return;
  renderClaude(lastData.mcp);
  renderSummary(lastData.devices);
  renderGroups(lastData.devices);
}

const ACTION_NAMES = { maintenance: "maintenance", self_healing: "self-healing", lutron_buttons: "Lutron keypad buttons" };

function renderClaude(mcp) {
  const badge = document.getElementById("claude");
  if (!mcp) { badge.hidden = true; return; }
  badge.hidden = false;
  badge.replaceChildren(el("span", "dot"), document.createTextNode(` Claude · used ${ago(mcp.last_used)} ago`));
  const acts = (mcp.actions || []).map((a) => ACTION_NAMES[a] || a);
  const calls = `${mcp.calls_24h} tool call${mcp.calls_24h === 1 ? "" : "s"}`;
  badge.title = `Connected through avfleet's MCP server. ${calls} in the last 24 h. `
    + `Claude can read fleet status and history${acts.length ? `, and with your approval: ${acts.join(", ")}` : ""}.`;
}

function renderSummary(devices) {
  const box = document.getElementById("summary");
  box.replaceChildren();
  const counts = { up: 0, degraded: 0, down: 0, unknown: 0, maintenance: 0 };
  devices.forEach((d) => { const k = d.maintenance ? "maintenance" : d.status; counts[k] = (counts[k] || 0) + 1; });
  for (const s of ["down", "degraded", "maintenance", "up", "unknown"]) {
    if (!counts[s] && s !== "up") continue;
    const c = el("span", "count");
    c.style.color = `var(--${s === "maintenance" ? "maint" : s})`;
    const b = el("b", null, String(counts[s]));
    c.append(b, document.createTextNode(` ${s}`));
    box.appendChild(c);
  }
}

function renderGroups(devices) {
  const root = document.getElementById("groups");
  root.replaceChildren();
  const groups = new Map();
  devices.forEach((d) => { if (!groups.has(d.group)) groups.set(d.group, []); groups.get(d.group).push(d); });
  for (const [name, devs] of groups) {
    const g = el("section", "group");
    g.appendChild(el("h2", null, name));
    const grid = el("div", "grid");
    devs.forEach((d) => grid.appendChild(tile(d)));
    g.appendChild(grid);
    root.appendChild(g);
  }
}

function renderEvents(events) {
  const list = document.getElementById("events");
  list.replaceChildren();
  const shown = events.filter((e) => eventFilter === "all" || e.kind === eventFilter
    || (eventFilter === "claude" && /via Claude/.test(e.message)));
  if (!shown.length) { const li = el("li"); li.appendChild(el("span", "empty", "No events yet.")); list.appendChild(li); return; }
  for (const e of shown) {
    const li = el("li");
    const label = { action: "heal", maintenance: "maint" }[e.kind] || e.kind;
    li.append(el("span", "t", fmtTime(e.ts)), el("span", `kind ${e.kind}`, label), el("span", "m", e.message));
    list.appendChild(li);
  }
}

let brandTitle = "AV Fleet";
let brandApplied = false;

function applyBranding(branding) {
  if (branding.title) brandTitle = branding.title;
  document.getElementById("title").textContent = brandTitle;
  if (brandApplied) return;
  brandApplied = true;
  if (branding.logo) {
    const logo = document.getElementById("logo");
    logo.onload = () => { logo.hidden = false; document.getElementById("mark").hidden = true; };
    logo.alt = `${brandTitle} logo`;
    logo.src = "/logo";
  }
}

async function refresh() {
  const stamp = document.getElementById("stamp");
  try {
    const res = await fetch("/api/status", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    lastData = await res.json();
    renderClaude(lastData.mcp);
    renderSummary(lastData.devices);
    renderGroups(lastData.devices);
    renderEvents(lastData.events);
    const newest = Math.max(0, ...lastData.devices.map((d) => d.checked_at || 0));
    const stale = newest && Date.now() / 1000 - newest > lastData.interval_s * 3;
    stamp.textContent = stale ? `monitor stalled: last check ${ago(newest)} ago` : `updated ${new Date().toLocaleTimeString()}`;
    stamp.classList.toggle("stale", Boolean(stale));
    applyBranding(lastData.branding || {});
    const down = lastData.devices.filter((d) => d.status === "down" && !d.maintenance).length;
    document.title = down ? `(${down} down) ${brandTitle}` : brandTitle;
  } catch (err) {
    stamp.textContent = `dashboard offline: ${err.message}`;
    stamp.classList.add("stale");
  }
}

document.querySelectorAll(".filters button").forEach((b) => b.addEventListener("click", () => {
  eventFilter = b.dataset.kind;
  document.querySelectorAll(".filters button").forEach((x) => x.classList.toggle("on", x === b));
  if (lastData) renderEvents(lastData.events);
}));

refresh();
setInterval(refresh, REFRESH_MS);
