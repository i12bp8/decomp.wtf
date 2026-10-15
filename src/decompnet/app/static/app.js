"use strict";
/* decomp.wtf GUI. Plain JS, no build step. Everything shown from agents or ROMs is untrusted text:
   it only ever reaches the page through textContent or esc(). */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const hex8 = (n) => "0x" + (n >>> 0).toString(16).toUpperCase().padStart(8, "0");
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const pctOf = (best, insns) => (best == null ? null : Math.max(0, 1 - best / Math.max(1, insns * 100)));
/* A match percentage that never reads 100% before the function really matches (99.6% stays 99.6%). */
const pctText = (p) => (p >= 0.99 ? `${(Math.floor(p * 1000) / 10).toFixed(1)}%` : `${Math.floor(p * 100)}%`);
const pctNum = (p) => (p < 10 ? p.toFixed(2) : p.toFixed(1));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
function since(t) {
  if (!t) return "";
  const s = Date.now() / 1000 - t;
  return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago`
    : s < 172800 ? "yesterday" : `${Math.round(s / 86400)} days ago`;
}
function duration(h) { return h < 1 ? `${Math.max(1, Math.round(h * 60))} min` : h < 48 ? `${h.toFixed(h < 10 ? 1 : 0)} h` : `${Math.round(h / 24)} days`; }
function bytes(n) { return n >= 1 << 20 ? `${(n / 1048576).toFixed(1)} MB` : n >= 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} bytes`; }
function money(usd) { return usd >= 10 ? `$${usd.toFixed(0)}` : `$${usd.toFixed(2)}`; }

async function api(path, { method = "GET", body, raw } = {}) {
  const opts = { method, headers: {} };
  if (method !== "GET") opts.headers["X-Decomp"] = "1";
  if (raw !== undefined) { opts.body = raw; opts.headers["Content-Type"] = "application/octet-stream"; }
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { /* not json */ }
  if (!res.ok) throw new Error((data && data.error) || `request failed (${res.status})`);
  return data;
}

function toast(html, kind = "") {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.innerHTML = html;
  $("toasts").append(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .4s"; }, kind === "err" ? 7000 : 4200);
  setTimeout(() => t.remove(), kind === "err" ? 7500 : 4700);
}

async function followJob(id, logEl, onUpdate) {
  for (;;) {
    const job = await api(`/api/job?id=${id}`);
    if (logEl) { logEl.textContent = job.log.join("\n"); logEl.scrollTop = logEl.scrollHeight; }
    if (onUpdate) onUpdate(job.log);
    if (job.state === "done") return job.result;
    if (job.state === "error") { const err = new Error(job.error); err.job = job; throw err; }
    await sleep(500);
  }
}

/* The map's still cells are drawn once into `base` (again only when one of them changes); a frame copies it and
   draws just what moves on top: the functions being worked on, fresh matches, the selection. A big game is tens of
   thousands of cells, and redrawing them all 60 times a second while agents or autopilot work made the app lag. */
let base = null, baseDirty = true, moving = [];
function mapChanged() { baseDirty = true; }

/* ------------------------------------------------------------------ theme */
const THEME_KEY = "decomp-wtf-theme";
function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  colors = null; baseDirty = true;
  if (S.map.length) drawMap();
}
function setTheme(t) {
  try { if (t) localStorage.setItem(THEME_KEY, t); else localStorage.removeItem(THEME_KEY); } catch { /* storage off */ }
  applyTheme(t);
}
function savedTheme() { try { return localStorage.getItem(THEME_KEY) || ""; } catch { return ""; } }
$("themeBtn").onclick = () => setTheme(getComputedStyle(document.documentElement).colorScheme.includes("dark") ? "light" : "dark");

/* ------------------------------------------------------------------ state + routing */
const S = {
  st: null, slug: null, ov: null, map: [], byName: new Map(), e: 0, a: 0, seq: {}, sessions: [], tab: "overview",
  scores: new Map(), claims: [], flash: new Map(), timer: null, statsTimer: null, lastMapFetch: 0, fn: null, stats: null,
  projects: [],
};
const VIEWS = ["home", "catalog", "project", "agentsPage", "toolsPage", "settingsPage"];
const TABS = ["overview", "functions", "agents", "activity", "team", "source", "settings"];

/* The project folder in the computer's file manager (decomp.wtf in Docker says where it is instead). */
function openFolder() {
  return api("/api/open", { method: "POST", body: { slug: S.slug } })
    .then((r) => { if (r && r.message) toast(esc(r.message)); })
    .catch((e) => toast(esc(e.message), "err"));
}

async function loadState() { S.st = await api("/api/state"); $("navVersion").textContent = `v${S.st.version}`; markPlatforms(); return S.st; }

function route() {
  const h = location.hash;
  const m = h.match(/^#\/p\/([\w-]+)(?:\/(\w+))?/);
  closeAll();
  closeNav();
  hideLaunch();
  if (m) {
    if (S.slug === m[1] && !$("project").hidden) { setTab(m[2] || "overview"); markNav(); return; }
    leaveProject();
    openProject(m[1], m[2] || "overview");
  } else {
    leaveProject();
    if (h === "#/catalog") showCatalog();
    else if (h === "#/agents") showAgentsPage();
    else if (h === "#/tools") showToolsPage();
    else if (h === "#/settings") showSettingsPage();
    else showHome();
  }
  markNav();
}
window.addEventListener("hashchange", route);

function show(view) {
  for (const v of VIEWS) $(v).hidden = view !== v;
  $("main").scrollTo(0, 0);
  window.scrollTo(0, 0);
}
function leaveProject() {
  stopPolling();
  S.slug = null;
  clearInterval(teamTimer);
}

/* ------------------------------------------------------------------ sidebar */
/* The page's CSP allows no inline style attributes: widths are set through the DOM instead. */
function setWidths(root) { root.querySelectorAll("[data-w]").forEach((el) => { el.style.width = el.dataset.w + "%"; }); }

function markNav() {
  const h = location.hash || "#/";
  const route = h.startsWith("#/p/") ? "project" : (h.replace("#/", "") || "home");
  document.querySelectorAll(".nav [data-route]").forEach((a) => a.classList.toggle("on", a.dataset.route === route));
  document.querySelectorAll(".nav-proj").forEach((a) => a.classList.toggle("on", a.dataset.slug === S.slug && route === "project"));
}
async function loadNav() {
  try { S.projects = await api("/api/projects"); } catch (e) {
    if (!S.projects?.length) $("navProjects").innerHTML = `<p class="nav-empty">Couldn't load your projects: ${esc(e.message)}</p>`;
    return;
  }
  renderNav();
}
function renderNav() {
  const box = $("navProjects");
  if (!S.projects.length) { box.innerHTML = `<p class="nav-empty">None yet: drop in a game.</p>`; return; }
  box.innerHTML = S.projects.map((p) => {
    const pct = p.bytes ? (100 * p.matched_bytes) / p.bytes : 0;
    const short = PLAT_SHORT[p.platform] || (p.platform_name || "").replace("Nintendo ", "") || "?";
    const state = p.working ? `<span class="dot on" title="${p.working} agent${p.working === 1 ? "" : "s"} working"></span>`
      : p.autopilot === "running" ? `<span class="dot work" title="autopilot running"></span>` : "";
    if (p.broken) return `<a class="nav-proj broken" href="#/p/${esc(p.slug)}" data-slug="${esc(p.slug)}" title="${esc(p.broken)}">
      <span class="np-top"><span class="np-name">${esc(p.title)}</span></span>
      <span class="np-meta"><span>can't be opened: hover for why</span></span></a>`;
    return `<a class="nav-proj" href="#/p/${esc(p.slug)}" data-slug="${esc(p.slug)}" title="${esc(p.title)}">
      <span class="np-top"><span class="np-name">${esc(p.title)}</span>${state}${p.room ? `<span class="np-room" title="${p.room === "host" ? "you share this project" : "in a team room"}">●●</span>` : ""}</span>
      <span class="np-meta"><span>${esc(short)}</span><span class="grow"></span><span>${p.kind === "fork" && !p.ready ? "set up" : pctNum(pct) + "%"}</span></span>
      <span class="np-bar"><i data-w="${pct.toFixed(2)}"></i></span></a>`;
  }).join("");
  setWidths(box);
  markNav();
}
setInterval(loadNav, 5000);
function openNav() { document.body.classList.add("nav-open"); $("navShade").hidden = false; }
function closeNav() { document.body.classList.remove("nav-open"); $("navShade").hidden = true; }
$("navOpen").onclick = openNav;
$("navClose").onclick = closeNav;
$("navShade").onclick = closeNav;
$("navJoin").onclick = () => { closeNav(); openJoin(); };

/* ------------------------------------------------------------------ home */
let picked = null;

async function showHome() {
  show("home");
  resetDrop();
  const st = await loadState();
  api("/api/catalog?cached=1").then((c) => { if (c.projects.length) $("forkCount").textContent = c.projects.length.toLocaleString(); }).catch(() => {});  // never a request to decomp.dev
  const box = $("projects");
  box.innerHTML = "";
  $("recent").hidden = !st.projects.length;
  for (const p of st.projects) {
    const pct = p.bytes ? (100 * p.matched_bytes) / p.bytes : 0;
    const a = document.createElement("a");
    a.className = "card";
    a.href = `#/p/${p.slug}`;
    if (p.broken) {
      a.innerHTML = `<div class="cc-top"><span class="plat">?</span></div><div><h4>${esc(p.title)}</h4>
        <p class="meta err-text">Can't be opened: ${esc(p.broken)}</p></div>`;
      box.append(a);
      continue;
    }
    const meta = p.kind === "fork"
      ? `${p.origin === "scratch" ? "from scratch" : "public decomp"}${p.version ? " · " + esc(p.version) : ""}${p.ready ? "" : " · needs setup"}`
      : `${esc(p.rom?.game_code || "")}${p.rom?.region ? " · " + esc(p.rom.region) : ""} · ${esc(p.compiler?.name || "")}`;
    a.innerHTML = `<div class="cc-top"><span class="plat">${esc(p.platform_name || "?")}</span><span class="meta">${esc(since(p.last_activity))}</span></div>
      <div><h4>${esc(p.title)}</h4><p class="meta">${meta}</p></div>
      <div class="meter"><i></i></div><p class="meta"><b>${pctNum(pct)}%</b> of code · ${(p.matched || 0).toLocaleString()} / ${(p.functions || 0).toLocaleString()} functions</p>`;
    box.append(a);
    requestAnimationFrame(() => { a.querySelector(".meter i").style.width = pct + "%"; });
  }
}

function resetDrop() {
  picked = null;
  $("dropIdle").hidden = false;
  $("dropPicked").hidden = true;
  $("dropBusy").hidden = true;
  $("dropError").hidden = true;
  $("own").checked = false;
  $("analyse").disabled = true;
  $("file").value = "";
}

const drop = $("drop");
drop.addEventListener("click", (e) => { if (!$("dropIdle").hidden && !e.target.closest("button")) $("file").click(); });
drop.addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === " ") && !$("dropIdle").hidden && e.target === drop) { e.preventDefault(); $("file").click(); } });
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files[0]) pickFile(e.dataTransfer.files[0]); });
$("file").addEventListener("change", () => { if ($("file").files[0]) pickFile($("file").files[0]); });
$("own").addEventListener("change", updateAnalyse);
$("cancelPick").onclick = resetDrop;

/* Read the header in the browser so the user sees what they picked before anything happens. */
function parseRom(buf) {
  const b = new Uint8Array(buf);
  const magic = [b[0], b[1], b[2], b[3]].map((x) => x.toString(16).padStart(2, "0")).join("");
  const order = { "80371240": "z64", "37804012": "v64", "40123780": "n64" }[magic];
  if (!order) throw new Error("This doesn't look like an N64 ROM (.z64, .n64 or .v64).");
  const z = new Uint8Array(Math.min(b.length, 0x101000));
  for (let i = 0; i < z.length; i += 4) {
    const w = order === "z64" ? [b[i], b[i + 1], b[i + 2], b[i + 3]] : order === "v64" ? [b[i + 1], b[i], b[i + 3], b[i + 2]] : [b[i + 3], b[i + 2], b[i + 1], b[i]];
    z.set(w, i);
  }
  const text = (from, to) => Array.from(z.slice(from, to)).map((c) => (c >= 32 && c < 127 ? String.fromCharCode(c) : "")).join("").trim();
  const dv = new DataView(z.buffer);
  let jumps = 0, branches = 0;
  for (let i = 0x1000; i + 4 <= z.length; i += 4) {
    const w = dv.getUint32(i);
    if (w >>> 26 === 2) jumps++;
    else if ((w & 0xffff0000) >>> 0 === 0x10000000) branches++;
  }
  const regions = { E: "North America", J: "Japan", P: "Europe", D: "Germany", F: "France", U: "Australia", I: "Italy", S: "Spain", A: "All regions", X: "Europe", Y: "Europe" };
  const code = text(0x3b, 0x3f);
  return {
    title: text(0x20, 0x34) || "Untitled", code, region: regions[code[3]] || "Unknown",
    size: b.length, order, entry: dv.getUint32(8), compiler: branches > jumps ? "IDO" : "KMC",
  };
}

/* What the user dropped, from its first bytes (nothing is uploaded until they confirm). */
const PLATFORM_NAME = { n64: "Nintendo 64", psx: "PlayStation", ps2: "PlayStation 2", playstation: "PlayStation 1 or 2",
  gc: "GameCube", wii: "Wii", gcwii: "GameCube or Wii", gba: "Game Boy Advance", nds: "Nintendo DS" };
const CW_PACK = {
  gcwii: { id: "mwcc", text: "Install CodeWarrior, the compilers GameCube and Wii games were built with (about 80 MB). It's proprietary software, kept by the decomp community: only if you accept that." },
  nds: { id: "mwccarm", text: "Install CodeWarrior for DS, the compilers DS games were built with (about 50 MB). It's proprietary software, kept by the decomp community: only if you accept that." },
};
function crc16(bytes, from, to) {
  let crc = 0xffff;
  for (let i = from; i < to; i++) {
    crc ^= bytes[i];
    for (let k = 0; k < 8; k++) crc = crc & 1 ? (crc >>> 1) ^ 0xa001 : crc >>> 1;
  }
  return crc;
}
const MB = (n) => (n < 1048576 ? `${Math.max(1, Math.round(n / 1024))} KiB` : `${(n / 1048576).toFixed(n < 10485760 ? 1 : 0)} MiB`);

function identifyHead(head, file) {
  const dv = new DataView(head.buffer);
  const text = (a, b) => String.fromCharCode(...head.slice(a, b));
  const ascii = (a, b) => text(a, b).split("\0")[0].replace(/[^\x20-\x7e]/g, "").trim();
  const stem = file.name.replace(/\.[^.]*$/, "");
  const base = { title: stem, size: file.size };
  if (text(0, 8) === "PS-X EXE") return { ...base, platform: "psx", order: "PS-X EXE", entry: dv.getUint32(0x10, true) };
  if (head.length > 0xc0 && head[0xb2] === 0x96 && head[3] === 0xea) {  // GBA: fixed byte, header checksum, ARM branch
    let sum = 0;
    for (let i = 0xa0; i < 0xbd; i++) sum += head[i];
    if (((-(sum + 0x19)) & 0xff) === head[0xbd]) return { ...base, platform: "gba", order: "ROM", code: ascii(0xac, 0xb0), title: ascii(0xa0, 0xac) || stem };
  }
  if (head.length >= 0x200 && crc16(head, 0, 0x15e) === dv.getUint16(0x15e, true)) {  // DS: the header's own checksum
    const ram = dv.getUint32(0x28, true), entry = dv.getUint32(0x24, true);
    if (ram >= 0x02000000 && ram < 0x02400000 && entry >= ram) return { ...base, platform: "nds", order: "ROM", code: ascii(12, 16), title: ascii(0, 12) || stem, entry };
  }
  if (text(0x8001, 0x8006) === "CD001" || text(0x9319, 0x931e) === "CD001" || text(0x9311, 0x9316) === "CD001") return { ...base, platform: "playstation", order: "disc image" };
  if (text(0, 8) === "MComprHD") return { ...base, platform: "playstation", order: "CHD disc image" };
  if (text(0, 4) === "\x7fELF" && head[5] === 1 && dv.getUint16(18, true) === 8) return { ...base, platform: "ps2", order: "ELF program", entry: dv.getUint32(24, true) };
  if (head.length > 0x60 && dv.getUint32(0x1c) === 0xc2339f3d) return { ...base, platform: "gc", order: "disc image", code: ascii(0, 6), title: ascii(0x20, 0x80) || stem };
  if (head.length > 0x60 && dv.getUint32(0x18) === 0x5d1c9ea3) return { ...base, platform: "wii", order: "disc image", code: ascii(0, 6), title: ascii(0x20, 0x80) || stem };
  const packed = { "RVZ\x01": "RVZ", "WIA\x01": "WIA", WBFS: "WBFS", CISO: "CISO" }[text(0, 4)] || (dv.getUint32(0, true) === 0xb10bc001 ? "GCZ" : null);
  if (packed) return { ...base, platform: "gcwii", order: `${packed} disc image` };
  if (head.length >= 0x100) {  // a DOL: section offsets, addresses and sizes that make sense, entry in RAM
    const entry = dv.getUint32(0xe0);
    let used = 0, ok = entry >= 0x80000000 && entry < 0x81800000;
    for (let i = 0; i < 18 && ok; i++) {
      const off = dv.getUint32(i * 4), addr = dv.getUint32(72 + i * 4), size = dv.getUint32(144 + i * 4);
      if (!size) continue;
      used++;
      ok = off >= 0x100 && off + size <= file.size && addr >= 0x80000000 && addr < 0x81800000;
    }
    if (ok && used) return { ...base, platform: "gc", order: "program (DOL)", entry };
  }
  return null;
}

/* Files people try that decomp.wtf can't start from: say what they are and why, not just "no". */
function notSupported(head, file) {
  const text = (a, b) => String.fromCharCode(...head.slice(a, b));
  const ext = (file.name.match(/\.([^.]+)$/) || [])[1]?.toLowerCase() || "";
  if (text(0, 2) === "MZ") return "That's a Windows program. PC games aren't supported yet: decomp.wtf starts from N64, PlayStation 1 and 2, GameCube, Wii, Game Boy Advance and DS games.";
  if (text(0, 4) === "XEX2" || ext === "xex") return "That's an Xbox 360 program. Xbox 360 games aren't supported yet.";
  if (["nsp", "xci", "nso", "nro", "nca"].includes(ext) || text(0, 4) === "NSO0" || text(0, 4) === "PFS0") return "That's a Nintendo Switch file. Switch games aren't supported yet.";
  if (["sfc", "smc"].includes(ext)) return "That's a Super Nintendo ROM. SNES games were written in assembly, not compiled from C, so there's no C source to match.";
  if (["nes", "gb", "gbc", "md", "gen", "sms", "gg", "pce"].includes(ext)) return "Games for that console were written in assembly, not compiled from C, so there's no C source to match.";
  if (ext === "cue") return "That's the .cue sheet: pick the .bin file next to it (the track with the game's data, usually Track 1).";
  if (["zip", "7z", "rar"].includes(ext)) return "That's an archive: unpack it first, then drop the game file inside.";
  if (ext === "apk") return "Android apps aren't supported yet.";
  if (ext === "pbp" || ext === "cso") return "That's a PSP file. PSP games aren't supported yet.";
  return null;
}
const NOT_A_GAME = "That isn't a game file decomp.wtf can start from scratch (yet). For other platforms, continue a public decomp.";

async function pickFile(file) {
  const head = new Uint8Array(await file.slice(0, 0x9400).arrayBuffer());
  let info = identifyHead(head, file), buf = file;
  if (!info) {
    const why = notSupported(head, file);
    if (why || file.size > 64 * 1024 * 1024) return toast(esc(why || NOT_A_GAME), "err");
    buf = await file.arrayBuffer();
    try { info = parseRom(buf); info.platform = "n64"; } catch (e) {
      return toast(esc(NOT_A_GAME), "err");
    }
  }
  picked = { buf, info, name: file.name };
  if (!S.st) await loadState().catch(() => {});  // dropped before the page finished loading: the compiler lists
  $("romFile").textContent = file.name;
  $("romTitle").textContent = info.title;
  const p = info.platform;
  const facts = p === "n64"
    ? [["Game code", info.code || "—"], ["Region", info.region], ["Size", MB(info.size)], ["Format", info.order],
       ["Entry", hex8(info.entry)], ["Looks like", info.compiler === "IDO" ? "IDO" : "GCC (KMC)"]]
    : [["Platform", PLATFORM_NAME[p]], ["Format", info.order], ["Size", MB(info.size)],
       info.code ? ["Game ID", info.code] : ["Entry", info.entry != null ? hex8(info.entry) : "from the disc"],
       ["Project", ["gc", "wii", "gcwii"].includes(p) ? "dtk-template" : p === "nds" ? "ds-decomp" : p === "gba" ? "luvdis" : "splat"]];
  $("romFacts").innerHTML = facts.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("");
  const level = platformStatus(p);
  $("romStatus").hidden = !level;
  $("romStatus").className = `plat-status ${level || ""}`;
  $("romStatus").innerHTML = level === "recommended"
    ? `<b>★ Recommended platform.</b> The most tested path to a full decomp you can port and mod.`
    : `<b>Beta.</b> ${esc(PLATFORM_NAME[p] || "This platform")} works and is tested on homemade games, less so on real ones yet. Problems are worth <a href="https://github.com/i12bp8/decomp.wtf/issues" target="_blank" rel="noopener">reporting</a>.`;
  setupCompilers(info);
  $("dropIdle").hidden = true;
  $("dropPicked").hidden = false;
  updateAnalyse();
}

function setupCompilers(info) {
  const p = info.platform, select = $("compilerPick"), note = $("compilerNote");
  const cw = S.st?.codewarrior || { installed: false, ds_installed: false, gc: [], wii: [], ds: [] };
  $("compilerRow").hidden = false; note.hidden = true; $("mwConsent").hidden = true; $("mwAccept").checked = false;
  if (p === "n64") fillCompilers(select, info.compiler === "IDO" ? "ido-7.1" : "gcc-2.7.2-kmc", "N64");
  else if (p === "psx") fillCompilers(select, "gcc-2.7.2-psx", "PS1");
  else if (p === "ps2") fillCompilers(select, "ee-gcc-2.96", "PS2");
  else if (p === "gba") fillCompilers(select, "agbcc", "GBA");
  else if (p === "playstation") {
    $("compilerRow").hidden = true; note.hidden = false;
    note.textContent = "The compiler is picked from the disc: PSY-Q-era GCC for PlayStation, EE-GCC for PlayStation 2. You can change it later.";
  } else if (p === "nds") {
    select.innerHTML = `<optgroup label="Nintendo DS">${cw.ds.map((v) => `<option value="${esc(v)}" ${v === "DS/2.0/sp1p5" ? "selected" : ""}>CodeWarrior for DS ${esc(v.slice(3))}</option>`).join("")}</optgroup>`;
    note.hidden = false;
    note.textContent = "Not sure? Keep the default; you can change it later in tools/configure.py if functions don't match.";
    $("mwText").textContent = CW_PACK.nds.text;
    $("mwConsent").hidden = cw.ds_installed;
  } else {
    $("mwText").textContent = CW_PACK.gcwii.text;
    const group = (label, list, dflt) => `<optgroup label="${label}">${list.map((v) => `<option value="${esc(v)}" ${v === dflt ? "selected" : ""}>CodeWarrior ${esc(v)}</option>`).join("")}</optgroup>`;
    select.innerHTML = (p !== "wii" ? group("GameCube", cw.gc, "GC/1.3.2") : "") + (p !== "gc" ? group("Wii", cw.wii, p === "wii" ? "Wii/1.0" : "") : "");
    note.hidden = false;
    note.textContent = "Not sure? Keep the default; you can change it later in configure.py if functions don't match.";
    $("mwConsent").hidden = cw.installed;
  }
}

function updateAnalyse() {
  const needsConsent = !$("mwConsent").hidden;
  $("analyse").disabled = !$("own").checked || (needsConsent && !$("mwAccept").checked);
}
$("mwAccept").addEventListener("change", updateAnalyse);

function fillCompilers(select, current, platform) {
  const tcs = (S.st?.toolchains || []).filter((t) => t.kind === "compiler" && (!platform || !t.platform || t.platform === platform));
  select.innerHTML = tcs.map((t) => {
    const note = !t.supported ? " (not available on this computer)" : t.installed ? "" : t.license === "proprietary" ? " (install in Compilers & tools)" : " (will be installed)";
    return `<option value="${esc(t.id)}" ${t.id === current ? "selected" : ""} ${t.supported ? "" : "disabled"}>${esc(t.name)} · ${esc(t.use)}${note}</option>`;
  }).join("");
}

async function runImport(query, file, title, name = "") {
  $("dropIdle").hidden = true;
  $("dropPicked").hidden = true;
  $("dropBusy").hidden = false;
  $("busyTitle").textContent = title;
  $("busyStep").textContent = "starting";
  $("jobLog").textContent = "";
  $("impUp").hidden = !(file instanceof Blob);
  const t0 = Date.now();
  const tick = setInterval(() => {
    const s = Math.round((Date.now() - t0) / 1000);
    $("busyTime").textContent = s >= 3 ? ` · ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "";
  }, 1000);
  try {
    let started;
    if (file instanceof Blob) {
      started = await uploadFile(`/api/import?${query}&name=${encodeURIComponent(name || file.name || "game.bin")}`, file,
        (p) => { $("impUp").firstElementChild.style.width = (p * 100).toFixed(1) + "%"; $("busyTitle").textContent = p < 1 ? `Copying ${title.replace(/^\w+ /, "")} into decomp.wtf… ${(p * 100).toFixed(0)}%` : title; });
    } else {
      started = await api(`/api/import?${query}`, { method: "POST", raw: file ?? new ArrayBuffer(0) });
    }
    $("impUp").hidden = true;
    const res = await followJob(started.job, $("jobLog"), (log) => {
      const last = log.filter((l) => l.trim()).pop();
      if (last) $("busyStep").textContent = last.trim().replace(/\s+/g, " ").slice(0, 120);
    });
    loadNav();
    location.hash = `#/p/${res.slug}`;
  } catch (e) {
    importFailed(name || title.replace(/^\w+ |…$/g, ""), e, $("jobLog").textContent);
  } finally { clearInterval(tick); $("busyTime").textContent = ""; }
}

/* A failed import stays on screen with what happened, ready to report: a toast is gone before it's read. */
function importFailed(name, e, logText) {
  const job = e.job || {};
  const log = (job.log ? job.log.join("\n") : logText || "").trim();
  $("dropIdle").hidden = true; $("dropPicked").hidden = true; $("dropBusy").hidden = true;
  $("dropError").hidden = false;
  $("errName").textContent = name || "the game";
  $("errMsg").textContent = e.message;
  $("errLog").textContent = [log, job.trace].filter(Boolean).join("\n\n") || "(nothing was logged)";
  $("errCopy").onclick = async () => {
    const st = S.st || {};
    const report = [`decomp.wtf ${st.version || "?"}${st.build ? ` (${st.build})` : ""} · ${st.runtime || st.platform || navigator.platform}`,
      `Importing: ${name}`, `Error: ${e.message}`, "", "Log:", "```", log.split("\n").slice(-80).join("\n"), job.trace || "", "```"].join("\n");
    try { await navigator.clipboard.writeText(report); toast("Copied: paste it into a GitHub issue"); }
    catch { toast("Couldn't reach the clipboard: select the text under What happened instead", "err"); }
  };
}
$("errBack").onclick = (e) => { e.stopPropagation(); resetDrop(); };

async function ensureCodeWarrior() {
  if ($("mwConsent").hidden) return;
  $("busyTitle").textContent = "Installing CodeWarrior…";
  const pack = picked && picked.info.platform === "nds" ? CW_PACK.nds : CW_PACK.gcwii;
  const { job } = await api("/api/toolchain", { method: "POST", body: { id: pack.id, accept_license: true } });
  await followJob(job, $("jobLog"));
  await loadState();
}

$("analyse").onclick = async () => {
  if (!picked || $("analyse").disabled) return;
  const tc = $("compilerRow").hidden ? "" : $("compilerPick").value;
  const title = `Analysing ${picked.info.title}…`;
  if (!$("mwConsent").hidden) {
    $("dropPicked").hidden = true; $("dropBusy").hidden = false; $("impUp").hidden = true;
    try { await ensureCodeWarrior(); } catch (e) { importFailed("CodeWarrior", e, $("jobLog").textContent); return; }
  }
  const body = picked.buf instanceof Blob ? picked.buf : new Blob([picked.buf]);
  runImport(`confirmed=1${tc ? `&toolchain=${encodeURIComponent(tc)}` : ""}`, body, title, picked.name);
};
$("pathPick").onclick = (e) => {
  e.stopPropagation();
  const body = dialog("Use a file on this computer", `<p class="muted">For big disc images: decomp.wtf reads the file where it is instead of copying it through the browser.</p>
    <label class="field"><span>Path to the game file</span><input id="ppPath" class="mono" placeholder="/home/me/games/my-game.iso"></label>
    <label class="check"><input type="checkbox" id="ppOwn"><span>This is my own copy of this game, obtained legally. The results stay with me for my own use.</span></label>
    <div class="row"><button class="primary" id="ppGo" type="button" disabled>Start decompiling</button><button class="ghost" data-close type="button">Cancel</button></div>`);
  body.querySelector("#ppOwn").onchange = () => { $("ppGo").disabled = !$("ppOwn").checked; };
  body.querySelector("#ppGo").onclick = () => {
    const path = $("ppPath").value.trim();
    if (!path) return;
    $("dialog").hidden = true;
    runImport(`confirmed=1&path=${encodeURIComponent(path)}`, undefined, "Analysing your game…");
  };
};
$("demoBtn").onclick = () => runImport("demo=1", undefined, "Opening Demo Quest 64…");

/* ------------------------------------------------------------------ project */
async function openProject(slug, tab = "overview") {
  show("project");
  S.slug = slug; S.e = 0; S.a = 0; S.seq = {}; S.scores = new Map(); S.flash = new Map(); S.sessions = []; S.claims = [];
  S.apFeed = null; S.stats = null; S.map = []; S.mapSig = null; geo = null;
  $("activity").innerHTML = ""; $("thoughts").innerHTML = ""; $("now").innerHTML = ""; $("expLog").hidden = true; $("expResult").innerHTML = ""; $("teamBox").innerHTML = "";
  try {
    if (!S.st) await loadState();
    S.ov = await api(`/api/project?slug=${slug}`);
  } catch (e) {
    toast(esc(e.message), "err");
    location.hash = "#/";
    return;
  }
  document.querySelectorAll("#ptabs [data-ptab]").forEach((a) => { a.href = `#/p/${slug}/${a.dataset.ptab}`; });
  const fork = S.ov.kind === "fork";
  $("mapTitle").textContent = fork ? "Function map" : "ROM map";
  $("mapHint").textContent = fork ? "Every square is a function, grouped by source file. Click one to see it." : "Every square is a function, in ROM order. Click one to see it.";
  $("thAddr").textContent = fork ? "File" : "Address";
  groups.clear(); S.claimSig = "";
  L.q = ""; L.filter = "all"; L.limit = 200; $("fnSearch").value = "";
  $("instrEdit").value = S.ov.instructions || "";
  renderHeader(S.ov);
  markNav();
  const setup = fork && !S.ov.ready;
  $("ptabs").hidden = setup;
  $("forkSetup").hidden = !setup;
  document.querySelectorAll("[data-pane]").forEach((p) => { p.hidden = true; });
  if (setup) { renderForkSetup(); return; }
  setTab(tab);
  renderStats(S.ov);
  await refreshMap();
  await poll(true);
  S.timer = setInterval(() => poll(false), 1000);
  S.statsTimer = setInterval(loadStats, 8000);
  loadStats();
}

function stopPolling() {
  if (S.timer) clearInterval(S.timer);
  if (S.statsTimer) clearInterval(S.statsTimer);
  S.timer = null; S.statsTimer = null;
}

function setTab(tab) {
  if (!TABS.includes(tab)) tab = "overview";
  S.tab = tab;
  document.querySelectorAll("#ptabs [data-ptab]").forEach((a) => { a.classList.toggle("on", a.dataset.ptab === tab); a.setAttribute("aria-selected", a.dataset.ptab === tab); });
  if (S.ov && S.ov.kind === "fork" && !S.ov.ready) return;
  document.querySelectorAll("[data-pane]").forEach((p) => { p.hidden = p.dataset.pane !== tab; });
  if (tab === "overview") { if (S.map.length) layoutMap(); renderOverview(); }
  if (tab === "functions") renderList();
  if (tab === "agents") { renderLauncher($("launcher")); renderSessions(S.sessions); renderPrio(); }
  if (tab === "team") renderTeam();
  if (tab === "source" && S.ov) renderSource(S.ov);
  if (tab === "settings") renderProjectSettings();
  if (tab !== "team") clearInterval(teamTimer);
}
document.querySelectorAll("[data-goto]").forEach((a) => {
  a.onclick = (e) => {
    e.preventDefault();
    if (a.dataset.filter) { L.filter = a.dataset.filter; L.limit = 200; }
    location.hash = `#/p/${S.slug}/${a.dataset.goto}`;
  };
});

/* The whole map only when something changed (`force`: a rename or a split, which the signature can't see). */
async function refreshMap(force = true) {
  const slug = S.slug;
  const data = await api(`/api/map?slug=${slug}${!force && S.mapSig ? `&since=${encodeURIComponent(S.mapSig)}` : ""}`);
  if (slug !== S.slug) return;
  S.lastMapFetch = Date.now();
  if (data.same) return;
  S.mapSig = data.sig;
  S.map = data.rows.map((r) => { const f = {}; data.keys.forEach((k, i) => { f[k] = r[i]; }); return f; });
  S.byName = new Map(S.map.map((f, i) => [f.name, i]));
  S.map.forEach((f, i) => { f.i = i; });
  baseDirty = true;
  $("fnTabCount").textContent = S.map.length.toLocaleString();
  if (S.tab === "overview") layoutMap(); else applyClaims(false);
  renderList();
  renderProgress();
  renderOverview();
}

/* Helpers that just stopped on an error (the provider out of credit, a sign-in...): said once per error, as eight
   agents on one account stop together */
function toastStopped(before, now) {
  const was = new Map(before.map((s) => [s.id, s.state]));
  const why = new Map();
  for (const s of now) {
    if (s.state !== "error" || !was.has(s.id) || was.get(s.id) === "error") continue;
    if (!why.has(s.error)) why.set(s.error, []);
    why.get(s.error).push(s.label);
  }
  for (const [error, labels] of why) {
    const who = labels.length > 1 ? `${labels[0]} and ${labels.length - 1} more` : labels[0];
    toast(`${esc(who)} stopped: ${esc(error || "an error")}`, "err");
  }
}

let polling = false;
async function poll(first) {
  if (polling || !S.slug) return;
  polling = true;
  const slug = S.slug;
  try {
    const live = await api(`/api/live?slug=${slug}&e=${S.e}&a=${S.a}&s=${encodeURIComponent(JSON.stringify(S.seq))}`);
    if (slug !== S.slug) return;
    S.ov = live.overview;
    let mapDirty = false;
    const rescanned = !first && live.events.some((ev) => ev.kind === "rescan");
    for (const ev of live.events) {
      S.e = Math.max(S.e, ev.id);
      if (["match", "split", "rename", "rescan"].includes(ev.kind)) mapDirty = true;
      if (ev.kind === "match" && !first) {
        S.flash.set(ev.detail, performance.now());
        if (ev.agent !== AUTO && ev.agent !== "@" + AUTO) toast(`<code>${esc(ev.detail)}</code>&nbsp;matched by ${esc(ev.agent || "an agent")}`, "match");
      }
      if (ev.kind === "autopilot" && !first) toast(`Autopilot: ${esc(ev.detail)}`, "match");
      if (ev.kind === "link" && !first) toast(`🔗 <code>${esc(ev.detail)}</code>&nbsp;is linked into the game`, "match");
    }
    if (rescanned) {
      S.scores.clear(); S.flash.clear(); S.stats = null;
      S.diffKey = null;
      if (!$("drawer").hidden) {
        S.fn = null;
        if (S.editDirty) {
          $("tryBtn").disabled = true;
          $("fnPin").hidden = true;
          $("noteAdd").disabled = true;
          $("fnStatus").textContent = "map changed";
          $("tryMsg").hidden = false;
          $("tryMsg").className = "try-msg bad";
          $("tryMsg").textContent = "The GBA function map was rescanned. Your unsaved source is kept here: copy it before reopening a function against the new map.";
          showFnTab("code");
        } else {
          $("drawer").hidden = true;
          if ($("dialog").hidden) document.body.classList.remove("locked");
        }
      }
    }
    for (const at of live.attempts) {
      S.a = Math.max(S.a, at.id);
      if (!S.scores.has(at.function)) S.scores.set(at.function, []);
      S.scores.get(at.function).push({ score: at.score, t: at.created_at, insns: at.n_insns });
    }
    if (!first) applyAttempts(live.attempts);
    addActivity(live.events, live.attempts, first);
    S.claims = live.claims;
    if (!first) toastStopped(S.sessions || [], live.sessions);
    S.sessions = live.sessions;
    for (const s of live.sessions) {
      if (s.feed.length) S.seq[s.id] = s.feed[s.feed.length - 1].seq;
    }
    addThoughts(live.sessions, first);
    if (S.tab === "agents") renderSessions(live.sessions);
    renderNow(live.claims);
    renderStats(live.overview);
    const claimSig = live.claims.map((c) => c.name + "\u0000" + c.agent).join("\n");
    if (mapDirty || Date.now() - S.lastMapFetch > 20000) await refreshMap(mapDirty);
    else {
      applyClaims();
      if (claimSig !== S.claimSig || live.attempts.length) renderList();
    }
    S.claimSig = claimSig;
    if (rescanned) await loadStats();
    if (S.fn && !$("drawer").hidden) {
      const touched = live.attempts.some((a) => a.function === S.fn) || live.events.some((e) => e.detail === S.fn);
      if (touched) refreshFn(false); else fnBadge();
    }
    $("feedHint").textContent = "";
  } catch (e) {
    if (String(e.message).includes("Failed to fetch")) $("feedHint").textContent = "app not reachable";
  } finally {
    polling = false;
  }
}

async function loadStats() {
  if (!S.slug || S.tab !== "overview") return;
  try { S.stats = await api(`/api/stats?slug=${S.slug}`); } catch { return; }
  renderOverview();
}

/* Agents at work on this project: the helpers this app runs, plus outside agents (yours, the room's) with a claim. */
function workingAgents() {
  const running = S.sessions.filter((s) => ["starting", "working", "waiting"].includes(s.state));
  const own = new Set(S.sessions.map((s) => s.agent));
  const outside = [...new Set(S.claims.filter((c) => !own.has(c.agent) && c.agent !== AUTO).map((c) => c.agent))];
  return { running, outside, n: running.length + outside.length };
}

/* recommended | beta | "" for a platform key, as the browser or a project names it. */
function platformStatus(p) {
  const st = (S.st && S.st.platform_status) || {};
  const key = { gcwii: "gc", ps: "psx", playstation: "psx" }[p] || p;
  return st[key] || "";
}
function markPlatforms() {
  const any = Object.keys((S.st && S.st.platform_status) || {}).length;
  document.querySelectorAll("#platRow [data-p]").forEach((s) => {
    const rec = platformStatus(s.dataset.p) === "recommended";
    s.classList.toggle("rec", rec);
    s.title = rec ? "Recommended: the most tested path to a full decomp you can port and mod" : "Beta: works, less tested on real games";
    if (rec && !s.querySelector(".rec-mark")) s.insertAdjacentHTML("afterbegin", `<span class="rec-mark">★</span>`);
  });
  $("platLegend").hidden = !any;
}

function renderHeader(ov) {
  const fork = ov.kind === "fork";
  const chips = [`<span class="plat">${esc(ov.platform_name || "?")}</span>`];
  if (platformStatus(ov.platform) === "beta") chips.push(`<span class="chip-s beta" title="This platform works and is tested on homemade games, less so on real ones yet">beta</span>`);
  if (ov.rom && ov.rom.game_code) chips.push(`<span class="chip-s mono">${esc(ov.rom.game_code)}</span>`);
  if (fork) chips.push(`<span class="chip-s">${ov.origin === "scratch" ? `from scratch · ${esc(LAYOUT[ov.platform] || "decomp")}` : "public decomp"}</span>`);
  else if (ov.rom && ov.rom.region) chips.push(`<span class="chip-s">${esc(ov.rom.region)}</span>`);
  const comp = fork ? (ov.compiler_name ? cwName(ov.compiler_name) : "") : ov.compiler.name;
  if (comp) chips.push(`<span class="chip-s" title="${esc(fork ? "" : ov.compiler.flags.join(" "))}">${esc(comp)}</span>`);
  $("phChips").innerHTML = chips.join("");
  $("phTitle").textContent = ov.title;
  const pct = ov.bytes ? (100 * ov.matched_bytes) / ov.bytes : 0;
  $("phPct").textContent = pctNum(pct);
  $("phBar").style.width = pct + "%";
  $("phSub").textContent = fork && !ov.ready ? "set up the project to start"
    : `${ov.matched.toLocaleString()} / ${ov.functions.toLocaleString()} functions${linkedText(ov)} · ${plural(ov.attempts || 0, "compile")}${S.today ? ` · ${S.today} matched today` : ""}`;
  const w = workingAgents();
  $("phWorking").hidden = !w.n;
  $("phWorkingText").textContent = `${w.n} agent${w.n === 1 ? "" : "s"} working`;
  $("phStartText").textContent = w.running.length ? "Add agents" : "Start agents";
  $("phStart").hidden = fork && !ov.ready;
  $("phRescan").hidden = fork || ov.platform !== "gba";
  const room = ov.room;
  const pill = $("phRoom");
  pill.hidden = !room;
  // a project nobody shares yet: one click opens a room (a git project once it's set up)
  $("phTogether").hidden = !!room || (S.ov.kind === "fork" && !S.ov.ready);
  if (room) {
    const members = room.members || [];
    const online = members.filter((m) => m.online).length;
    pill.classList.toggle("off", room.role === "guest" && !room.connected);
    pill.innerHTML = room.role === "host" ? `<span class="dot ${online ? "on" : ""}"></span>Sharing · ${online} online`
      : /waiting for the host/i.test(room.error || "") ? `<span class="dot work"></span>Waiting to be let in`
      : `<span class="dot ${room.connected ? "on" : "err"}"></span>${room.connected ? `In a room as ${esc(room.member)}` : "Room offline"}`;
    $("teamTabCount").textContent = members.length ? members.length : "";
  } else $("teamTabCount").textContent = "";
  $("agTabCount").textContent = w.n || "";
}
$("phWorking").onclick = () => { location.hash = `#/p/${S.slug}/agents`; };
$("phRoom").onclick = () => { location.hash = `#/p/${S.slug}/team`; };
$("phTogether").onclick = () => openTogether();

/* Decompile together: open a room for this project in one step (a quick tunnel, an invite link); the Team tab has
   every option (an address of your own, approving members, who's in). */
function openTogether() {
  const ov = S.ov, fork = ov.kind === "fork";
  const body = dialog("Decompile together", `<p>Friends who own this game join from their own decomp.wtf, and their agents work with yours: nobody does the same function twice, and every match is checked here, against your copy, before it counts.</p>
    <p class="muted small">${fork ? "They get this project's source tree from you and build it with their own copy of the game and their own compilers." : "Each of them brings their own copy, and decomp.wtf checks it's exactly this game."} Only the work travels between you, never the game. You decide who's in.</p>
    <div class="field"><span>Who can join</span><div class="seg" id="tgVis"><button type="button" data-vis="private" class="on">Private: an invite link</button><button type="button" data-vis="public">Public: anyone with the address</button></div></div>
    <label class="check"><input type="checkbox" id="tgApprove"><span>I let each new member in (they wait until I say yes)</span></label>
    <div class="row"><button class="primary" id="tgGo" type="button">Open the room</button><a class="link small" href="#/p/${esc(S.slug)}/team">More options: your own address (LAN, Tailscale, a server)</a></div>
    <pre class="joblog" id="tgLog" hidden></pre><div id="tgDone"></div>`);
  body.querySelectorAll("[data-vis]").forEach((b) => { b.onclick = () => body.querySelectorAll("[data-vis]").forEach((x) => x.classList.toggle("on", x === b)); });
  body.querySelector("#tgGo").onclick = async (e) => {
    e.target.disabled = true;
    body.querySelector("#tgLog").hidden = false;
    try {
      const { job } = await api("/api/collab/start", { method: "POST", body: { slug: S.slug, mode: "tunnel",
        visibility: body.querySelector("[data-vis].on").dataset.vis, approve: body.querySelector("#tgApprove").checked } });
      const res = await followJob(job, body.querySelector("#tgLog"));
      body.querySelector("#tgLog").hidden = true;
      body.querySelector("#tgDone").innerHTML = `<p><b>The room is open.</b> Send this link to friends who own the game:</p>${cmdBox(res.link)}
        <p class="muted small">They open decomp.wtf, choose <b>Join a team</b>, paste it and pick their own copy (or run <code>decomp join &lt;link&gt; &lt;their copy&gt; --name &lt;name&gt;</code>). The link works while this app is open; for a room that stays up, use your own address in the Team tab.</p>
        <div class="row"><a class="primary small-btn as-btn" href="#/p/${esc(S.slug)}/team">See who's in</a></div>`;
      wireCopies(body.querySelector("#tgDone"));
      S.ov = await api(`/api/project?slug=${S.slug}`);
    } catch (err) { toast(esc(err.message), "err"); e.target.disabled = false; }
  };
}
$("phFolder").onclick = () => openFolder();

$("phRescan").onclick = () => {
  if (S.ov.kind === "fork" || S.ov.platform !== "gba") return;
  const slug = S.slug;
  const body = dialog("Rescan GBA functions", `<p>Analyse this project's ROM again using your edited <code>functions.cfg</code>. Preview does not change the project.</p>
    <p class="muted small">Stop agents and autopilot and release any claims first. Applying keeps unchanged functions and safely archives prior work for changed or removed targets. Nothing starts automatically.</p>
    <div class="row"><button class="primary" id="rsPreview" type="button">Preview changes</button><button class="ghost" id="rsApply" type="button" disabled>Apply reviewed changes</button></div>
    <p id="rsStatus" role="status" aria-live="polite"></p>
    <p id="rsError" class="bad" role="alert" hidden></p>
    <div id="rsReport" hidden><p id="rsCounts"></p><details open><summary>Full report · inspect before applying</summary><pre class="joblog tall" id="rsDetails"></pre></details></div>
    <details id="rsLogBox" hidden><summary>Analysis log</summary><pre class="joblog" id="rsLog"></pre></details>`);
  const preview = body.querySelector("#rsPreview"), apply = body.querySelector("#rsApply");
  const status = body.querySelector("#rsStatus"), error = body.querySelector("#rsError");
  const report = body.querySelector("#rsReport"), counts = body.querySelector("#rsCounts");
  const details = body.querySelector("#rsDetails"), log = body.querySelector("#rsLog");
  let revision = null;
  const run = async (applying) => {
    if (applying && !revision) return;
    if (applying && S.slug === slug && S.fn && S.editDirty) {
      error.textContent = "Keep your unsaved editor source before applying, then preview again.";
      error.hidden = false;
      revision = null; apply.disabled = true;
      return;
    }
    let applied = false;
    const reviewedRevision = revision;
    revision = null;
    preview.disabled = true; apply.disabled = true;
    error.hidden = true;
    status.textContent = applying ? "Applying reviewed changes…" : "Analysing functions.cfg…";
    body.querySelector("#rsLogBox").hidden = false;
    log.textContent = "";
    try {
      const { job } = await api("/api/rescan", { method: "POST", body: { slug, apply: applying, ...(applying ? { revision: reviewedRevision } : {}) } });
      const result = await followJob(job, log);
      applied = applying;
      const c = result.counts;
      counts.textContent = `${c.added.toLocaleString()} added · ${c.removed.toLocaleString()} removed · ${c.changed.toLocaleString()} changed · ${c.unchanged.toLocaleString()} unchanged`;
      details.textContent = JSON.stringify(result, null, 2);
      report.hidden = false;
      if (applying) {
        status.textContent = "Changes applied. Agents and autopilot remain stopped.";
        if (S.slug === slug) {
          $("drawer").hidden = true; S.fn = null;
          S.scores.clear(); S.flash.clear(); S.stats = null;
          S.ov = await api(`/api/project?slug=${encodeURIComponent(slug)}`);
          renderStats(S.ov);
          await refreshMap();
          await loadStats();
        }
      } else {
        revision = result.revision;
        apply.disabled = !revision;
        status.textContent = "Preview ready. Inspect the full report, then explicitly apply if it is correct.";
      }
    } catch (e) {
      status.textContent = applied ? "Changes applied, but refreshing the view failed. Reopen the project to refresh it."
        : applying ? "Apply was not confirmed. Preview again before another apply; no automatic retry." : "Preview did not complete.";
      error.textContent = e.message;
      error.hidden = false;
    } finally {
      preview.disabled = false;
    }
  };
  preview.onclick = () => run(false);
  apply.onclick = () => run(true);
};

/* Forks: how much of the game is built from linked C (decomp.dev's "linked"), next to what matches. */
function linkedText(ov) {
  const p = ov.kind === "fork" && ov.measures ? ov.measures.complete_code_percent : null;
  return p == null ? "" : ` · ${pctNum(Number(p))}% linked`;
}

function renderStats(ov) {
  renderHeader(ov);
  const pct = ov.bytes ? (100 * ov.matched_bytes) / ov.bytes : 0;
  $("pct").textContent = pctNum(pct);
  $("pctSub").textContent = (ov.bytes ? `${bytes(ov.matched_bytes)} of ${bytes(ov.bytes)}` : "") + linkedText(ov) + (S.today ? ` · ${S.today} matched today` : "");
  $("fnCount").textContent = `${ov.matched.toLocaleString()} / ${ov.functions.toLocaleString()}`;
  renderAutopilot(ov);
  renderSteering(ov);
  if (S.tab === "source") renderSource(ov);
  renderCompilerNotice(ov);
  renderAgentsKpi(ov);
  renderNext(ov);
}

/* Linux-only compilers need a Linux side: WSL 2 on Windows, the compiler container (Docker Desktop) on a Mac. */
function linuxHint(side) {
  return side === "wsl"
    ? " It's a Linux program: on Windows it runs inside WSL 2 (one-time setup: <code>wsl --install</code> in an administrator PowerShell, then restart)."
    : " It's a Linux program: on a Mac it runs in a small compiler-only container (needs Docker Desktop running; installing builds the container's image once).";
}

function renderCompilerNotice(ov) {
  const n = $("compilerNotice");
  const unc = ov.kind === "fork" && ov.ready ? ov.uncovered : null;
  if (unc && unc.functions) {
    n.hidden = false;
    n.className = "notice";
    n.innerHTML = `<span><b>${unc.functions.toLocaleString()} functions</b> are in code no source file covers yet, so agents can't decompile them. decomp.wtf can give each stretch of code a file (checked against the game's checksum, committed to git).</span><span class="grow"></span><button class="primary small-btn" id="startFiles" type="button"${S.startingFiles ? " disabled" : ""}>${S.startingFiles ? "Splitting the game…" : "Give them source files"}</button>`;
    $("startFiles").onclick = async () => {
      S.startingFiles = true;
      renderCompilerNotice(ov);
      try {
        const { job } = await api("/api/fork/start-files", { method: "POST", body: { slug: S.slug } });
        const res = await followJob(job);
        toast(`${(res && res.functions || 0).toLocaleString()} more functions can be decompiled now.`, "match");
        await refreshMap();
      } catch (e) { toast(esc(e.message), "err"); }
      S.startingFiles = false;
    };
    return;
  }
  if (ov.kind === "fork" || ov.compiler.installed) { n.hidden = true; return; }
  n.hidden = false;
  if (!ov.compiler.supported) {
    n.className = "notice bad";
    n.innerHTML = ov.compiler.why
      ? `<span><b>${esc(ov.compiler.name)}</b> has no build for this computer: ${esc(ov.compiler.why)}.</span>`
      : `<span><b>${esc(ov.compiler.name)}</b> has no build for this computer yet. Pick another compiler in this project's Settings.</span>`;
    return;
  }
  n.className = "notice";
  n.innerHTML = ov.compiler.proprietary
    ? `<span><b>${esc(ov.compiler.name)}</b> isn't installed. It's proprietary software (recompiled by the decomp community); install it only if you accept that.</span><span class="grow"></span><button class="primary small-btn" id="tcInstall" type="button">Install…</button>`
    : `<span><b>${esc(ov.compiler.name)}</b> isn't installed yet.${ov.compiler.linux ? linuxHint(ov.compiler.linux_side) : ""}</span><span class="grow"></span><button class="primary small-btn" id="tcInstall" type="button">Install now</button>`;
  $("tcInstall").onclick = () => installToolchain(ov.compiler.toolchain, ov.compiler.proprietary);
}

function fnCounts() {
  const c = { matched: 0, working: 0, tried: 0, open: 0, asm: 0 };
  for (const f of S.map) c[fnState(f)]++;
  return c;
}

/* The overview's numbers: function states, pace, who matched what, recent matches. */
function renderOverview() {
  if (!S.ov || S.tab !== "overview") return;
  const c = fnCounts(), total = Math.max(1, S.map.length);
  const seg = (k, cls) => (c[k] ? `<i class="${cls}" data-w="${(100 * c[k]) / total}" title="${c[k]} ${k === "open" ? "to do" : k}"></i>` : "");
  $("fnStack").innerHTML = seg("matched", "s-match") + seg("working", "s-work") + seg("tried", "s-tried") + seg("asm", "s-asm") + seg("open", "s-open");
  setWidths($("fnStack"));
  const togo = S.ov.functions - S.ov.matched;
  $("fnSub").textContent = `${togo.toLocaleString()} to go${c.tried ? ` · ${c.tried.toLocaleString()} tried` : ""}${c.working ? ` · ${c.working} in progress` : ""}${c.asm ? ` · ${c.asm} assembly` : ""}`;
  const st = S.stats;
  if (st) {
    const ph = st.matches_per_hour, max = Math.max(1, ...ph);
    const last3 = ph.slice(-3).reduce((a, b) => a + b, 0) / 3;
    $("paceNum").textContent = `${ph[23]}`;
    $("paceBars").innerHTML = ph.map((n, i) => `<rect x="${i * 10 + 1}" y="${28 - Math.max(n ? 3 : 1, (26 * n) / max)}" width="8" height="${Math.max(n ? 3 : 1, (26 * n) / max)}" rx="1.5" class="${n ? "on" : ""}"><title>${n} matched ${23 - i ? `${23 - i} h ago` : "this hour"}</title></rect>`).join("");
    $("paceSub").textContent = `matched in the last hour · ${plural(st.compiles_last_hour, "compile")}` + (last3 > 0 && togo > 0 ? ` · ≈ ${duration(togo / last3)} to go at this pace` : "");
    $("board").innerHTML = st.agents.length ? st.agents.slice(0, 8).map((r) => `<li><span class="who">${esc(r.agent)}</span><span class="grow"></span>
        <span class="mono small">${r.matched ? `<b class="ok">${r.matched.toLocaleString()}</b> matched · ` : ""}${plural(r.compiles, "compile")}</span></li>`).join("")
      : `<li class="muted small">Nobody yet. Matches by agents, the autopilot and you show up here.</li>`;
  }
  const recent = S.map.filter((f) => f.status === "matched" && f.matched_at).sort((a, b) => b.matched_at - a.matched_at).slice(0, 8);
  $("recentMatches").innerHTML = recent.length ? recent.map((f) => `<li><span class="ok">✓</span>${fnLink(f.name)}<span class="grow"></span><span class="muted small">${esc(f.matched_by || "")} · ${esc(since(f.matched_at))}</span></li>`).join("")
    : `<li class="muted small">No matches yet.</li>`;
  $("recentMatches").querySelectorAll(".fn").forEach((x) => { x.onclick = () => openFn(x.dataset.fn); });
}

function renderAgentsKpi(ov) {
  const w = workingAgents();
  $("agNum").textContent = w.n;
  const names = [...w.running.map((s) => s.label), ...w.outside];
  $("agSub").textContent = names.length ? names.slice(0, 4).join(", ") + (names.length > 4 ? ` +${names.length - 4}` : "") : "none working right now";
  const sp = ov.spend, n = ov.helper_matches || 0, budget = S.st?.settings?.budget_usd || 0;
  $("spendSub").textContent = sp && sp.usd ? `${money(sp.usd)} spent${n ? ` · ${money(sp.usd / n)} per match` : ""}${budget ? ` · budget ${money(budget)}` : ""}`
    : sp && sp.tokens ? `${(sp.tokens / 1000).toFixed(0)}k tokens used` : budget ? `budget ${money(budget)}` : "";
}

/* What to do next, so a first project is plug and play. */
function renderNext(ov) {
  const box = $("nextStep");
  const w = workingAgents();
  const done = ov.functions > 0 && ov.matched >= ov.functions;
  const compilerOk = ov.kind === "fork" || ov.compiler.installed;
  if (done) {
    box.hidden = false;
    box.innerHTML = `<div class="next-text"><b>Every function matches.</b><span class="muted small">Export the project: a source tree that rebuilds your game byte for byte.</span></div><a class="primary small-btn as-btn" href="#/p/${esc(S.slug)}/source">Export</a>`;
    return;
  }
  if (ov.kind === "fork" && ov.ready && !ov.functions) {
    box.hidden = false;
    box.innerHTML = `<div class="next-text"><b>Not measured yet</b><span class="muted small">objdiff compares every unit of the project with your game: it takes a moment.</span></div><button class="primary" id="nextMeasure" type="button">Measure now</button>`;
    $("nextMeasure").onclick = async () => {
      $("nextMeasure").disabled = true;
      toast("Measuring the whole project with objdiff…");
      try { const { job } = await api("/api/fork/check", { method: "POST", body: { slug: S.slug } }); await followJob(job); toast("Measured.", "match"); await refreshMap(); }
      catch (e) { toast(esc(e.message), "err"); $("nextMeasure").disabled = false; }
    };
    return;
  }
  if (w.n || !compilerOk || !S.st) { box.hidden = true; return; }
  launcherInit();
  const a = S.st.agents.find((x) => x.id === LZ.kind);
  const ap = ov.autopilot;
  const steps = [`<span class="ok">✓</span> ${ov.kind === "fork" ? "Project set up" : "Game analysed"}: ${plural(ov.functions, "function")}`];
  if (ap) steps.push(ap.state === "running" ? `<span class="spin-dot"></span> Autopilot trying machine drafts` : `<span class="ok">✓</span> Autopilot: ${ap.matched.toLocaleString()} free match${ap.matched === 1 ? "" : "es"}`);
  steps.push(`<span class="todo">○</span> Agents`, `<span class="todo">○</span> Export`);
  box.hidden = false;
  box.innerHTML = `<div class="next-text"><b>Next: start your agents</b><span class="steps-line">${steps.join("")}</span></div>
    <div class="next-actions">
      ${a && (a.installed || a.id === "api") ? `<button class="primary" id="nextGo" type="button">${esc(launchLabel(a, LZ.model, LZ.count))}</button>` : ""}
      <button class="ghost" id="nextPick" type="button">Choose agent &amp; model</button>
    </div>`;
  const go = $("nextGo");
  if (go) go.onclick = () => a.id !== "api" ? startAgents(a, LZ.model, LZ.count, go)
    : (LZ.eps || []).length ? startAgents(a, "", LZ.count, go, { endpoints: LZ.eps }) : showLaunch();
  $("nextPick").onclick = () => showLaunch();
}

/* ------------------------------------------------------------------ team (rooms) */
let teamTimer = null;
async function renderTeam() {
  const box = $("teamBox");
  clearInterval(teamTimer);
  if (!S.ov) return;
  const room = S.ov.room;
  if (room && room.role === "guest") { renderGuest(box, room); teamTimer = setInterval(() => { if (S.tab === "team" && S.ov.room) renderGuest(box, S.ov.room); }, 4000); return; }
  if (S.ov.kind === "fork" && !S.ov.ready) {
    box.innerHTML = `<h2 class="pane-title">Decompile together</h2>
      <p class="muted">Set the project up first (Overview): then friends who own the game can join it from their own decomp.wtf.</p>
      <div class="row"><button class="ghost" id="tmJoin" type="button">Join a team's room</button></div>`;
    $("tmJoin").onclick = openJoin;
    return;
  }
  if (!box.firstElementChild) box.innerHTML = `<p class="muted">Looking at this computer's network…</p>`;
  const info = await api(`/api/collab?slug=${S.slug}`).catch((e) => { box.innerHTML = `<p class="bad">${esc(e.message)}</p>`; return null; });
  if (!info || S.tab !== "team") return;
  renderHost(box, info);
  teamTimer = setInterval(async () => {
    if (S.tab !== "team" || !S.slug) { clearInterval(teamTimer); return; }
    if (box.contains(document.activeElement) && document.activeElement.matches("input")) return;  // typing
    const inf = await api(`/api/collab?slug=${S.slug}`).catch(() => null);
    if (inf && inf.sharing && S.tab === "team") renderHost(box, inf);
  }, 5000);
}

function renderGuest(box, room) {
  const members = room.members || [];
  const waiting = /waiting for the host/i.test(room.error || "");
  const people = members.map((m) => `<div class="line-item"><span class="dot ${m.online ? "on" : ""}"></span><div class="txt"><b>${esc(m.name)}${m.name === room.member ? " (you)" : ""}</b>
      <span>${m.agents && m.agents.length ? esc(m.agents.join(", ")) : "no agents yet"} · ${m.matched || 0} matched${m.working && m.working.length ? ` · on ${esc(m.working.slice(0, 3).join(", "))}` : ""}</span></div></div>`).join("");
  box.innerHTML = `<h2 class="pane-title">${waiting ? "Waiting for the host to let you in" : room.connected ? "In a team room" : "The room is offline"}</h2>
    <p class="muted">${waiting ? `You asked to join as <b>${esc(room.member)}</b>. Your agents start working with the room as soon as the host lets you in.`
      : room.connected ? `You're <b>${esc(room.member)}</b>. Your agents claim functions through the room, so nobody does the same one twice; matches are checked and shared by the host.`
      : `${esc(room.error || "Waiting for the host…")} Your agents wait and carry on when it's back.`}</p>
    <dl class="facts"><div><dt>Host</dt><dd>${esc(room.host)}</dd></div><div><dt>Members</dt><dd>${members.length}</dd></div><div><dt>Online</dt><dd>${members.filter((m) => m.online).length}</dd></div></dl>
    ${people ? `<div class="setup-group"><h3>In the room</h3>${people}</div>` : ""}
    <div class="row"><button class="ghost" id="roomReconnect" type="button">${room.connected ? "The host shared a new link?" : "Reconnect with a new link"}</button><button class="ghost danger" id="roomLeave" type="button">Leave room</button></div>`;
  $("roomReconnect").onclick = openReconnect;
  $("roomLeave").onclick = async () => {
    if (!confirm("Leave this room? Your local copy of the project stays; it stops syncing.")) return;
    await api("/api/collab/leave", { method: "POST", body: { slug: S.slug } }).catch((e) => toast(esc(e.message), "err"));
  };
}

function renderHost(box, info) {
  const intro = `<h2 class="pane-title">Decompile together</h2>
    <p class="muted">Your team works on this game from their own computers, each with <b>their own copy of exactly this game</b> (decomp.wtf checks it's identical) and their own agents. This computer is the room's host: it keeps the shared work and checks every match against your copy.${S.ov.kind === "fork" ? " Members get this project's source tree from you (only what git tracks: never your game or its extracted files) and follow every commit made here." : ""} Only the work travels, never ROM data. You host the room and decide who's in.</p>`;
  const opts = (inf) => `<div class="setup-group"><h3>Who can join</h3>
      <div class="seg" id="visSeg"><button type="button" data-vis="private" class="${inf.settings.visibility !== "public" ? "on" : ""}">Private: invite link</button><button type="button" data-vis="public" class="${inf.settings.visibility === "public" ? "on" : ""}">Public: anyone with the address</button></div>
      <label class="check"><input type="checkbox" id="apprChk" ${inf.settings.approve ? "checked" : ""}><span>I let each new member in (they wait until you approve)</span></label></div>`;
  if (!info.sharing) {
    const sugg = (info.addresses || []).map((a) => `<button type="button" class="chip" data-addr="${esc(a.address)}">${esc(a.label)}: <span>${esc(a.address)}</span></button>`).join("");
    box.innerHTML = `${intro}${info.was_sharing ? `<div class="notice"><span>This room was shared before the app restarted. Share it again: members keep their place.</span></div>` : ""}
      <div class="setup-group"><h3>How teammates reach this computer</h3>
        <label class="check"><input type="radio" name="mode" value="tunnel" ${info.mode !== "address" ? "checked" : ""} ${info.tunnel_supported ? "" : "disabled"}><span><b>Quick tunnel</b>: works from anywhere, nothing to set up (Cloudflare's free tunnel). The address changes when the app restarts; members then paste the new link.</span></label>
        <label class="check"><input type="radio" name="mode" value="address" ${info.mode === "address" ? "checked" : ""}><span><b>An address you control</b>: this network, Tailscale, or a server with its own domain. It stays the same, so the room reopens by itself after a restart.</span></label>
        <div id="addrBox" ${info.mode === "address" ? "" : "hidden"}>
          <label class="field"><span>The address teammates use (the room listens on port <input id="addrPort" class="mono port" type="number" value="${info.port || 6481}">)</span><input id="addrIn" class="mono" placeholder="http://192.168.1.20:6481" value="${esc(info.address || "")}"></label>
          <div class="chips">${sugg}</div></div></div>
      ${opts(info)}
      <div class="row"><button class="primary" id="startShare" type="button">Start sharing</button></div><pre class="joblog" id="shareLog" hidden></pre>`;
    box.querySelectorAll('[name="mode"]').forEach((r) => { r.onchange = () => { box.querySelector("#addrBox").hidden = r.value !== "address" || !r.checked; }; });
    box.querySelectorAll("[data-addr]").forEach((b) => { b.onclick = () => { box.querySelector("#addrIn").value = b.dataset.addr; }; });
    box.querySelectorAll("[data-vis]").forEach((b) => { b.onclick = () => box.querySelectorAll("[data-vis]").forEach((x) => x.classList.toggle("on", x === b)); });
    box.querySelector("#startShare").onclick = async () => {
      const mode = box.querySelector('[name="mode"]:checked')?.value || "tunnel";
      const visibility = box.querySelector("[data-vis].on")?.dataset.vis || "private";
      box.querySelector("#shareLog").hidden = false;
      box.querySelector("#startShare").disabled = true;
      try {
        const { job } = await api("/api/collab/start", { method: "POST", body: { slug: S.slug, mode, visibility,
          approve: box.querySelector("#apprChk").checked, address: box.querySelector("#addrIn")?.value || "",
          port: Number(box.querySelector("#addrPort")?.value) || 6481, keep: info.was_sharing } });
        await followJob(job, box.querySelector("#shareLog"));
        renderHost(box, await api(`/api/collab?slug=${S.slug}`));
      } catch (e) { toast(esc(e.message), "err"); box.querySelector("#startShare").disabled = false; }
    };
    return;
  }
  const members = info.room?.members || [];
  const online = members.filter((m) => m.online).length;
  const people = members.map((m) => `<div class="line-item"><span class="dot ${m.online ? "on" : ""}"></span><div class="txt"><b>${esc(m.name)}</b>
      <span>${m.agents.length ? esc(m.agents.join(", ")) : "no agents yet"} · ${m.matched} matched${m.working.length ? ` · on ${esc(m.working.slice(0, 3).join(", "))}` : ""}</span></div>
      <button class="ghost small-btn danger" data-remove="${esc(m.name)}" type="button">Remove</button></div>`).join("");
  const waiting = (info.pending || []).map((p) => `<div class="line-item"><div class="txt"><b>${esc(p.name)}</b><span>asks to join</span></div>
      <button class="primary small-btn" data-approve="${esc(p.name)}" type="button">Let in</button><button class="ghost small-btn" data-decline="${esc(p.name)}" type="button">Decline</button></div>`).join("");
  box.innerHTML = `${intro}<label class="field"><span>${info.settings.visibility === "public" ? "The room's address (anyone with it and their own copy of the game can join)" : "Invite link (send it to teammates who own this game)"}</span></label>${cmdBox(info.link || "")}
    <p class="muted small">${info.mode === "address" ? "Shared at your own address: the room reopens by itself when this app restarts." : "Shared through a quick tunnel: the address changes if this app restarts."} Teammates choose <i>Join a team</i> in decomp.wtf, or run <code>decomp join &lt;link&gt; &lt;their copy&gt; --name &lt;name&gt;</code>.</p>
    ${waiting ? `<div class="setup-group"><h3>Waiting to join</h3>${waiting}</div>` : ""}
    <div class="setup-group"><h3>In the room · ${members.length} member${members.length === 1 ? "" : "s"}, ${online} online</h3>${people || `<p class="muted small">Nobody yet.</p>`}</div>
    ${opts(info)}
    <div class="setup-group"><h3>Keep it open</h3><div class="row" id="svcRow"><span class="muted small">Checking…</span></div></div>
    <div class="row"><button class="ghost" id="rotate" type="button">New invite link</button><button class="ghost danger" id="stopShare" type="button">Stop sharing</button></div>`;
  wireCopies(box);
  const manage = async (payload) => {
    try { await api("/api/collab/manage", { method: "POST", body: { slug: S.slug, ...payload } }); renderHost(box, await api(`/api/collab?slug=${S.slug}`)); }
    catch (e) { toast(esc(e.message), "err"); }
  };
  box.querySelectorAll("[data-remove]").forEach((b) => { b.onclick = () => { if (confirm(`Remove ${b.dataset.remove} from the room? Their agents stop working with it.`)) manage({ action: "remove", name: b.dataset.remove }); }; });
  box.querySelectorAll("[data-approve]").forEach((b) => { b.onclick = () => manage({ action: "approve", name: b.dataset.approve }); });
  box.querySelectorAll("[data-decline]").forEach((b) => { b.onclick = () => manage({ action: "decline", name: b.dataset.decline }); });
  box.querySelectorAll("[data-vis]").forEach((b) => { b.onclick = () => manage({ action: "settings", visibility: b.dataset.vis }); });
  box.querySelector("#apprChk").onchange = (e) => manage({ action: "settings", approve: e.target.checked });
  box.querySelector("#rotate").onclick = () => { if (confirm("Make a new invite link? The old one stops working for people who haven't joined yet; members stay in.")) manage({ action: "rotate" }); };
  box.querySelector("#stopShare").onclick = async () => {
    await api("/api/collab/stop", { method: "POST", body: { slug: S.slug } });
    renderHost(box, await api(`/api/collab?slug=${S.slug}`));
  };
  serviceRow(box.querySelector("#svcRow"), "The room is open while decomp.wtf runs.");
}

/* Start decomp.wtf with the computer (systemd user service, launchd, or a log-on task). */
function serviceRow(row, offText) {
  if (!row) return;
  if (S.st && S.st.runtime === "docker") { row.innerHTML = `<span class="muted small">Docker keeps decomp.wtf running in the background.</span>`; return; }
  if (S.st && S.st.runtime === "wsl") { row.innerHTML = `<span class="muted small">decomp.wtf runs while its window is open (the Start menu's decomp.wtf).</span>`; return; }
  api("/api/service").then((sv) => {
    if (!row.isConnected) return;
    row.innerHTML = sv.installed ? `<span class="ok-mark">decomp.wtf starts with this computer</span><button class="ghost small-btn" data-svc="remove" type="button">Don't</button>`
      : `<span class="muted small grow">${esc(offText)}</span><button class="ghost small-btn" data-svc="install" type="button">Start decomp.wtf with this computer</button>`;
    row.querySelector("[data-svc]").onclick = async (e) => {
      try {
        const r = await api("/api/service", { method: "POST", body: { action: e.target.dataset.svc } });
        if (r.message) toast(esc(r.message));
        serviceRow(row, offText);
      } catch (err) { toast(esc(err.message), "err"); }
    };
  }).catch(() => { row.innerHTML = ""; });
}

function openReconnect() {
  const body = dialog("Reconnect to the room", `<p class="muted">When the host restarts decomp.wtf with a quick tunnel, their invite link changes. Paste the new one; you stay the same member and keep everything you've done.</p>
    <label class="field"><span>New invite link</span><input id="rLink" class="mono" placeholder="https://….trycloudflare.com/join/…"></label>
    <div class="row"><button class="primary" id="rGo" type="button">Reconnect</button></div><pre class="joblog" id="rLog" hidden></pre>`);
  body.querySelector("#rGo").onclick = async () => {
    if (!$("rLink").value.trim()) return toast("Paste the host's new invite link.", "err");
    $("rLog").hidden = false;
    $("rGo").disabled = true;
    try {
      const { job } = await api("/api/collab/reconnect", { method: "POST", body: { slug: S.slug, link: $("rLink").value.trim() } });
      await followJob(job, $("rLog"));
      $("dialog").hidden = true;
      toast("Back in the room.", "match");
    } catch (e) { toast(esc(e.message), "err"); $("rGo").disabled = false; }
  };
}

function openJoin() {
  const body = dialog("Join a team's room", `<p class="muted">Paste the invite link (or a public room's address), then pick <b>your own copy</b> of the same game. decomp.wtf checks it's identical to the host's; your copy itself is never sent.</p>
    <label class="field"><span>Invite link</span><input id="jLink" class="mono" placeholder="https://….trycloudflare.com/join/…"></label>
    <label class="field"><span>Your name in the room</span><input id="jName" maxlength="24" placeholder="e.g. alex"></label>
    <label class="field"><span>Your copy of the game</span><input id="jRom" type="file"></label>
    <label class="field"><span>…or its path on this computer (best for a GameCube or Wii disc: it's read where it is)</span><input id="jPath" class="mono" placeholder="${S.st && S.st.host_os === "windows" ? "C:\\Games\\game.rvz" : "/home/you/games/game.iso"}"></label>
    <label class="check"><input type="checkbox" id="jOwn"><span>This is my own copy of this game, obtained legally. I'll keep what the room makes private.</span></label>
    <p class="muted small">A GameCube, Wii or DS project comes with its whole source tree from the host, and is then built here with your copy and your compilers: the first time takes a few minutes.</p>
    <div class="row"><button class="primary" id="jGo" type="button">Join</button></div><pre class="joblog" id="jLog" hidden></pre>`);
  body.querySelector("#jGo").onclick = async () => {
    const file = $("jRom").files[0], path = $("jPath").value.trim();
    if (!$("jLink").value.trim() || !/^[A-Za-z0-9_-]{1,24}$/.test($("jName").value) || !(file || path) || !$("jOwn").checked) {
      return toast("Fill in the link, a name (letters, digits, - or _), your copy of the game, and confirm it's your own.", "err");
    }
    $("jLog").hidden = false;
    $("jGo").disabled = true;
    try {
      let q = `link=${encodeURIComponent($("jLink").value.trim())}&name=${encodeURIComponent($("jName").value)}&confirmed=1`;
      q += path ? `&path=${encodeURIComponent(path)}` : `&filename=${encodeURIComponent(file.name)}`;
      // the file streams from disk as it's sent: a disc image is never read into the page whole
      const { job } = await api(`/api/collab/join?${q}`, { method: "POST", raw: path ? new Blob([]) : file });
      const res = await followJob(job, $("jLog"));
      $("dialog").hidden = true;
      loadNav();
      location.hash = `#/p/${res.slug}`;
    } catch (e) { toast(esc(e.message), "err"); $("jGo").disabled = false; }
  };
}

async function installToolchain(id, proprietary) {
  if (proprietary && !confirm("This downloads a proprietary compiler, kept by the decomp community. Install it only if you accept that. Continue?")) return;
  toast("Installing…");
  try {
    const { job } = await api("/api/toolchain", { method: "POST", body: { id, accept_license: proprietary } });
    await followJob(job);
    toast("Installed.", "match");
    await loadState();
    if (S.slug) renderStats(await api(`/api/project?slug=${S.slug}`));
    if (!$("toolsPage").hidden) renderTools();
  } catch (e) { toast(esc(e.message), "err"); }
}

/* ------------------------------------------------------------------ public decomps */
const PLAT_ORDER = ["gc", "wii", "ps", "ps2", "n64", "gba", "nds", "switch", "win32", "xbox360", "3ds", "xbox", "psp"];
const PLAT_SHORT = { gc: "GameCube", wii: "Wii", ps: "PS1", psx: "PS1", ps2: "PS2", n64: "N64", gba: "GBA", nds: "DS", switch: "Switch", win32: "PC", xbox360: "Xbox 360", "3ds": "3DS", xbox: "Xbox", psp: "PSP" };
const cat = { list: null, plat: "", q: "", done: false };

async function loadCatalog() {
  if (!cat.list) cat.list = (await api("/api/catalog")).projects;
  return cat.list;
}

function ago(iso) {
  if (!iso) return "";
  const d = (Date.now() - Date.parse(iso)) / 86400000;
  return d < 1 ? "today" : d < 2 ? "yesterday" : d < 60 ? `${Math.round(d)} days ago` : d < 730 ? `${Math.round(d / 30)} months ago` : `${Math.round(d / 365)} years ago`;
}

async function showCatalog() {
  show("catalog");
  $("catGrid").innerHTML = `<p class="muted">Loading projects from decomp.dev…</p>`;
  try { await loadCatalog(); } catch (e) {
    $("catGrid").innerHTML = `<div class="cat-empty"><p class="muted">${esc(e.message)}, so its list can't be shown here. You can still continue any public decomp:</p>
      <p><a class="btn ghost" href="https://decomp.dev" target="_blank" rel="noopener">Browse decomp.dev</a> <span class="muted small">open a project there, copy its repository link and paste it below.</span></p></div>`;
    $("catNote").textContent = ""; $("catChips").innerHTML = "";
    return;
  }
  if (!S.st) await loadState().catch(() => {});
  renderCatalog();
}

function renderCatalog() {
  const all = cat.list || [];
  const counts = {};
  const shown = all.filter((p) => cat.done || p.matched < 99.99);
  for (const p of shown) counts[p.platform] = (counts[p.platform] || 0) + 1;
  const plats = PLAT_ORDER.filter((k) => counts[k]).concat(Object.keys(counts).filter((k) => !PLAT_ORDER.includes(k)));
  $("catChips").innerHTML = [`<button class="chip ${cat.plat ? "" : "on"}" data-plat="" type="button">All <span>${shown.length}</span></button>`]
    .concat(plats.map((k) => `<button class="chip ${cat.plat === k ? "on" : ""}" data-plat="${esc(k)}" type="button">${esc(PLAT_SHORT[k] || k.toUpperCase())} <span>${counts[k]}</span></button>`)).join("");
  $("catChips").querySelectorAll(".chip").forEach((b) => { b.onclick = () => { cat.plat = b.dataset.plat; renderCatalog(); }; });
  const q = cat.q.toLowerCase();
  const forked = new Map((S.st?.projects || []).filter((p) => p.kind === "fork").map((p) => [p.repo_url, p.slug]));
  const rows = all.filter((p) => (!cat.plat || p.platform === cat.plat) && (cat.done || p.matched < 99.99)
    && (!q || `${p.name} ${p.id}`.toLowerCase().includes(q)));
  $("catNote").textContent = `${rows.length} of ${all.length} projects${cat.done ? "" : " (finished ones hidden)"}. The list and progress are decomp.dev's (updated here once a day); decomp.wtf isn't affiliated with these projects.`;
  $("catGrid").innerHTML = rows.slice(0, 240).map((p) => `<button class="cat-card" type="button" data-id="${esc(p.id)}">
      <div class="cc-top"><span class="plat">${esc(PLAT_SHORT[p.platform] || p.platform_name)}</span>${forked.has(p.repo_url) ? `<span class="badge matched">forked</span>` : p.matched >= 99.99 ? `<span class="badge">done</span>` : ""}</div>
      <h4>${esc(p.name)}</h4>
      <p class="meta">${esc(p.id)}${p.version ? " · " + esc(p.version) : ""}</p>
      <div class="meter"><i data-w="${p.matched}"></i></div>
      <p class="meta"><b>${p.matched.toFixed(1)}%</b> of code · ${p.functions ? `${p.matched_functions.toLocaleString()} / ${p.functions.toLocaleString()} functions` : "functions unknown"}${p.updated ? " · " + ago(p.updated) : ""}</p>
    </button>`).join("") || `<p class="muted">Nothing matches.</p>`;
  requestAnimationFrame(() => $("catGrid").querySelectorAll(".meter i").forEach((i) => { i.style.width = i.dataset.w + "%"; }));
  $("catGrid").querySelectorAll(".cat-card").forEach((b) => {
    b.onclick = () => {
      const p = all.find((x) => x.id === b.dataset.id);
      if (forked.has(p.repo_url)) location.hash = `#/p/${forked.get(p.repo_url)}`;
      else openForkDialog(p);
    };
  });
}
$("catSearch").addEventListener("input", () => { cat.q = $("catSearch").value; renderCatalog(); });
$("catDone").addEventListener("change", () => { cat.done = $("catDone").checked; renderCatalog(); });
$("repoFork").onclick = () => {
  const url = $("repoUrl").value.trim();
  if (!url) return;
  openForkDialog({ name: url.replace(/\/+$/, "").split("/").pop().replace(/\.git$/, ""), repo_url: url, platform: "", platform_name: "", id: url });
};

function openForkDialog(p) {
  const body = dialog(`Fork ${p.name}`, `<p>decomp.wtf clones <a class="link" href="${esc(p.repo_url)}" target="_blank" rel="noopener">${esc(p.id)}</a> to this computer and sets it up for your agents.</p>
    <dl class="facts">${[["Platform", p.platform_name || "?"], ["Version", p.version || "the project's default"], ["Progress", p.functions ? `${p.matched.toFixed(1)}% of code` : "?"]]
      .map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>
    ${p.functions ? `<p class="muted small">Progress from <a class="link" href="https://decomp.dev/${esc(p.id)}" target="_blank" rel="noopener">its page on decomp.dev</a>, where the project's history, every file and its contributors are.</p>` : ""}
    <p class="muted small">Next you add <b>your own copy of the game</b>. Its code stays on your computer; your agent's provider sees the functions it works on. Setting up runs the project's own build scripts, as building it yourself would, so fork projects you trust. Matches are committed to a local <code>decomp-wtf</code> branch: what you push upstream is up to you and the project's rules.</p>
    <div class="row"><button class="primary" id="forkGo" type="button">Fork it</button><button class="ghost" data-close type="button">Cancel</button></div>
    <pre class="joblog" id="forkLog" hidden></pre>`);
  body.querySelector("#forkGo").onclick = async () => {
    $("forkGo").disabled = true;
    $("forkLog").hidden = false;
    try {
      const { job } = await api("/api/fork", { method: "POST", body: { repo: p.repo_url, name: p.name, platform: p.platform, version: p.version } });
      const r = await followJob(job, $("forkLog"));
      $("dialog").hidden = true;
      await loadState();
      loadNav();
      location.hash = `#/p/${r.slug}`;
    } catch (e) { toast(esc(e.message), "err"); $("forkGo").disabled = false; }
  };
}

function uploadFile(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", url);
    x.setRequestHeader("X-Decomp", "1");
    x.setRequestHeader("Content-Type", "application/octet-stream");
    x.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    x.onload = () => {
      let data = null;
      try { data = JSON.parse(x.responseText); } catch { /* not json */ }
      if (x.status >= 200 && x.status < 300) resolve(data); else reject(new Error((data && data.error) || `upload failed (${x.status})`));
    };
    x.onerror = () => reject(new Error("upload failed"));
    x.send(file);
  });
}

async function renderForkSetup() {
  const box = $("forkSetup");
  box.hidden = false;
  box.innerHTML = `<p class="muted">Looking at the project…</p>`;
  let n;
  try { n = await api(`/api/fork/needs?slug=${S.slug}`); } catch (e) { box.innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
  const kinds = { manifest: "decomp.wtf manifest", dtk: "dtk-template (configure.py + ninja)", configure: "configure.py + ninja", splat: "splat + make", objdiff: "objdiff project", custom: "its own setup" };
  const facts = `<dl class="facts one">${[["Repository", (S.ov.repo_url || "").replace(/^https?:\/\//, "") || "made by decomp.wtf"], ["Layout", kinds[n.kind] || n.kind], ["Version", n.version || "—"], ["Game goes to", n.path || "see the README"]]
    .map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>
    <div class="row"><button class="ghost small-btn" id="fsFolder" type="button">Open project folder</button></div>`;
  const run = async (start) => {
    $("fsLog").hidden = false;
    box.querySelectorAll("button, input").forEach((b) => { b.disabled = true; });
    try {
      const { job } = await start();
      await followJob(job, $("fsLog"));
      toast("The project is ready for your agents.", "match");
      loadNav();
      openProject(S.slug);
    } catch (e) {
      toast(esc(e.message), "err");
      $("fsLog").textContent += `\n\n✕ ${e.message}`;  // stays visible after the toast is gone
      $("fsLog").scrollTop = $("fsLog").scrollHeight;
      box.querySelectorAll("button, input").forEach((b) => { b.disabled = false; });
      if ($("fsOwn")) $("fsGo").disabled = !$("fsOwn").checked;
    }
  };
  if (n.auto) {
    box.innerHTML = `<div class="fs-main">
        <p class="eyebrow">Last step</p>
        <h2>Add your copy of the game</h2>
        <p class="muted">${esc(n.what)}. ${esc(n.help || "")}</p>
        <label class="check"><input type="checkbox" id="fsOwn"><span>This is my own copy of this game, obtained legally (for example, dumped from my console). The results stay with me for my own use.</span></label>
        <div class="row"><input type="file" id="fsFile" hidden><button class="primary" id="fsGo" type="button" disabled>Choose the game file…</button>
          <span class="muted small">or</span><input id="fsPath" class="mono grow" placeholder="paste its path on this computer"><button class="ghost" id="fsPathGo" type="button">Use path</button></div>
        <div class="upbar" id="fsUp" hidden><i></i></div>
        <pre class="joblog tall" id="fsLog" hidden></pre>
        <p class="muted small">Then decomp.wtf runs the project's setup and build, which can download the project's own tools and take a few minutes. Put the game in place yourself? <button class="link-btn" id="fsBuild" type="button">Just build</button></p>
      </div><aside class="fs-side">${facts}</aside>`;
    $("fsOwn").onchange = () => { $("fsGo").disabled = !$("fsOwn").checked; };
    $("fsGo").onclick = () => $("fsFile").click();
    $("fsFile").onchange = () => {
      const f = $("fsFile").files[0];
      if (!f) return;
      $("fsUp").hidden = false;
      run(() => uploadFile(`/api/fork/game?slug=${encodeURIComponent(S.slug)}&confirmed=1&name=${encodeURIComponent(f.name)}`, f,
        (p) => { $("fsUp").firstElementChild.style.width = (p * 100).toFixed(1) + "%"; }));
    };
    $("fsPathGo").onclick = () => {
      if (!$("fsOwn").checked) return toast("Confirm that this is your own copy of the game first.", "err");
      if (!$("fsPath").value.trim()) return;
      run(() => api("/api/fork/setup", { method: "POST", body: { slug: S.slug, path: $("fsPath").value.trim(), confirmed: true } }));
    };
    $("fsBuild").onclick = () => run(() => api("/api/fork/setup", { method: "POST", body: { slug: S.slug } }));
  } else {
    box.innerHTML = `<div class="fs-main">
        <p class="eyebrow">Last step</p>
        <h2>Set it up the project's way</h2>
        <p class="muted">${esc(n.help)}</p>
        <ol class="steps"><li>Open a terminal in the project folder.</li><li>Follow the README: it says where your copy of the game goes and how to build.</li><li>Come back and click <b>Check again</b>.</li></ol>
        ${cmdBox(`cd "${S.ov.folder}"`)}
        ${n.readme ? `<p class="col-title">From the project's README</p><pre class="readme" id="fsReadme"></pre>` : ""}
        <div class="row"><button class="primary" id="fsCheck" type="button">Check again</button></div>
        <pre class="joblog tall" id="fsLog" hidden></pre>
      </div><aside class="fs-side">${facts}</aside>`;
    if (n.readme) $("fsReadme").textContent = n.readme;
    wireCopies(box);
    $("fsCheck").onclick = () => run(() => api("/api/fork/check", { method: "POST", body: { slug: S.slug } }));
  }
  $("fsFolder").onclick = () => openFolder();
}

/* ------------------------------------------------------------------ map */
let colors = null, geo = null, animating = false;

function cssColor(name) {
  const probe = document.createElement("i");
  probe.style.color = `var(${name})`;
  document.body.append(probe);
  const rgb = getComputedStyle(probe).color.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number);
  probe.remove();
  return rgb;
}
function readColors() {
  colors = { open: cssColor("--open"), tried: cssColor("--tried"), work: cssColor("--work"), match: cssColor("--match"), hi: cssColor("--match-hi"), ink: cssColor("--ink") };
}
const mix = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));
const rgb = (c, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;

function layoutMap() {
  const wrap = $("mapWrap"), canvas = $("map");
  if (!wrap.clientWidth) return;  // not on screen: laid out when the overview shows
  const w = Math.max(200, wrap.clientWidth - 32), n = Math.max(1, S.map.length);
  let size = Math.floor(Math.sqrt((w * 300) / n));
  size = Math.max(5, Math.min(26, size));
  const gap = size >= 12 ? 3 : 2;
  const cols = Math.max(1, Math.floor((w + gap) / (size + gap)));
  const rows = Math.ceil(n / cols);
  const h = rows * (size + gap) - gap;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  canvas.style.height = h + "px";
  geo = { size, gap, cols, w, h, dpr };
  mapChanged();
  applyClaims();
}

function applyClaims(draw = true) {
  const working = new Map(S.claims.map((c) => [c.name, c.agent]));
  let changed = false;
  for (const f of S.map) {
    const status = f.status === "matched" || f.status === "asm" ? f.status : working.has(f.name) ? "working" : "open";
    if (status !== f.status) { f.status = status; changed = true; }
    f.agent = working.get(f.name) || null;
  }
  if (changed) mapChanged();
  if (draw) drawMap();
}

/* New attempts, as the live feed brings them: each function's tries and best score, without a refetch. */
function applyAttempts(attempts) {
  for (const at of attempts) {
    const i = S.byName && S.byName.get(at.function);
    if (i == null) continue;
    const f = S.map[i];
    f.attempts = (f.attempts || 0) + 1;
    if (at.score != null && (f.best == null || at.score < f.best)) { f.best = at.score; mapChanged(); }
  }
}

function cellColor(f) {
  if (f.status === "matched") return [colors.match, 1];
  if (f.status === "asm") return [colors.tried, 0.35];
  if (f.best != null) return [mix(colors.tried, colors.hi, Math.pow(pctOf(f.best, f.insns) || 0, 3)), 1];
  return [colors.open, 1];
}

function drawBase() {
  const { size, gap, cols, dpr } = geo;
  base = base || document.createElement("canvas");
  base.width = $("map").width; base.height = $("map").height;
  const ctx = base.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const r = size >= 10 ? 2.5 : size >= 7 ? 1.5 : 1;
  moving = [];
  S.map.forEach((f, i) => {
    if (f.status === "working") { moving.push(i); return; }  // drawn every frame, pulsing
    const x = (i % cols) * (size + gap), y = Math.floor(i / cols) * (size + gap);
    const [c, a] = cellColor(f);
    ctx.fillStyle = rgb(c, a);
    ctx.beginPath(); ctx.roundRect(x, y, size, size, r); ctx.fill();
  });
  baseDirty = false;
}

function drawMap() {
  if (!geo || S.tab !== "overview") return;
  if (!colors) readColors();
  if (baseDirty || !base || base.width !== $("map").width || base.height !== $("map").height) drawBase();
  const ctx = $("map").getContext("2d");
  const { size, gap, cols, dpr } = geo;
  const now = performance.now();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, $("map").width, $("map").height);
  ctx.drawImage(base, 0, 0);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const r = size >= 10 ? 2.5 : size >= 7 ? 1.5 : 1;
  const at = (i) => [(i % cols) * (size + gap), Math.floor(i / cols) * (size + gap)];
  let live = moving.length > 0;
  for (const i of moving) {
    const [x, y] = at(i);
    ctx.fillStyle = rgb(colors.work, 0.55 + 0.45 * Math.sin(now / 260 + i));
    ctx.beginPath(); ctx.roundRect(x, y, size, size, r); ctx.fill();
  }
  for (const [name, fl] of S.flash) {
    const i = S.byName && S.byName.get(name), t = (now - fl) / 1600;
    if (i == null || t >= 1) { S.flash.delete(name); continue; }
    live = true;
    const [x, y] = at(i), g = size * (0.6 + 1.8 * t);
    ctx.strokeStyle = rgb(colors.match, 1 - t);
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.roundRect(x - g / 2, y - g / 2, size + g, size + g, r + 2); ctx.stroke();
  }
  const sel = S.fn && S.byName ? S.byName.get(S.fn) : null;
  if (sel != null) { const [x, y] = at(sel); ctx.strokeStyle = rgb(colors.ink, 0.9); ctx.lineWidth = 1.5; ctx.strokeRect(x - 1.5, y - 1.5, size + 3, size + 3); }
  if (live && !animating) {  // about 30 frames a second is plenty for a pulse
    animating = true;
    setTimeout(() => requestAnimationFrame(() => { animating = false; drawMap(); }), 33);
  }
}

function mapIndexAt(ev) {
  if (!geo) return -1;
  const rect = $("map").getBoundingClientRect();
  const x = ev.clientX - rect.left, y = ev.clientY - rect.top;
  const col = Math.floor(x / (geo.size + geo.gap)), row = Math.floor(y / (geo.size + geo.gap));
  if (col < 0 || col >= geo.cols || x % (geo.size + geo.gap) > geo.size || y % (geo.size + geo.gap) > geo.size) return -1;
  const i = row * geo.cols + col;
  return i < S.map.length ? i : -1;
}
$("map").addEventListener("mousemove", (ev) => {
  const i = mapIndexAt(ev), tip = $("tip");
  if (i < 0) { tip.hidden = true; return; }
  const f = S.map[i];
  const p = pctOf(f.best, f.insns);
  const status = f.status === "matched" ? `matched${f.matched_by ? " by " + esc(f.matched_by) : ""}` : f.status === "working" ? `${esc(f.agent)} is on it` : f.best != null ? `best ${pctText(p)}` : "not started";
  const where = S.ov && S.ov.kind === "fork" ? esc(f.unit || "") : hex8(f.vram);
  tip.innerHTML = `<b>${esc(f.name)}</b><br>${where} · ${f.insns} instructions<br>${status}`;
  tip.hidden = false;
  const wrap = $("mapWrap").getBoundingClientRect();
  let left = ev.clientX - wrap.left + 14, top = ev.clientY - wrap.top + 14;
  if (left + 240 > wrap.width) left -= 260;
  tip.style.left = left + "px"; tip.style.top = top + "px";
});
$("map").addEventListener("mouseleave", () => { $("tip").hidden = true; });
$("map").addEventListener("click", (ev) => { const i = mapIndexAt(ev); if (i >= 0) openFn(S.map[i].name); });
let resizeT = null;
window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { if (S.map.length && S.tab === "overview") layoutMap(); }, 120); });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { colors = null; baseDirty = true; drawMap(); });

/* ------------------------------------------------------------------ now working */
function spark(points, w = 240, h = 34) {
  const vals = points.filter((p) => p.score != null);
  if (!vals.length) return `<svg viewBox="0 0 ${w} ${h}"></svg>`;
  const max = Math.max(...vals.map((p) => p.score), 1);
  const step = vals.length > 1 ? w / (vals.length - 1) : 0;
  const pts = vals.map((p, i) => [i * step, 3 + (h - 6) * (p.score / max)]);
  const d = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
  const last = pts[pts.length - 1];
  const done = vals[vals.length - 1].score === 0;
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><path d="${d}" fill="none" stroke="var(${done ? "--match" : "--work"})" stroke-width="1.6" vector-effect="non-scaling-stroke"/>
    <circle cx="${last[0]}" cy="${last[1]}" r="2.6" fill="var(${done ? "--match" : "--work"})"/></svg>`;
}

/* No compile yet: the agent is reading (the function, its file, look-alikes), for this long so far */
function reading(c) {
  const min = c.claimed_at ? Math.floor((Date.now() / 1000 - c.claimed_at) / 60) : 0;
  return min >= 1 ? `reading · ${min} min` : "reading";
}

function renderNow(claims) {
  const box = $("now");
  claims = claims.filter((c) => c.agent !== AUTO);
  if (!claims.length) { box.innerHTML = ""; return; }
  box.innerHTML = claims.map((c) => {
    const sc = S.scores.get(c.name) || [];
    const p = pctOf(c.best_score, c.n_insns);
    const [sym, unit] = c.name.split(/@(.*)/s);  // a fork's symbol@file: the symbol first, the file after it
    return `<div class="job" data-fn="${esc(c.name)}"><div class="job-top"><span class="job-name" title="${esc(c.name)}">${esc(sym)}${unit ? `<small>@${esc(unit)}</small>` : ""}</span><span class="job-who" title="${esc(c.agent)}">${esc(c.agent)}</span></div>
      ${spark(sc)}<div class="job-foot"><span>${c.n_insns} insns · ${c.attempts} tries</span><b>${p == null ? reading(c) : pctText(p)}</b></div></div>`;
  }).join("");
  box.querySelectorAll(".job").forEach((j) => { j.onclick = () => openFn(j.dataset.fn); });
}

/* ------------------------------------------------------------------ agents on this project */
const STATE_TEXT = { starting: "starting…", working: "working", waiting: "next session in a moment", stopped: "stopped", error: "stopped with an error" };

function renderSessions(sessions) {
  const box = $("sessions");
  const board = new Map((S.stats?.agents || []).map((r) => [r.agent, r]));
  const running = sessions.filter((s) => ["starting", "working", "waiting"].includes(s.state));
  $("stopAll").hidden = running.length < 2;
  const mine = sessions.map((s) => {
    const claim = S.claims.find((c) => c.agent === s.agent);
    const dot = s.state === "error" ? "err" : s.state === "working" ? "on" : s.state === "stopped" ? "" : "work";
    const live = ["starting", "working", "waiting"].includes(s.state);
    const b = board.get(s.agent);
    const facts = [s.model ? esc(s.model) : "default model", s.runs ? `session ${s.runs}` : "", b && b.matched ? `<b class="ok">${b.matched} matched</b>` : "", b ? plural(b.compiles, "compile") : "",
      s.usd ? money(s.usd) : s.tokens ? `${(s.tokens / 1000).toFixed(0)}k tokens` : "", s.started_at ? `since ${clock(s.started_at).slice(0, 5)}` : ""].filter(Boolean);
    return `<div class="session"><div class="session-top"><span class="dot ${dot}"></span><b>${esc(s.label)}</b>
        <span class="state ${s.state === "error" ? "err" : ""}">${esc(STATE_TEXT[s.state] || s.state)}</span>
        <button class="ghost" data-stop="${esc(s.id)}" data-remove="${live ? "" : "1"}" type="button">${live ? "Stop" : "Remove"}</button></div>
      <span class="facts-line">${facts.join(" · ")}</span>
      ${s.state === "error" ? `<span class="state err">${esc(s.error)}</span>` : ""}
      ${claim ? `<span class="last">on ${fnLink(claim.name)}</span>` : ""}</div>`;
  }).join("");
  const own = new Set(sessions.map((s) => s.agent));
  const outside = [...new Set(S.claims.filter((c) => !own.has(c.agent) && c.agent !== AUTO).map((c) => c.agent))];
  const ext = outside.map((a) => `<div class="session ext"><div class="session-top"><span class="dot on"></span><b>${esc(a)}</b><span class="tag">${a.startsWith("@") ? "in the room" : "connected agent"}</span></div>
      <span class="last">on ${S.claims.filter((c) => c.agent === a).map((c) => fnLink(c.name)).join(", ")}</span></div>`).join("");
  $("noSessions").hidden = sessions.length + outside.length > 0;
  // Same content as last time: leave the DOM alone. The poll runs every second or two, and replacing a button
  // between a mouse press and its release drops the click, which made Stop impossible to hit while an agent worked.
  if (box.dataset.sig === mine + ext) return;
  box.dataset.sig = mine + ext;
  box.innerHTML = mine + ext;
  box.querySelectorAll(".fn").forEach((x) => { x.onclick = () => openFn(x.dataset.fn); });
  box.querySelectorAll("[data-stop]").forEach((b) => {
    b.onclick = async () => {
      await api("/api/agents/stop", { method: "POST", body: { id: b.dataset.stop, remove: !!b.dataset.remove } }).catch((e) => toast(esc(e.message), "err"));
      poll(false);
    };
  });
}
$("stopAll").onclick = async () => {
  await api("/api/agents/stop", { method: "POST", body: { slug: S.slug } }).catch((e) => toast(esc(e.message), "err"));
  toast("Stopping every agent on this project.");
  poll(false);
};

/* ------------------------------------------------------------------ the launcher: agent, model, how many */
const LZ = { kind: null, model: "", count: 1, per: null };
function launcherInit() {
  if (LZ.kind || !S.st) return;
  const l = S.st.settings.launcher || {};
  const ready = S.st.agents.filter((a) => a.installed && a.id !== "api");
  const known = S.st.agents.find((a) => a.id === l.kind && (a.installed || a.id === "api"));
  LZ.kind = known ? known.id : (ready[0] ? ready[0].id : "api");
  LZ.model = known ? (l.model || "") : "";
  LZ.count = l.count || 1;
  const eps = S.st.settings.endpoints || [];
  LZ.eps = (l.endpoints || []).filter((id) => eps.some((e) => e.id === id));
  if (!LZ.eps.length && eps.length) LZ.eps = [eps[0].id];
}
function modelOptions(a) {
  const recent = (S.st.settings.recent_models || {})[a.id] || [];
  const opts = [{ id: "", name: a.default_model ? `Default · ${a.default_model}` : "Default", note: "" }];
  for (const m of a.models || []) opts.push(m);
  for (const r of recent) if (!opts.some((o) => o.id === r)) opts.push({ id: r, name: r, note: "used before" });
  return opts;
}
function modelName(a, model) {
  if (!model) return "";
  const m = (a.models || []).find((x) => x.id === model);
  return m ? m.name : model;
}
function launchLabel(a, model, count) {
  if (a.id === "api") {
    const picked = (S.st.settings.endpoints || []).filter((e) => (LZ.eps || []).includes(e.id));
    if (picked.length > 1) return `Start ${count} across ${picked.length} endpoints`;
    return `Start ${picked[0] ? picked[0].name : "API model"}${count > 1 ? ` × ${count}` : ""}`;
  }
  return `Start ${a.name}${model ? ` · ${modelName(a, model)}` : ""}${count > 1 ? ` × ${count}` : ""}`;
}

async function startAgents(a, model, count, btn, extra = {}) {
  if (btn) btn.disabled = true;
  try {
    const options = { ...extra };
    if (model && a.id !== "api") options.model = model;
    if (LZ.per) options.per_session = LZ.per;
    await api("/api/agents/start", { method: "POST", body: { kind: a.id, slug: S.slug, count, options } });
    S.st.settings.launcher = { kind: a.id, model: model || "", count };
    if (model && a.id !== "api") {
      const rec = (S.st.settings.recent_models ||= {});
      rec[a.id] = [model, ...(rec[a.id] || []).filter((m) => m !== model)].slice(0, 8);
    }
    hideLaunch();
    toast(`${esc(a.id === "api" ? "Your API model" : a.name)} ${count > 1 ? `× ${count} are` : "is"} starting. Watch it on the map and in Activity.`, "match");
    poll(false);
    loadNav();
  } catch (e) { toast(esc(e.message), "err"); } finally { if (btn) btn.disabled = false; }
}

function renderLauncher(box, compact = false) {
  if (!box || !S.st) return;
  launcherInit();
  const list = S.st.agents;
  const a = list.find((x) => x.id === LZ.kind) || list[0];
  const kinds = list.map((x) => `<button type="button" class="lz-kind ${x.id === a.id ? "on" : ""}" data-k="${esc(x.id)}">
      <b>${esc(x.name)}</b><span>${x.id === "api" ? "DeepSeek, OpenRouter, Ollama…" : x.installed ? esc(x.version ? x.version.replace(/\s*\(.*\)/, "") : x.vendor) : "not installed"}</span>
      ${x.installed || x.id === "api" ? "" : `<em>install</em>`}</button>`).join("");
  let body = "";
  if (!a.installed) {
    body = `<p class="small"><b>${esc(a.name)}</b> isn't installed on this computer.${a.needs ? ` It needs ${esc(a.needs)} first.` : ""}</p>
      ${a.install ? cmdBox(a.install) : ""}
      <div class="row"><button class="primary" data-act="install" type="button" ${a.needs ? "disabled" : ""}>Install it for me</button><a class="link small" href="${esc(a.docs)}" target="_blank" rel="noopener">About ${esc(a.name)}</a></div>
      <p class="muted small">${esc(a.signin)}</p><pre class="joblog" data-log hidden></pre>`;
  } else if (a.id === "api") {
    const eps = S.st.settings.endpoints || [];
    LZ.eps = (LZ.eps || []).filter((id) => eps.some((e) => e.id === id));
    if (!LZ.eps.length && eps.length) LZ.eps = [eps[0].id];
    body = eps.length && !LZ.adding ? `<div class="ep-pick">${eps.map((e) => `<label class="check inline"><input type="checkbox" data-ep="${esc(e.id)}" ${LZ.eps.includes(e.id) ? "checked" : ""}>
        <span><b>${esc(e.name)}</b> <span class="mono small">${esc(e.model)}</span><br><span class="muted small mono">${esc(hostOf(e.base_url))}${e.max_sessions ? ` · at most ${e.max_sessions} at once` : ""}</span></span></label>`).join("")}</div>
      <p class="muted small">${eps.length > 1 ? "Tick several to spread the sessions across them. " : ""}<a class="link" href="#/agents">Manage endpoints</a> · <button type="button" class="link-btn" data-act="add-ep">Add one</button></p>`
      : `<div class="lz-fields">${endpointFields({ provider: "deepseek" }, compact)}</div>${eps.length ? `<p class="muted small"><button type="button" class="link-btn" data-act="pick-ep">Back to your endpoints</button></p>` : ""}`;
  } else {
    const opts = modelOptions(a);
    const known = opts.some((o) => o.id === LZ.model);
    body = `<label class="field"><span>Model</span><select data-f="model">${opts.map((o) => `<option value="${esc(o.id)}" ${known && o.id === LZ.model ? "selected" : ""}>${esc(o.name)}${o.note ? ` — ${esc(o.note)}` : ""}</option>`).join("")}
        <option value="__other" ${known ? "" : "selected"}>Another model…</option></select></label>
      <input data-f="custom" class="mono lz-custom" placeholder="model name, as ${esc(a.name)} calls it" value="${known ? "" : esc(LZ.model)}" ${known ? "hidden" : ""}>`;
  }
  const ready = a.installed;
  box.innerHTML = `<div class="lz-kinds">${kinds}</div>
    <div class="lz-body">${body}
      ${ready ? `<div class="lz-row">
        <div class="field"><span>How many</span><span class="stepper"><button type="button" data-step="-1">−</button><output>${LZ.count}</output><button type="button" data-step="1">+</button></span></div>
        ${compact ? "" : `<label class="field"><span>Functions per session</span><input data-f="per" type="number" min="1" max="20" value="${LZ.per || S.st.settings.per_session || 2}"></label>`}
        <span class="grow"></span>
        <button class="primary" data-act="start" type="button">${esc(launchLabel(a, LZ.model, LZ.count))}</button></div>
        <p class="muted small">${a.id === "api" ? "A built-in agent loop drives the model through the decomp tools." : esc(a.signin)} Each agent takes a different function; a fresh session every few functions keeps its context small.</p>` : ""}
    </div>`;
  wireCopies(box);
  const rerender = () => { renderLauncher(box, compact); if (box !== $("launcher") && S.tab === "agents") renderLauncher($("launcher")); };
  box.querySelectorAll(".lz-kind").forEach((b) => { b.onclick = () => { LZ.kind = b.dataset.k; LZ.model = (S.st.settings.launcher || {}).kind === LZ.kind ? S.st.settings.launcher.model || "" : ""; rerender(); }; });
  const sel = box.querySelector('select[data-f="model"]'), custom = box.querySelector('[data-f="custom"]');
  const label = () => { const btn = box.querySelector('[data-act="start"]'); if (btn) btn.textContent = launchLabel(a, LZ.model, LZ.count); };
  if (sel) sel.onchange = () => { if (sel.value === "__other") { custom.hidden = false; custom.focus(); LZ.model = custom.value.trim(); } else { custom.hidden = true; LZ.model = sel.value; } label(); };
  if (custom) custom.oninput = () => { LZ.model = custom.value.trim(); label(); };
  box.querySelectorAll("[data-step]").forEach((b) => { b.onclick = () => { LZ.count = Math.max(1, Math.min(8, LZ.count + Number(b.dataset.step))); box.querySelector(".stepper output").textContent = LZ.count; label(); }; });
  const per = box.querySelector('[data-f="per"]');
  if (per) per.onchange = () => { LZ.per = Math.max(1, Math.min(20, Number(per.value) || 4)); };
  wireEndpointFields(box);
  box.querySelectorAll("[data-ep]").forEach((c) => { c.onchange = () => { LZ.eps = [...box.querySelectorAll("[data-ep]:checked")].map((x) => x.dataset.ep); label(); }; });
  const addEp = box.querySelector('[data-act="add-ep"]'), pickEp = box.querySelector('[data-act="pick-ep"]');
  if (addEp) addEp.onclick = () => { LZ.adding = true; rerender(); };
  if (pickEp) pickEp.onclick = () => { LZ.adding = false; rerender(); };
  const inst = box.querySelector('[data-act="install"]');
  if (inst) inst.onclick = async () => {
    const log = box.querySelector("[data-log]");
    log.hidden = false; inst.disabled = true;
    try {
      const { job } = await api("/api/agents/install", { method: "POST", body: { kind: a.id } });
      await followJob(job, log);
      toast(`${esc(a.name)} installed. ${esc(a.signin)}`, "match");
      await loadState();
      rerender();
    } catch (e) { toast(esc(e.message), "err"); inst.disabled = false; }
  };
  const go = box.querySelector('[data-act="start"]');
  if (go) go.onclick = async () => {
    if (a.id === "api") {
      if (box.querySelector('[data-f="prov"]')) {  // a new endpoint, typed in here: saved, then started on
        const before = new Set((S.st.settings.endpoints || []).map((e) => e.id));
        try { S.st.settings = await api("/api/settings", { method: "POST", body: { endpoint: endpointData(box) } }); } catch (e) { return toast(esc(e.message), "err"); }
        const added = (S.st.settings.endpoints || []).find((e) => !before.has(e.id));
        LZ.eps = added ? [added.id] : LZ.eps;
        LZ.adding = false;
      }
      if (!(LZ.eps || []).length) return toast("Tick an endpoint to start on.", "err");
      return startAgents(a, "", LZ.count, go, { endpoints: LZ.eps });
    }
    startAgents(a, LZ.model, LZ.count, go);
  };
}

/* API endpoints: OpenAI-compatible servers (DeepSeek, a llama.cpp box on the network...), each with its own address,
   model and key. The same fields in the launcher and on the Agents & models page. */
function hostOf(url) { return String(url || "").replace(/^\w+:\/\//, "").split("/")[0]; }
function endpointFields(e, compact = false) {
  const presets = S.st.api_presets, prov = presets[e.provider] ? e.provider : "custom";
  return `<label class="field"><span>Provider</span><select data-f="prov">${Object.entries(presets).map(([k, p]) => `<option value="${k}" ${k === prov ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
    <label class="field"><span>Model</span><input data-f="model" class="mono" value="${esc(e.model || presets[prov].model)}"></label>
    <label class="field wide"><span>API address</span><input data-f="base" class="mono" value="${esc(e.base_url || presets[prov].base_url)}" placeholder="http://gpu-box:8080/v1"></label>
    <label class="field wide"><span>API key (stored only on this computer, sent only to this address)</span><input data-f="key" type="password" placeholder="${e.has_key ? esc(e.api_key) : "paste your key (none for local models)"}" autocomplete="off"></label>
    ${compact ? "" : `<label class="field"><span>Name</span><input data-f="name" value="${esc(e.id ? e.name : "")}" placeholder="${esc(e.id ? e.name : "e.g. gpu-box")}"></label>
    <label class="field"><span>Context size (tokens)</span><input data-f="ctx" class="mono" inputmode="numeric" placeholder="automatic" value="${esc(String(e.context_tokens || ""))}"></label>
    <label class="field"><span>Sessions at once</span><input data-f="max" class="mono" inputmode="numeric" placeholder="no limit" value="${esc(String(e.max_sessions || ""))}"></label>`}`;
}
function wireEndpointFields(box) {
  box.querySelectorAll('[data-f="prov"]').forEach((sel) => {
    sel.onchange = () => {
      const p = S.st.api_presets[sel.value], scope = sel.closest(".ep-card, .lz-fields") || box;
      scope.querySelector('[data-f="model"]').value = p.model;
      scope.querySelector('[data-f="base"]').value = p.base_url;
    };
  });
}
function endpointData(scope, id = "") {
  const f = (k) => scope.querySelector(`[data-f="${k}"]`);
  const out = { provider: f("prov").value, base_url: f("base").value, model: f("model").value, api_key: f("key").value };
  if (id) out.id = id;
  if (f("name")) Object.assign(out, { name: f("name").value, context_tokens: f("ctx").value, max_sessions: f("max").value });
  return out;
}

function showLaunch() {
  if (!S.st || !S.slug) return;
  $("launchPop").hidden = false;
  renderLauncher($("launchPopBody"), true);
}
function hideLaunch() { $("launchPop").hidden = true; }
$("phStart").onclick = (e) => { e.stopPropagation(); if ($("launchPop").hidden) showLaunch(); else hideLaunch(); };
// (the path as clicked: choosing an agent re-renders the popover, so the clicked tile isn't in the page any more)
document.addEventListener("click", (e) => {
  if (!$("launchPop").hidden && !e.composedPath().some((el) => el.classList && el.classList.contains("pop-wrap"))) hideLaunch();
});

/* ------------------------------------------------------------------ feeds */
function fnLink(name) { return `<span class="fn" data-fn="${esc(name)}">${esc(name)}</span>`; }
function eventHtml(ev) {
  const who = `<span class="who">${esc(ev.agent || "")}</span>`;
  const d = ev.detail || "";
  switch (ev.kind) {
    case "claim": return `${who} started ${fnLink(d)}`;
    case "match": return `<span class="ok">✓</span> ${who} matched ${fnLink(d)}`;
    case "link": return `<span class="ok">🔗</span> ${esc(d)} is linked: the game builds from its C, byte for byte`;
    case "release": return `${who} set ${fnLink(d)} aside, with notes for the next agent`;
    case "expired": return `${fnLink(d)} was left unfinished`;
    case "context": return `${who} updated ${ev.agent ? "shared code" : "context"}: ${esc(d)}`;
    case "rename": return `${who} renamed ${esc(d)}`;
    case "split": return `${who} split ${esc(d)}`;
    case "files": return `${who ? who + " " : ""}gave code without a source file one: ${esc(d)}`;
    case "import": return `ROM analysed: ${esc(d)}`;
    case "rescan": return `GBA function map rescanned: ${esc(d)}`;
    case "compiler": return `compiler set to ${esc(d)}`;
    case "collab": return esc(d);
    default: return `${who} ${esc(ev.kind)} ${esc(d)}`;
  }
}
/* One line per agent and function, updated in place: "claude-1 · lerp · 4 compiles · best 87%" until it's matched. */
const groups = new Map();
function groupHtml(g) {
  const who = `<span class="who">${esc(g.agent || "")}</span>`;
  const p = pctOf(g.best, g.insns);
  if (g.matched) return `<span class="ok">✓</span> ${who} matched ${fnLink(g.fn)}${g.tries ? ` <span class="score">after ${plural(g.tries, "compile")}</span>` : ""}`;
  if (g.released) return `${who} set ${fnLink(g.fn)} aside, with notes for the next agent${g.tries ? ` <span class="score">${plural(g.tries, "compile")}${p != null ? ` · best ${pctText(p)}` : ""}</span>` : ""}`;
  if (!g.tries) return `${who} started ${fnLink(g.fn)}`;
  const last = g.lastScore === undefined ? "" : g.lastScore === null ? ` · <span class="bad">last one didn't compile</span>` : "";
  return `${who} working on ${fnLink(g.fn)} <span class="score">${plural(g.tries, "compile")}${p != null ? ` · best ${pctText(p)}` : ""}</span>${last}`;
}
function addLine(t, html, first) {
  const list = $("activity");
  const li = document.createElement("li");
  if (!first) li.className = "fresh";
  li.innerHTML = `<time>${clock(t)}</time><span>${html}</span>`;
  list.querySelector(".placeholder")?.remove();
  list.prepend(li);
  return li;
}
function touchGroup(agent, fn, t, change, first) {
  const key = agent + "\u0000" + fn;
  let g = groups.get(key);
  if (!g) { g = { agent, fn, tries: 0, best: null, insns: 1, li: null }; groups.set(key, g); }
  change(g);
  if (g.li && g.li.isConnected) {
    g.li.querySelector("time").textContent = clock(t);
    g.li.querySelector("span").innerHTML = groupHtml(g);
    if (g.li !== $("activity").firstElementChild) $("activity").prepend(g.li);
    if (!first) { g.li.classList.remove("fresh"); void g.li.offsetWidth; g.li.classList.add("fresh"); }
  } else g.li = addLine(t, groupHtml(g), first);
  if (g.matched || g.released) groups.delete(key);
}

const AUTO = "autopilot";
function autopilotLine(t, first, done) {
  const a = S.apFeed || (S.apFeed = { tried: 0, matched: 0, li: null });
  const html = `<span class="who">autopilot</span> ${done ? esc(done) : `tried ${plural(a.tried, "machine draft")}${a.matched ? ` · <span class="ok">${a.matched} matched</span>` : ""}`}`;
  if (a.li && a.li.isConnected) { a.li.querySelector("time").textContent = clock(t); a.li.querySelector("span").innerHTML = html; }
  else a.li = addLine(t, html, first);
}
function addActivity(events, attempts, first) {
  const items = [...events.map((e) => ({ t: e.created_at, ev: e })), ...attempts.map((a) => ({ t: a.created_at, at: a }))];
  items.sort((a, b) => a.t - b.t);
  for (const it of items) {
    const auto = (who) => who === AUTO || who === "@" + AUTO;  // ours, or the room host's
    if ((it.at && auto(it.at.agent)) || (it.ev && auto(it.ev.agent) && ["match", "claim", "autopilot"].includes(it.ev.kind))) {
      const a = S.apFeed || (S.apFeed = { tried: 0, matched: 0, li: null });
      if (it.at) a.tried++;
      if (it.ev && it.ev.kind === "match") a.matched++;
      autopilotLine(it.t, first, it.ev && it.ev.kind === "autopilot" ? it.ev.detail : null);
      continue;
    }
    if (it.at) {
      const at = it.at;
      touchGroup(at.agent, at.function, it.t, (g) => {
        g.tries++; g.insns = at.n_insns; g.lastScore = at.score;
        if (at.score != null) g.best = g.best == null ? at.score : Math.min(g.best, at.score);
      }, first);
    } else if (it.ev.kind === "claim") touchGroup(it.ev.agent, it.ev.detail, it.t, () => {}, first);
    else if (it.ev.kind === "match") touchGroup(it.ev.agent, it.ev.detail, it.t, (g) => { g.matched = true; }, first);
    else if (it.ev.kind === "release") touchGroup(it.ev.agent, it.ev.detail, it.t, (g) => { g.released = true; }, first);
    else addLine(it.t, eventHtml(it.ev), first);
  }
  const list = $("activity");
  while (list.children.length > 300) list.lastChild.remove();
  list.querySelectorAll(".fn").forEach((x) => { x.onclick = () => openFn(x.dataset.fn); });
  if (!list.children.length) list.innerHTML = `<li class="placeholder"><time></time><span class="muted">Nothing yet. Start an agent and its work shows up here.</span></li>`;
}

/* Agents write Markdown. Escape everything first, then allow only **bold** and `code`. */
function miniMarkdown(text) {
  return esc(text)
    .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/^#{1,4} (.*)$/gm, "<b>$1</b>")
    .replace(/^- /gm, "• ");
}

function addThoughts(sessions, first) {
  const list = $("thoughts");
  const items = [];
  for (const s of sessions) for (const f of s.feed) items.push({ ...f, label: s.label });
  items.sort((a, b) => a.t - b.t);
  for (const it of items) {
    const li = document.createElement("li");
    if (!first) li.className = "fresh";
    const time = document.createElement("time"); time.textContent = clock(it.t);
    const src = document.createElement("span"); src.className = "src"; src.textContent = it.label;
    const body = document.createElement("span"); body.className = "t-" + it.type;
    if (it.type === "text") body.innerHTML = miniMarkdown(it.text.slice(0, 1600));
    else body.textContent = it.type === "think" ? it.text.slice(0, 1200) : it.text;
    li.append(time, src, body);
    list.prepend(li);
  }
  while (list.children.length > 400) list.lastChild.remove();
  if (!list.children.length) list.innerHTML = `<li class="placeholder"><time></time><span class="muted">Helper agents you start from decomp.wtf think out loud here.</span></li>`;
  else list.querySelector(".placeholder")?.remove();
}

/* ------------------------------------------------------------------ inspector */
function highlightAsm(text) {
  return text.split("\n").map((line) => {
    if (/^\s*(glabel|endlabel)/.test(line)) return `<span class="cm">${esc(line)}</span>`;
    if (/^\.L[0-9A-F]+:/.test(line)) return `<span class="lbl">${esc(line)}</span>`;
    const m = line.match(/^(\/\* [^*]+ \*\/)(\s+)(\S+)(.*)$/);
    if (!m) return esc(line);
    const addr = m[1].split(" ")[2] || "";
    return `<span class="addr">${esc(addr)}</span>  <span class="op">${esc(m[3].padEnd(8))}</span> ${esc(m[4].trimStart()).replace(/(\.L[0-9A-F]{8})/g, '<span class="lbl">$1</span>')}`;
  }).join("\n");
}
function highlightC(text) {
  const kw = /\b(void|int|char|short|long|float|double|unsigned|signed|struct|union|enum|typedef|static|extern|const|volatile|return|if|else|for|while|do|switch|case|default|break|continue|goto|sizeof|s8|u8|s16|u16|s32|u32|s64|u64|f32|f64)\b/g;
  return esc(text).split("\n").map((line) => {
    if (/^\s*(\/\*|\*|\/\/)/.test(line)) return `<span class="cm">${line}</span>`;
    return line.replace(kw, '<span class="kw">$1</span>').replace(/\b(0x[0-9A-Fa-f]+|\d+)\b/g, '<span class="num">$1</span>');
  }).join("\n");
}

async function openFn(name) {
  const again = S.fn === name && !$("drawer").hidden;
  if (!again) { giveBack(name); S.editDirty = false; $("tryMsg").hidden = true; }
  $("tryBtn").disabled = false; $("noteAdd").disabled = false;
  S.fn = name;
  $("drawer").hidden = false;
  document.body.classList.add("locked");
  if (!again) { S.ftab = null; S.which = "latest"; S.diffKey = null; $("diff").innerHTML = ""; $("diffSum").textContent = ""; }
  drawMap();
  document.querySelectorAll("#fnRows tr").forEach((tr) => tr.classList.toggle("sel", tr.dataset.fn === name));
  await refreshFn(true);
}

function fnBadge(d = S.fnDetail) {
  if (!d) return;
  const working = S.claims.find((c) => c.name === d.name);
  const st = $("fnStatus");
  st.className = "badge " + (d.status === "matched" ? "matched" : working ? "working" : "");
  st.textContent = d.status === "matched" ? "matched" : working ? `${working.agent} working` : d.best != null ? `best ${pctText(pctOf(d.best, d.insns))}` : "not started";
}

function showFnTab(tab) {
  document.querySelectorAll("[data-ftab]").forEach((b) => b.classList.toggle("on", b.dataset.ftab === tab));
  $("fnDiffView").hidden = tab !== "diff";
  $("fnCodeView").hidden = tab !== "code";
  if (tab === "diff") loadDiff();
}
document.querySelectorAll("[data-ftab]").forEach((b) => { b.onclick = () => { S.ftab = b.dataset.ftab; showFnTab(S.ftab); }; });
document.querySelectorAll("#diffWhich [data-which]").forEach((b) => {
  b.onclick = () => {
    S.which = b.dataset.which;
    document.querySelectorAll("#diffWhich [data-which]").forEach((x) => x.classList.toggle("on", x === b));
    loadDiff();
  };
});

let fnBusy = false;
async function refreshFn(first) {
  if (fnBusy || !S.fn) return;
  fnBusy = true;
  try {
    const d = await api(`/api/function?slug=${S.slug}&name=${encodeURIComponent(S.fn)}`);
    if (d.name !== S.fn) return;
    S.fnDetail = d;
    $("fnAddr").textContent = S.ov.kind === "fork"
      ? `${d.unit} · ${d.insns} instructions${d.match_percent != null ? ` · ${d.match_percent >= 100 ? "100" : (Math.floor(d.match_percent * 10) / 10).toFixed(1)}% now` : ""}`
      : `${hex8(d.vram)} · ROM 0x${d.rom_offset.toString(16).toUpperCase()} · ${d.insns} instructions`;
    if (d.compiler) $("fnAddr").textContent += ` · ${d.compiler.profile || "default"}: ${d.compiler.toolchain} ${d.compiler.flags.join(" ")}`;
    $("fnName").textContent = d.name;
    fnBadge(d);
    const pinned = (S.ov.priority || []).includes(d.name);
    $("fnPin").hidden = S.ov.kind === "fork" || d.status !== "open";
    $("fnPin").classList.toggle("on", pinned);
    $("fnPin").textContent = pinned ? "★ Next (on)" : "★ Next";
    $("fnPin").onclick = () => togglePrio(d.name, !pinned).then(() => refreshFn(false));
    $("noteAdd").onclick = async () => {
      const text = $("noteText").value.trim();
      if (!text) return;
      try {
        await api("/api/note", { method: "POST", body: { slug: S.slug, function: d.name, text } });
        $("noteText").value = "";
        toast("Note saved: the next agent on this function reads it.");
        refreshFn(false);
      } catch (e) { toast(esc(e.message), "err"); }
    };
    $("fnAsm").innerHTML = highlightAsm(d.asm);
    const kinds = { matched: "Matching C", best: "Best attempt so far", latest: S.ov.kind === "fork" ? "In the source now" : "Latest attempt", draft: "Machine draft (m2c)" };
    $("fnSrcKind").textContent = kinds[d.source_kind] || "No C yet";
    if (!S.editDirty || first) setEditor(d.source || "", d);
    $("copySrc").onclick = () => navigator.clipboard.writeText($("fnEdit").value).then(() => toast("Copied."));
    renderFnChart(d);
    $("diffWhich").hidden = d.status === "matched" || S.ov.kind === "fork" || d.scores.filter((x) => x.score != null).length < 2;
    const xr = [];
    if (d.calls.length) xr.push(`<div><b>calls</b>${d.calls.map(fnLink).join("")}</div>`);
    if (d.called_by.length) xr.push(`<div><b>called by</b>${d.called_by.map(fnLink).join("")}</div>`);
    $("fnXref").innerHTML = xr.join("");
    $("fnXref").querySelectorAll(".fn").forEach((x) => { x.onclick = () => openFn(x.dataset.fn.split("+")[0]); });
    $("fnNotes").innerHTML = d.notes.length ? `<p class="col-title">Notes from agents</p>` + d.notes.map((n) => `<div class="note"><small>${esc(n.agent)} · ${clock(n.created_at)}</small>${esc(n.text)}</div>`).join("") : "";
    const tab = S.ftab || (d.status === "matched" || !d.scores.length ? "code" : "diff");
    showFnTab(tab);
  } catch (e) {
    if (first) toast(esc(e.message), "err");
  } finally { fnBusy = false; }
}

/* Editing a function yourself: a highlighted editor over a textarea, judged like an agent's attempt. */
function setEditor(source, d) {
  const ta = $("fnEdit");
  ta.value = source || (d && d.status !== "matched" ? `/* write ${d.name} here */\n` : "");
  paintEditor();
  S.editDirty = false;
}
function paintEditor() {
  const ta = $("fnEdit");
  $("fnSrc").innerHTML = highlightC(ta.value) + "\n";
  $("fnSrc").scrollTop = ta.scrollTop; $("fnSrc").scrollLeft = ta.scrollLeft;
}
$("fnEdit").addEventListener("input", () => { S.editDirty = true; paintEditor(); });
$("fnEdit").addEventListener("scroll", () => { $("fnSrc").scrollTop = $("fnEdit").scrollTop; $("fnSrc").scrollLeft = $("fnEdit").scrollLeft; });
$("fnEdit").addEventListener("keydown", (e) => {
  const ta = e.target;
  if (e.key === "Tab" && !e.shiftKey) {
    e.preventDefault();
    const { selectionStart: a, selectionEnd: b } = ta;
    ta.setRangeText("    ", a, b, "end");
    S.editDirty = true; paintEditor();
  } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    tryIt();
  }
});
$("tryBtn").onclick = () => tryIt();

async function tryIt() {
  const name = S.fn, source = $("fnEdit").value, msg = $("tryMsg");
  if (!name || !source.trim() || $("tryBtn").disabled) return;
  $("tryBtn").disabled = true;
  msg.hidden = false; msg.className = "try-msg"; msg.textContent = "Compiling…";
  try {
    const r = await api("/api/try", { method: "POST", body: { slug: S.slug, name, source } });
    if (r.matched) {
      S.myClaim = null;
      msg.className = "try-msg ok"; msg.textContent = "Byte-identical. Saved as a match, credited to you.";
      toast(`<code>${esc(name)}</code>&nbsp;matched by you`, "match");
    } else {
      S.myClaim = name;
      msg.className = "try-msg" + (r.score == null ? " bad" : "");
      msg.textContent = r.score == null ? "It didn't compile:" : (r.text.split("\n")[0] || `score ${r.score}`);
      if (r.score == null) msg.textContent += "\n" + r.text.split("\n").slice(1, 12).join("\n");
    }
    S.editDirty = false;
    S.diffKey = null;
    await refreshFn(false);
    if (r.score != null) { S.ftab = "diff"; showFnTab("diff"); }
  } catch (e) {
    msg.className = "try-msg bad"; msg.textContent = e.message;
  } finally { $("tryBtn").disabled = false; }
}
/* The function stays yours only while you're on it: give it back when you move on. */
function giveBack(next) {
  if (S.myClaim && S.myClaim !== next && S.slug) {
    api("/api/release", { method: "POST", body: { slug: S.slug, name: S.myClaim } }).catch(() => {});
    S.myClaim = null;
  }
}

/* Score per compile, as a match percentage: dots for each compile, a line for the best so far. */
function renderFnChart(d) {
  const wrap = $("fnChartWrap"), svg = $("fnChart");
  const pts = d.scores;
  wrap.hidden = !pts.length;
  if (!pts.length) return;
  const w = Math.max(300, svg.clientWidth || 600), h = 92, padL = 34, padR = 10, padT = 8, padB = 16;
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  const n = pts.length;
  const X = (i) => padL + (n === 1 ? (w - padL - padR) / 2 : (i * (w - padL - padR)) / (n - 1));
  const Y = (p) => padT + (1 - p) * (h - padT - padB);
  let out = "";
  for (const g of [0, 0.5, 1]) out += `<line class="grid" x1="${padL}" x2="${w - padR}" y1="${Y(g)}" y2="${Y(g)}"/><text class="axis" x="${padL - 6}" y="${Y(g) + 3.5}">${g * 100}%</text>`;
  let best = null, path = "";
  const agents = [...new Set(pts.map((p) => p.agent))];
  pts.forEach((p, i) => {
    if (p.score != null) best = best == null ? p.score : Math.min(best, p.score);
    if (best != null) {
      const y = Y(pctOf(best, d.insns));
      path += path ? `H${X(i).toFixed(1)}V${y.toFixed(1)}` : `M${X(i).toFixed(1)},${y.toFixed(1)}`;
    }
  });
  if (path) out += `<path class="bestline" d="${path}"/>`;
  pts.forEach((p, i) => {
    const title = `<title>${esc(p.agent || "")} · ${clock(p.t)} · ${p.score == null ? "didn't compile" : p.score === 0 ? "byte-identical" : `score ${p.score}`}</title>`;
    if (p.score == null) out += `<g class="fail"><path d="M${X(i) - 3},${Y(0) + 5}l6,6m0,-6l-6,6"/>${title}</g>`;
    else out += `<circle class="${p.score === 0 ? "hit" : "pt"} a${agents.indexOf(p.agent) % 4}" cx="${X(i).toFixed(1)}" cy="${Y(pctOf(p.score, d.insns)).toFixed(1)}" r="${p.score === 0 ? 4.5 : 3}">${title}</circle>`;
  });
  svg.innerHTML = out;
  const fails = pts.filter((p) => p.score == null).length;
  const bp = best == null ? null : pctOf(best, d.insns);
  $("fnChartNote").innerHTML = `<b>${plural(n, "compile")}</b>${bp != null ? ` · best ${best === 0 ? "byte-identical" : pctText(bp)}` : ""}${fails ? ` · ${fails} didn't compile` : ""}${agents.length > 1 ? ` · ${agents.map(esc).join(", ")}` : ` · ${esc(agents[0] || "")}`}`;
}

/* The judge's diff: target and attempt side by side, instruction by instruction. */
const MARK = { " ": "eq", r: "arg", s: "arg", i: "arg", "|": "repl", "<": "del", ">": "ins", o: "mov" };
const MARK_TEXT = { r: "registers differ", s: "stack offset differs", i: "operand differs", "|": "different instruction", "<": "missing in yours", ">": "extra in yours", o: "in a different place" };
function insnHtml(text, other, cls) {
  if (!text) return "";
  const m = text.match(/^(\S+)(\s*)(.*)$/);
  if (!m) return esc(text);
  let ops = esc(m[3]);
  if (cls === "arg" && other) {
    const a = m[3].split(/(,\s*|\(|\))/), b = (other.match(/^\S+\s*(.*)$/) || [, ""])[1].split(/(,\s*|\(|\))/);
    ops = a.map((tok, k) => (tok !== b[k] && !/^(,\s*|\(|\))$/.test(tok) ? `<mark>${esc(tok)}</mark>` : esc(tok))).join("");
  }
  return `<span class="op">${esc(m[1])}</span>${m[2] ? " " : ""}${ops}`;
}
function renderDiff(d) {
  const sum = $("diffSum"), box = $("diff"), probs = $("diffProblems");
  probs.innerHTML = "";
  if (!d.rows) {
    sum.innerHTML = d.error ? `<span class="bad">The ${d.kind === "best" ? "best" : "latest"} attempt didn't compile.</span>` : "No attempt yet. The diff appears after the first compile.";
    box.innerHTML = d.error ? `<pre class="code errlog">${esc(d.error)}</pre>` : "";
    return;
  }
  const rows = d.rows;
  const same = rows.filter((r) => r[0] === " ").length, target = rows.filter((r) => r[1]).length;
  const label = { matched: "Matching C", best: "Best attempt", latest: S.ov.kind === "fork" ? "The source now" : "Latest attempt" }[d.kind] || "Attempt";
  sum.innerHTML = `<b>${label}</b> · ${d.percent === 100 && d.score === 0 ? `<span class="ok">byte-identical</span>` : `${d.percent}%${d.score != null ? ` · score ${d.score}` : ""}`} · ${same} of ${target} instructions identical
    <span class="diff-key"><i class="k arg"></i>operands <i class="k repl"></i>different <i class="k mov"></i>moved</span>`;
  const html = [];
  let t = 0, run = [];
  const rowHtml = (r) => {
    const cls = MARK[r[0]] || "repl";
    if (r[1]) t++;
    return `<div class="dr ${cls}"${r[0] !== " " ? ` title="${MARK_TEXT[r[0]] || ""}"` : ""}><span class="dn">${r[1] ? t : ""}</span><code>${insnHtml(r[1], r[2], cls)}</code><span class="dm">${r[0] === " " ? "" : esc(r[0])}</span><code>${insnHtml(r[2], r[1], cls)}</code></div>`;
  };
  const flush = (end) => {
    if (run.length > 10 && end !== "all") {
      const head = run.slice(0, end === "start" ? 0 : 3), tail = run.slice(end === "end" ? run.length : run.length - 3);
      const mid = run.slice(head.length, run.length - tail.length);
      html.push(...head.map(rowHtml));
      html.push(`<button class="dfold" type="button">··· ${mid.length} identical instructions ···</button><div class="dfolded" hidden>${mid.map(rowHtml).join("")}</div>`);
      html.push(...tail.map(rowHtml));
    } else html.push(...run.map(rowHtml));
    run = [];
  };
  rows.forEach((r) => {
    if (r[0] === " ") { run.push(r); return; }
    flush(html.length ? "mid" : "start");
    html.push(rowHtml(r));
  });
  flush(html.length ? "end" : "all");
  box.innerHTML = `<div class="dh"><span></span><span>Target (your game)</span><span></span><span>${esc(label)}</span></div>` + html.join("");
  box.querySelectorAll(".dfold").forEach((b) => { b.onclick = () => { b.nextElementSibling.hidden = false; b.remove(); }; });
  probs.innerHTML = (d.problems || []).map((p) => `<li>${esc(p)}</li>`).join("");
}
async function loadDiff() {
  const d0 = S.fnDetail;
  if (!d0) return;
  const key = `${d0.name}|${S.which}|${d0.scores.length}|${d0.status}`;
  if (key === S.diffKey) return;
  S.diffKey = key;
  if (!$("diff").innerHTML) $("diffSum").textContent = "Compiling…";
  try {
    const d = await api(`/api/diff?slug=${S.slug}&name=${encodeURIComponent(d0.name)}&which=${S.which}`);
    if (S.fnDetail && S.fnDetail.name === d0.name) renderDiff(d);
  } catch (e) { $("diffSum").innerHTML = `<span class="bad">${esc(e.message)}</span>`; S.diffKey = null; }
}

function stepFn(delta) {
  if (!S.fn || !S.map.length) return;
  const order = S.tab === "functions" && L.rows.length ? L.rows : S.map;
  const k = order.findIndex((f) => f.name === S.fn);
  const next = order[(k + delta + order.length) % order.length];
  if (next) openFn(next.name);
}
$("fnPrev").onclick = () => stepFn(-1);
$("fnNext").onclick = () => stepFn(1);

/* ------------------------------------------------------------------ function list */
const L = { q: "", filter: "all", sort: "i", dir: 1, limit: 200, rows: [] };
const FILTERS = [["all", "All"], ["open", "To do"], ["working", "Working"], ["tried", "Tried"], ["matched", "Matched"]];
const fnState = (f) => (f.status === "matched" ? "matched" : f.status === "asm" ? "asm" : f.status === "working" ? "working" : f.best != null ? "tried" : "open");
const STATE_RANK = { working: 0, tried: 1, open: 2, matched: 3, asm: 4 };

function renderList() {
  if (!S.map.length || S.tab !== "functions") return;
  const counts = { all: S.map.length, open: 0, working: 0, tried: 0, matched: 0, asm: 0 };
  for (const f of S.map) counts[fnState(f)]++;
  $("fnFilter").innerHTML = FILTERS.map(([k, label]) => `<button type="button" role="radio" aria-checked="${L.filter === k}" class="${L.filter === k ? "on" : ""}" data-filter="${k}">${label}<span>${counts[k].toLocaleString()}</span></button>`).join("");
  const q = L.q.trim().toLowerCase();
  let rows = S.map.filter((f) => (L.filter === "all" || fnState(f) === L.filter)
    && (!q || f.name.toLowerCase().includes(q) || hex8(f.vram).toLowerCase().includes(q) || (f.unit || "").toLowerCase().includes(q)));
  const key = {
    i: (f) => f.i, name: (f) => f.name.toLowerCase(), insns: (f) => f.insns, attempts: (f) => f.attempts || 0,
    best: (f) => (f.status === "matched" ? 2 : f.best == null ? -1 : pctOf(f.best, f.insns)), status: (f) => STATE_RANK[fnState(f)],
  }[L.sort === "vram" ? "i" : L.sort];
  rows = rows.slice().sort((a, b) => { const x = key(a), y = key(b); return (x < y ? -1 : x > y ? 1 : a.i - b.i) * L.dir; });
  L.rows = rows;
  const fork = S.ov && S.ov.kind === "fork";
  $("fnRows").innerHTML = rows.slice(0, L.limit).map((f) => {
    const st = fnState(f), p = pctOf(f.best, f.insns);
    const best = st === "matched" ? `<span class="ok">100%</span>` : p == null ? `<span class="dim">–</span>` : pctText(p);
    const status = st === "matched" ? `<span class="st matched" title="${esc(f.matched_by ? "by " + f.matched_by : "")}">matched</span>` : st === "asm" ? `<span class="st" title="hand-written assembly: no C compiles to it">assembly</span>` : st === "working" ? `<span class="st working">${esc(f.agent || "working")}</span>`
      : st === "tried" ? `<span class="st tried">tried</span>` : `<span class="st">to do</span>`;
    return `<tr data-fn="${esc(f.name)}" class="${S.fn === f.name ? "sel" : ""}" tabindex="0"><td class="mono dim">${fork ? esc(f.unit || "") : hex8(f.vram)}</td>
      <td class="mono name">${(S.ov && (S.ov.priority || []).includes(f.name)) ? `<span class="star" title="agents take it next">★</span>` : ""}${esc(f.name)}</td><td class="r mono">${f.insns.toLocaleString()}</td><td class="r mono">${best}</td>
      <td class="r mono">${f.attempts || `<span class="dim">0</span>`}</td><td>${status}</td></tr>`;
  }).join("") || `<tr class="none"><td colspan="6">No function matches${q ? ` “${esc(L.q)}”` : ""}.</td></tr>`;
  $("fnShown").textContent = rows.length > L.limit ? `${L.limit.toLocaleString()} of ${rows.length.toLocaleString()} shown` : plural(rows.length, "function");
  $("fnMore").hidden = rows.length <= L.limit;
  document.querySelectorAll(".fn-table th").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === L.sort || (th.dataset.sort === "vram" && L.sort === "i"));
    th.classList.toggle("desc", (th.dataset.sort === L.sort || (th.dataset.sort === "vram" && L.sort === "i")) && L.dir < 0);
  });
}
$("fnFilter").addEventListener("click", (e) => { const b = e.target.closest("[data-filter]"); if (b) { L.filter = b.dataset.filter; L.limit = 200; renderList(); } });
$("fnSearch").addEventListener("input", () => { L.q = $("fnSearch").value; L.limit = 200; renderList(); });
$("fnMore").onclick = () => { L.limit += 500; renderList(); };
$("fnRows").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-fn]"); if (tr) openFn(tr.dataset.fn); });
$("fnRows").addEventListener("keydown", (e) => { const tr = e.target.closest("tr[data-fn]"); if (tr && e.key === "Enter") openFn(tr.dataset.fn); });
document.querySelectorAll(".fn-table th[data-sort]").forEach((th) => {
  th.onclick = () => {
    const k = th.dataset.sort === "vram" ? "i" : th.dataset.sort;
    if (L.sort === k) L.dir = -L.dir;
    else { L.sort = k; L.dir = ["best", "attempts", "insns"].includes(k) ? -1 : 1; }
    renderList();
  };
});

/* Matched code over time, from when each function was matched. */
function renderProgress() {
  const svg = $("progress"), total = S.map.reduce((n, f) => n + (f.size || 0), 0);
  const done = S.map.filter((f) => f.status === "matched");
  S.today = done.filter((f) => f.matched_at > Date.now() / 1000 - 86400).length;
  const dated = done.filter((f) => f.matched_at).sort((a, b) => a.matched_at - b.matched_at);
  if (!total || dated.length < 2) { svg.innerHTML = ""; return; }
  let cum = done.filter((f) => !f.matched_at).reduce((n, f) => n + (f.size || 0), 0);
  const t0 = dated[0].matched_at, t1 = Math.max(Date.now() / 1000, dated[dated.length - 1].matched_at + 1);
  const top = cum + dated.reduce((n, f) => n + (f.size || 0), 0);  // scaled to what's done, so early progress shows
  const X = (t) => (200 * (t - t0)) / (t1 - t0), Y = (v) => 42 - (40 * v) / Math.max(top, 1);
  let line = `M0,${Y(cum).toFixed(1)}`;
  for (const f of dated) { cum += f.size || 0; line += `H${X(f.matched_at).toFixed(1)}V${Y(cum).toFixed(1)}`; }
  line += "H200";
  svg.innerHTML = `<path class="area" d="${line}V44H0Z"/><path class="line" d="${line}"/>`;
}

/* ------------------------------------------------------------------ drawers, keys */
function closeAll() {
  if (!$("drawer").hidden) giveBack(null);
  for (const id of ["drawer", "dialog"]) $(id).hidden = true;
  document.body.classList.remove("locked");
  S.fn = null;
  if (S.map.length) drawMap();
}
document.querySelectorAll(".drawer").forEach((d) => {
  d.addEventListener("click", (e) => {
    if (e.target !== d && !e.target.closest("[data-close]")) return;
    if (d.id === "drawer") { giveBack(null); S.fn = null; drawMap(); }
    d.hidden = true;
    if ($("drawer").hidden && $("dialog").hidden) document.body.classList.remove("locked");
  });
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { if (!$("launchPop").hidden) { hideLaunch(); return; } closeAll(); return; }
  if (e.target.closest("input, textarea, select") || e.metaKey || e.ctrlKey || e.altKey) return;  // typing
  if (!$("drawer").hidden && (e.key === "j" || e.key === "k")) { e.preventDefault(); stepFn(e.key === "j" ? 1 : -1); return; }
  if (e.key === "/" && S.slug && $("drawer").hidden) {
    e.preventDefault();
    if (S.tab !== "functions") location.hash = `#/p/${S.slug}/functions`;
    setTimeout(() => $("fnSearch").focus(), 50);
  }
});

function cmdBox(cmd) {
  return `<div class="cmd"><code>${esc(cmd)}</code><button type="button" data-copy="${esc(cmd)}">Copy</button></div>`;
}
function wireCopies(root) {
  root.querySelectorAll("[data-copy]").forEach((b) => { b.onclick = () => navigator.clipboard.writeText(b.dataset.copy).then(() => { b.textContent = "Copied"; setTimeout(() => (b.textContent = "Copy"), 1500); }); });
}

function dialog(title, html) {
  $("dialogTitle").textContent = title;
  $("dialogBody").innerHTML = html;
  $("dialog").hidden = false;
  document.body.classList.add("locked");
  wireCopies($("dialogBody"));
  return $("dialogBody");
}

$("joinBtn").onclick = openJoin;
$("askBtn").onclick = openConnect;
$("talkBtn").onclick = openConnect;
$("connectBtn").onclick = openConnect;

/* Your own agent with decomp.wtf's tools: registered once, then you just ask it. */
async function connectRows(box) {
  const render = (rows) => {
    box.innerHTML = rows.map((a) => `<div class="line-item"><div class="txt"><b>${esc(a.name)}</b><span class="mono">${esc(a.command)}</span></div>
      ${!a.installed ? `<span class="muted small">not installed</span>` : a.connected ? `<span class="ok-mark">connected</span>` : `<button class="primary small-btn" data-connect="${esc(a.id)}" type="button">Connect</button>`}</div>`).join("");
    box.querySelectorAll("[data-connect]").forEach((b) => {
      b.onclick = async () => {
        b.disabled = true; b.textContent = "Connecting…";
        try {
          await api("/api/connect", { method: "POST", body: { id: b.dataset.connect } });
          toast(`Connected. Ask it to decompile your game.`, "match");
          render((await api("/api/connect")).agents);
        } catch (e) { toast(esc(e.message), "err"); b.disabled = false; b.textContent = "Connect"; }
      };
    });
  };
  try {
    const r = await api("/api/connect");
    render(r.agents);
    return r;
  } catch (e) { box.innerHTML = `<p class="bad">${esc(e.message)}</p>`; return null; }
}
const HOST_NAMES = { windows: "Windows", macos: "your Mac", linux: "this computer" };
async function openConnect() {
  if (S.st && S.st.runtime && S.st.runtime !== "native") return openHostConnect();
  const body = dialog("Use decomp.wtf from your AI agent", `<p>Connect the agent you already use, once. Then ask it in your terminal, for example:</p>
    ${cmdBox("decompile my game at ~/roms/game.z64")}
    <p class="muted small">It confirms the game is your own copy, imports it, opens this live view and works with every tool here: the byte-for-byte judge, the autopilot, helper agents, export. Anything it does shows up here as it happens.</p>
    <div class="setup-group" id="connectRows"><p class="muted small">Looking for your agents…</p></div>
    <div class="setup-group"><h3>Any other MCP client (Cursor, Claude Desktop, Windsurf, your own)</h3><pre class="code" id="bridgeJson"></pre>
      <p class="muted small">It starts decomp.wtf when needed; no token, nothing to update when the app restarts.</p></div>`);
  const r = await connectRows(body.querySelector("#connectRows"));
  if (r) body.querySelector("#bridgeJson").textContent = JSON.stringify({ mcpServers: { decomp: { command: r.bridge[0], args: r.bridge.slice(1) } } }, null, 2);
}

/* decomp.wtf in WSL (Windows) or in Docker: the agents you talk to run on the computer itself, so they're
   connected from there, by the `decomp` command the installer put on it. */
async function openHostConnect() {
  const host = HOST_NAMES[S.st.host_os] || "your computer";
  const shell = S.st.host_os === "windows" ? "PowerShell" : "a terminal";
  const body = dialog("Use decomp.wtf from your AI agent", `<p>Run this once in ${esc(shell)} on ${esc(host)}: it connects Claude Code, Codex, Antigravity CLI and opencode there.</p>
    ${cmdBox("decomp connect")}
    <p>Then ask your agent, for example:</p>
    ${cmdBox(S.st.host_os === "windows" ? "decompile my game at C:\\Games\\game.z64" : "decompile my game at ~/roms/game.z64")}
    <p class="muted small">decomp.wtf runs in ${S.st.runtime === "wsl" ? "its own WSL 2 Linux on Windows" : "Docker"}; your agent reaches it through the command below. Anything it does shows up here as it happens.</p>
    <div class="setup-group"><h3>Any other MCP client (Cursor, Claude Desktop, Windsurf, your own)</h3><pre class="code" id="bridgeJson"></pre></div>`);
  try {
    const r = await api("/api/connect");
    body.querySelector("#bridgeJson").textContent = JSON.stringify({ mcpServers: { decomp: { command: r.bridge[0], args: r.bridge.slice(1) } } }, null, 2);
  } catch (e) { body.querySelector("#bridgeJson").textContent = e.message; }
}

/* ------------------------------------------------------------------ steering: instructions, next in line */
function renderSteering(ov) {
  const sp = ov.spend;
  const n = ov.helper_matches || 0;
  $("spendLine").textContent = sp && sp.usd ? `· ${money(sp.usd)} spent${n ? ` · ${money(sp.usd / n)}/match` : ""}`
    : sp && sp.tokens ? `· ${(sp.tokens / 1000).toFixed(0)}k tokens` : "";
  $("instrState").textContent = ov.instructions ? "agents get these" : "none yet";
}
$("instrSave").onclick = async () => {
  try {
    S.ov = await api("/api/instructions", { method: "POST", body: { slug: S.slug, text: $("instrEdit").value } });
    renderSteering(S.ov);
    toast("Saved. Agents get them from their next task on.");
  } catch (e) { toast(esc(e.message), "err"); }
};
async function togglePrio(name, on) {
  try {
    const r = await api("/api/priority", { method: "POST", body: { slug: S.slug, name, on } });
    S.ov.priority = r.priority;
    toast(on ? `Agents take <code>${esc(name)}</code> next.` : "No longer first in line.");
    renderList(); renderPrio();
  } catch (e) { toast(esc(e.message), "err"); }
}
function renderPrio() {
  const list = (S.ov && S.ov.priority) || [];
  const box = $("prioList");
  if (S.ov && S.ov.kind === "fork") { box.innerHTML = `<li class="muted small">Agents pick the easiest open functions first.</li>`; return; }
  box.innerHTML = list.length ? list.map((n) => `<li><span class="star">★</span>${fnLink(n)}<span class="grow"></span><button class="link-btn" data-unpin="${esc(n)}" type="button">remove</button></li>`).join("")
    : `<li class="muted small">Nothing pinned: agents take the easiest open functions first. Open a function and click ★ Next to put it first.</li>`;
  box.querySelectorAll(".fn").forEach((x) => { x.onclick = () => openFn(x.dataset.fn); });
  box.querySelectorAll("[data-unpin]").forEach((b) => { b.onclick = () => togglePrio(b.dataset.unpin, false); });
}

/* Autopilot: free matches before any agent spends a token: identical code (twins) of what's matched, then every
   function's machine draft through the judge; the permuter works on near misses meanwhile. */
function renderAutopilot(ov) {
  const box = $("autopilot"), ap = ov.autopilot, tw = ov.twins, pm = ov.permuter;
  const canRun = (ov.kind !== "fork" || ov.ready) && ov.functions - ov.matched > 0;
  box.hidden = !ap && !canRun && !tw && !pm;
  if (box.hidden) return;
  const running = ap && ap.state === "running";
  const twinsRunning = tw && tw.state === "running";
  box.classList.toggle("on", !!(running || twinsRunning));
  $("apBar").hidden = !running || ap.phase === "twins";
  const perm = pm && (pm.tried || pm.state === "running")
    ? ` The permuter ${pm.state === "running" ? `is rewriting near misses${pm.current ? ` (now ${pm.current})` : ""}` : "rewrote near misses"}: ${plural(pm.tried, "function")}, ${pm.matched} matched.` : "";
  const twinsDone = (ap && ap.twins) || (tw && tw.matched) || 0;
  if (running && ap.phase === "twins") {
    $("apTitle").textContent = "Autopilot: matching code the game has more than once (twins of matched functions)";
    $("apSub").textContent = `Each match's code is tried on its identical copies, names swapped; the judge decides.${perm}`;
    $("apBtn").textContent = "Stop";
  } else if (running) {
    $("apTitle").textContent = `Autopilot: trying each function's machine draft · ${ap.matched.toLocaleString()} matched for free`;
    $("apSub").textContent = `${ap.tried.toLocaleString()} of ${ap.total.toLocaleString()} tried${ap.near ? ` · ${ap.near} more are 90%+ there` : ""}${ap.twins ? ` · ${ap.twins.toLocaleString()} from identical code` : ""}. Agents work alongside it.${perm}`;
    $("apBar").firstElementChild.style.width = `${(100 * ap.tried) / Math.max(1, ap.total)}%`;
    $("apBtn").textContent = "Stop";
  } else if (ap || tw) {
    const total = (ap ? ap.matched : 0) + (ap ? 0 : twinsDone);
    $("apTitle").textContent = twinsRunning ? `Matching identical code: ${tw.matched.toLocaleString()} so far`
      : total ? `Autopilot matched ${total.toLocaleString()} function${total === 1 ? "" : "s"} for free` : "Autopilot found no free matches this time";
    const parts = [];
    if (twinsDone) parts.push(`${twinsDone.toLocaleString()} from identical code`);
    if (tw && tw.asm) parts.push(`${tw.asm.toLocaleString()} copies of assembly set aside (not for agents)`);
    if (ap && ap.tried) parts.push(`${ap.tried.toLocaleString()} machine drafts tried${ap.near ? `, ${ap.near} more 90%+ there` : ""}`);
    $("apSub").textContent = (parts.length ? parts.join(" · ") + ". " : "") + (ap && ap.no_drafts ? "" : "Run it again after agents add types and names: drafts get better.") + perm;
    $("apBtn").textContent = "Run again";
  } else {
    $("apTitle").textContent = "Free matches first";
    $("apSub").textContent = (ov.kind === "fork" && ov.origin !== "scratch"
      ? "Autopilot matches the code this game has more than once from its matched copies (names swapped, judged like any attempt)"
      : "Autopilot matches identical copies of matched code and tries every function's machine draft (m2c) through the judge") + `; whatever matches costs no agent time.${perm}`;
    $("apBtn").textContent = "Run autopilot";
  }
  $("apBtn").onclick = async () => {
    $("apBtn").disabled = true;
    try {
      S.ov = await api("/api/autopilot", { method: "POST", body: { slug: S.slug, action: running ? "stop" : "start" } });
      renderAutopilot(S.ov);
    } catch (e) { toast(esc(e.message), "err"); } finally { $("apBtn").disabled = false; }
  };
}

const LAYOUT = { gc: "dtk-template", wii: "dtk-template", nds: "ds-decomp" };
const cwName = (c) => (c.startsWith("DS/") ? `CodeWarrior for DS ${c.slice(3)}` : `CodeWarrior ${c}`);

/* ------------------------------------------------------------------ source: the base for ports and mods */
function renderSource(ov) {
  const ex = ov.export, fork = ov.kind === "fork";
  $("srcExport").hidden = fork || !ov.exportable;
  $("srcWhen").textContent = !fork && ex ? `exported ${since(ex.t)}` : "";
  if (fork) {
    const rb = ov.rebuild;
    $("srcText").innerHTML = ov.origin === "scratch"
      ? `This project <b>is</b> the source tree: a ${LAYOUT[ov.platform] || "decomp"} project that rebuilds your game on every build. Every match is committed to git in the project folder.`
        + (rb ? `<p class="small ${rb.identical ? "ok" : "warn-text"}">${rb.identical ? "✓ The rebuilt game is your copy, byte for byte" + (/padding/.test(rb.detail) ? " (up to the padding at its end: your copy is trimmed)." : ".")
          : `The rebuilt game isn't your copy byte for byte yet: ${esc(rb.detail)}. Decompiling works anyway: every function is compared with your copy's own code.`}</p>` : "")
      : `This project <b>is</b> the source tree${ov.repo_url ? ` (a fork of <a class="link" href="${esc(ov.repo_url)}" target="_blank" rel="noopener">${esc(ov.repo_url.replace(/^https?:\/\//, ""))}</a>)` : ""}. Every match is committed to the <code>decomp-wtf</code> branch, ready to send upstream.`;
    $("srcState").innerHTML = `<div class="row"><button class="ghost small-btn" id="forkMeasure" type="button">Re-measure with objdiff</button>${rb ? `<button class="ghost small-btn" id="romCheck" type="button">Rebuild the game and compare</button>` : ""}</div>`;
    if (rb) $("romCheck").onclick = async () => {
      $("romCheck").disabled = true;
      toast("Rebuilding the game and comparing it with your copy…");
      try {
        const { job } = await api("/api/fork/rom-check", { method: "POST", body: { slug: S.slug } });
        const r = await followJob(job);
        toast(r.identical ? "The rebuilt game is your copy, byte for byte." : `Not identical yet: ${esc(r.detail)}`, r.identical ? "match" : "");
        S.ov = await api(`/api/project?slug=${S.slug}`);
        renderSource(S.ov);
      } catch (e) { toast(esc(e.message), "err"); if ($("romCheck")) $("romCheck").disabled = false; }
    };
    $("forkMeasure").onclick = async () => {
      toast("Measuring the whole project with objdiff…");
      try { const { job } = await api("/api/fork/check", { method: "POST", body: { slug: S.slug } }); await followJob(job); toast("Progress updated.", "match"); refreshMap(); }
      catch (e) { toast(esc(e.message), "err"); }
    };
    return;
  }
  if (!ov.exportable) {
    $("srcText").textContent = `Exporting a rebuildable project isn't available for ${ov.platform_name || "this platform"} yet. Your matched C is kept in the project.`;
    $("srcState").innerHTML = "";
    return;
  }
  $("srcText").innerHTML = "decomp.wtf writes a complete decompilation project: the matched functions as C, everything else as assembly, a linker script and <code>build.py</code>. Then it builds it to prove it rebuilds your game <b>byte for byte</b>. It's the base for ports and mods; game code and data stay out of git, so the tree is safe to put in a repository.";
  const stale = ex && ex.matched != null && ex.matched < ov.matched;
  $("srcState").innerHTML = !ex ? `<span class="muted small">Not exported yet.</span>`
    : `<div class="src-line ${ex.identical === false ? "bad" : "ok"}">${ex.identical === false ? "✕ the last rebuild differed" : "✓ rebuilds byte for byte"}</div>
       <div class="src-line">${ex.matched != null ? `${ex.matched.toLocaleString()} of ${ex.functions.toLocaleString()} functions in C` : ""}${stale ? ` · <b>${ov.matched - ex.matched} new matches since</b>` : ""}</div>
       ${ex.path ? `<div class="src-line">${esc(ex.path)}</div>` : ""}`;
  $("srcExport").textContent = ex ? "Export again" : "Export project";
}
$("srcOpen").onclick = () => openFolder();
$("srcExport").onclick = async () => {
  $("srcExport").disabled = true;
  $("expLog").hidden = false;
  $("expResult").innerHTML = "";
  try {
    const { job } = await api("/api/export", { method: "POST", body: { slug: S.slug } });
    const r = await followJob(job, $("expLog"));
    $("expResult").innerHTML = r.identical === false
      ? `<div class="notice bad"><span><b>Exported, but the rebuild differs.</b> ${esc(r.log.split("\n").filter(Boolean).pop() || "")}</span></div>`
      : `<div class="notice room"><span><b>Rebuilds byte for byte.</b> ${r.matched != null ? `${r.matched} of ${r.functions} functions are C.` : ""}</span></div>${cmdBox(`cd "${r.path}" && python build.py`)}`;
    wireCopies($("expResult"));
    S.ov = await api(`/api/project?slug=${S.slug}`);
    renderSource(S.ov);
  } catch (e) { toast(esc(e.message), "err"); } finally { $("srcExport").disabled = false; }
};

/* ------------------------------------------------------------------ project settings */
async function renderProjectSettings() {
  const ov = S.ov;
  if (!ov) return;
  const fork = ov.kind === "fork";
  $("compilerPanel").hidden = fork;
  if (!fork) {
    if (!S.st) await loadState();
    fillCompilers($("setTc"), ov.compiler.toolchain, { n64: "N64", psx: "PS1", ps2: "PS2", gba: "GBA" }[ov.platform]);
    $("setFlags").value = ov.compiler.flags.join(" ");
    $("setProfiles").value = JSON.stringify(ov.compiler_profiles || {}, null, 2);
    $("setRanges").value = JSON.stringify(ov.compiler_ranges || [], null, 2);
    $("setFunctions").value = JSON.stringify(ov.compiler_functions || {}, null, 2);
  }
  const facts = [["Platform", ov.platform_name], ["Game", ov.rom?.title || ov.title], [fork ? "Version" : "Game code", ov.rom?.game_code || ov.version],
    !fork && ov.rom?.region ? ["Region", ov.rom.region] : null, fork ? ["Layout", ov.origin === "scratch" ? (LAYOUT[ov.platform] || "decomp") + " (from scratch)" : ov.fork_kind] : ["Compiler", `${ov.compiler.name} ${ov.compiler.flags.join(" ")}`],
    fork && ov.repo_url ? ["Repository", ov.repo_url.replace(/^https?:\/\//, "")] : null,
    ["Functions", `${ov.functions.toLocaleString()} (${bytes(ov.bytes || 0)} of code)`], ["Folder", ov.folder]].filter(Boolean);
  $("projFacts").innerHTML = facts.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v || "—")}</dd></div>`).join("");
}
$("setTcReset").onclick = () => { $("setFlags").value = ""; };
$("setTcSave").onclick = async () => {
  try {
    const flags = $("setFlags").value.trim();
    S.ov = await api("/api/compiler", { method: "POST", body: { slug: S.slug, toolchain: $("setTc").value, flags: flags ? flags.split(/\s+/) : null } });
    renderStats(S.ov);
    renderProjectSettings();
    toast("Compiler settings saved.");
  } catch (e) { toast(esc(e.message), "err"); }
};
$("setProfilesSave").onclick = async () => {
  const button = $("setProfilesSave");
  try {
    const body = { slug: S.slug,
      compiler_profiles: JSON.parse($("setProfiles").value),
      compiler_ranges: JSON.parse($("setRanges").value),
      compiler_functions: JSON.parse($("setFunctions").value) };
    button.disabled = true;
    S.ov = await api("/api/compiler", { method: "POST", body });
    renderStats(S.ov);
    await renderProjectSettings();
    toast("Compiler profiles saved.");
  } catch (e) { toast(esc(e.message), "err"); }
  finally { button.disabled = false; }
};
$("folderBtn").onclick = () => openFolder();
$("deleteBtn").onclick = () => {
  dialog("Remove project", `<p>Remove <b>${esc(S.ov.title)}</b> from decomp.wtf? Matched functions and notes are deleted.</p>
    <label class="check"><input type="checkbox" id="delFiles"><span>Also delete the project folder (your ROM copy and exported sources in it)</span></label>
    <div class="row"><button class="primary" id="delGo" type="button">Remove</button><button class="ghost" data-close type="button">Cancel</button></div>`);
  $("delGo").onclick = async () => {
    try {
      await api("/api/delete", { method: "POST", body: { slug: S.slug, files: $("delFiles").checked } });
      $("dialog").hidden = true;
      document.body.classList.remove("locked");
      loadNav();
      location.hash = "#/";
    } catch (e) { toast(esc(e.message), "err"); }
  };
};

/* ------------------------------------------------------------------ agents & models page */
async function showAgentsPage() {
  show("agentsPage");
  $("agList").innerHTML = `<p class="muted small">Looking for your agents…</p>`;
  await loadState();
  renderAgentsPage();
}
function renderAgentsPage() {
  const rows = S.st.agents.filter((a) => a.id !== "api");
  $("agList").innerHTML = rows.map((a) => `<div class="agent-row">
      <div class="ar-head"><b>${esc(a.name)}</b><span class="muted small">${esc(a.vendor)}</span><span class="grow"></span>
        ${a.installed ? `<span class="ok-mark">installed</span>` : `<button class="primary small-btn" data-inst="${esc(a.id)}" type="button" ${a.needs ? "disabled" : ""}>Install</button>`}</div>
      ${a.installed ? `<dl class="facts"><div><dt>Version</dt><dd>${esc(a.version || "?")}</dd></div><div><dt>Default model</dt><dd>${esc(a.default_model || "the app's own")}</dd></div><div><dt>Models offered</dt><dd>${(a.models || []).length ? (a.models || []).length : "any you type"}</dd></div></dl>
        ${(a.models || []).length ? `<div class="model-chips">${a.models.slice(0, 12).map((m) => `<span class="chip-s mono" title="${esc(m.note || "")}">${esc(m.name)}</span>`).join("")}${a.models.length > 12 ? `<span class="muted small">+${a.models.length - 12} more</span>` : ""}</div>` : ""}
        <p class="muted small">${esc(a.signin)}</p>`
      : `<p class="muted small">${a.needs ? `Needs ${esc(a.needs)} first. ` : ""}Or install it yourself:</p>${cmdBox(a.install)}`}
      <pre class="joblog" data-ilog="${esc(a.id)}" hidden></pre></div>`).join("");
  wireCopies($("agList"));
  $("agList").querySelectorAll("[data-inst]").forEach((b) => {
    b.onclick = async () => {
      const log = $("agList").querySelector(`[data-ilog="${b.dataset.inst}"]`);
      log.hidden = false; b.disabled = true;
      try {
        const { job } = await api("/api/agents/install", { method: "POST", body: { kind: b.dataset.inst } });
        await followJob(job, log);
        toast("Installed.", "match");
        showAgentsPage();
      } catch (e) { toast(esc(e.message), "err"); b.disabled = false; }
    };
  });
  const eps = S.st.settings.endpoints || [];
  const card = (e) => `<div class="ep-card" data-id="${esc(e.id || "")}">
      ${e.id ? `<div class="ar-head"><b>${esc(e.name)}</b><span class="muted small mono">${esc(e.model)} · ${esc(hostOf(e.base_url))}</span></div>` : `<div class="ar-head"><b>A new endpoint</b></div>`}
      <div class="lz-fields">${endpointFields(e)}</div>
      <div class="row"><button class="primary small-btn" data-save type="button">${e.id ? "Save" : "Add"}</button>
        ${e.has_key ? `<button class="ghost small-btn" data-forget type="button">Forget the key</button>` : ""}
        ${e.id ? `<button class="ghost small-btn danger" data-remove type="button">Remove</button>` : eps.length ? `<button class="ghost small-btn" data-cancel type="button">Cancel</button>` : ""}</div></div>`;
  $("apiBox").innerHTML = `<p class="muted small">Any OpenAI-compatible API runs through decomp.wtf's built-in agent loop: DeepSeek, OpenRouter, OpenAI, or models on your own machines with llama.cpp, Ollama or LM Studio. Save one endpoint per machine or provider: when you start agents you tick the ones to use, and the sessions are spread across them. Each key stays on this computer and is only sent to its own endpoint.</p>
    ${eps.map(card).join("")}
    <div id="epNew" ${eps.length && !S.addingEndpoint ? "hidden" : ""}>${card({ provider: "custom" })}</div>
    ${eps.length && !S.addingEndpoint ? `<div class="row"><button class="ghost small-btn" id="epAdd" type="button">Add an endpoint</button></div>` : ""}
    <p class="muted small">Context size, for a local model: what you started it with (found by itself from llama.cpp). The agent keeps its conversation within it. Give it at least 16384 (llama.cpp: <code>-c 16384</code>; Ollama: <code>OLLAMA_CONTEXT_LENGTH=16384</code>, or it quietly cuts the start off). Sessions at once: for a machine that serves one request at a time, 1.</p>`;
  wireEndpointFields($("apiBox"));
  const post = async (body, said) => {
    try {
      S.st.settings = await api("/api/settings", { method: "POST", body });
      S.addingEndpoint = false;
      if (said) toast(said);
      renderAgentsPage();
    } catch (e) { toast(esc(e.message), "err"); }
  };
  $("apiBox").querySelectorAll(".ep-card").forEach((c) => {
    const id = c.dataset.id;
    c.querySelector("[data-save]").onclick = () => post({ endpoint: endpointData(c, id) }, id ? "Saved." : "Added. Tick it when you start agents (“API key”).");
    const forget = c.querySelector("[data-forget]"), remove = c.querySelector("[data-remove]"), cancel = c.querySelector("[data-cancel]");
    if (forget) forget.onclick = () => post({ endpoint: { id, clear_key: true } }, "The key is gone from this computer.");
    if (remove) remove.onclick = () => post({ remove_endpoint: id }, "Removed.");
    if (cancel) cancel.onclick = () => { S.addingEndpoint = false; renderAgentsPage(); };
  });
  if ($("epAdd")) $("epAdd").onclick = () => { S.addingEndpoint = true; renderAgentsPage(); };
  $("mcpBox").innerHTML = `<p class="muted small">Connect the agent you talk to, once. Then ask it in your terminal: <i>“decompile my game at ~/roms/game.z64”</i>. It imports it, opens this view and works with every tool here.</p>
    <div class="setup-group" id="mcpRows"><p class="muted small">Looking…</p></div>
    <p class="col-title">Any other MCP client (Cursor, Claude Desktop, Windsurf…)</p><pre class="code" id="mcpJson"></pre>
    <p class="col-title">A client that connects by URL (HTTP MCP)</p>
    <p class="muted small">It needs a token: a password for this app's tools, so keep it to yourself. Making a new one for the same name replaces the old one.</p>
    <div class="lz-row"><label class="field"><span>Name it goes by</span><input id="mcpName" class="mono" value="http-client"></label><button class="ghost small-btn" id="mcpToken" type="button">Make a token</button></div>
    <pre class="code" id="mcpHttp" hidden></pre>`;
  connectRows($("mcpRows")).then((r) => { if (r) $("mcpJson").textContent = JSON.stringify({ mcpServers: { decomp: { command: r.bridge[0], args: r.bridge.slice(1) } } }, null, 2); });
  $("mcpToken").onclick = async () => {
    try {
      const t = await api("/api/agents/token", { method: "POST", body: { name: $("mcpName").value.trim() || "http-client" } });
      $("mcpHttp").hidden = false;
      $("mcpHttp").textContent = JSON.stringify({ mcpServers: { decomp: { type: "http", url: t.url, headers: { Authorization: `Bearer ${t.token}` } } } }, null, 2);
    } catch (e) { toast(esc(e.message), "err"); }
  };
}
$("agRefresh").onclick = showAgentsPage;

/* ------------------------------------------------------------------ compilers & tools page */
const TOOL_GROUPS = [["", "All"], ["N64", "N64"], ["PS1", "PS1"], ["PS2", "PS2"], ["GBA", "GBA"], ["cw", "GameCube, Wii, DS"], ["tool", "Tools"]];
let toolFilter = "";
async function showToolsPage() {
  show("toolsPage");
  await loadState();
  renderTools();
}
function renderTools() {
  const all = S.st.toolchains;
  const group = (t) => (t.kind === "compiler" ? (t.platform || "") : /^mwcc/.test(t.id) ? "cw" : "tool");
  $("toolChips").innerHTML = TOOL_GROUPS.map(([k, label]) => `<button class="chip ${toolFilter === k ? "on" : ""}" data-g="${k}" type="button">${label} <span>${k ? all.filter((t) => group(t) === k).length : all.length}</span></button>`).join("");
  $("toolChips").querySelectorAll("[data-g]").forEach((b) => { b.onclick = () => { toolFilter = b.dataset.g; renderTools(); }; });
  const rows = all.filter((t) => !toolFilter || group(t) === toolFilter);
  $("toolGrid").innerHTML = rows.map((t) => `<div class="tool-card ${t.installed ? "ok" : ""}">
      <div class="tc-top"><b>${esc(t.name)}</b>${t.license === "proprietary" ? `<span class="chip-s warn">proprietary</span>` : ""}${t.platform ? `<span class="chip-s">${esc(t.platform)}</span>` : ""}${t.linux ? `<span class="chip-s" title="A Linux program: ${esc(t.linux_side)}">${t.linux_side === "wsl" ? "WSL 2" : "Docker"}</span>` : ""}</div>
      <p class="muted small">${esc(t.use)}</p>
      <div class="row">${!t.supported ? `<span class="muted small">not available for this computer</span>` : t.installed ? `<span class="ok-mark">✓ installed</span>` : `<button class="ghost small-btn" data-tc="${esc(t.id)}" data-prop="${t.license === "proprietary" ? 1 : ""}" type="button">Install</button>`}</div></div>`).join("");
  $("toolGrid").querySelectorAll("[data-tc]").forEach((b) => { b.onclick = () => { b.disabled = true; installToolchain(b.dataset.tc, !!b.dataset.prop).finally(() => { b.disabled = false; }); }; });
}

/* ------------------------------------------------------------------ settings page */
async function showSettingsPage() {
  show("settingsPage");
  const st = await loadState();
  $("setAgents").innerHTML = `
    <label class="field"><span>Functions per agent session (a fresh session keeps the agent's context small)</span><input id="perSession" type="number" min="1" max="20" value="${st.settings.per_session}"></label>
    <label class="field"><span>Budget: stop helper agents when a project's agents have spent this much (US dollars, as the agents report it; 0 = no limit)</span><input id="budgetSet" type="number" min="0" step="0.5" value="${st.settings.budget_usd || 0}"></label>
    <label class="check"><input type="checkbox" id="autopilotSet" ${st.settings.autopilot !== false ? "checked" : ""}><span><b>Autopilot after each import</b>: try every function's machine draft first, so free matches cost no agent time.</span></label>
    <label class="check"><input type="checkbox" id="autopilotAfterMatchSet" ${st.settings.autopilot_after_match ? "checked" : ""}><span><b>Restart autopilot after a manual match</b>: when autopilot is idle, try remaining machine drafts again after you or an agent matches a function.</span></label>`;
  const theme = savedTheme();
  $("setComputer").innerHTML = `
    <div class="field"><span>Appearance</span><div class="seg" id="themeSeg">${[["", "System"], ["light", "Light"], ["dark", "Dark"]].map(([k, l]) => `<button type="button" data-theme="${k}" class="${theme === k ? "on" : ""}">${l}</button>`).join("")}</div></div>
    <div class="field"><span>Start with this computer (keeps team rooms open, resumes agents)</span><div class="row" id="svcRow2"><span class="muted small">Checking…</span></div></div>
    <div class="field"><span>Projects live in</span>${cmdBox(st.home)}</div>`;
  const where = { wsl: " (in its own WSL 2 Linux)", docker: " (in Docker)" }[st.runtime] || "";
  const inst = st.install || {};
  const from = inst.channel === "branch" ? `the ${esc(inst.branch)} branch @ <code>${esc((inst.commit || "?").slice(0, 7))}</code>` : "releases on decomp.wtf";
  $("setAbout").innerHTML = `<p class="small">decomp.wtf ${esc(st.version)} on ${esc(st.platform)}${esc(where)}. Free and open source (MIT). Everything runs on this computer: no account, nothing sent to us.</p>
    <div class="field"><span>Updates come from ${from}</span><div class="row"><span class="muted small grow" id="upText">Checking…</span><button class="ghost small-btn" id="upCheck" type="button">Check now</button></div></div>
    <label class="check"><input type="checkbox" id="checkUpdateSet" ${st.settings.check_updates ? "checked" : ""}><span><b>Look for updates</b> now and then (it only asks ${inst.channel === "branch" ? "GitHub for the branch's newest commit" : "decomp.wtf which version is current"}; nothing about you or your projects is sent).</span></label>
    <label class="check"><input type="checkbox" id="autoUpdateSet" ${st.settings.auto_update ? "checked" : ""}><span><b>Install updates by itself</b> when no helper agent is working (it restarts in a few seconds; agents carry on).</span></label>
    <div class="row"><a class="link small" href="https://decomp.wtf" target="_blank" rel="noopener">decomp.wtf</a><a class="link small" href="https://github.com/i12bp8/decomp.wtf" target="_blank" rel="noopener">Source code</a></div>`;
  const showUpdate = (u) => {
    if (!$("upText") || !u) return;
    $("upText").textContent = u.error ? u.error : u.available ? `New: ${updateName(u)}` : u.disabled ? "Not looking for updates." : "Up to date.";
    if (u.available && !$("upGo2")) {
      const b = document.createElement("button");
      b.className = "primary small-btn"; b.id = "upGo2"; b.type = "button"; b.textContent = "Update now";
      b.onclick = offerUpdate;
      $("upCheck").before(b);
    }
  };
  checkUpdate(false, false).then(showUpdate);
  $("upCheck").onclick = async () => { $("upText").textContent = "Checking…"; showUpdate(await checkUpdate(true, false)); };
  $("checkUpdateSet").onchange = () => save({ check_updates: $("checkUpdateSet").checked }, $("checkUpdateSet").checked ? "decomp.wtf looks for updates." : "decomp.wtf won't look for updates (Check now still does).");
  $("autoUpdateSet").onchange = () => save({ auto_update: $("autoUpdateSet").checked }, $("autoUpdateSet").checked ? "decomp.wtf installs updates by itself." : "decomp.wtf asks before updating.");
  wireCopies($("setComputer"));
  serviceRow($("svcRow2"), "decomp.wtf runs while you have it open.");
  $("themeSeg").querySelectorAll("[data-theme]").forEach((b) => { b.onclick = () => { setTheme(b.dataset.theme); $("themeSeg").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); }; });
  const save = (body, msg) => api("/api/settings", { method: "POST", body }).then((s) => { S.st.settings = s; toast(msg); }).catch((e) => toast(esc(e.message), "err"));
  $("perSession").onchange = () => save({ per_session: Number($("perSession").value) }, "Saved.");
  $("autopilotSet").onchange = () => save({ autopilot: $("autopilotSet").checked }, "Saved.");
  $("autopilotAfterMatchSet").onchange = () => save({ autopilot_after_match: $("autopilotAfterMatchSet").checked }, "Saved.");
  $("budgetSet").onchange = () => save({ budget_usd: Number($("budgetSet").value) || 0 }, "Budget saved.");
}

/* ------------------------------------------------------------------ updates (app/updates.py)
   The app checks now and then; when there's something newer, it asks once (per version) and installs it with one
   click: it restarts in place, and this tab reloads itself once the new version answers. */
const UPDATE_SKIP = "decomp-wtf-skip-update";
let updateInfo = null;
const updateTarget = (u) => (u && u.latest ? u.latest.commit || u.latest.version || "" : "");
function updateName(u) {
  const l = u.latest || {}, c = u.current || {};
  if (c.channel === "branch") return `${c.branch} @ ${(l.commit || "").slice(0, 7)}${l.version && l.version !== c.version ? ` (${l.version})` : ""}`;
  return `decomp.wtf ${l.version}`;
}
async function checkUpdate(refresh = false, ask = true) {
  try { updateInfo = await api(`/api/update${refresh ? "?refresh=1" : ""}`); } catch { return null; }
  $("navUpdate").hidden = !updateInfo.available;
  const job = updateInfo.job || {};
  if (job.state === "installing" || job.state === "restarting") { showUpdating(); return updateInfo; }
  let skipped = "";
  try { skipped = localStorage.getItem(UPDATE_SKIP) || ""; } catch { /* storage off */ }
  if (ask && updateInfo.available && skipped !== updateTarget(updateInfo) && $("dialog").hidden && $("drawer").hidden) offerUpdate();
  return updateInfo;
}
function offerUpdate() {
  const u = updateInfo;
  if (!u) return;
  const c = u.current || {};
  const notes = (u.notes || []).map((n) => `<li>${esc(n)}</li>`).join("");
  const body = dialog("A new decomp.wtf is ready", `<p>${c.channel === "branch"
      ? `New on the <b>${esc(c.branch)}</b> branch: <code>${esc((u.latest.commit || "").slice(0, 7))}</code> (you have <code>${esc((c.commit || "?").slice(0, 7))}</code>).`
      : `decomp.wtf <b>${esc(u.latest.version)}</b> is out. You have ${esc(c.version)}.`}</p>
    ${notes ? `<ul class="up-notes">${notes}</ul>` : ""}
    ${u.link ? `<p class="small"><a class="link" href="${esc(u.link)}" target="_blank" rel="noopener">Every change</a></p>` : ""}
    <p class="muted small">${u.can_apply ? "One click: decomp.wtf installs it and restarts by itself, and this tab follows along. Helper agents pause for a moment and carry on." : esc(u.why_not)}</p>
    <div class="row"><button class="primary" id="upGo" type="button" ${u.can_apply ? "" : "disabled"}>Update now</button><button class="ghost" id="upLater" type="button">Later</button></div>`);
  body.querySelector("#upLater").onclick = () => {
    try { localStorage.setItem(UPDATE_SKIP, updateTarget(u)); } catch { /* storage off */ }
    closeAll();
  };
  body.querySelector("#upGo").onclick = async (e) => {
    e.target.disabled = true;
    try { await api("/api/update", { method: "POST", body: { apply: true } }); } catch (err) { e.target.disabled = false; return toast(esc(err.message), "err"); }
    showUpdating();
  };
}
async function showUpdating() {
  if (S.updating) return;
  S.updating = true;
  const was = { version: S.st ? S.st.version : "", build: S.st ? S.st.build || "" : "" };
  dialog("Updating decomp.wtf", `<p id="upStep">Installing the update…</p><pre class="joblog tall" id="upLog"></pre>
    <p class="muted small">This tab reloads by itself once the new version is running.</p>`);
  let away = 0;
  for (;;) {
    await sleep(1000);
    let u = null;
    try { u = await api("/api/update"); away = 0; } catch { away++; }
    const job = (u && u.job) || {};
    if (u && $("upLog")) { $("upLog").textContent = (job.log || []).join("\n"); $("upLog").scrollTop = $("upLog").scrollHeight; }
    if (job.state === "error") {
      S.updating = false;
      if ($("upStep")) $("upStep").textContent = `The update didn't go through: ${job.error}`;
      return;
    }
    if (job.state === "restarting" && $("upStep")) $("upStep").textContent = "Restarting…";
    try {
      const h = await (await fetch("/health", { cache: "no-store" })).json();
      if (h.version !== was.version || (h.build || "") !== was.build) { location.reload(); return; }
    } catch { /* not back yet */ }
    if (away > 180) {
      S.updating = false;
      if ($("upStep")) $("upStep").textContent = "decomp.wtf didn't come back after the update. Start it again (decomp.wtf in your apps, or the decomp command).";
      return;
    }
  }
}
$("navUpdate").onclick = () => (updateInfo && updateInfo.available ? offerUpdate() : checkUpdate(true, true));

$("quitBtn").onclick = async () => {
  if (!confirm("Quit decomp.wtf? Running agents stop.")) return;
  await api("/api/quit", { method: "POST" }).catch(() => {});
  document.body.innerHTML = `<main class="view"><p class="muted">decomp.wtf has stopped. You can close this tab.</p></main>`;
};

loadNav();
route();
setTimeout(() => checkUpdate(false, true), 4000);
setInterval(() => checkUpdate(false, true), 10 * 60 * 1000);
