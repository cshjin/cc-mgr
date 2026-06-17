"use strict";

const state = {
  projects: [],
  activeProject: null,
  sessions: [],
  activeSession: null,
  detailTab: "conversation",
  showPrompts: true,
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
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const now = Date.now();
  const diff = (now - d.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  if (diff < 604800) return Math.floor(diff / 86400) + "d ago";
  return d.toLocaleDateString();
}

const CTX_LIMIT = 200000; // context window reference for the bar

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------
async function loadProjects() {
  state.projects = await api("/api/projects");
  renderProjects();
}

function renderProjects() {
  const el = $("#projectList");
  if (!state.projects.length) {
    el.innerHTML = '<div class="empty">No projects found.</div>';
    return;
  }
  el.innerHTML = state.projects.map((p) => `
    <div class="project ${p.name === state.activeProject ? "active" : ""}" data-name="${esc(p.name)}">
      <div class="pname">${esc(p.name)}</div>
      <div class="pmeta">
        <span>${p.session_count} session${p.session_count === 1 ? "" : "s"}</span>
        ${p.has_memory ? '<span>· memory</span>' : ""}
      </div>
    </div>`).join("");
  el.querySelectorAll(".project").forEach((node) => {
    node.addEventListener("click", () => selectProject(node.dataset.name));
  });
}

async function selectProject(name) {
  state.activeProject = name;
  state.activeSession = null;
  closeDetail();
  renderProjects();
  $("#sessionPane").innerHTML = '<div class="loading">loading sessions…</div>';
  state.sessions = await api(`/api/projects/${encodeURIComponent(name)}/sessions`);
  renderSessions();
}

function renderSessions() {
  const el = $("#sessionPane");
  if (!state.sessions.length) {
    el.innerHTML = '<div class="empty">No sessions in this project.</div>';
    return;
  }
  const rows = state.sessions.map((s) => {
    const pct = Math.min(100, (s.context_tokens / CTX_LIMIT) * 100);
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
          <div class="label"><span>context</span><span>${fmtTokens(s.context_tokens)} tok</span></div>
          <div class="track"><div class="fill" style="width:${pct}%"></div></div>
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

function shortModel(m) {
  if (m.includes("opus")) return "opus";
  if (m.includes("sonnet")) return "sonnet";
  if (m.includes("haiku")) return "haiku";
  return m.slice(0, 12);
}

// ---------------------------------------------------------------------------
// Detail pane (conversation / memory / tasks)
// ---------------------------------------------------------------------------
async function openSession(id) {
  state.activeSession = id;
  state.detailTab = "conversation";
  renderSessions();
  $(".layout").classList.add("detail-open");
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
  // trigger a download of the markdown
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
    const hard = $("#mHard").checked;
    $("#mStatus").textContent = "deleting…";
    try {
      const r = await fetch(`/api/projects/${encodeURIComponent(state.activeProject)}/sessions/${state.activeSession}/delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ export_first: exportFirst, hard }),
      });
      if (!r.ok) throw new Error(`${r.status}`);
      const res = await r.json();
      close();
      closeDetail();
      // refresh session list
      state.sessions = await api(`/api/projects/${encodeURIComponent(state.activeProject)}/sessions`);
      renderSessions();
      const note = res.export ? ` Exported to ${res.export}.` : "";
      const where = res.trash ? ` Moved to trash: ${res.trash}.` : " Permanently removed.";
      flash(`Session deleted.${note}${where}`);
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
  $(".layout").classList.remove("detail-open");
  const pane = $("#detailPane");
  pane.hidden = true;
  pane.innerHTML = "";
  renderSessions();
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
      renderMemory(body, mem);
    }
  } catch (e) {
    body.innerHTML = `<div class="empty">Failed to load: ${esc(e.message)}</div>`;
  }
}

function turnPreview(turn) {
  for (const b of turn.blocks) {
    if (b.type === "text" && b.text) return b.text.slice(0, 120);
  }
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

function renderTasks(body, tasks) {
  if (!tasks.length) { body.innerHTML = '<div class="empty">No tasks for this session.</div>'; return; }
  const cols = {
    pending: { title: "Pending", items: [] },
    in_progress: { title: "In Progress", items: [] },
    completed: { title: "Completed", items: [] },
  };
  for (const t of tasks) {
    const key = cols[t.status] ? t.status : "pending";
    cols[key].items.push(t);
  }
  const colHtml = Object.values(cols).map((c) => `
    <div class="kcol">
      <h4>${c.title} (${c.items.length})</h4>
      ${c.items.map((t) => `
        <div class="kcard">
          <div class="ksub">${esc(t.subject || "(untitled)")}</div>
          ${t.owner ? `<div class="kowner">@${esc(t.owner)}</div>` : ""}
        </div>`).join("")}
    </div>`).join("");
  body.innerHTML = `<div class="kanban">${colHtml}</div>`;
}

function renderMemory(body, mem) {
  if (!mem.index && !mem.files.length) { body.innerHTML = '<div class="empty">No memory for this project.</div>'; return; }
  let html = '<div class="md">';
  if (mem.index) html += `<div class="mem-file"><h4>MEMORY.md</h4><pre>${esc(mem.index)}</pre></div>`;
  for (const f of mem.files) {
    html += `<div class="mem-file"><h4>${esc(f.name)}</h4><pre>${esc(f.content)}</pre></div>`;
  }
  html += "</div>";
  body.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
$("#showPrompts").addEventListener("change", (e) => {
  state.showPrompts = e.target.checked;
  document.querySelectorAll(".session-prompt").forEach((p) => p.classList.toggle("hidden", !state.showPrompts));
});

loadProjects().catch((e) => {
  $("#projectList").innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
});
