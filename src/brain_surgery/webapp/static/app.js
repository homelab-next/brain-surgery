"use strict";
const $ = (s, r = document) => r.querySelector(s);
const api = async (path, opts) => {
  const r = await fetch("api/" + path, opts);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
  return d;
};
const postJSON = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function el(tag, attrs = {}, kids = []) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v != null) e.setAttribute(k, v);
  }
  for (const c of [].concat(kids)) if (c != null) e.append(c.nodeType ? c : document.createTextNode(c));
  return e;
}
let toastTimer;
function toast(msg, kind = "") {
  const t = $("#toast"); t.textContent = msg; t.className = "toast " + kind;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 4200);
}

const state = { tree: null, zoom: [], groups: null, file: null, blocks: [], lastIdx: -1,
                selection: new Map(), holdsStatus: "held", readOnly: false };

// ---- boot --------------------------------------------------------------------------
async function boot() {
  $("#btn-rescan").onclick = rescan;
  $("#btn-roots").onclick = showRoots;
  $("#btn-theme").onclick = toggleTheme;
  $("#holds-toggle").onclick = () => { const h = $("#holds"); h.classList.toggle("hidden"); if (!h.classList.contains("hidden")) loadHolds(); };
  $("#tray-clear").onclick = () => { state.selection.clear(); renderTray(); renderFile(); };
  $("#tray-go").onclick = doQuarantine;
  if (localStorage.getItem("bs-theme")) document.documentElement.dataset.theme = localStorage.getItem("bs-theme");
  await refreshAll();
  window.addEventListener("resize", () => renderTreemap());
}
async function refreshAll() {
  const [ov, groups, tree] = await Promise.all([api("overview"), api("groups"), api("tree")]);
  state.readOnly = ov.read_only; state.groups = groups; state.tree = tree;
  $("#ro-badge").classList.toggle("hidden", !ov.read_only);
  if (ov.read_only) { $("#tray-go").disabled = true; $("#btn-rescan").disabled = true; }
  renderStats(ov); renderBand(); renderTreemap(); updateHoldsBadge(ov);
}

// ---- header stats + band -----------------------------------------------------------
function renderStats(ov) {
  const scr = Object.fromEntries((ov.screen || []).map(s => [s.outcome, s.hashes]));
  const items = [["files", ov.files], ["blocks", ov.blocks], ["agents", ov.agents],
    ["flagged", (scr.quarantine || 0) + (scr.review || 0)], ["held", ov.holds.held]];
  if (ov.regrowth) items.push(["regrowth", ov.regrowth, true]);
  $("#stats").replaceChildren(...items.map(([label, n, alert]) =>
    el("div", { class: "stat" + (alert ? " alert" : "") }, [el("b", { text: String(n) }), el("span", { text: label })])));
}
function renderBand() {
  const band = $("#band"); band.replaceChildren();
  for (const g of state.groups.flagged || []) {
    const r = g.outcome === "quarantine" ? "r3" : "r2";
    band.append(el("div", { class: "gcard " + r, onclick: () => pickGroup(g.block_ids, g.outcome) }, [
      el("div", { class: "gtop" }, [el("span", { text: g.outcome }), el("span", { text: g.agents.length + " agents" })]),
      el("div", { class: "gsample", text: g.sample }),
      el("div", { class: "gmeta", text: `${g.copies} ${g.copies === 1 ? "copy" : "copies"} · ${g.exposure === 3 ? "always loaded" : "on demand"}` }),
    ]));
  }
  for (const d of (state.groups.regrowth || []).slice(0, 6)) {
    band.append(el("div", { class: "gcard r3", onclick: () => openFile(d.path) }, [
      el("div", { class: "gtop" }, [el("span", { text: "regrowth" }), el("span", { text: d.kind })]),
      el("div", { class: "gsample", text: (d.path || "").split("/").pop() }),
      el("div", { class: "gmeta", text: `excised content reappeared · hold ${d.hold_id}` }),
    ]));
  }
  const dup = (state.groups.duplicates || []).filter(d => d.agents >= 3).slice(0, 6);
  for (const d of dup) {
    band.append(el("div", { class: "gcard" }, [
      el("div", { class: "gtop" }, [el("span", { text: "duplicate" }), el("span", { text: d.agents + " agents" })]),
      el("div", { class: "gsample", text: d.kind + " block" }),
      el("div", { class: "gmeta", text: `${d.files} files share this exact block` }),
    ]));
  }
}
async function pickGroup(ids, label) {
  const blocks = await Promise.all(ids.map(id => api("block?id=" + encodeURIComponent(id)).catch(() => null)));
  let n = 0;
  for (const b of blocks) if (b) { addSel(b); n++; }
  renderTray();
  toast(`added ${n} ${label} block(s) to the tray`, "ok");
}

// ---- treemap -----------------------------------------------------------------------
function zoomNode() { let n = state.tree; for (const name of state.zoom) { n = (n.children || []).find(c => c.name === name) || n; } return n; }
function renderCrumbs() {
  const c = $("#crumbs"); c.replaceChildren(el("a", { text: "memory", onclick: () => { state.zoom = []; renderTreemap(); } }));
  state.zoom.forEach((name, i) => { c.append(" › ", el("a", { text: name, onclick: () => { state.zoom = state.zoom.slice(0, i + 1); renderTreemap(); } })); });
}
function renderTreemap() {
  renderCrumbs();
  const box = $("#treemap"), W = box.clientWidth, H = box.clientHeight;
  box.replaceChildren();
  const node = zoomNode();
  const kids = (node.children || []).map(c => ({ node: c, value: Math.max(c.bytes || 0, 1) }));
  if (!kids.length) { box.append(el("p", { class: "muted pad", text: "no children" })); return; }
  for (const r of squarify(kids, { x: 0, y: 0, w: W, h: H })) {
    const c = r.node, rc = "r" + (c.risk || 0);
    const cell = el("div", { class: "cell" + (c.managed_by ? " managed" : ""), title: `${c.name} · ${fmtBytes(c.bytes)} · ${c.blocks} blocks` });
    cell.style.cssText += `left:${r.x}px;top:${r.y}px;width:${r.w}px;height:${r.h}px;background:var(--${rc})`;
    if (r.w > 42 && r.h > 16) cell.append(el("span", { class: "lbl", text: c.name }));
    cell.onclick = () => { if (c.kind === "file") openFile(c.path); else { state.zoom = [...state.zoom, c.name]; renderTreemap(); } };
    box.append(cell);
  }
}
function squarify(items, rect) {
  const nodes = items.filter(i => i.value > 0).sort((a, b) => b.value - a.value);
  const out = []; let free = { ...rect }, i = 0;
  const worst = (row, side) => { const s = row.reduce((a, b) => a + b.a, 0), mx = Math.max(...row.map(x => x.a)), mn = Math.min(...row.map(x => x.a)); return Math.max(side * side * mx / (s * s), s * s / (side * side * mn)); };
  while (i < nodes.length && free.w > 0.5 && free.h > 0.5) {
    const side = Math.min(free.w, free.h);
    const rem = nodes.slice(i), remVal = rem.reduce((s, n) => s + n.value, 0) || 1;
    const scale = (free.w * free.h) / remVal, scaled = rem.map(n => ({ node: n.node, a: n.value * scale }));
    let row = [scaled[0]], j = 1;
    while (j < scaled.length && worst(row, side) >= worst(row.concat(scaled[j]), side)) { row.push(scaled[j]); j++; }
    const rowArea = row.reduce((a, b) => a + b.a, 0), thick = Math.min(rowArea / side, free.w <= free.h ? free.h : free.w);
    if (free.w <= free.h) {
      let cx = free.x; for (const r of row) { const cw = r.a / thick; out.push({ node: r.node, x: cx, y: free.y, w: cw, h: thick }); cx += cw; }
      free.y += thick; free.h -= thick;
    } else {
      let cy = free.y; for (const r of row) { const ch = r.a / thick; out.push({ node: r.node, x: free.x, y: cy, w: thick, h: ch }); cy += ch; }
      free.x += thick; free.w -= thick;
    }
    i += row.length;
  }
  return out;
}

// ---- file view ---------------------------------------------------------------------
async function openFile(path) {
  try { state.file = await api("file?path=" + encodeURIComponent(path)); }
  catch (e) { return toast(e.message, "err"); }
  state.blocks = state.file.blocks; state.lastIdx = -1; renderFile();
}
function renderFile() {
  if (!state.file) return;
  const f = state.file, d = $("#detail"); d.replaceChildren();
  const head = el("div", { class: "fhead" }, [
    el("h2", { text: f.path }),
    el("div", { class: "fmeta" }, [el("span", { text: "agent: " + (f.agent || "—") }),
      el("span", { text: "source: " + f.source_id }), el("span", { text: "role: " + f.role }),
      el("span", { text: f.blocks.length + " blocks" })]),
    el("div", { class: "frow" }, [
      el("button", { class: "ghost small", text: "select flagged", onclick: () => selectFlagged() }),
      el("button", { class: "ghost small", text: "clear selection", onclick: () => { for (const b of state.blocks) state.selection.delete(b.id); renderTray(); renderFile(); } }),
    ]),
  ]);
  d.append(head);
  if (f.managed_by) d.append(el("div", { class: "managed-note", html: `⚙ <b>Managed copy.</b> Editing here is undone on the next deploy — change it at its source: <code>${escapeHtml(f.managed_by)}</code>. Quarantine needs “force”.` }));
  state.blocks.forEach((b, idx) => {
    const sel = state.selection.has(b.id);
    const row = el("div", { class: "blk r" + (b.risk || 0) + (sel ? " sel" : "") });
    row.append(el("div", { class: "gutter" }));
    const body = el("div");
    body.append(el("pre", { text: b.text }));
    row.append(body);
    const tags = [];
    if (b.screen_outcome && b.screen_outcome !== "keep") tags.push(`<span class="tag r${b.risk}">${b.screen_outcome}</span>`);
    if (b.regrew) tags.push(`<span class="tag r3">regrew</span>`);
    row.append(el("div", { class: "bmeta", html: `L${b.start_line}–${b.end_line} · ${b.kind}<br>${b.loaded}${tags.join("")}<br><a onclick="event.stopPropagation()" href="#" data-id="${b.id}" class="trace-link">trace</a>` }));
    row.onclick = (e) => toggleSel(idx, e.shiftKey);
    d.append(row);
  });
  d.querySelectorAll(".trace-link").forEach(a => a.addEventListener("click", (e) => { e.preventDefault(); showTrace(a.dataset.id); }));
}
function toggleSel(idx, shift) {
  const b = state.blocks[idx];
  if (shift && state.lastIdx >= 0) {
    const [lo, hi] = [Math.min(idx, state.lastIdx), Math.max(idx, state.lastIdx)];
    const want = !state.selection.has(b.id);
    for (let k = lo; k <= hi; k++) want ? addSel(state.blocks[k]) : state.selection.delete(state.blocks[k].id);
  } else { state.selection.has(b.id) ? state.selection.delete(b.id) : addSel(b); }
  state.lastIdx = idx; renderTray(); renderFile();
}
function selectFlagged() {
  for (const b of state.blocks) if ((b.risk || 0) >= 2) addSel(b);
  renderTray(); renderFile();
}
function addSel(b) { state.selection.set(b.id, { id: b.id, agent: b.agent || (state.file && state.file.agent), path: b.path || (state.file && state.file.path), sample: (b.text || "").slice(0, 80) }); }

// ---- tray / quarantine -------------------------------------------------------------
function renderTray() {
  const tray = $("#tray"); tray.classList.toggle("hidden", state.selection.size === 0);
  $("#tray-count").textContent = state.selection.size;
  const list = $("#tray-list"); list.replaceChildren();
  for (const m of state.selection.values()) {
    list.append(el("div", { class: "tray-item" }, [
      el("span", {}, [el("b", { text: (m.agent || "—") + " " }), el("span", { class: "muted", text: (m.sample || "").replace(/\s+/g, " ") })]),
      el("span", { class: "x", text: "✕", onclick: () => { state.selection.delete(m.id); renderTray(); renderFile(); } }),
    ]));
  }
}
async function doQuarantine() {
  const reason = $("#tray-reason").value.trim();
  if (!reason) return toast("a reason is required", "err");
  const ids = [...state.selection.keys()];
  try {
    const res = await postJSON("quarantine", { block_ids: ids, reason, force: $("#tray-force").checked });
    const held = res.held.length, skipped = res.skipped.length;
    toast(`quarantined ${held}${skipped ? ", " + skipped + " skipped" : ""}`, skipped ? "" : "ok");
    if (skipped) res.skipped.slice(0, 3).forEach(s => console.warn(s.reason));
    state.selection.clear(); $("#tray-reason").value = "";
    await refreshAll();
    if (state.file) await openFile(state.file.path);
    renderTray();
  } catch (e) { toast(e.message, "err"); }
}

// ---- provenance --------------------------------------------------------------------
async function showTrace(id) {
  let t; try { t = await api("trace?id=" + encodeURIComponent(id)); } catch (e) { return toast(e.message, "err"); }
  const d = $("#detail"); d.replaceChildren();
  d.append(el("div", { class: "fhead" }, [el("h2", { text: "provenance" }),
    el("div", { class: "frow" }, [el("button", { class: "ghost small", text: "← back to file", onclick: renderFile })])]));
  const wrap = el("div", { class: "trace" });
  wrap.append(el("div", { class: "rootbox" }, [
    el("div", {}, [el("span", { class: "rootkind " + t.root.kind, text: t.root.kind }), " root"]),
    t.root.where ? el("div", { class: "muted", text: t.root.where }) : null,
    el("p", { text: t.root.action }),
  ]));
  for (const s of t.steps) {
    wrap.append(el("div", { class: "step" }, [el("div", {}, [
      el("b", { text: s.node === "block" ? "written" : ("read (" + (s.origin || "?") + ")") }),
      el("div", { class: "muted", text: (s.path || s.ref || "") }),
      el("div", { class: "muted", text: `${s.agent ? "agent " + s.agent + " · " : ""}${s.method} ${s.confidence}${s.ts ? " · " + s.ts : ""}` }),
    ])]));
  }
  (t.notes || []).forEach(n => wrap.append(el("p", { class: "muted", text: n })));
  d.append(wrap);
}
async function showRoots() {
  let r; try { r = await api("roots?scope=flagged"); } catch (e) { return toast(e.message, "err"); }
  const d = $("#detail"); d.replaceChildren(el("div", { class: "fhead" }, [el("h2", { text: "roots of flagged memory" }),
    el("div", { class: "fmeta" }, [el("span", { text: "fix the source; it covers every copy" })])]));
  const wrap = el("div", { class: "trace" });
  if (!r.roots.length) wrap.append(el("p", { class: "muted", text: "no flagged blocks, or no provenance yet" }));
  for (const g of r.roots) {
    wrap.append(el("div", { class: "rootbox" }, [
      el("div", {}, [el("span", { class: "rootkind " + g.root_kind, text: g.root_kind }),
        ` · ${g.blocks} block(s) · ${g.agents.length} agent(s)`]),
      g.root_where ? el("div", { class: "muted", text: g.root_where }) : null,
      el("p", { text: g.action }),
      el("button", { class: "ghost small", text: "add to tray", onclick: () => pickGroup(g.block_ids, g.root_kind) }),
    ]));
  }
  d.append(wrap);
}

// ---- holds -------------------------------------------------------------------------
function updateHoldsBadge(ov) { const n = ov.holds ? ov.holds.held : 0; $("#holds-badge").textContent = n; $("#holds-badge").style.display = n ? "" : "none"; }
async function loadHolds() {
  let data; try { data = await api("holds?status=" + state.holdsStatus); } catch (e) { return toast(e.message, "err"); }
  const h = $("#holds"); h.replaceChildren();
  h.append(el("h3", {}, ["Holds", el("button", { class: "ghost small", text: "✕", onclick: () => h.classList.add("hidden") })]));
  const filter = el("div", { class: "hfilter" });
  for (const s of ["held", "excised", "released", "all"])
    filter.append(el("button", { class: state.holdsStatus === s ? "on" : "", text: s, onclick: () => { state.holdsStatus = s; loadHolds(); } }));
  h.append(filter);
  if (!data.holds.length) h.append(el("p", { class: "muted pad", text: "nothing here" }));
  for (const it of data.holds) {
    const acts = el("div", { class: "hacts" });
    if (it.status === "held") {
      acts.append(el("button", { class: "ghost small", text: "open file", onclick: () => it.path && openFile(it.path) }));
      if (!state.readOnly) {
        acts.append(el("button", { class: "ghost small", text: "release", onclick: () => holdAction("release", it.id) }));
        acts.append(el("button", { class: "danger small", text: "excise", onclick: () => holdAction("excise", it.id) }));
      }
    }
    h.append(el("div", { class: "hitem" }, [
      el("div", { class: "htop" }, [el("span", { class: "status-" + it.status, text: it.status }), el("span", { text: it.agent || "" })]),
      el("div", { class: "hsample", text: it.sample || "(text cleared)" }),
      el("div", { class: "htop" }, [el("span", { text: (it.path || "").split("/").pop() }), el("span", { text: it.reason })]),
      acts,
    ]));
  }
}
async function holdAction(kind, id) {
  if (kind === "excise" && !confirm("Excise permanently? The held text is scrubbed and the block is watchlisted. Release it instead if unsure.")) return;
  try { await postJSON(kind, { hold_id: id, note: "via review page" }); toast(`hold ${id} ${kind}d`, "ok"); }
  catch (e) { return toast(e.message, "err"); }
  await refreshAll(); await loadHolds(); if (state.file) openFile(state.file.path);
}

// ---- misc --------------------------------------------------------------------------
async function rescan() {
  try { const s = await postJSON("rescan", {}); toast(`scanned: ${s.files_changed} changed, ${s.blocks} blocks${s.regrowth.length ? ", regrowth!" : ""}`, s.regrowth.length ? "err" : "ok"); }
  catch (e) { return toast(e.message, "err"); }
  await refreshAll();
}
function toggleTheme() {
  const cur = document.documentElement.dataset.theme;
  const next = cur === "dark" ? "light" : cur === "light" ? "" : "dark";
  if (next) document.documentElement.dataset.theme = next; else delete document.documentElement.dataset.theme;
  localStorage.setItem("bs-theme", next); renderTreemap();
}
const fmtBytes = (n) => n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB";
const escapeHtml = (s) => s.replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
boot().catch(e => toast("failed to load: " + e.message, "err"));
