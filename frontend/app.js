"use strict";

// ---- Theme (dark | light | system) -----------------------------------------
// Applied ASAP to avoid a flash; cycles on button click; persists choice.
const THEMES = ["dark", "light", "system"];
const THEME_ICON = { dark: "🌙", light: "☀️", system: "🖥️" };

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("cc_theme", theme);
  const btn = document.getElementById("themeBtn");
  if (btn) {
    btn.textContent = THEME_ICON[theme];
    btn.title = `Theme: ${theme} (click to cycle dark / light / system)`;
  }
}

function initTheme() {
  const saved = localStorage.getItem("cc_theme") || "dark";
  applyTheme(THEMES.includes(saved) ? saved : "dark");
  const btn = document.getElementById("themeBtn");
  if (btn) {
    btn.addEventListener("click", () => {
      const cur = localStorage.getItem("cc_theme") || "dark";
      applyTheme(THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length]);
    });
  }
}
// set the attribute immediately (before DOM/render) to prevent a flash
document.documentElement.setAttribute(
  "data-theme", localStorage.getItem("cc_theme") || "dark");

const state = {
  projects: [],
  activeProject: null,
  activeProjectMeta: null,
  sessions: [],
  activeSession: null,
  detailTab: "conversation",
  projView: "sessions", // sessions | tasks | memory | claude
  showPrompts: true,
  sortBy: "time", // time | name
  onlyMissing: false, // sidebar filter: only projects whose directory is gone
  convOffset: 0,
  convTotal: 0,
  agent: localStorage.getItem("cc_agent") || "",
  agents: [],
  caps: null,
  showSubagents: localStorage.getItem("cc_showSubagents") === "1",
  pendingView: null, // the project view whose request is in flight (loadProjView)
  convTurns: [], // turns currently loaded in the Conversation tab
  trajMode: localStorage.getItem("cc_trajMode") || "duration", // duration | wall | sequence
  trajRange: null, // active timeline filter range { start, end } in projected domain
  trajSearch: "", // filter query in the Trajectory tab
  trajFoldTurns: false, // whether all turns are folded in the Trajectory tab
  trajShowPhases: true, // whether model rows expand Thinking / Output sub-phase bars
};

const CONV_PAGE = 40;
const $ = (sel) => document.querySelector(sel);

// Error carrying the server's `detail` (e.g. the 401 hint about ?token=…).
async function httpError(r) {
  let detail = r.statusText;
  try { detail = (await r.json()).detail || detail; } catch { /* not JSON */ }
  return new Error(`${r.status} ${detail}`);
}

async function api(path) {
  const u = new URL(path, location.origin);
  if (!u.searchParams.has("agent")) u.searchParams.set("agent", state.agent);
  const r = await fetch(u);
  if (!r.ok) throw await httpError(r);
  return r.json();
}

// Escapes for both text and quoted-attribute contexts.
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function fmtTokens(n) {
  if (!n) return "0";
  if (n >= 1000000) return (n / 1000000).toFixed(2) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  if (diff < 604800) return Math.floor(diff / 86400) + "d ago";
  return d.toLocaleDateString();
}

function fmtDuration(ms) {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "";
  if (ms === 0) return "0ms";
  if (ms < 1000) return Math.round(ms) + "ms";
  const sec = ms / 1000;
  if (sec < 60) return (sec < 10 ? sec.toFixed(2) : sec.toFixed(1)) + "s";
  const m = Math.floor(sec / 60);
  const rem = Math.round(sec % 60);
  return rem ? `${m}m ${rem}s` : `${m}m`;
}

function fmtClockTime(ms) {
  if (ms == null || !Number.isFinite(ms) || ms <= 0) return "";
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return "";
  return d.toISOString().slice(11, 23);
}

function parseTsMs(ts) {
  if (ts == null || ts === "" || typeof ts === "boolean") return null;
  if (typeof ts === "number") {
    if (!Number.isFinite(ts) || ts <= 0) return null;
    return ts > 1e11 ? ts : ts * 1000;
  }
  const s = String(ts).trim();
  if (!s) return null;
  const num = Number(s);
  if (Number.isFinite(num) && num > 0) return num > 1e11 ? num : num * 1000;
  const parsed = Date.parse(s);
  return Number.isFinite(parsed) ? parsed : null;
}

function shortModel(m) {
  if (!m) return "";
  if (m.includes("opus")) return "opus";
  if (m.includes("sonnet")) return "sonnet";
  if (m.includes("haiku")) return "haiku";
  if (m.includes("synthetic")) return "synthetic";
  return m.slice(0, 12);
}

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------
function currentCaps() {
  return state.agents.find((a) => a.agent_id === state.agent) || null;
}

// Agents with data first (stable), so the dropdown leads with what's in use.
function orderAgents(agents) {
  const noData = (a) => (a.has_data === false ? 1 : 0);
  return [...agents].sort((a, b) => noData(a) - noData(b));
}

async function loadAgents() {
  try {
    const r = await fetch("/api/agents");
    if (!r.ok) throw await httpError(r);
    state.agents = orderAgents(await r.json());
  } catch (e) {
    if (e.message.startsWith("401")) throw e;  // not logged in: show the hint
    state.agents = [{ agent_id: "claude", label: "Claude", doc_filename: "CLAUDE.md",
      has_memory: true, has_tasks: true, can_edit_doc: true,
      can_delete: true, can_export: true }];
  }
  state.caps = currentCaps();
  if (!state.caps && state.agents.length) {
    const first = state.agents.find((a) => a.has_data !== false) || state.agents[0];
    state.agent = first.agent_id;
    state.caps = first;
  }
  const sel = $("#agentSel");
  sel.innerHTML = state.agents
    .map((a) => `<option value="${esc(a.agent_id)}">${esc(a.label)}${a.has_data === false ? " — no data" : ""}</option>`)
    .join("");
  sel.value = state.agent;
  sel.addEventListener("change", async () => {
    // A dialog belongs to the agent it was opened for: never let it act on another one.
    document.querySelectorAll(".modal-overlay").forEach((o) => o.remove());
    state.agent = sel.value;
    localStorage.setItem("cc_agent", state.agent);
    state.caps = currentCaps();
    state.activeProject = null;
    state.activeProjectMeta = null;
    state.activeSession = null;
    state.sessions = [];
    closeDetail();
    $("#projNav").hidden = true;
    $("#sessionPane").innerHTML =
      '<div class="empty">Select a project to view its sessions.</div>';
    updateSubLabel();
    await loadProjects();
  });
  updateSubLabel();
}

function updateSubLabel() {
  const label = (state.caps && state.caps.label) || "coding-agent";
  const el = $("#subLabel");
  if (el) el.textContent = `local ${label} project viewer`;
}

async function loadProjects() {
  state.projects = await api("/api/projects");
  if (state.activeProject) {
    const p = followProject(state.projects, state.activeProject, (state.activeProjectMeta || {}).cwd);
    if (!p) {
      deselectProject();  // re-renders the list
      return;
    }
    const renamed = p.name !== state.activeProject;
    state.activeProject = p.name;
    state.activeProjectMeta = p;
    renderProjNav();  // its counts come from the project row
    updateSearchPlaceholder();  // shows the project name
    // a view still loading under the old name will be dropped when it answers: ask again
    if (renamed && state.pendingView) loadProjView();
  }
  renderProjects();
}

// The row that is still the open project after the list reloads. Directories
// decide, since names can change (a directory that mangles alike appeared or
// went): the same name and directory, else the same directory under its new
// name; a project without a known directory goes by its name. null when the
// directory is gone, even if its old name now means another directory.
function followProject(projects, name, cwd) {
  if (!cwd) return projects.find((p) => p.name === name) || null;
  return projects.find((p) => p.name === name && p.cwd === cwd)
    || projects.find((p) => p.cwd === cwd) || null;
}

// Projects to show, filtered (only those whose directory is gone) and sorted.
function visibleProjects(projects, sortBy, onlyMissing) {
  const ps = projects.filter((p) => !onlyMissing || p.cwd_missing);
  if (sortBy === "name") {
    ps.sort((a, b) => a.name.localeCompare(b.name));
  } else {
    ps.sort((a, b) => b.mtime - a.mtime);
  }
  return ps;
}

function renderMissingFilter() {
  const btn = $("#missingFilter");
  const n = state.projects.filter((p) => p.cwd_missing).length;
  if (!n) state.onlyMissing = false;  // nothing left to filter: never strand the list empty
  btn.hidden = !n;
  btn.textContent = `⚠ missing ${n}`;
  btn.classList.toggle("active", state.onlyMissing);
}

function renderProjects() {
  const el = $("#projectList");
  renderMissingFilter();
  if (!state.projects.length) {
    el.innerHTML = '<div class="empty">No projects found.</div>';
    return;
  }
  const canDelete = Boolean(state.caps && state.caps.can_delete);
  el.innerHTML = visibleProjects(state.projects, state.sortBy, state.onlyMissing).map((p) => {
    const git = p.git
      ? `<span class="pchip git" title="git repository">⎇ ${esc(p.git.branch || p.git.repo)}</span>`
      : "";
    const mem = p.has_memory ? '<span class="pchip mem">◆ mem</span>' : "";
    const docName = (state.caps && state.caps.doc_filename) || "CLAUDE.md";
    const cmd = p.has_claude_md ? `<span class="pchip cmd">${esc(docName)}</span>` : "";
    const missing = p.cwd_missing
      ? `<span class="pchip missing" title="This working directory no longer exists: renamed, moved, deleted, or on another machine. Its sessions are still here, under the old path.">⚠ dir missing</span>`
      : "";
    let task = "";
    let prog = "";
    if (p.total_tasks) {
      if (p.open_tasks) {
        task = `<span class="pchip task-open">⏳ ${p.open_tasks}/${p.total_tasks}</span>`;
      } else {
        task = `<span class="pchip task-done">✓ ${p.total_tasks}</span>`;
      }
      const donePct = ((p.total_tasks - p.open_tasks) / p.total_tasks) * 100;
      prog = `<div class="pprog"><div class="pfill" style="width:${donePct}%"></div></div>`;
    }
    const del = canDelete
      ? `<button class="pdel" data-name="${esc(p.name)}" title="Delete this project's sessions…">🗑</button>`
      : "";
    return `
      <div class="project ${p.name === state.activeProject ? "active" : ""} ${p.cwd_missing ? "gone" : ""}" data-name="${esc(p.name)}">
        <div class="pname" title="${esc(p.display_path || p.name)}">${esc(p.display_path || p.name)}</div>
        <div class="ptime">${p.session_count} session${p.session_count === 1 ? "" : "s"} · ${fmtTime(p.mtime)}${del}</div>
        <div class="pbadges">${missing}${git}${mem}${cmd}${task}</div>
        ${prog}
      </div>`;
  }).join("") || '<div class="empty">No projects with a missing directory.</div>';
  el.querySelectorAll(".project").forEach((node) => {
    node.addEventListener("click", () => selectProject(node.dataset.name));
  });
  el.querySelectorAll(".pdel").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();  // don't also open the project
      openProjectDeleteModal(btn.dataset.name);
    });
  });
}

async function selectProject(name) {
  // clicking the already-active folder toggles it closed
  if (name === state.activeProject) {
    deselectProject();
    return;
  }
  state.activeProject = name;
  state.activeProjectMeta = state.projects.find((p) => p.name === name) || null;
  state.activeSession = null;
  state.projView = "sessions";
  closeDetail();
  renderProjects();
  renderProjNav();
  updateSearchPlaceholder();
  loadProjView();
}

function deselectProject() {
  state.activeProject = null;
  state.activeProjectMeta = null;
  state.activeSession = null;
  state.sessions = [];
  closeDetail();
  $("#projNav").hidden = true;
  $("#sessionPane").innerHTML = '<div class="empty">Select a project to view its sessions.</div>';
  renderProjects();
  updateSearchPlaceholder();
  // if scope was set to this folder, revert to all
  const sc = $("#searchScope");
  if (sc.value === "project") sc.value = "all";
}

function renderProjNav() {
  const nav = $("#projNav");
  if (!state.activeProject) { nav.hidden = true; return; }
  nav.hidden = false;
  const allowed = new Set(["sessions", "claude"]);
  if ((state.caps || {}).has_tasks) allowed.add("tasks");
  if ((state.caps || {}).has_memory) allowed.add("memory");
  if (!allowed.has(state.projView)) state.projView = "sessions";
  const m = state.activeProjectMeta || {};
  const taskCount = m.total_tasks ? `<span class="pn-count">${m.open_tasks}/${m.total_tasks}</span>` : "";
  const caps = state.caps || {};
  const docName = caps.doc_filename || "CLAUDE.md";
  const views = [
    { key: "sessions", label: "Sessions", count: `<span class="pn-count">${m.session_count || 0}</span>` },
  ];
  if (caps.has_tasks) views.push({ key: "tasks", label: "Tasks", count: taskCount });
  if (caps.has_memory) views.push({ key: "memory", label: "Memory", count: "" });
  views.push({ key: "claude", label: docName, count: "" });
  nav.innerHTML = views.map((v) =>
    `<button class="pnav-btn ${state.projView === v.key ? "active" : ""}" data-view="${v.key}">${v.label}${v.count}</button>`
  ).join("");
  nav.querySelectorAll(".pnav-btn").forEach((b) => {
    b.addEventListener("click", () => {
      state.projView = b.dataset.view;
      renderProjNav();
      loadProjView();
    });
  });
}

async function loadProjView() {
  const el = $("#sessionPane");
  el.innerHTML = '<div class="loading">loading…</div>';
  // What this view is for; a response for anything else (the user moved on
  // meanwhile) is dropped, so an editor never shows one project's file bound
  // to another.
  const view = { agent: state.agent, project: state.activeProject, kind: state.projView };
  state.pendingView = view;
  // only the latest request may render: an older one is stale even when the name it was
  // for comes back (hashed → plain → hashed)
  const current = () => state.pendingView === view && state.agent === view.agent
    && state.activeProject === view.project && state.projView === view.kind;
  const path = (what) => `/api/projects/${encodeURIComponent(view.project)}/${what}` +
    `?agent=${encodeURIComponent(view.agent)}`;
  try {
    if (view.kind === "sessions") {
      const sessions = await api(path("sessions"));
      if (!current()) return;
      state.sessions = sessions;
      renderSessions();
    } else if (view.kind === "tasks") {
      const tasks = await api(path("tasks"));
      if (current()) renderProjectTasks(el, tasks);
    } else if (view.kind === "memory") {
      const mem = await api(path("memory"));
      if (current()) renderMemory(el, mem, view);
    } else if (view.kind === "claude") {
      const cm = await api(path("doc"));
      if (current()) renderClaudeMd(el, cm, view);
    }
  } catch (e) {
    if (current()) el.innerHTML = `<div class="empty">Failed: ${esc(e.message)}</div>`;
  } finally {
    if (state.pendingView === view) state.pendingView = null;
  }
}

function ctxPct(s) {
  const limit = s.context_limit || 200000;
  return Math.min(100, (s.context_tokens / limit) * 100);
}

// The transcript records only the API model id (e.g. claude-opus-4-8), never the
// [1m] alias or the window size. So the window is only *known* when usage crossed
// 200k (then it must be the 1M tier). Otherwise we show tokens without claiming a
// window, to avoid presenting a guess as fact.
function ctxLabel(s) {
  const tokens = s.context_tokens || 0;
  if (s.context_limit_known) {
    const win = s.context_limit >= 1000000 ? "1M" : (s.context_limit / 1000) + "k";
    const pct = Math.min(100, (tokens / s.context_limit) * 100);
    return {
      known: true,
      windowLabel: `${win} window`,
      pctLabel: ` (${pct.toFixed(0)}%)`,
      title: `Peak observed context ${fmtTokens(tokens)} tokens. Window ${win} (proven: usage exceeded the 200k standard window).`,
    };
  }
  return {
    known: false,
    windowLabel: "window unknown",
    pctLabel: "",
    title: `Peak observed context ${fmtTokens(tokens)} tokens. The transcript does not record the model's context window; usage stayed under 200k so the window can't be determined (could be 200k or 1M).`,
  };
}

// Top-level sessions, plus (when shown) their subagent children depth-first.
function sessionTree(sessions, showSubagents) {
  const ids = new Set(sessions.map((s) => s.session_id));
  const isChild = (s) => Boolean(s.parent_id) && ids.has(s.parent_id);
  const kids = {};
  for (const s of sessions) if (isChild(s)) (kids[s.parent_id] = kids[s.parent_id] || []).push(s);
  const tops = sessions.filter((s) => !isChild(s));
  const visible = [];
  const seen = new Set();
  const pushTree = (s, depth) => {
    if (seen.has(s.session_id)) return;
    seen.add(s.session_id);
    visible.push({ s, depth });
    if (showSubagents) for (const k of kids[s.session_id] || []) pushTree(k, depth + 1);
  };
  for (const s of tops) pushTree(s, 0);
  return { visible, kids, topCount: tops.length, childTotal: sessions.length - tops.length };
}

function renderSessions() {
  const el = $("#sessionPane");
  if (!state.sessions.length) {
    el.innerHTML = '<div class="empty">No sessions in this project.</div>';
    return;
  }
  // Subagent (child) sessions hide under their parent unless toggled on.
  const { visible, kids, topCount, childTotal } = sessionTree(state.sessions, state.showSubagents);

  const rows = visible.map(({ s, depth }) => {
    const pct = ctxPct(s);
    const ctx = ctxLabel(s);
    const taskBadge = s.total_tasks
      ? (s.open_tasks
          ? `<span class="badge task-open">⏳ ${s.open_tasks}/${s.total_tasks} tasks</span>`
          : `<span class="badge task-done">✓ ${s.total_tasks} tasks</span>`)
      : "";
    const memBadge = s.has_memory ? '<span class="badge mem">◆ memory</span>' : "";
    const modelBadge = s.model ? `<span class="badge model">${esc(shortModel(s.model))}</span>` : "";
    const nKids = (kids[s.session_id] || []).length;
    const subBadge = nKids ? `<span class="badge sub" title="subagent sessions">⑂ ${nKids} subagent${nKids === 1 ? "" : "s"}</span>` : "";
    return `
      <div class="session ${s.session_id === state.activeSession ? "active" : ""} ${depth ? "child" : ""}" data-id="${esc(s.session_id)}" style="margin-left:${depth * 18}px">
        <div class="session-head">
          <span class="session-id" title="${esc(s.session_id)}">${depth ? "↳ " : ""}${esc(s.title || s.session_id.slice(0, 8))}</span>
          <span class="session-time">${fmtTime(s.mtime)}</span>
        </div>
        <div class="session-prompt ${state.showPrompts ? "" : "hidden"}">${esc(s.last_prompt || s.first_prompt || "(no prompt)")}</div>
        <div class="ctxbar">
          <div class="label" title="${esc(ctx.title)}"><span>context · ${ctx.windowLabel}</span><span>${fmtTokens(s.context_tokens)}${ctx.pctLabel}</span></div>
          <div class="track ${ctx.known ? "" : "unknown"}"><div class="fill" style="width:${pct}%"></div></div>
        </div>
        <div class="badges">
          <span class="badge">${s.user_turns}↗ ${s.assistant_turns}↙</span>
          ${modelBadge}${subBadge}${memBadge}${taskBadge}
        </div>
      </div>`;
  }).join("");
  const toggle = childTotal
    ? `<label class="subtoggle"><input type="checkbox" id="showSubagents" ${state.showSubagents ? "checked" : ""} /> show ${childTotal} subagent session${childTotal === 1 ? "" : "s"}</label>`
    : "";
  el.innerHTML = `<h2 class="pane-title">${topCount} sessions · newest first ${toggle}</h2>` + rows;
  el.querySelectorAll(".session").forEach((node) => {
    node.addEventListener("click", () => openSession(node.dataset.id));
  });
  const tg = $("#showSubagents");
  if (tg) {
    tg.addEventListener("change", () => {
      state.showSubagents = tg.checked;
      localStorage.setItem("cc_showSubagents", tg.checked ? "1" : "0");
      renderSessions();
    });
  }
}

// Aggregated task board across all sessions in the project
function renderProjectTasks(el, tasks) {
  if (!tasks.length) { el.innerHTML = '<div class="empty">No tasks in this project.</div>'; return; }
  const byCol = { pending: [], in_progress: [], completed: [] };
  for (const t of tasks) (byCol[t.status] || byCol.pending).push(t);
  const colHtml = KCOLS.map((c) => `
    <div class="kcol" data-status="${c.key}">
      <h4>${c.title} <span class="kcount">${byCol[c.key].length}</span></h4>
      <div class="kdrop" data-status="${c.key}">
        ${byCol[c.key].map((t) => `
          <div class="kcard" draggable="true" data-id="${esc(t.id)}" data-sid="${esc(t.session_id)}">
            <div class="ksub">${esc(t.subject || "(untitled)")}</div>
            <div class="kowner">${t.owner ? "@" + esc(t.owner) + " · " : ""}${esc(t.session_id.slice(0,8))}</div>
          </div>`).join("")}
      </div>
    </div>`).join("");
  el.innerHTML = `<h2 class="pane-title">${tasks.length} tasks across ${new Set(tasks.map(t=>t.session_id)).size} sessions</h2>
    <div class="kanban">${colHtml}</div><div class="khint">Drag to update status — writes back to each task's file.</div>`;
  wireKanban(el, { perCard: true });
}

// view: the {agent, project} the document was loaded for; saving goes there,
// with the directory shown here, so the server refuses (409) if the project
// means another directory by now.
function renderClaudeMd(el, cm, view) {
  const docName = (state.caps && state.caps.doc_filename) || "CLAUDE.md";
  if (!cm.cwd) {
    el.innerHTML = '<div class="empty">No working directory known for this project (no session has a cwd yet).</div>';
    return;
  }
  if (state.caps && state.caps.can_edit_doc === false) {
    el.innerHTML = `
      <div class="editor-wrap">
        <div class="editor-head">
          <strong>${esc(docName)}</strong>
          <span class="ehpath">${esc(cm.path)}</span>
        </div>
        ${cm.exists ? `<pre class="editor readonly">${esc(cm.content)}</pre>`
                    : `<div class="empty">${esc(docName)} does not exist.</div>`}
        <div class="editor-actions">
          <span class="editor-status">Read-only for ${esc(state.caps.label)}.</span>
        </div>
      </div>`;
    return;
  }
  const status = cm.exists ? "" : `${docName} does not exist yet — saving will create it.`;
  el.innerHTML = `
    <div class="editor-wrap">
      <div class="editor-head">
        <strong>${esc(docName)}</strong>
        <span class="ehpath">${esc(cm.path)}</span>
      </div>
      <textarea class="editor" id="cmEditor" spellcheck="false">${esc(cm.content)}</textarea>
      <div class="editor-actions">
        <button class="dbtn" id="cmSave">Save</button>
        <span class="editor-status" id="cmStatus">${esc(status)}</span>
      </div>
    </div>`;
  // The elements this editor was rendered with: the pane may show something else by the
  // time a save finishes.
  const editor = el.querySelector("#cmEditor");
  const statusEl = el.querySelector("#cmStatus");
  el.querySelector("#cmSave").addEventListener("click", async () => {
    statusEl.textContent = "saving…";
    try {
      const u = new URL(`/api/projects/${encodeURIComponent(view.project)}/doc`, location.origin);
      u.searchParams.set("agent", view.agent);
      const r = await fetch(u, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: editor.value, expect_cwd: cm.cwd }),
      });
      if (!r.ok) throw await httpError(r);
      const res = await r.json();
      statusEl.textContent = "saved ✓";
      flash(docName + " saved to " + res.saved);
    } catch (e) {
      statusEl.textContent = "failed: " + e.message;
    }
  });
}

// ---------------------------------------------------------------------------
// Detail pane (per-session: conversation / tasks / memory)
// ---------------------------------------------------------------------------
async function openSession(id, targetSeq) {
  state.activeSession = id;
  state.detailTab = "conversation";
  state.targetSeq = (typeof targetSeq === "number") ? targetSeq : null;
  if (state.projView === "sessions") renderSessions();
  $("#layout").classList.add("detail-open");
  $("#splitter2").hidden = false;
  const pane = $("#detailPane");
  pane.hidden = false;
  pane.innerHTML = '<div class="loading">loading…</div>';
  renderDetailShell();
  loadTab();
}

function renderDetailShell() {
  const s = state.sessions.find((x) => x.session_id === state.activeSession);
  const caps = state.caps || {};
  // if the active tab isn't supported by this agent, fall back to conversation
  if ((state.detailTab === "tasks" && !caps.has_tasks) ||
      (state.detailTab === "memory" && !caps.has_memory)) {
    state.detailTab = "conversation";
  }
  const pane = $("#detailPane");
  pane.innerHTML = `
    <div class="detail-head">
      <span class="dclose" id="dclose">×</span>
      <div class="dtitle" title="${esc(s ? s.session_id : '')}">${esc(s ? (s.title || s.session_id.slice(0, 8)) : "")}</div>
      <div class="dmeta">${esc(s ? (s.git_branch || "") : "")} ${s ? fmtTokens(s.context_tokens) + " tok" : ""}</div>
      <div class="dactions">
        ${caps.can_export ? '<button class="dbtn" id="exportBtn">⬇ Export .md</button>' : ""}
        ${caps.can_delete ? '<button class="dbtn danger" id="deleteBtn">🗑 Delete</button>' : ""}
      </div>
    </div>
    <div class="tabs">
      <div class="tab ${state.detailTab === "conversation" ? "active" : ""}" data-tab="conversation">Conversation</div>
      <div class="tab ${state.detailTab === "trajectory" ? "active" : ""}" data-tab="trajectory">Trajectory</div>
      ${caps.has_tasks ? `<div class="tab ${state.detailTab === "tasks" ? "active" : ""}" data-tab="tasks">Tasks${s && s.total_tasks ? ` (${s.total_tasks})` : ""}</div>` : ""}
      ${caps.has_memory ? `<div class="tab ${state.detailTab === "memory" ? "active" : ""}" data-tab="memory">Memory</div>` : ""}
    </div>
    <div id="tabBody"></div>`;
  $("#dclose").addEventListener("click", closeDetail);
  const exportBtn = $("#exportBtn");
  if (exportBtn) exportBtn.addEventListener("click", exportSession);
  const deleteBtn = $("#deleteBtn");
  if (deleteBtn) deleteBtn.addEventListener("click", openDeleteModal);
  pane.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => { state.detailTab = t.dataset.tab; renderDetailShell(); loadTab(); });
  });
}

function exportSession() {
  const url = `/api/projects/${encodeURIComponent(state.activeProject)}/sessions/${encodeURIComponent(state.activeSession)}/export?agent=${encodeURIComponent(state.agent)}`;
  const a = document.createElement("a");
  a.href = url;
  a.download = `${state.activeSession.slice(0, 8)}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function openDeleteModal() {
  const sid = state.activeSession.slice(0, 8);
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal">
      <h3>Delete session ${esc(sid)}?</h3>
      <p>This removes the transcript, sidecar files, and tasks. By default it
         is a <b>soft delete</b> (moved to a trash folder, reversible).</p>
      ${(state.caps || {}).delete_note ? `<p>${esc(state.caps.delete_note)}</p>` : ""}
      <label class="mrow"><input type="checkbox" id="mExport" checked /> Export to Markdown first</label>
      ${(state.caps || {}).has_memory ? '<label class="mrow"><input type="checkbox" id="mMemory" /> Save a summary to project memory</label>' : ""}
      <label class="mrow"><input type="checkbox" id="mHard" /> Permanent delete (skip trash)</label>
      <div class="modal-actions">
        <button class="dbtn" id="mCancel">Cancel</button>
        <button class="dbtn danger" id="mConfirm">Delete</button>
      </div>
      <div class="modal-status" id="mStatus"></div>
    </div>`;
  document.body.appendChild(overlay);
  // Bound now: the dialog acts on exactly the session it was opened for.
  const agent = state.agent;
  const project = state.activeProject;
  const session = state.activeSession;
  const close = () => overlay.remove();
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  // this dialog's own elements: switching agents may remove it mid-request
  const q = (sel) => overlay.querySelector(sel);
  const confirmBtn = q("#mConfirm");
  const statusEl = q("#mStatus");
  q("#mCancel").addEventListener("click", close);
  confirmBtn.addEventListener("click", async () => {
    const exportFirst = q("#mExport").checked;
    const saveMemory = Boolean(q("#mMemory") && q("#mMemory").checked);
    const hard = q("#mHard").checked;
    confirmBtn.disabled = true;  // one request at a time
    statusEl.textContent = "deleting…";
    try {
      const du = new URL(`/api/projects/${encodeURIComponent(project)}/sessions/${encodeURIComponent(session)}/delete`,
                         location.origin);
      du.searchParams.set("agent", agent);
      const r = await fetch(du, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ export_first: exportFirst, save_memory: saveMemory, hard }),
      });
      if (!r.ok) throw await httpError(r);
      const res = await r.json();
      close();
      if (state.agent === agent && state.activeProject === project) {
        if (state.activeSession === session) closeDetail();
        state.sessions = await api(`/api/projects/${encodeURIComponent(project)}/sessions`);
        renderSessions();
      }
      const note = res.export ? ` Exported.` : "";
      const mem = res.memory ? ` Saved to memory.` : "";
      const where = res.note ? ` ${res.note}`
        : (res.trash ? " Moved to trash." : " Permanently removed.");
      const warn = res.warnings && res.warnings.length ? ` Warning: ${res.warnings.join("; ")}` : "";
      flash(`Session deleted.${note}${mem}${where}${warn}`);
    } catch (e) {
      statusEl.textContent = "Failed: " + e.message;
      confirmBtn.disabled = false;
    }
  });
}

function flash(msg) {
  const f = document.createElement("div");
  f.className = "flash";
  f.textContent = msg;
  document.body.appendChild(f);
  setTimeout(() => f.remove(), msg.length > 160 ? 12000 : 6000);
}

// ---------------------------------------------------------------------------
// Project delete (every session of a project)
// ---------------------------------------------------------------------------

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

// "3 sessions (+2 subagent sessions) · 1 not listed (empty or archived) · 4 memory files"
function projectDeleteSummary(pv) {
  let head = plural(pv.sessions || 0, "session");
  if (pv.subagents) head += ` (+${plural(pv.subagents, "subagent session")})`;
  const parts = [head];
  if (pv.hidden) parts.push(`${pv.hidden} not listed (empty or archived)`);
  if (pv.memory_files) parts.push(plural(pv.memory_files, "memory file"));
  return parts.join(" · ");
}

// What the user types to confirm a permanent delete: the folder's own name.
function confirmWord(p) {
  const path = String(p.display_path || p.name).replace(/[\\/]+$/, "");
  return path.split(/[\\/]/).pop() || p.name;
}

function projectDeleteResultText(res, hard) {
  const n = (res.sessions || []).length;
  const trash = res.trash || [];
  let msg = hard ? `Permanently deleted ${plural(n, "session")}.`
    : `Removed ${plural(n, "session")} (reversible${trash.length === 1 ? `: ${trash[0]}`
      : trash.length ? `: ${trash.length} trash folders` : ""}).`;
  if (res.exported && res.exported.length) msg += ` Exported ${res.exported.length} to Markdown first.`;
  if ((res.kept || []).includes("memory")) msg += " The project memory was kept.";
  const errors = Object.entries(res.errors || {});
  if (errors.length) {
    msg += ` Not deleted (${errors.length}): ` +
      errors.map(([sid, m]) => `${sid.slice(0, 8)}: ${m}`).join("; ");
  }
  if (res.warnings && res.warnings.length) msg += ` Warning: ${res.warnings.join("; ")}`;
  return msg;
}

async function openProjectDeleteModal(name) {
  const p = state.projects.find((x) => x.name === name);
  if (!p) return;
  // Bound now: the preview and the delete are for this agent's project, whatever
  // the agent dropdown says later (switching agents also closes the dialog).
  const agent = state.agent;
  const caps = state.caps || {};
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal">
      <h3>Delete this project's sessions?</h3>
      <p class="mpath">${esc(p.display_path || p.name)}</p>
      <div id="pdBody"><div class="loading">checking…</div></div>
    </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  const body = overlay.querySelector("#pdBody");
  let pv;
  try {
    pv = await api(`/api/projects/${encodeURIComponent(name)}/delete-preview?agent=${encodeURIComponent(agent)}`);
  } catch (e) {
    body.innerHTML = `<p class="mwarn">${esc(e.message)}</p>
      <div class="modal-actions"><button class="dbtn" id="pdClose">Close</button></div>`;
    overlay.querySelector("#pdClose").addEventListener("click", close);
    return;
  }
  const word = confirmWord(p);
  body.innerHTML = `
    ${p.cwd_missing ? `<p class="mwarn">⚠ Its working directory no longer exists (renamed, moved,
       or on another machine). The sessions below are still stored under the old path.</p>` : ""}
    <p>${esc(projectDeleteSummary(pv))}${pv.newest ? ` · last active ${esc(fmtTime(pv.newest))}` : ""}</p>
    <p>All of them are removed; by default it is a <b>soft delete</b> (reversible).
       Files in the working directory itself (e.g. ${esc(caps.doc_filename || "CLAUDE.md")}) are never touched.</p>
    ${caps.delete_note ? `<p>${esc(caps.delete_note)}</p>` : ""}
    ${caps.can_export ? '<label class="mrow"><input type="checkbox" id="pdExport" checked /> Export every session to Markdown first</label>' : ""}
    ${pv.memory_files ? `<label class="mrow"><input type="checkbox" id="pdMemory" /> Also delete the project memory (${plural(pv.memory_files, "file")})</label>
       <p class="mhint">Unticked, the memory stays and the project remains listed with its memory only.</p>` : ""}
    <label class="mrow"><input type="checkbox" id="pdHard" ${pv.outside ? "disabled" : ""} /> Permanent delete (skip trash / archive)</label>
    ${pv.outside ? `<p class="mhint">Not available: it would also delete ${plural(pv.outside, "subagent session")}
       stored under other directories.</p>` : ""}
    <div class="mrow" id="pdTypeRow" hidden>Type <b>${esc(word)}</b> to confirm:
      <input type="text" id="pdType" class="mtext" autocomplete="off" /></div>
    <div class="modal-actions">
      <button class="dbtn" id="pdCancel">Cancel</button>
      <button class="dbtn danger" id="pdConfirm"></button>
    </div>
    <div class="modal-status" id="pdStatus"></div>`;
  const hardBox = body.querySelector("#pdHard");
  const memBox = body.querySelector("#pdMemory");
  const typed = body.querySelector("#pdType");
  const confirmBtn = body.querySelector("#pdConfirm");
  const total = (pv.sessions || 0) + (pv.subagents || 0);
  const what = total ? plural(total, "session")
    : (pv.hidden ? `${pv.hidden} unlisted` : "the project");
  let busy = false;  // a request is running: nothing may re-enable the button
  let stale = false;  // the server says the project changed since this dialog opened
  const sync = () => {
    const hard = hardBox.checked;
    body.querySelector("#pdTypeRow").hidden = !hard;
    body.querySelectorAll("input").forEach((i) => {
      i.disabled = busy || stale || (i === hardBox && Boolean(pv.outside));
    });
    // A project whose only content is memory that is being kept: nothing would change.
    const nothing = !total && !pv.hidden && pv.memory_files && !(memBox && memBox.checked);
    confirmBtn.textContent = nothing ? "Nothing to remove unless the memory goes too"
      : (hard ? `Delete ${what} permanently` : `Remove ${what}`);
    confirmBtn.disabled = busy || stale || Boolean(nothing) || (hard && typed.value.trim() !== word);
  };
  hardBox.addEventListener("change", sync);
  if (memBox) memBox.addEventListener("change", sync);
  typed.addEventListener("input", sync);
  sync();
  body.querySelector("#pdCancel").addEventListener("click", close);
  confirmBtn.addEventListener("click", async () => {
    if (busy || stale) return;
    const hard = hardBox.checked;
    const exportFirst = Boolean(body.querySelector("#pdExport") && body.querySelector("#pdExport").checked);
    const includeMemory = Boolean(memBox && memBox.checked);
    busy = true;
    sync();
    body.querySelector("#pdStatus").textContent =
      "deleting… (agents that go through their own CLI take about a second per session)";
    try {
      const u = new URL(`/api/projects/${encodeURIComponent(name)}/delete`, location.origin);
      u.searchParams.set("agent", agent);
      const r = await fetch(u, {
        method: "POST", headers: { "Content-Type": "application/json" },
        // the directory this dialog showed: the server refuses if the name means another one now
        body: JSON.stringify({ export_first: exportFirst, include_memory: includeMemory, hard,
                               expect_cwd: p.cwd || "" }),
      });
      if (!r.ok) {
        const err = await httpError(r);
        if (r.status === 409) {
          stale = true;
          if (state.agent === agent) await loadProjects().catch(() => {});
        }
        throw err;
      }
      const res = await r.json();
      close();
      if (state.agent === agent) {
        if (state.activeProject === name) deselectProject();
        await loadProjects();
      }
      flash(projectDeleteResultText(res, hard));
    } catch (e) {
      body.querySelector("#pdStatus").textContent = "Failed: " + e.message +
        (stale ? " The list has been refreshed: open this dialog again from it." : "");
      busy = false;
      sync();
    }
  });
}

function closeDetail() {
  state.activeSession = null;
  $("#layout").classList.remove("detail-open");
  $("#splitter2").hidden = true;
  const pane = $("#detailPane");
  pane.hidden = true;
  pane.innerHTML = "";
  if (state.projView === "sessions") renderSessions();
}

async function loadTab() {
  const body = $("#tabBody");
  if (!body) return;
  body.innerHTML = '<div class="loading">loading…</div>';
  try {
    if (state.detailTab === "conversation") {
      state.convOffset = 0;
      state.convTotal = 0;
      state.convTurns = [];
      state.trajRange = null;
      // If navigating to a specific turn, load enough to include it in one go.
      const need = (typeof state.targetSeq === "number")
        ? Math.max(CONV_PAGE, state.targetSeq + 5) : CONV_PAGE;
      const data = await api(`/api/projects/${encodeURIComponent(state.activeProject)}/sessions/${encodeURIComponent(state.activeSession)}?offset=0&limit=${need}`);
      state.convTotal = data.total;
      state.convOffset = data.turns.length;
      state.convTurns = data.turns.slice();
      renderConversation(body, data.turns, true);
      if (typeof state.targetSeq === "number") {
        scrollToTurn(state.targetSeq);
        state.targetSeq = null;
      }
    } else if (state.detailTab === "trajectory") {
      state.trajRange = null;
      const data = await api(`/api/projects/${encodeURIComponent(state.activeProject)}/sessions/${encodeURIComponent(state.activeSession)}?offset=0&limit=0`);
      state.convTurns = data.turns || [];
      renderTrajectoryView(body, state.convTurns);
    } else if (state.detailTab === "tasks") {
      const tasks = await api(`/api/sessions/${encodeURIComponent(state.activeSession)}/tasks`);
      renderTasks(body, tasks);
    } else if (state.detailTab === "memory") {
      const mem = await api(`/api/projects/${encodeURIComponent(state.activeProject)}/memory`);
      renderMemory(body, mem, false);
    }
  } catch (e) {
    body.innerHTML = `<div class="empty">Failed to load: ${esc(e.message)}</div>`;
  }
}

function turnPreview(turn) {
  for (const b of turn.blocks) if (b.type === "text" && b.text) return b.text.slice(0, 120);
  for (const b of turn.blocks) {
    if (b.type === "thinking" && b.text) return "(thinking) " + b.text.slice(0, 100);
    if (b.type === "tool_use") return `→ ${b.name}`;
    if (b.type === "tool_result") return "(tool result)";
  }
  return "(empty)";
}

function turnTimingBadges(t) {
  const tm = t.timing || {};
  const roleLabel = t.kind === "tool" ? "tool" : t.role;
  const badges = [];
  if (roleLabel === "assistant") {
    if (tm.thinking_ms != null && tm.thinking_ms > 0) {
      badges.push(`<span class="turn-time-pill thinking" title="Model thinking time">${esc(fmtDuration(tm.thinking_ms))} think</span>`);
    }
    if (tm.output_ms != null && tm.output_ms > 0) {
      badges.push(`<span class="turn-time-pill output" title="Model output time">${esc(fmtDuration(tm.output_ms))} out</span>`);
    }
    if (tm.tool_ms != null && tm.tool_ms > 0) {
      badges.push(`<span class="turn-time-pill tool" title="Inline tool execution time">${esc(fmtDuration(tm.tool_ms))} tool</span>`);
    }
    if (tm.duration_ms != null && tm.duration_ms > 0 && badges.length === 0) {
      badges.push(`<span class="turn-time-pill total" title="Total turn duration">${esc(fmtDuration(tm.duration_ms))}</span>`);
    }
  } else if (roleLabel === "tool") {
    const tools = (tm.tools && tm.tools.length) ? tm.tools.join(", ") : "";
    const dur = tm.tool_ms != null ? tm.tool_ms : tm.duration_ms;
    if (tools || (dur != null && dur >= 0)) {
      const label = [tools, dur != null ? fmtDuration(dur) : ""].filter(Boolean).join(" · ");
      if (label) badges.push(`<span class="turn-time-pill tool" title="Tool calling duration">${esc(label)}</span>`);
    }
  } else if (roleLabel === "user") {
    const clk = fmtClockTime(tm.started_at != null ? tm.started_at : parseTsMs(t.timestamp));
    if (clk) badges.push(`<span class="turn-time-pill clock" title="User input timestamp">${esc(clk.slice(0, 8))}</span>`);
  }
  return badges.join("");
}

function turnHtml(t, seq) {
  const roleLabel = t.kind === "tool" ? "tool" : t.role;
  const toks = t.output_tokens ? `<span class="turn-toks">${fmtTokens(t.output_tokens)} tok</span>` : "";
  const skill = t.attribution_skill ? ` · ${esc(t.attribution_skill)}` : "";
  const timingHtml = turnTimingBadges(t);
  const blocks = t.blocks.map(renderBlock).join("");
  const openByDefault = t.role === "user" && t.kind !== "tool";
  return `
    <div class="turn ${roleLabel} ${openByDefault ? "open" : ""}" data-seq="${seq}">
      <div class="turn-head">
        <span class="turn-caret">▶</span>
        <span class="turn-role">${esc(roleLabel)}${skill}</span>
        <span class="turn-preview">${esc(turnPreview(t))}</span>
        ${timingHtml}${toks}
      </div>
      <div class="turn-body">${blocks}</div>
    </div>`;
}

function scrollToTurn(seq) {
  const node = document.querySelector(`.turn[data-seq="${seq}"]`);
  if (!node) return;
  node.hidden = false;
  node.classList.add("open", "turn-highlight");
  node.scrollIntoView({ behavior: "smooth", block: "center" });
  setTimeout(() => node.classList.remove("turn-highlight"), 2600);
}

function wireTurns(scope) {
  scope.querySelectorAll(".turn-head").forEach((h) => {
    if (h.dataset.wired) return;
    h.dataset.wired = "1";
    h.addEventListener("click", () => h.parentElement.classList.toggle("open"));
  });
}

function loadMoreBar() {
  const remaining = state.convTotal - state.convOffset;
  if (remaining <= 0) return "";
  return `<button class="loadmore" id="loadMore">Load more — ${remaining} of ${state.convTotal} remaining</button>`;
}

function renderConversation(body, turns, fresh) {
  if (fresh && !turns.length) { body.innerHTML = '<div class="empty">No turns.</div>'; return; }
  // seq is the absolute index; on append, it starts after already-rendered turns.
  const base = fresh ? 0 : (state.convOffset - turns.length);
  const turnsHtml = turns.map((t, i) => turnHtml(t, base + i)).join("");
  if (fresh) {
    body.innerHTML = `<div id="convTrajWrap"></div><div id="convFilterBanner" hidden></div><div class="turns" id="turnsWrap">${turnsHtml}</div><div id="loadMoreWrap">${loadMoreBar()}</div>`;
  } else {
    $("#turnsWrap").insertAdjacentHTML("beforeend", turnsHtml);
    $("#loadMoreWrap").innerHTML = loadMoreBar();
  }
  renderConversationTrajectory();
  wireTurns(body);
  const btn = $("#loadMore");
  if (btn) btn.addEventListener("click", loadMoreTurns);
}

async function loadMoreTurns() {
  const btn = $("#loadMore");
  if (btn) btn.textContent = "loading…";
  try {
    const data = await api(`/api/projects/${encodeURIComponent(state.activeProject)}/sessions/${encodeURIComponent(state.activeSession)}?offset=${state.convOffset}&limit=${CONV_PAGE}`);
    state.convTotal = data.total;
    state.convOffset += data.turns.length;
    state.convTurns = state.convTurns.concat(data.turns);
    renderConversation($("#tabBody"), data.turns, false);
  } catch (e) {
    if (btn) btn.textContent = `Load more failed (${e.message}) — click to retry`;
  }
}

// ---------------------------------------------------------------------------
// Trajectory & Timing Visualization (deepseek-harness inspired)
// ---------------------------------------------------------------------------

// Derive 3-lane trajectory spans (Input / Model [Thinking + Output] / Tools),
// turn-grouped rounds, and aggregate timing metrics from a conversation's turns.
function deriveTrajectory(turns) {
  const list = Array.isArray(turns) ? turns : [];
  const spans = [];
  const roundsMap = new Map();
  let turnNum = 0;
  let index = 0;
  let prevEnd = null;
  let lastToolNames = [];

  let userTurns = 0;
  let modelTurns = 0;
  let toolCalls = 0;
  let thinkingTotal = 0;
  let outputTotal = 0;
  let toolTotal = 0;
  let minTs = null;
  let maxTs = null;

  const noteTs = (v) => {
    if (v == null || !Number.isFinite(v)) return;
    minTs = minTs == null ? v : Math.min(minTs, v);
    maxTs = maxTs == null ? v : Math.max(maxTs, v);
  };

  const ensureRound = (num) => {
    if (!roundsMap.has(num)) {
      roundsMap.set(num, {
        turn: num,
        startedAt: null,
        endedAt: null,
        durationMs: 0,
        thinkingMs: 0,
        outputMs: 0,
        toolMs: 0,
        tools: {},
        items: [],
      });
    }
    return roundsMap.get(num);
  };

  for (let seq = 0; seq < list.length; seq++) {
    const t = list[seq] || {};
    const role = t.role || "user";
    const kind = t.kind || role;
    const blocks = Array.isArray(t.blocks) ? t.blocks : [];
    const tm = t.timing || {};

    let thinkChars = 0;
    let outChars = 0;
    let thinkPreview = "";
    let outPreview = "";
    const ownTools = [];
    let isError = false;
    let hasInlineToolResult = false;

    for (const b of blocks) {
      if (!b || typeof b !== "object") continue;
      if (b.type === "thinking") {
        const txt = String(b.text || "");
        thinkChars += txt.length;
        if (!thinkPreview && txt.trim()) thinkPreview = txt.trim().slice(0, 140);
      } else if (b.type === "text") {
        const txt = String(b.text || "");
        outChars += txt.length;
        if (!outPreview && txt.trim()) outPreview = txt.trim().slice(0, 140);
        if (/^(\[error\]|error:)/i.test(txt.trim())) isError = true;
      } else if (b.type === "tool_use") {
        const name = String(b.name || "tool");
        ownTools.push(name);
        let inpStr = "";
        try { inpStr = b.input ? JSON.stringify(b.input) : ""; } catch { inpStr = ""; }
        outChars += name.length + inpStr.length;
        if (!outPreview) outPreview = `→ ${name}(${inpStr.slice(0, 80)})`;
      } else if (b.type === "tool_result") {
        hasInlineToolResult = true;
        const txt = String(b.text || "").trim();
        if (/^(\[error\]|error:)/i.test(txt)) isError = true;
        if (!outPreview && txt) outPreview = txt.slice(0, 140);
      }
    }

    const tsMs = parseTsMs(t.timestamp);
    let startedAt = tm.started_at != null ? Number(tm.started_at) : null;
    let endedAt = tm.ended_at != null ? Number(tm.ended_at) : null;
    let durationMs = tm.duration_ms != null ? Number(tm.duration_ms) : null;

    if (startedAt == null || endedAt == null) {
      if (kind === "user") {
        startedAt = tsMs;
        endedAt = tsMs;
        durationMs = tsMs != null ? 0 : null;
      } else if (tsMs != null && prevEnd != null && tsMs >= prevEnd && (tsMs - prevEnd) <= 1800000) {
        startedAt = prevEnd;
        endedAt = tsMs;
        if (durationMs == null) durationMs = tsMs - prevEnd;
      } else {
        startedAt = tsMs;
        endedAt = tsMs;
      }
    }
    if (endedAt != null) prevEnd = endedAt;
    else if (tsMs != null) prevEnd = tsMs;
    noteTs(startedAt);
    noteTs(endedAt);

    if (kind === "user") {
      turnNum += 1;
      userTurns += 1;
      lastToolNames = [];
      const r = ensureRound(turnNum);
      if (startedAt != null && (r.startedAt == null || startedAt < r.startedAt)) r.startedAt = startedAt;
      if (endedAt != null && (r.endedAt == null || endedAt > r.endedAt)) r.endedAt = endedAt;
      const item = {
        index: ++index,
        seq,
        turn: turnNum,
        lane: 0,
        kind: "user",
        label: outPreview || turnPreview(t),
        tools: [],
        startedAt,
        endedAt,
        durationMs: 0,
        thinkingMs: 0,
        outputMs: 0,
        toolMs: 0,
        thinkingPct: 0,
        hasThinking: false,
        hasOutput: true,
        thinkingPreview: "",
        outputPreview: outPreview,
        outputTokens: 0,
        model: "",
        isError: false,
        splitSource: null,
      };
      spans.push(item);
      r.items.push(item);
      continue;
    }

    const curTurn = Math.max(1, turnNum || 1);
    if (turnNum === 0) turnNum = 1;
    const r = ensureRound(curTurn);
    if (startedAt != null && (r.startedAt == null || startedAt < r.startedAt)) r.startedAt = startedAt;
    if (endedAt != null && (r.endedAt == null || endedAt > r.endedAt)) r.endedAt = endedAt;

    if (kind === "tool") {
      toolCalls += 1;
      const tools = (Array.isArray(tm.tools) && tm.tools.length)
        ? tm.tools : (ownTools.length ? ownTools : lastToolNames.slice());
      if (ownTools.length) lastToolNames = ownTools;
      const toolMs = tm.tool_ms != null ? Number(tm.tool_ms) : (durationMs != null ? durationMs : null);
      if (toolMs != null && toolMs > 0) {
        toolTotal += toolMs;
        r.toolMs += toolMs;
        r.durationMs += toolMs;
      }
      for (const tn of tools) r.tools[tn] = (r.tools[tn] || 0) + 1;
      const toolLabel = tools.length ? tools.join(", ") : "tool";
      const item = {
        index: ++index,
        seq,
        turn: curTurn,
        lane: 2,
        kind: "tool",
        label: outPreview ? `${toolLabel} — ${outPreview}` : toolLabel,
        tools,
        startedAt,
        endedAt,
        durationMs: toolMs,
        thinkingMs: 0,
        outputMs: 0,
        toolMs,
        thinkingPct: 0,
        hasThinking: false,
        hasOutput: Boolean(outPreview),
        thinkingPreview: "",
        outputPreview: outPreview || turnPreview(t),
        outputTokens: 0,
        model: t.model || "",
        isError,
        splitSource: tm.split_source || (toolMs != null ? "recorded" : null),
      };
      spans.push(item);
      r.items.push(item);
      continue;
    }

    if (role === "assistant") {
      modelTurns += 1;
      const tools = (Array.isArray(tm.tools) && tm.tools.length) ? tm.tools : ownTools;
      if (tools.length) lastToolNames = tools;
      const hasThinking = thinkChars > 0;
      const hasOutput = outChars > 0 || ownTools.length > 0;
      const charRatio = (hasThinking && hasOutput)
        ? (thinkChars / Math.max(1, thinkChars + outChars))
        : (hasThinking ? 1 : 0);

      let toolMs = tm.tool_ms != null ? Number(tm.tool_ms) : null;
      let modelDur = durationMs != null ? Math.max(0, durationMs - (toolMs || 0)) : null;
      let thinkingMs = tm.thinking_ms != null ? Number(tm.thinking_ms) : null;
      let outputMs = tm.output_ms != null ? Number(tm.output_ms) : null;
      let splitSource = tm.split_source || null;

      if ((thinkingMs == null || outputMs == null) && modelDur != null) {
        thinkingMs = Math.round(modelDur * charRatio * 10) / 10;
        outputMs = Math.round(Math.max(0, modelDur - thinkingMs) * 10) / 10;
        splitSource = (hasThinking && hasOutput) ? "estimated" : "recorded";
      } else if (thinkingMs != null && outputMs != null && modelDur == null) {
        modelDur = thinkingMs + outputMs;
      }

      if (thinkingMs != null && thinkingMs > 0) {
        thinkingTotal += thinkingMs;
        r.thinkingMs += thinkingMs;
      }
      if (outputMs != null && outputMs > 0) {
        outputTotal += outputMs;
        r.outputMs += outputMs;
      }
      if (modelDur != null && modelDur > 0) {
        r.durationMs += modelDur;
      }

      const thinkingPct = (modelDur != null && modelDur > 0 && thinkingMs != null)
        ? Math.min(100, Math.max(0, (thinkingMs / modelDur) * 100))
        : (charRatio * 100);

      const modelEndedAt = (startedAt != null && modelDur != null && toolMs != null && toolMs > 0)
        ? (startedAt + modelDur) : endedAt;

      const modelItem = {
        index: ++index,
        seq,
        turn: curTurn,
        lane: 1,
        kind: "model",
        label: outPreview || thinkPreview || turnPreview(t),
        tools,
        startedAt,
        endedAt: modelEndedAt,
        durationMs: modelDur,
        thinkingMs,
        outputMs,
        toolMs: 0,
        thinkingPct,
        hasThinking,
        hasOutput,
        thinkingPreview: thinkPreview,
        outputPreview: outPreview,
        outputTokens: Number(t.output_tokens || 0),
        model: t.model || "",
        isError,
        splitSource,
      };
      spans.push(modelItem);
      r.items.push(modelItem);

      // If the assistant turn also executed inline tools (e.g. OpenCode), emit a Tools lane span too.
      if ((toolMs != null && toolMs > 0) || hasInlineToolResult) {
        toolCalls += 1;
        if (toolMs != null && toolMs > 0) {
          toolTotal += toolMs;
          r.toolMs += toolMs;
          r.durationMs += toolMs;
        }
        for (const tn of tools) r.tools[tn] = (r.tools[tn] || 0) + 1;
        const toolLabel = tools.length ? tools.join(", ") : "tool";
        const toolItem = {
          index: ++index,
          seq,
          turn: curTurn,
          lane: 2,
          kind: "tool",
          label: `${toolLabel} (inline)`,
          tools,
          startedAt: modelEndedAt,
          endedAt,
          durationMs: toolMs,
          thinkingMs: 0,
          outputMs: 0,
          toolMs,
          thinkingPct: 0,
          hasThinking: false,
          hasOutput: true,
          thinkingPreview: "",
          outputPreview: outPreview,
          outputTokens: 0,
          model: t.model || "",
          isError,
          splitSource: toolMs != null ? "recorded" : null,
        };
        spans.push(toolItem);
        r.items.push(toolItem);
      }
      continue;
    }

    // System / checkpoint / compaction turn
    const sysItem = {
      index: ++index,
      seq,
      turn: curTurn,
      lane: 0,
      kind: "system",
      label: outPreview || turnPreview(t),
      tools: [],
      startedAt,
      endedAt,
      durationMs: durationMs || 0,
      thinkingMs: 0,
      outputMs: 0,
      toolMs: 0,
      thinkingPct: 0,
      hasThinking: false,
      hasOutput: true,
      thinkingPreview: "",
      outputPreview: outPreview,
      outputTokens: 0,
      model: "",
      isError,
      splitSource: null,
    };
    spans.push(sysItem);
    r.items.push(sysItem);
  }

  const modelMs = Math.round((thinkingTotal + outputTotal) * 10) / 10;
  const thinkingMs = Math.round(thinkingTotal * 10) / 10;
  const outputMs = Math.round(outputTotal * 10) / 10;
  const toolMs = Math.round(toolTotal * 10) / 10;
  const activeMs = Math.round((modelMs + toolMs) * 10) / 10;
  const wallMs = (minTs != null && maxTs != null && maxTs >= minTs)
    ? Math.round((maxTs - minTs) * 10) / 10 : activeMs;

  const pct = (num, den) => (den > 0 ? Math.min(100, Math.max(0, (num / den) * 100)) : 0);

  return {
    summary: {
      userTurns,
      modelTurns,
      toolCalls,
      thinkingMs,
      outputMs,
      modelMs,
      toolMs,
      activeMs,
      wallMs,
      thinkingPct: pct(thinkingMs, activeMs),
      outputPct: pct(outputMs, activeMs),
      modelPct: pct(modelMs, activeMs),
      toolPct: pct(toolMs, activeMs),
      modelThinkingPct: pct(thinkingMs, modelMs),
      modelOutputPct: pct(outputMs, modelMs),
      startedAt: minTs,
      endedAt: maxTs,
    },
    spans,
    rounds: Array.from(roundsMap.values()),
  };
}

// Project trajectory spans onto a horizontal timeline domain [start, end]:
// mode = "duration" (idle-compressed actual durations) | "wall" (wall-clock) | "sequence" (equal steps).
function projectTrajectoryTimeline(traj, mode = "duration") {
  const spans = (traj && Array.isArray(traj.spans)) ? traj.spans : [];
  if (!spans.length) return null;

  const hasTimed = spans.some((s) => s.startedAt != null && Number.isFinite(s.startedAt));
  const hasPositiveDur = spans.some((s) => s.durationMs != null && s.durationMs > 0);
  const effMode = (mode === "sequence" || !hasTimed || (!hasPositiveDur && mode === "duration"))
    ? "sequence" : mode;

  if (effMode === "sequence") {
    const n = spans.length;
    const turnFirst = new Map();
    const projSpans = spans.map((s, i) => {
      if (!turnFirst.has(s.turn)) turnFirst.set(s.turn, i);
      return {
        ...s,
        projStart: i,
        projEnd: i + 1,
        leftPct: (i / n) * 100,
        widthPct: (1 / n) * 100,
      };
    });
    const turnBoundaries = Array.from(turnFirst.entries())
      .filter(([, pos]) => pos > 0)
      .map(([turn, pos]) => ({ turn, time: pos, leftPct: (pos / n) * 100 }));
    return { mode: "sequence", start: 0, end: n, duration: n, spans: projSpans, turnBoundaries };
  }

  if (effMode === "duration") {
    // Compress idle gaps between steps and allocate each step its actual duration
    // (with a small floor for 0ms user input markers so they stay visible).
    const activeTotal = Math.max(1, (traj.summary && traj.summary.activeMs) || 1000);
    const markerMs = Math.max(20, activeTotal * 0.018);
    let cursor = 0;
    const turnFirst = new Map();
    const rawProj = spans.map((s) => {
      if (!turnFirst.has(s.turn)) turnFirst.set(s.turn, cursor);
      const w = (s.durationMs != null && s.durationMs > 0) ? s.durationMs : markerMs;
      const st = cursor;
      const en = cursor + w;
      cursor = en;
      return { ...s, projStart: st, projEnd: en };
    });
    const total = Math.max(1, cursor);
    const projSpans = rawProj.map((s) => ({
      ...s,
      leftPct: (s.projStart / total) * 100,
      widthPct: Math.max(0.8, ((s.projEnd - s.projStart) / total) * 100),
    }));
    const turnBoundaries = Array.from(turnFirst.entries())
      .filter(([, pos]) => pos > 0)
      .map(([turn, pos]) => ({ turn, time: pos, leftPct: (pos / total) * 100 }));
    return { mode: "duration", start: 0, end: total, duration: total, spans: projSpans, turnBoundaries };
  }

  // Wall-clock mode: project onto [minStart, maxEnd]
  const timed = spans.filter((s) => s.startedAt != null && Number.isFinite(s.startedAt));
  const minStart = Math.min(...timed.map((s) => s.startedAt));
  const maxEnd = Math.max(...timed.map((s) => (s.endedAt != null ? s.endedAt : s.startedAt + (s.durationMs || 0))));
  const total = Math.max(1, maxEnd - minStart);
  const markerMs = Math.max(10, total * 0.015);
  const turnFirst = new Map();
  let lastPos = minStart;
  const projSpans = spans.map((s) => {
    const st = s.startedAt != null ? s.startedAt : lastPos;
    const dur = (s.durationMs != null && s.durationMs > 0) ? s.durationMs : markerMs;
    const en = s.endedAt != null && s.endedAt > st ? s.endedAt : (st + dur);
    lastPos = en;
    if (!turnFirst.has(s.turn)) turnFirst.set(s.turn, st);
    return {
      ...s,
      projStart: st,
      projEnd: en,
      leftPct: Math.min(99.2, Math.max(0, ((st - minStart) / total) * 100)),
      widthPct: Math.max(0.8, Math.min(100, ((en - st) / total) * 100)),
    };
  });
  const turnBoundaries = Array.from(turnFirst.entries())
    .filter(([, pos]) => pos > minStart)
    .map(([turn, pos]) => ({ turn, time: pos, leftPct: ((pos - minStart) / total) * 100 }));
  return { mode: "wall", start: minStart, end: minStart + total, duration: total, spans: projSpans, turnBoundaries };
}

// Identify conversation turn seqs active inside a projected range {start, end}.
function trajectoryFocusSeqs(proj, range) {
  if (!proj || !range) return null;
  const lo = Math.min(range.start, range.end);
  const hi = Math.max(range.start, range.end);
  const seqs = new Set();
  for (const s of proj.spans || []) {
    if (s.projStart <= hi && s.projEnd >= lo) seqs.add(s.seq);
  }
  return Array.from(seqs).sort((a, b) => a - b);
}

function spanTooltipText(s) {
  const kindName = s.kind === "user" ? "User Input"
    : s.kind === "model" ? `Model${s.model ? ` (${shortModel(s.model)})` : ""}`
    : s.kind === "tool" ? `Tool (${(s.tools && s.tools.length) ? s.tools.join(", ") : "call"})`
    : "System";
  const parts = [`#${s.index} · Turn ${s.turn} · ${kindName}`];
  if (s.startedAt != null) {
    const st = fmtClockTime(s.startedAt);
    const en = (s.endedAt != null && s.endedAt > s.startedAt) ? fmtClockTime(s.endedAt) : "";
    if (st) parts.push(en ? `${st} → ${en}` : st);
  }
  if (s.kind === "model") {
    const dur = s.durationMs != null ? fmtDuration(s.durationMs) : "—";
    const th = s.thinkingMs != null ? fmtDuration(s.thinkingMs) : "0ms";
    const out = s.outputMs != null ? fmtDuration(s.outputMs) : "0ms";
    const thPct = Math.round(s.thinkingPct || 0);
    const outPct = 100 - thPct;
    let detail = `Total: ${dur} · Thinking: ${th} (${thPct}%) · Output: ${out} (${outPct}%)`;
    if (s.outputTokens) {
      const tps = (s.outputMs && s.outputMs > 0) ? ` (${(s.outputTokens / (s.outputMs / 1000)).toFixed(1)} tok/s)` : "";
      detail += ` · ${fmtTokens(s.outputTokens)} tok${tps}`;
    }
    parts.push(detail);
  } else if (s.kind === "tool") {
    if (s.durationMs != null) parts.push(`Duration: ${fmtDuration(s.durationMs)}`);
  }
  if (s.label) parts.push(s.label.slice(0, 100));
  return parts.join(" | ");
}

function renderTrajectoryOverviewHtml(traj, proj, opts = {}) {
  const s = traj.summary;
  const activeMode = state.trajMode || "duration";
  const hasRange = Boolean(state.trajRange);
  const focusSet = hasRange ? new Set(trajectoryFocusSeqs(proj, state.trajRange) || []) : null;

  const boundariesHtml = (proj ? proj.turnBoundaries : []).map((b) =>
    `<span class="traj-turn-line" style="left:${b.leftPct.toFixed(2)}%" title="Turn ${b.turn}">
       <span class="traj-turn-tag">T${b.turn}</span>
     </span>`
  ).join("");

  const spansHtml = (proj ? proj.spans : []).map((sp) => {
    const selected = !focusSet || focusSet.has(sp.seq);
    const tip = spanTooltipText(sp);
    const styleParts = [
      `left:${sp.leftPct.toFixed(2)}%`,
      `width:${Math.min(100 - sp.leftPct, Math.max(0.8, sp.widthPct)).toFixed(2)}%`,
      `top:${sp.lane * 16 + 4}px`,
    ];
    if (sp.kind === "model") {
      styleParts.push(`--traj-think-pct:${(sp.thinkingPct || 0).toFixed(1)}%`);
    }
    return `<span class="traj-span ${esc(sp.kind)} ${sp.isError ? "error" : ""} ${selected ? "" : "dimmed"}"
                  data-seq="${sp.seq}" data-index="${sp.index}" data-kind="${esc(sp.kind)}"
                  title="${esc(tip)}" style="${styleParts.join(";")}"></span>`;
  }).join("");

  let selHtml = "";
  if (hasRange && proj && proj.duration > 0) {
    const lo = Math.max(proj.start, Math.min(state.trajRange.start, state.trajRange.end));
    const hi = Math.min(proj.end, Math.max(state.trajRange.start, state.trajRange.end));
    const leftP = ((lo - proj.start) / proj.duration) * 100;
    const widthP = Math.max(0.5, ((hi - lo) / proj.duration) * 100);
    selHtml = `<div class="traj-selection" style="left:${leftP.toFixed(2)}%;width:${widthP.toFixed(2)}%"></div>`;
  }

  const distBar = s.activeMs > 0 ? `
    <div class="traj-dist-bar" title="Active time breakdown: Thinking ${fmtDuration(s.thinkingMs)} (${s.thinkingPct.toFixed(0)}%) · Output ${fmtDuration(s.outputMs)} (${s.outputPct.toFixed(0)}%) · Tools ${fmtDuration(s.toolMs)} (${s.toolPct.toFixed(0)}%)">
      ${s.thinkingPct > 0 ? `<div class="traj-dist-seg thinking" style="width:${s.thinkingPct.toFixed(2)}%"></div>` : ""}
      ${s.outputPct > 0 ? `<div class="traj-dist-seg output" style="width:${s.outputPct.toFixed(2)}%"></div>` : ""}
      ${s.toolPct > 0 ? `<div class="traj-dist-seg tool" style="width:${s.toolPct.toFixed(2)}%"></div>` : ""}
    </div>` : "";

  return `
    <section class="traj-overview" aria-label="Trajectory timing overview">
      <div class="traj-ov-head">
        <div class="traj-legend">
          <span class="traj-chip input" title="User inputs">
            <span class="traj-dot input"></span>Input <b>${s.userTurns}</b>
          </span>
          <span class="traj-chip model" title="Total model time (Thinking + Output)">
            <span class="traj-dot model"></span>Model <b>${fmtDuration(s.modelMs) || "0ms"}</b>
            <span class="traj-sub-pill thinking" title="Model thinking / reasoning time">🧠 Think ${fmtDuration(s.thinkingMs) || "0ms"}${s.modelMs > 0 ? ` (${s.modelThinkingPct.toFixed(0)}%)` : ""}</span>
            <span class="traj-sub-pill output" title="Model output / generation time">💬 Out ${fmtDuration(s.outputMs) || "0ms"}${s.modelMs > 0 ? ` (${s.modelOutputPct.toFixed(0)}%)` : ""}</span>
          </span>
          <span class="traj-chip tool" title="Tool calling execution time">
            <span class="traj-dot tool"></span>Tools <b>${fmtDuration(s.toolMs) || "0ms"}</b> <span class="traj-dim">(${s.toolCalls})</span>
          </span>
          <span class="traj-chip total" title="Total active execution time (wall clock: ${fmtDuration(s.wallMs) || "0ms"})">
            ⏱ Active <b>${fmtDuration(s.activeMs) || "0ms"}</b>
          </span>
        </div>
        <div class="traj-modes">
          ${hasRange ? '<button class="traj-mode-btn reset" data-traj-reset="1" title="Clear timeline selection (Esc)">✕ Clear range</button>' : ""}
          <button class="traj-mode-btn ${activeMode === "duration" ? "active" : ""}" data-traj-mode="duration" title="Proportional to actual duration (idle gaps compressed)">Duration</button>
          <button class="traj-mode-btn ${activeMode === "wall" ? "active" : ""}" data-traj-mode="wall" title="Wall-clock timeline (retains idle gaps)">Wall</button>
          <button class="traj-mode-btn ${activeMode === "sequence" ? "active" : ""}" data-traj-mode="sequence" title="Equal width per operation step">Seq</button>
          ${opts.showOpenTabBtn ? '<button class="traj-mode-btn open-tab" id="openTrajTabBtn" title="Open full Trajectory ledger view">Trajectory ↗</button>' : ""}
        </div>
      </div>
      ${distBar}
      <div class="traj-plot">
        <div class="traj-lane-labels" aria-hidden="true">
          <span>Input</span>
          <span>Model</span>
          <span>Tools</span>
        </div>
        <div class="traj-track" id="trajTrack" tabindex="0">
          ${selHtml}
          <div class="traj-boundaries">${boundariesHtml}</div>
          <div class="traj-lanes">${spansHtml}</div>
        </div>
      </div>
      <div class="traj-hover-bar" id="trajHoverBar">
        Hover any bar for Thinking / Output / Tool timing · Click to jump to turn · Drag across track to filter range
      </div>
    </section>`;
}

function wireTrajectoryOverview(scope, traj, proj, handlers = {}) {
  scope.querySelectorAll(".traj-mode-btn[data-traj-mode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.trajMode = btn.dataset.trajMode;
      localStorage.setItem("cc_trajMode", state.trajMode);
      state.trajRange = null;
      if (handlers.onUpdate) handlers.onUpdate();
    });
  });
  const resetBtn = scope.querySelector("[data-traj-reset]");
  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      state.trajRange = null;
      if (handlers.onUpdate) handlers.onUpdate();
    });
  }
  const openTabBtn = scope.querySelector("#openTrajTabBtn");
  if (openTabBtn) {
    openTabBtn.addEventListener("click", () => {
      state.detailTab = "trajectory";
      renderDetailShell();
      loadTab();
    });
  }
  const hoverBar = scope.querySelector("#trajHoverBar");
  const defaultHint = "Hover any bar for Thinking / Output / Tool timing · Click to jump to turn · Drag across track to filter range";
  const spanByIndex = new Map((proj ? proj.spans : []).map((s) => [String(s.index), s]));

  scope.querySelectorAll(".traj-span").forEach((el) => {
    el.addEventListener("mouseenter", () => {
      const sp = spanByIndex.get(el.dataset.index);
      if (sp && hoverBar) hoverBar.textContent = spanTooltipText(sp);
    });
    el.addEventListener("mouseleave", () => {
      if (hoverBar) hoverBar.textContent = defaultHint;
    });
  });

  const track = scope.querySelector("#trajTrack");
  if (!track || !proj) return;

  let dragStartFrac = null;
  let dragStartX = 0;
  let dragTargetSeq = null;

  const fracAt = (ev) => {
    const rect = track.getBoundingClientRect();
    return Math.min(1, Math.max(0, (ev.clientX - rect.left) / Math.max(1, rect.width)));
  };

  track.addEventListener("mousedown", (ev) => {
    if (ev.button === 2) {
      ev.preventDefault();
      state.trajRange = null;
      if (handlers.onUpdate) handlers.onUpdate();
      return;
    }
    if (ev.button !== 0) return;
    dragStartFrac = fracAt(ev);
    dragStartX = ev.clientX;
    const spanEl = ev.target.closest ? ev.target.closest(".traj-span") : null;
    dragTargetSeq = spanEl ? Number(spanEl.dataset.seq) : null;
  });

  track.addEventListener("mouseup", (ev) => {
    if (dragStartFrac == null) return;
    const movedPx = Math.abs(ev.clientX - dragStartX);
    const endFrac = fracAt(ev);
    const startFrac = dragStartFrac;
    dragStartFrac = null;

    if (movedPx < 4) {
      if (dragTargetSeq != null && Number.isFinite(dragTargetSeq)) {
        if (handlers.onSelectSeq) handlers.onSelectSeq(dragTargetSeq);
      } else if (state.trajRange) {
        state.trajRange = null;
        if (handlers.onUpdate) handlers.onUpdate();
      }
      return;
    }
    const t1 = proj.start + Math.min(startFrac, endFrac) * proj.duration;
    const t2 = proj.start + Math.max(startFrac, endFrac) * proj.duration;
    state.trajRange = { start: t1, end: t2 };
    if (handlers.onUpdate) handlers.onUpdate();
  });

  track.addEventListener("dblclick", () => {
    if (state.trajRange) {
      state.trajRange = null;
      if (handlers.onUpdate) handlers.onUpdate();
    }
  });

  track.addEventListener("contextmenu", (ev) => ev.preventDefault());

  track.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && state.trajRange) {
      ev.preventDefault();
      state.trajRange = null;
      if (handlers.onUpdate) handlers.onUpdate();
    }
  });
}

function renderConversationTrajectory() {
  const wrap = $("#convTrajWrap");
  if (!wrap) return;
  const turns = state.convTurns || [];
  if (!turns.length) {
    wrap.innerHTML = "";
    return;
  }
  const traj = deriveTrajectory(turns);
  const proj = projectTrajectoryTimeline(traj, state.trajMode);
  wrap.innerHTML = renderTrajectoryOverviewHtml(traj, proj, { showOpenTabBtn: true });

  // Apply range filter to visible conversation turns if active
  const banner = $("#convFilterBanner");
  const focusSeqs = state.trajRange ? trajectoryFocusSeqs(proj, state.trajRange) : null;
  const focusSet = focusSeqs ? new Set(focusSeqs) : null;
  document.querySelectorAll("#turnsWrap .turn").forEach((node) => {
    const seq = Number(node.dataset.seq);
    node.hidden = Boolean(focusSet && !focusSet.has(seq));
  });
  if (banner) {
    if (focusSet) {
      banner.hidden = false;
      banner.className = "traj-filter-banner";
      banner.innerHTML = `<span>Showing <b>${focusSet.size}</b> of <b>${turns.length}</b> loaded turns in selected timeline interval</span>
        <button class="dbtn" id="clearConvRangeBtn">Reset filter</button>`;
      const cbtn = banner.querySelector("#clearConvRangeBtn");
      if (cbtn) {
        cbtn.addEventListener("click", () => {
          state.trajRange = null;
          renderConversationTrajectory();
        });
      }
    } else {
      banner.hidden = true;
      banner.innerHTML = "";
    }
  }

  wireTrajectoryOverview(wrap, traj, proj, {
    onUpdate: () => renderConversationTrajectory(),
    onSelectSeq: (seq) => scrollToTurn(seq),
  });
}

function renderTrajectoryView(body, turns) {
  if (!turns || !turns.length) {
    body.innerHTML = '<div class="empty">No trajectory records in this session.</div>';
    return;
  }
  const traj = deriveTrajectory(turns);
  const proj = projectTrajectoryTimeline(traj, state.trajMode);
  const s = traj.summary;
  const focusSeqs = state.trajRange ? trajectoryFocusSeqs(proj, state.trajRange) : null;
  const focusSet = focusSeqs ? new Set(focusSeqs) : null;
  const query = (state.trajSearch || "").trim().toLowerCase();

  // Max step duration across the session for relative waterfall bars
  const maxStepMs = Math.max(
    1,
    ...traj.spans.map((sp) => (sp.durationMs != null && sp.durationMs > 0 ? sp.durationMs : 0))
  );

  const cardsHtml = `
    <div class="traj-cards">
      <div class="traj-card input">
        <div class="tc-label"><span class="traj-dot input"></span>User Input</div>
        <div class="tc-val">${s.userTurns} <span class="tc-unit">turn${s.userTurns === 1 ? "" : "s"}</span></div>
        <div class="tc-sub">${turns.length} total records</div>
      </div>
      <div class="traj-card model">
        <div class="tc-label"><span class="traj-dot model"></span>Model Total Time</div>
        <div class="tc-val">${fmtDuration(s.modelMs) || "0ms"} <span class="tc-unit">${s.activeMs > 0 ? `${s.modelPct.toFixed(0)}%` : ""}</span></div>
        <div class="tc-sub">
          <span class="tc-think">🧠 Think: <b>${fmtDuration(s.thinkingMs) || "0ms"}</b> (${s.modelThinkingPct.toFixed(0)}%)</span>
          ·
          <span class="tc-out">💬 Output: <b>${fmtDuration(s.outputMs) || "0ms"}</b> (${s.modelOutputPct.toFixed(0)}%)</span>
        </div>
      </div>
      <div class="traj-card tool">
        <div class="tc-label"><span class="traj-dot tool"></span>Tool Calling Time</div>
        <div class="tc-val">${fmtDuration(s.toolMs) || "0ms"} <span class="tc-unit">${s.activeMs > 0 ? `${s.toolPct.toFixed(0)}%` : ""}</span></div>
        <div class="tc-sub">${s.toolCalls} tool execution${s.toolCalls === 1 ? "" : "s"}</div>
      </div>
    </div>`;

  const toolbarHtml = `
    <div class="traj-toolbar" role="toolbar" aria-label="Trajectory controls">
      <button type="button" class="traj-tb-btn" id="trajFoldTurnsBtn" title="Collapse or expand all turns">
        ${state.trajFoldTurns ? "⊞ Expand turns" : "⊟ Fold turns"}
      </button>
      <button type="button" class="traj-tb-btn ${state.trajShowPhases ? "active" : ""}" id="trajTogglePhasesBtn" title="Toggle Thinking / Output sub-phase breakdown rows under Model steps">
        ${state.trajShowPhases ? "⊟ Thinking / Output phases" : "⊞ Thinking / Output phases"}
      </button>
      <div class="traj-tb-search">
        <input type="search" id="trajSearchBox" value="${esc(state.trajSearch)}" placeholder="Filter turns, tools, thinking, output…" />
      </div>
    </div>`;

  const roundsHtml = traj.rounds.map((r) => {
    const visibleItems = r.items.filter((item) => {
      if (focusSet && !focusSet.has(item.seq)) return false;
      if (!query) return true;
      const hay = `${item.kind} ${item.label} ${item.thinkingPreview} ${item.outputPreview} ${(item.tools || []).join(" ")} ${item.model}`.toLowerCase();
      return hay.includes(query);
    });
    if (!visibleItems.length) return "";

    const toolHist = Object.entries(r.tools)
      .map(([name, cnt]) => `<span class="traj-hist-chip">🔧 ${esc(cnt > 1 ? `${name}×${cnt}` : name)}</span>`)
      .join("");

    const rowsHtml = visibleItems.map((item) => {
      const turnObj = turns[item.seq] || {};
      const wfWidth = item.durationMs != null && item.durationMs > 0
        ? Math.max(4, Math.min(100, (item.durationMs / maxStepMs) * 100))
        : (item.kind === "user" ? 3 : 8);

      let wfBar = "";
      if (item.kind === "model") {
        const thPct = (item.thinkingPct || 0).toFixed(1);
        wfBar = `<div class="traj-wf-track" title="Thinking ${thPct}% · Output ${(100 - item.thinkingPct).toFixed(1)}%">
          <div class="traj-wf-fill model" style="width:${wfWidth.toFixed(1)}%;--traj-think-pct:${thPct}%"></div>
        </div>`;
      } else {
        wfBar = `<div class="traj-wf-track">
          <div class="traj-wf-fill ${esc(item.kind)} ${item.isError ? "error" : ""}" style="width:${wfWidth.toFixed(1)}%"></div>
        </div>`;
      }

      const kindTag = item.kind === "user" ? "Input"
        : item.kind === "model" ? "Model"
        : item.kind === "tool" ? `Tool${item.tools.length ? ` · ${item.tools[0]}` : ""}`
        : "System";

      let subPhasesHtml = "";
      if (item.kind === "model" && state.trajShowPhases && (item.hasThinking || item.hasOutput)) {
        const thPct = Math.round(item.thinkingPct || 0);
        const outPct = 100 - thPct;
        const tps = (item.outputTokens && item.outputMs && item.outputMs > 0)
          ? ` · ${(item.outputTokens / (item.outputMs / 1000)).toFixed(1)} tok/s` : "";
        subPhasesHtml = `
          <div class="traj-subphases">
            ${item.hasThinking || (item.thinkingMs != null && item.thinkingMs > 0) ? `
              <div class="traj-subrow thinking">
                <span class="traj-sub-indent">↳</span>
                <span class="traj-sub-tag thinking">🧠 Thinking</span>
                <span class="traj-sub-text">${esc(item.thinkingPreview || "(reasoning phase)")}</span>
                <span class="traj-sub-bar"><span class="traj-sub-fill thinking" style="width:${thPct}%"></span></span>
                <span class="traj-sub-pct">${thPct}%</span>
                <span class="traj-sub-dur">${fmtDuration(item.thinkingMs) || "0ms"}</span>
              </div>` : ""}
            ${item.hasOutput || (item.outputMs != null && item.outputMs > 0) ? `
              <div class="traj-subrow output">
                <span class="traj-sub-indent">↳</span>
                <span class="traj-sub-tag output">💬 Output</span>
                <span class="traj-sub-text">${esc(item.outputPreview || "(response output)")}${item.outputTokens ? ` <span class="traj-sub-toks">(${fmtTokens(item.outputTokens)} tok${tps})</span>` : ""}</span>
                <span class="traj-sub-bar"><span class="traj-sub-fill output" style="width:${outPct}%"></span></span>
                <span class="traj-sub-pct">${outPct}%</span>
                <span class="traj-sub-dur">${fmtDuration(item.outputMs) || "0ms"}</span>
              </div>` : ""}
          </div>`;
      }

      const timingMeta = [];
      if (item.startedAt != null) timingMeta.push(`<span><b>Started:</b> ${esc(fmtClockTime(item.startedAt))}</span>`);
      if (item.endedAt != null && item.endedAt > item.startedAt) timingMeta.push(`<span><b>Completed:</b> ${esc(fmtClockTime(item.endedAt))}</span>`);
      if (item.durationMs != null) timingMeta.push(`<span><b>Total Duration:</b> ${esc(fmtDuration(item.durationMs))}</span>`);
      if (item.kind === "model") {
        timingMeta.push(`<span><b>Thinking Time:</b> ${esc(fmtDuration(item.thinkingMs) || "0ms")} (${Math.round(item.thinkingPct || 0)}%)</span>`);
        timingMeta.push(`<span><b>Output Time:</b> ${esc(fmtDuration(item.outputMs) || "0ms")} (${100 - Math.round(item.thinkingPct || 0)}%)</span>`);
        if (item.splitSource) timingMeta.push(`<span><b>Timing Split:</b> ${esc(item.splitSource)}</span>`);
      }

      const blocksHtml = (turnObj.blocks || []).map(renderBlock).join("");

      return `
        <div class="traj-item" data-seq="${item.seq}" data-index="${item.index}">
          <div class="traj-row ${esc(item.kind)} ${item.isError ? "error" : ""}">
            <span class="traj-idx">#${item.index}</span>
            <span class="traj-tag ${esc(item.kind)}">${esc(kindTag)}</span>
            <span class="traj-text">${esc(item.label)}</span>
            <span class="traj-metrics">
              ${item.kind === "model" && item.thinkingMs != null ? `<span class="traj-m-pill thinking" title="Thinking time">🧠 ${fmtDuration(item.thinkingMs)}</span>` : ""}
              ${item.kind === "model" && item.outputMs != null ? `<span class="traj-m-pill output" title="Output time">💬 ${fmtDuration(item.outputMs)}</span>` : ""}
              ${item.outputTokens ? `<span class="traj-m-tok">${fmtTokens(item.outputTokens)} tok</span>` : ""}
            </span>
            <span class="traj-wf">${wfBar}</span>
            <span class="traj-dur">${item.kind === "user" ? (fmtClockTime(item.startedAt).slice(0, 8) || "—") : (fmtDuration(item.durationMs) || "—")}</span>
          </div>
          ${subPhasesHtml}
          <div class="traj-inspector" hidden>
            <div class="traj-insp-timing">${timingMeta.join(" · ")}</div>
            <div class="traj-insp-blocks">${blocksHtml}</div>
          </div>
        </div>`;
    }).join("");

    return `
      <div class="traj-turn ${state.trajFoldTurns ? "folded" : ""}" data-turn="${r.turn}">
        <div class="traj-turn-head">
          <span class="traj-turn-caret">▼</span>
          <span class="traj-turn-title">Turn ${r.turn}</span>
          <span class="traj-turn-summary">
            ${r.durationMs > 0 ? `<span class="traj-ts-total">⏱ ${fmtDuration(r.durationMs)}</span>` : ""}
            ${r.thinkingMs > 0 ? `<span class="traj-ts-think">🧠 ${fmtDuration(r.thinkingMs)}</span>` : ""}
            ${r.outputMs > 0 ? `<span class="traj-ts-out">💬 ${fmtDuration(r.outputMs)}</span>` : ""}
            ${r.toolMs > 0 ? `<span class="traj-ts-tool">🔧 ${fmtDuration(r.toolMs)}</span>` : ""}
            ${toolHist}
          </span>
        </div>
        <div class="traj-turn-body">${rowsHtml}</div>
      </div>`;
  }).join("");

  body.innerHTML = `
    <div class="traj-view">
      ${cardsHtml}
      <div id="trajOvMount">${renderTrajectoryOverviewHtml(traj, proj, { showOpenTabBtn: false })}</div>
      ${toolbarHtml}
      <div class="traj-ledger" id="trajLedger">
        ${roundsHtml || '<div class="empty">No matching trajectory records in the selected filter.</div>'}
      </div>
    </div>`;

  wireTrajectoryOverview(body.querySelector("#trajOvMount"), traj, proj, {
    onUpdate: () => renderTrajectoryView(body, turns),
    onSelectSeq: (seq) => {
      const row = body.querySelector(`.traj-item[data-seq="${seq}"]`);
      if (!row) return;
      const turnGroup = row.closest(".traj-turn");
      if (turnGroup) turnGroup.classList.remove("folded");
      const insp = row.querySelector(".traj-inspector");
      if (insp) insp.hidden = false;
      row.classList.add("turn-highlight");
      row.scrollIntoView({ behavior: "smooth", block: "center" });
      setTimeout(() => row.classList.remove("turn-highlight"), 2600);
    },
  });

  const foldBtn = body.querySelector("#trajFoldTurnsBtn");
  if (foldBtn) {
    foldBtn.addEventListener("click", () => {
      state.trajFoldTurns = !state.trajFoldTurns;
      renderTrajectoryView(body, turns);
    });
  }
  const phasesBtn = body.querySelector("#trajTogglePhasesBtn");
  if (phasesBtn) {
    phasesBtn.addEventListener("click", () => {
      state.trajShowPhases = !state.trajShowPhases;
      renderTrajectoryView(body, turns);
    });
  }
  const searchBox = body.querySelector("#trajSearchBox");
  if (searchBox) {
    searchBox.addEventListener("input", (e) => {
      state.trajSearch = e.target.value;
      const pos = e.target.selectionStart;
      renderTrajectoryView(body, turns);
      const nextBox = body.querySelector("#trajSearchBox");
      if (nextBox) {
        nextBox.focus();
        try { nextBox.setSelectionRange(pos, pos); } catch { /* ignore */ }
      }
    });
  }

  body.querySelectorAll(".traj-turn-head").forEach((th) => {
    th.addEventListener("click", () => th.parentElement.classList.toggle("folded"));
  });
  body.querySelectorAll(".traj-row").forEach((row) => {
    row.addEventListener("click", () => {
      const insp = row.parentElement.querySelector(".traj-inspector");
      if (insp) insp.hidden = !insp.hidden;
    });
  });
}

function renderBlock(b) {
  if (b.type === "text") {
    return `<div class="block text"><div class="block-text">${esc(b.text)}</div></div>`;
  }
  if (b.type === "thinking") {
    return `<div class="block thinking"><div class="block-label">thinking</div><div class="block-text">${esc(b.text)}</div></div>`;
  }
  if (b.type === "tool_use") {
    const inp = JSON.stringify(b.input, null, 2);
    return `<div class="block tool_use"><div class="block-label">tool: ${esc(b.name)}</div><pre>${esc(inp)}</pre></div>`;
  }
  if (b.type === "tool_result") {
    const txt = b.text || "";
    const clipped = txt.length > 2000 ? txt.slice(0, 2000) + "\n… (truncated)" : txt;
    return `<div class="block tool_result"><div class="block-label">tool result</div><pre>${esc(clipped)}</pre></div>`;
  }
  return "";
}

const KCOLS = [
  { key: "pending", title: "Pending" },
  { key: "in_progress", title: "In Progress" },
  { key: "completed", title: "Completed" },
];

function renderTasks(body, tasks) {
  if (!tasks.length) { body.innerHTML = '<div class="empty">No tasks for this session.</div>'; return; }
  const byCol = { pending: [], in_progress: [], completed: [] };
  for (const t of tasks) (byCol[t.status] || byCol.pending).push(t);
  const colHtml = KCOLS.map((c) => `
    <div class="kcol" data-status="${c.key}">
      <h4>${c.title} <span class="kcount">${byCol[c.key].length}</span></h4>
      <div class="kdrop" data-status="${c.key}">
        ${byCol[c.key].map((t) => `
          <div class="kcard" draggable="true" data-id="${esc(t.id)}">
            <div class="ksub">${esc(t.subject || "(untitled)")}</div>
            ${t.owner ? `<div class="kowner">@${esc(t.owner)}</div>` : ""}
          </div>`).join("")}
      </div>
    </div>`).join("");
  body.innerHTML = `<div class="kanban">${colHtml}</div><div class="khint">Drag cards between columns to update status — writes back to the task file.</div>`;
  wireKanban(body, { sid: state.activeSession });
}

// opts.sid: fixed session for all cards (session view).
// opts.perCard: read session id from each card's data-sid (project view).
function wireKanban(scope, opts = {}) {
  // Remember the dragged element itself: task ids repeat across sessions on the
  // project board, so looking a card up by data-id can find the wrong one.
  let dragCard = null;
  let dragId = null;
  let dragSid = null;
  scope.querySelectorAll(".kcard").forEach((card) => {
    card.addEventListener("dragstart", (e) => {
      dragCard = card;
      dragId = card.dataset.id;
      dragSid = opts.perCard ? card.dataset.sid : opts.sid;
      card.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
    });
    card.addEventListener("dragend", () => card.classList.remove("dragging"));
  });
  scope.querySelectorAll(".kdrop").forEach((drop) => {
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", async (e) => {
      e.preventDefault();
      drop.classList.remove("over");
      const newStatus = drop.dataset.status;
      const card = dragCard;
      if (!card || !dragId || !dragSid) return;
      if (card.parentElement !== drop) {
        drop.appendChild(card);
        try {
          const tu = new URL(`/api/sessions/${encodeURIComponent(dragSid)}/tasks/${encodeURIComponent(dragId)}`,
                             location.origin);
          tu.searchParams.set("agent", state.agent);
          const r = await fetch(tu, {
            method: "PATCH", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ status: newStatus }),
          });
          if (!r.ok) throw await httpError(r);
          reloadTasksView();
        } catch (err) {
          flash("Failed to update task: " + err.message);
          reloadTasksView();
        }
      }
      dragCard = null; dragId = null; dragSid = null;
    });
  });
}

async function reloadTasksView() {
  if (state.activeSession && state.detailTab === "tasks") {
    const tasks = await api(`/api/sessions/${encodeURIComponent(state.activeSession)}/tasks`);
    renderTasks($("#tabBody"), tasks);
  } else if (state.projView === "tasks") {
    const tasks = await api(`/api/projects/${encodeURIComponent(state.activeProject)}/tasks`);
    renderProjectTasks($("#sessionPane"), tasks);
  }
}

// editFor: null for a read-only view, else the {agent, project} the memory was
// loaded for; saves go there, with the directory shown here (409 if it moved).
function renderMemory(scope, mem, editFor) {
  const editable = Boolean(editFor);
  if (!mem.index && !mem.files.length) {
    scope.innerHTML = `<div class="md"><div class="empty">No memory for this project.</div>
      <div class="mem-path">dir: ${esc(mem.dir || "")}</div></div>`;
    return;
  }
  let html = '<div class="md">';
  html += `<div class="mem-path">memory dir: ${esc(mem.dir || "")}</div>`;
  if (mem.index) {
    html += memBlock("MEMORY.md", mem.index_path, mem.index, editable);
  }
  for (const f of mem.files) {
    html += memBlock(f.name, f.path, f.content, editable);
  }
  html += "</div>";
  scope.innerHTML = html;
  if (editable) wireMemoryEditors(scope, editFor, mem.dir || "");
}

function memBlock(name, path, content, editable) {
  if (editable) {
    return `
      <div class="mem-file" data-name="${esc(name)}">
        <h4>${esc(name)}</h4>
        <div class="mem-path">${esc(path || "")}</div>
        <textarea class="editor mem-editor" spellcheck="false">${esc(content)}</textarea>
        <div class="editor-actions">
          <button class="dbtn mem-save">Save</button>
          <span class="editor-status"></span>
        </div>
      </div>`;
  }
  return `
    <div class="mem-file">
      <h4>${esc(name)}</h4>
      <div class="mem-path">${esc(path || "")}</div>
      <pre>${esc(content)}</pre>
    </div>`;
}

function wireMemoryEditors(scope, view, dir) {
  scope.querySelectorAll(".mem-file").forEach((blk) => {
    const btn = blk.querySelector(".mem-save");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      const name = blk.dataset.name;
      const content = blk.querySelector(".mem-editor").value;
      const status = blk.querySelector(".editor-status");
      status.textContent = "saving…";
      try {
        const mu = new URL(`/api/projects/${encodeURIComponent(view.project)}/memory`, location.origin);
        mu.searchParams.set("agent", view.agent);
        const r = await fetch(mu, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, content, expect_dir: dir }),
        });
        if (!r.ok) throw await httpError(r);
        status.textContent = "saved ✓";
      } catch (e) {
        status.textContent = "failed: " + e.message;
      }
    });
  });
}

// ---------------------------------------------------------------------------
// Layout: sidebar collapse + resizable splitters
// ---------------------------------------------------------------------------
function initLayout() {
  // restore persisted widths/collapse
  const sw = localStorage.getItem("cc_sidebarWidth");
  if (sw) $("#sidebar").style.width = sw + "px";
  const dw = localStorage.getItem("cc_detailWidth");
  if (dw) $("#detailPane").style.width = dw + "px";
  if (localStorage.getItem("cc_sidebarCollapsed") === "1") {
    $("#layout").classList.add("sidebar-collapsed");
  }

  $("#sidebarToggle").addEventListener("click", () => {
    const collapsed = $("#layout").classList.toggle("sidebar-collapsed");
    localStorage.setItem("cc_sidebarCollapsed", collapsed ? "1" : "0");
  });

  makeSplitter($("#splitter1"), $("#sidebar"), "cc_sidebarWidth", 160, 600, false);
  makeSplitter($("#splitter2"), $("#detailPane"), "cc_detailWidth", 280, 900, true);
}

function makeSplitter(splitter, target, storageKey, min, max, fromRight) {
  splitter.addEventListener("mousedown", (e) => {
    e.preventDefault();
    splitter.classList.add("dragging");
    const startX = e.clientX;
    const startW = target.getBoundingClientRect().width;
    const onMove = (ev) => {
      const delta = fromRight ? (startX - ev.clientX) : (ev.clientX - startX);
      let w = Math.max(min, Math.min(max, startW + delta));
      target.style.width = w + "px";
    };
    const onUp = () => {
      splitter.classList.remove("dragging");
      localStorage.setItem(storageKey, Math.round(target.getBoundingClientRect().width));
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------
let searchTimer = null;
function updateSearchPlaceholder() {
  const scope = $("#searchScope").value;
  const box = $("#searchBox");
  if (scope === "project" && state.activeProject) {
    box.placeholder = `Search in ${state.activeProject.slice(0, 28)}…`;
  } else {
    box.placeholder = "Search all conversations…";
  }
}

function initSearch() {
  $("#searchScope").addEventListener("change", () => {
    updateSearchPlaceholder();
    const q = $("#searchBox").value.trim();
    if (q) runSearch(q);
  });
  $("#searchBox").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    const q = e.target.value.trim();
    if (!q) { if (state.activeProject) loadProjView(); return; }
    searchTimer = setTimeout(() => runSearch(q), 300);
  });
  $("#reindexBtn").addEventListener("click", async () => {
    const btn = $("#reindexBtn");
    btn.textContent = "indexing…";
    try {
      const r = await fetch("/api/reindex", { method: "POST" });
      if (!r.ok) throw await httpError(r);
      const res = await r.json();
      const pruned = res.pruned ? `, ${res.pruned} stale removed` : "";
      const errs = res.errors ? ` Failed: ${Object.entries(res.errors).map(([a, m]) => `${a} (${m})`).join("; ")}` : "";
      flash(`Index updated: ${res.indexed} sessions, ${res.turns} turns, ${res.docs} docs${pruned} across all agents.${errs}`);
    } catch (e) {
      flash("Reindex failed: " + e.message);
    } finally {
      btn.textContent = "↻ index";
    }
  });
}

async function runSearch(q) {
  const el = $("#sessionPane");
  el.innerHTML = '<div class="loading">searching…</div>';
  const scopeProject = $("#searchScope").value === "project" && state.activeProject
    ? state.activeProject : null;
  let url = `/api/search?q=${encodeURIComponent(q)}&limit=80&agent=${encodeURIComponent(state.agent)}`;
  if (scopeProject) url += `&project=${encodeURIComponent(scopeProject)}`;
  try {
    const data = await api(url);
    renderSearchResults(el, data.results, q, scopeProject);
  } catch (e) {
    el.innerHTML = `<div class="empty">Search failed: ${esc(e.message)}</div>`;
  }
}

function renderSearchResults(el, results, q, scopeProject) {
  const scopeLabel = scopeProject ? `in ${esc(scopeProject.slice(0, 30))}` : "across all folders";
  if (!results.length) {
    el.innerHTML = `<h2 class="pane-title">No matches for “${esc(q)}” ${scopeLabel} — try ↻ index if this is a new session.</h2>`;
    return;
  }
  const html = results.map((r, i) => {
    // \u0002 / \u0003 mark the match (the index never emits them otherwise).
    const snip = esc(r.snippet).replace(/\u0002/g, "<mark>").replace(/\u0003/g, "</mark>");
    if (r.source === "conversation") {
      return `
        <div class="searchres" data-i="${i}">
          <div class="sr-head">
            <span class="sr-role ${esc(r.role)}">${esc(r.role)}</span>
            <span class="sr-proj">${esc(r.project)}</span>
            <span class="sr-sid">${esc(r.session_id.slice(0,8))} · #${r.seq}</span>
          </div>
          <div class="sr-snip">${snip}</div>
        </div>`;
    }
    // memory / claude_md / agent_doc hit
    const tag = r.source === "claude_md" ? "CLAUDE.md"
      : r.source === "agent_doc" ? ((state.caps && state.caps.doc_filename) || "doc")
      : "memory";
    return `
      <div class="searchres doc" data-i="${i}">
        <div class="sr-head">
          <span class="sr-role doc">${esc(tag)}</span>
          <span class="sr-proj">${esc(r.project)}</span>
          <span class="sr-sid">${esc(r.ref || "")}</span>
        </div>
        <div class="sr-snip">${snip}</div>
      </div>`;
  }).join("");
  el.innerHTML = `<h2 class="pane-title">${results.length} matches for “${esc(q)}” ${scopeLabel}</h2>${html}`;
  el.querySelectorAll(".searchres").forEach((node) => {
    node.addEventListener("click", () => openSearchResult(results[+node.dataset.i]));
  });
}

async function openSearchResult(r) {
  if (!r) return;
  if (r.project !== state.activeProject) await selectProject(r.project);
  if (r.source === "conversation") {
    openSession(r.session_id, typeof r.seq === "number" ? r.seq : null);
  } else if (r.source === "claude_md" || r.source === "agent_doc") {
    state.projView = "claude";
    renderProjNav();
    loadProjView();
  } else {
    // memory: open the project Memory view (editable) and scroll to the file
    state.projView = "memory";
    renderProjNav();
    await loadProjView();
    if (r.ref) {
      const blk = document.querySelector(`.mem-file[data-name="${CSS.escape(r.ref)}"]`);
      if (blk) {
        blk.classList.add("turn-highlight");
        blk.scrollIntoView({ behavior: "smooth", block: "center" });
        setTimeout(() => blk.classList.remove("turn-highlight"), 2600);
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
$("#showPrompts").addEventListener("change", (e) => {
  state.showPrompts = e.target.checked;
  document.querySelectorAll(".session-prompt").forEach((p) => p.classList.toggle("hidden", !state.showPrompts));
});

document.querySelectorAll(".sortbtn[data-sort]").forEach((b) => {
  b.addEventListener("click", () => {
    state.sortBy = b.dataset.sort;
    document.querySelectorAll(".sortbtn[data-sort]").forEach((x) => x.classList.toggle("active", x === b));
    renderProjects();
  });
});

$("#missingFilter").addEventListener("click", () => {
  state.onlyMissing = !state.onlyMissing;
  renderProjects();
});

initTheme();
initLayout();
initSearch();
loadAgents().then(() => loadProjects()).catch((e) => {
  $("#projectList").innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
});
