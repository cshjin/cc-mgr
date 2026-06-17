"use strict";

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
  convOffset: 0,
  convTotal: 0,
};

const CONV_PAGE = 40;
const $ = (sel) => document.querySelector(sel);

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
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
async function loadProjects() {
  state.projects = await api("/api/projects");
  renderProjects();
}

function sortedProjects() {
  const ps = [...state.projects];
  if (state.sortBy === "name") {
    ps.sort((a, b) => a.name.localeCompare(b.name));
  } else {
    ps.sort((a, b) => b.mtime - a.mtime);
  }
  return ps;
}

function renderProjects() {
  const el = $("#projectList");
  if (!state.projects.length) {
    el.innerHTML = '<div class="empty">No projects found.</div>';
    return;
  }
  el.innerHTML = sortedProjects().map((p) => {
    const git = p.git
      ? `<span class="pchip git" title="git repository">⎇ ${esc(p.git.branch || p.git.repo)}</span>`
      : "";
    const mem = p.has_memory ? '<span class="pchip mem">◆ mem</span>' : "";
    const cmd = p.has_claude_md ? '<span class="pchip cmd">CLAUDE.md</span>' : "";
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
    return `
      <div class="project ${p.name === state.activeProject ? "active" : ""}" data-name="${esc(p.name)}">
        <div class="pname">${esc(p.name)}</div>
        <div class="ptime">${p.session_count} session${p.session_count === 1 ? "" : "s"} · ${fmtTime(p.mtime)}</div>
        <div class="pbadges">${git}${mem}${cmd}${task}</div>
        ${prog}
      </div>`;
  }).join("");
  el.querySelectorAll(".project").forEach((node) => {
    node.addEventListener("click", () => selectProject(node.dataset.name));
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
  const m = state.activeProjectMeta || {};
  const taskCount = m.total_tasks ? `<span class="pn-count">${m.open_tasks}/${m.total_tasks}</span>` : "";
  const views = [
    { key: "sessions", label: "Sessions", count: `<span class="pn-count">${m.session_count || 0}</span>` },
    { key: "tasks", label: "Tasks", count: taskCount },
    { key: "memory", label: "Memory", count: m.has_memory ? "" : "" },
    { key: "claude", label: "CLAUDE.md", count: "" },
  ];
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
  try {
    if (state.projView === "sessions") {
      state.sessions = await api(`/api/projects/${encodeURIComponent(state.activeProject)}/sessions`);
      renderSessions();
    } else if (state.projView === "tasks") {
      const tasks = await api(`/api/projects/${encodeURIComponent(state.activeProject)}/tasks`);
      renderProjectTasks(el, tasks);
    } else if (state.projView === "memory") {
      const mem = await api(`/api/projects/${encodeURIComponent(state.activeProject)}/memory`);
      renderMemory(el, mem, true);
    } else if (state.projView === "claude") {
      const cm = await api(`/api/projects/${encodeURIComponent(state.activeProject)}/claude-md`);
      renderClaudeMd(el, cm);
    }
  } catch (e) {
    el.innerHTML = `<div class="empty">Failed: ${esc(e.message)}</div>`;
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

function renderSessions() {
  const el = $("#sessionPane");
  if (!state.sessions.length) {
    el.innerHTML = '<div class="empty">No sessions in this project.</div>';
    return;
  }
  const rows = state.sessions.map((s) => {
    const pct = ctxPct(s);
    const ctx = ctxLabel(s);
    const taskBadge = s.total_tasks
      ? (s.open_tasks
          ? `<span class="badge task-open">⏳ ${s.open_tasks}/${s.total_tasks} tasks</span>`
          : `<span class="badge task-done">✓ ${s.total_tasks} tasks</span>`)
      : "";
    const memBadge = s.has_memory ? '<span class="badge mem">◆ memory</span>' : "";
    const modelBadge = s.model ? `<span class="badge model">${esc(shortModel(s.model))}</span>` : "";
    return `
      <div class="session ${s.session_id === state.activeSession ? "active" : ""}" data-id="${esc(s.session_id)}">
        <div class="session-head">
          <span class="session-id">${esc(s.session_id.slice(0, 8))}</span>
          <span class="session-time">${fmtTime(s.mtime)}</span>
        </div>
        <div class="session-prompt ${state.showPrompts ? "" : "hidden"}">${esc(s.last_prompt || s.first_prompt || "(no prompt)")}</div>
        <div class="ctxbar">
          <div class="label" title="${esc(ctx.title)}"><span>context · ${ctx.windowLabel}</span><span>${fmtTokens(s.context_tokens)}${ctx.pctLabel}</span></div>
          <div class="track ${ctx.known ? "" : "unknown"}"><div class="fill" style="width:${pct}%"></div></div>
        </div>
        <div class="badges">
          <span class="badge">${s.user_turns}↗ ${s.assistant_turns}↙</span>
          ${modelBadge}${memBadge}${taskBadge}
        </div>
      </div>`;
  }).join("");
  el.innerHTML = `<h2 class="pane-title">${state.sessions.length} sessions · newest first</h2>` + rows;
  el.querySelectorAll(".session").forEach((node) => {
    node.addEventListener("click", () => openSession(node.dataset.id));
  });
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

function renderClaudeMd(el, cm) {
  if (!cm.cwd) {
    el.innerHTML = '<div class="empty">No working directory known for this project (no session has a cwd yet).</div>';
    return;
  }
  const status = cm.exists ? "" : "CLAUDE.md does not exist yet — saving will create it.";
  el.innerHTML = `
    <div class="editor-wrap">
      <div class="editor-head">
        <strong>CLAUDE.md</strong>
        <span class="ehpath">${esc(cm.path)}</span>
      </div>
      <textarea class="editor" id="cmEditor" spellcheck="false">${esc(cm.content)}</textarea>
      <div class="editor-actions">
        <button class="dbtn" id="cmSave">Save</button>
        <span class="editor-status" id="cmStatus">${esc(status)}</span>
      </div>
    </div>`;
  $("#cmSave").addEventListener("click", async () => {
    $("#cmStatus").textContent = "saving…";
    try {
      const r = await fetch(`/api/projects/${encodeURIComponent(state.activeProject)}/claude-md`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: $("#cmEditor").value }),
      });
      if (!r.ok) throw new Error(`${r.status}`);
      const res = await r.json();
      $("#cmStatus").textContent = "saved ✓";
      flash("CLAUDE.md saved to " + res.saved);
    } catch (e) {
      $("#cmStatus").textContent = "failed: " + e.message;
    }
  });
}

// ---------------------------------------------------------------------------
// Detail pane (per-session: conversation / tasks / memory)
// ---------------------------------------------------------------------------
async function openSession(id) {
  state.activeSession = id;
  state.detailTab = "conversation";
  renderSessions();
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
  const pane = $("#detailPane");
  pane.innerHTML = `
    <div class="detail-head">
      <span class="dclose" id="dclose">×</span>
      <div class="dtitle">${esc(s ? s.session_id.slice(0, 8) : "")}</div>
      <div class="dmeta">${esc(s ? (s.git_branch || "") : "")} ${s ? fmtTokens(s.context_tokens) + " tok" : ""}</div>
      <div class="dactions">
        <button class="dbtn" id="exportBtn">⬇ Export .md</button>
        <button class="dbtn danger" id="deleteBtn">🗑 Delete</button>
      </div>
    </div>
    <div class="tabs">
      <div class="tab ${state.detailTab === "conversation" ? "active" : ""}" data-tab="conversation">Conversation</div>
      <div class="tab ${state.detailTab === "tasks" ? "active" : ""}" data-tab="tasks">Tasks${s && s.total_tasks ? ` (${s.total_tasks})` : ""}</div>
      <div class="tab ${state.detailTab === "memory" ? "active" : ""}" data-tab="memory">Memory</div>
    </div>
    <div id="tabBody"></div>`;
  $("#dclose").addEventListener("click", closeDetail);
  $("#exportBtn").addEventListener("click", exportSession);
  $("#deleteBtn").addEventListener("click", openDeleteModal);
  pane.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => { state.detailTab = t.dataset.tab; renderDetailShell(); loadTab(); });
  });
}

function exportSession() {
  const url = `/api/projects/${encodeURIComponent(state.activeProject)}/sessions/${state.activeSession}/export`;
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
      <label class="mrow"><input type="checkbox" id="mExport" checked /> Export to Markdown first</label>
      <label class="mrow"><input type="checkbox" id="mMemory" /> Save a summary to project memory</label>
      <label class="mrow"><input type="checkbox" id="mHard" /> Permanent delete (skip trash)</label>
      <div class="modal-actions">
        <button class="dbtn" id="mCancel">Cancel</button>
        <button class="dbtn danger" id="mConfirm">Delete</button>
      </div>
      <div class="modal-status" id="mStatus"></div>
    </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  $("#mCancel").addEventListener("click", close);
  $("#mConfirm").addEventListener("click", async () => {
    const exportFirst = $("#mExport").checked;
    const saveMemory = $("#mMemory").checked;
    const hard = $("#mHard").checked;
    $("#mStatus").textContent = "deleting…";
    try {
      const r = await fetch(`/api/projects/${encodeURIComponent(state.activeProject)}/sessions/${state.activeSession}/delete`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ export_first: exportFirst, save_memory: saveMemory, hard }),
      });
      if (!r.ok) throw new Error(`${r.status}`);
      const res = await r.json();
      close();
      closeDetail();
      state.sessions = await api(`/api/projects/${encodeURIComponent(state.activeProject)}/sessions`);
      renderSessions();
      const note = res.export ? ` Exported.` : "";
      const mem = res.memory ? ` Saved to memory.` : "";
      const where = res.trash ? ` Moved to trash.` : " Permanently removed.";
      flash(`Session deleted.${note}${mem}${where}`);
    } catch (e) {
      $("#mStatus").textContent = "Failed: " + e.message;
    }
  });
}

function flash(msg) {
  const f = document.createElement("div");
  f.className = "flash";
  f.textContent = msg;
  document.body.appendChild(f);
  setTimeout(() => f.remove(), 6000);
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
      const data = await api(`/api/projects/${encodeURIComponent(state.activeProject)}/sessions/${state.activeSession}?offset=0&limit=${CONV_PAGE}`);
      state.convTotal = data.total;
      state.convOffset = data.turns.length;
      renderConversation(body, data.turns, true);
    } else if (state.detailTab === "tasks") {
      const tasks = await api(`/api/sessions/${state.activeSession}/tasks`);
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

function turnHtml(t) {
  const roleLabel = t.kind === "tool" ? "tool" : t.role;
  const toks = t.output_tokens ? `<span class="turn-toks">${fmtTokens(t.output_tokens)}</span>` : "";
  const skill = t.attribution_skill ? ` · ${esc(t.attribution_skill)}` : "";
  const blocks = t.blocks.map(renderBlock).join("");
  const openByDefault = t.role === "user" && t.kind !== "tool";
  return `
    <div class="turn ${roleLabel} ${openByDefault ? "open" : ""}">
      <div class="turn-head">
        <span class="turn-caret">▶</span>
        <span class="turn-role">${esc(roleLabel)}${skill}</span>
        <span class="turn-preview">${esc(turnPreview(t))}</span>
        ${toks}
      </div>
      <div class="turn-body">${blocks}</div>
    </div>`;
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
  const turnsHtml = turns.map(turnHtml).join("");
  if (fresh) {
    body.innerHTML = `<div class="turns" id="turnsWrap">${turnsHtml}</div><div id="loadMoreWrap">${loadMoreBar()}</div>`;
  } else {
    $("#turnsWrap").insertAdjacentHTML("beforeend", turnsHtml);
    $("#loadMoreWrap").innerHTML = loadMoreBar();
  }
  wireTurns(body);
  const btn = $("#loadMore");
  if (btn) btn.addEventListener("click", loadMoreTurns);
}

async function loadMoreTurns() {
  const btn = $("#loadMore");
  if (btn) btn.textContent = "loading…";
  const data = await api(`/api/projects/${encodeURIComponent(state.activeProject)}/sessions/${state.activeSession}?offset=${state.convOffset}&limit=${CONV_PAGE}`);
  state.convTotal = data.total;
  state.convOffset += data.turns.length;
  renderConversation($("#tabBody"), data.turns, false);
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
  let dragId = null;
  let dragSid = null;
  scope.querySelectorAll(".kcard").forEach((card) => {
    card.addEventListener("dragstart", (e) => {
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
      if (!dragId || !dragSid) return;
      const card = scope.querySelector(`.kcard[data-id="${CSS.escape(dragId)}"]`);
      if (card && card.parentElement !== drop) {
        drop.appendChild(card);
        try {
          const r = await fetch(`/api/sessions/${dragSid}/tasks/${encodeURIComponent(dragId)}`, {
            method: "PATCH", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ status: newStatus }),
          });
          if (!r.ok) throw new Error(`${r.status}`);
          reloadTasksView();
        } catch (err) {
          flash("Failed to update task: " + err.message);
          reloadTasksView();
        }
      }
      dragId = null; dragSid = null;
    });
  });
}

async function reloadTasksView() {
  if (state.activeSession && state.detailTab === "tasks") {
    const tasks = await api(`/api/sessions/${state.activeSession}/tasks`);
    renderTasks($("#tabBody"), tasks);
  } else if (state.projView === "tasks") {
    const tasks = await api(`/api/projects/${encodeURIComponent(state.activeProject)}/tasks`);
    renderProjectTasks($("#sessionPane"), tasks);
  }
}

function renderMemory(scope, mem, editable) {
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
  if (editable) wireMemoryEditors(scope);
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

function wireMemoryEditors(scope) {
  scope.querySelectorAll(".mem-file").forEach((blk) => {
    const btn = blk.querySelector(".mem-save");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      const name = blk.dataset.name;
      const content = blk.querySelector(".mem-editor").value;
      const status = blk.querySelector(".editor-status");
      status.textContent = "saving…";
      try {
        const r = await fetch(`/api/projects/${encodeURIComponent(state.activeProject)}/memory`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, content }),
        });
        if (!r.ok) throw new Error(`${r.status}`);
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
      const res = await r.json();
      flash(`Index updated: ${res.indexed} indexed, ${res.skipped} unchanged, ${res.turns} turns.`);
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
  let url = `/api/search?q=${encodeURIComponent(q)}&limit=80`;
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
  const html = results.map((r) => {
    const snip = esc(r.snippet).replace(/\[/g, '<mark>').replace(/\]/g, '</mark>');
    return `
      <div class="searchres" data-project="${esc(r.project)}" data-id="${esc(r.session_id)}">
        <div class="sr-head">
          <span class="sr-role ${esc(r.role)}">${esc(r.role)}</span>
          <span class="sr-proj">${esc(r.project)}</span>
          <span class="sr-sid">${esc(r.session_id.slice(0,8))}</span>
        </div>
        <div class="sr-snip">${snip}</div>
      </div>`;
  }).join("");
  el.innerHTML = `<h2 class="pane-title">${results.length} matches for “${esc(q)}” ${scopeLabel}</h2>${html}`;
  el.querySelectorAll(".searchres").forEach((node) => {
    node.addEventListener("click", async () => {
      const proj = node.dataset.project;
      if (proj !== state.activeProject) await selectProject(proj);
      openSession(node.dataset.id);
    });
  });
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
$("#showPrompts").addEventListener("change", (e) => {
  state.showPrompts = e.target.checked;
  document.querySelectorAll(".session-prompt").forEach((p) => p.classList.toggle("hidden", !state.showPrompts));
});

document.querySelectorAll(".sortbtn").forEach((b) => {
  b.addEventListener("click", () => {
    state.sortBy = b.dataset.sort;
    document.querySelectorAll(".sortbtn").forEach((x) => x.classList.toggle("active", x === b));
    renderProjects();
  });
});

initLayout();
initSearch();
loadProjects().catch((e) => {
  $("#projectList").innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
});
