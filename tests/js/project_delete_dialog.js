// Drives the real frontend (index.html + app.js) in jsdom with a stubbed fetch:
// the project-delete dialog, the missing-directory filter, agent binding, a
// stale confirmation (409), and following the open project when its name changes.
// Run by tests/test_frontend_dom.py when jsdom is installed (NODE_PATH).
const { JSDOM } = require("jsdom");  // resolved via NODE_PATH
const fs = require("fs");
const root = process.argv[2];  // the frontend/ directory
const html = fs.readFileSync(root + "/index.html", "utf8").replace(/<script[^>]*><\/script>/g, "");
const dom = new JSDOM(html, { url: "http://localhost:8765/", runScripts: "outside-only", pretendToBeVisual: true });
const w = dom.window;
w.matchMedia = w.matchMedia || (() => ({ matches: false, addEventListener() {}, addListener() {} }));
w.CSS = w.CSS || { escape: (s) => s };

const calls = [];
const posted = [];
const HASHED = "-here~0123456789ab";  // named with a hash while another directory mangled alike
const projects = {
  claude: [
    { name: "-gone", display_path: "/old/SwarmZoo_paper", cwd: "/old/SwarmZoo_paper", session_count: 2,
      mtime: 1700000000, cwd_missing: true, has_memory: true },
    { name: HASHED, display_path: "/new/here", cwd: "/new/here", session_count: 1, mtime: 1700000100 },
  ],
  codex: [{ name: "-here", display_path: "/codex/here", cwd: "/codex/here", session_count: 5, mtime: 1 }],
};
const SESSION = { session_id: "s1", title: "kept session", mtime: 1700000100, context_tokens: 0,
                  user_turns: 1, assistant_turns: 1 };
let releaseDelete;
let deleteStatus = 200;
let holdDoc = null;  // a promise the doc GET waits for, to deliver it late
const gates = {};    // per project name: a promise that doc GETs for that name wait for
const docPuts = [];
w.fetch = async (u, opts = {}) => {
  const url = new w.URL(String(u), "http://localhost:8765");
  const agent = url.searchParams.get("agent");
  calls.push(`${opts.method || "GET"} ${url.pathname}?agent=${agent}`);
  const json = (obj, status = 200) => ({ ok: status < 400, status, statusText: "", json: async () => obj });
  if (url.pathname === "/api/agents") return json([
    { agent_id: "claude", label: "Claude", doc_filename: "CLAUDE.md", has_memory: true, has_tasks: true,
      can_edit_doc: true, can_delete: true, can_export: true, delete_note: "claude note", has_data: true },
    { agent_id: "codex", label: "Codex", doc_filename: "AGENTS.md", has_memory: false, has_tasks: false,
      can_edit_doc: true, can_delete: true, can_export: true, delete_note: "codex note", has_data: true }]);
  if (url.pathname === "/api/projects") return json(projects[agent] || []);
  if (url.pathname.endsWith("/doc")) {
    const name = decodeURIComponent(url.pathname.split("/")[3]);
    const p = (projects[agent] || []).find((x) => x.name === name);
    if (opts.method === "PUT") {
      const body = JSON.parse(opts.body);
      docPuts.push({ name, body });
      if (!p || p.cwd !== body.expect_cwd)  // like the server: the name must still mean that directory
        return json({ detail: `this project now refers to ${p ? p.cwd : "no directory"}, ` +
                              `not ${body.expect_cwd}; reload and try again` }, 409);
      return json({ saved: `${p.cwd}/CLAUDE.md` });
    }
    if (holdDoc) await holdDoc;
    if (gates[name]) await gates[name];
    return json({ exists: true, path: `${p.cwd}/CLAUDE.md`, content: `doc of ${p.cwd}`, cwd: p.cwd });
  }
  if (url.pathname.endsWith("/sessions")) {  // like the server: only names that exist right now
    const name = decodeURIComponent(url.pathname.split("/")[3]);
    return (projects[agent] || []).some((p) => p.name === name) ? json([SESSION])
      : json({ detail: `project ${name} not found` }, 404);
  }
  if (url.pathname.endsWith("/delete-preview"))
    return json({ sessions: 2, subagents: 1, hidden: 1, memory_files: 3, newest: 1700000000, outside: 0 });
  if (url.pathname.endsWith("/delete")) {
    posted.push(JSON.parse(opts.body));
    await new Promise((r) => { releaseDelete = r; });
    if (deleteStatus === 409)
      return json({ detail: "project -here now refers to /other, not /new/here; reload the list and try again" }, 409);
    // -gone is deleted; without it, the other directory's name is plain again
    projects.claude = [{ ...projects.claude[1], name: "-here" }];
    return json({ sessions: ["a", "b", "c"], trash: ["/t/x"], exported: ["/e/a.md"], kept: [],
                  errors: {}, warnings: [], project_removed: true });
  }
  return json({});
};
w.localStorage.setItem("cc_agent", "claude");
w.eval(fs.readFileSync(root + "/app.js", "utf8") +
       "\n;window.__state = () => state; window.__loadProjects = () => loadProjects();");

const $ = (s) => w.document.querySelector(s);
const tick = () => new Promise((r) => setTimeout(r, 20));
async function until(fn, what) {
  for (let i = 0; i < 200; i++) { if (fn()) return; await tick(); }
  throw new Error("timed out waiting for " + what);
}
function check(cond, what) { if (!cond) throw new Error("FAILED: " + what); console.log("ok -", what); }
function set(el, prop, value, ev) { el[prop] = value; el.dispatchEvent(new w.Event(ev, { bubbles: true })); }
const row = (name) => $(`.project[data-name='${name}']`);
const listLoads = () => calls.filter((c) => c === "GET /api/projects?agent=claude").length;

(async () => {
  await until(() => w.document.querySelectorAll("#projectList .project").length === 2, "projects");
  const filter = $("#missingFilter");
  check(!filter.hidden && filter.textContent === "⚠ missing 1", "filter button shows the missing count");
  check($(".project[data-name='-gone'] .pchip.missing") !== null, "missing project has the badge");
  filter.click();
  check(w.document.querySelectorAll("#projectList .project").length === 1, "filter shows only the missing project");
  filter.click();
  check(w.document.querySelectorAll("#projectList .project").length === 2, "filter toggles back");
  check(w.__state().sortBy === "time", "the filter button didn't change the sort");

  // open the other project (under its hashed name) and keep it open during the delete
  row(HASHED).click();
  await until(() => $("#sessionPane").textContent.includes("kept session"), "the open project's sessions");

  // project delete dialog
  $(".project[data-name='-gone'] .pdel").click();
  check(w.__state().activeProject === HASHED, "the delete button doesn't open the project");
  await until(() => $("#pdConfirm"), "preview");
  const text = $(".modal").textContent;
  check(text.includes("2 sessions (+1 subagent session) · 1 not listed (empty or archived) · 3 memory files"), "preview summary");
  check(text.includes("no longer exists") && text.includes("claude note"), "missing-dir warning and agent note");
  check($("#pdConfirm").textContent === "Remove 3 sessions" && !$("#pdConfirm").disabled, "soft confirm ready");
  set($("#pdHard"), "checked", true, "change");
  check($("#pdConfirm").disabled && !$("#pdTypeRow").hidden, "permanent needs the typed name");
  set($("#pdType"), "value", "nope", "input");
  check($("#pdConfirm").disabled, "a wrong name keeps it disabled");
  set($("#pdType"), "value", "SwarmZoo_paper", "input");
  check(!$("#pdConfirm").disabled && $("#pdConfirm").textContent === "Delete 3 sessions permanently", "the right name enables it");
  set($("#pdHard"), "checked", false, "change");
  $("#pdConfirm").click();
  await until(() => releaseDelete, "delete request");
  check($("#pdConfirm").disabled, "confirm is disabled while the request runs");
  set($("#pdType"), "value", "SwarmZoo_paper", "input");
  check($("#pdConfirm").disabled && $("#pdHard").disabled, "typing can't re-enable it mid-request");
  $("#pdConfirm").click();
  check(calls.filter((c) => c.startsWith("POST")).length === 1, "a second click sends nothing");
  check(posted[0].expect_cwd === "/old/SwarmZoo_paper" && posted[0].hard === false,
        "the request names the directory the dialog showed");
  releaseDelete();
  await until(() => !$(".modal-overlay"), "dialog closes");
  check(calls.includes("POST /api/projects/-gone/delete?agent=claude"), "deleted with the agent it previewed");
  check(calls.includes("GET /api/projects/-gone/delete-preview?agent=claude"), "previewed with that agent");
  check($(".flash") && $(".flash").textContent.startsWith("Removed 3 sessions (reversible: /t/x)."), "result message");

  // the open project's name changed (its hash is gone): the view follows its directory
  await until(() => row("-here"), "the reloaded list");
  check(w.__state().activeProject === "-here" && row("-here").classList.contains("active"),
        "the open project is followed to its new name");
  $(".pnav-btn[data-view='sessions']").click();
  await until(() => calls.includes("GET /api/projects/-here/sessions?agent=claude"), "sessions reload");
  await until(() => $("#sessionPane").textContent.includes("kept session"), "the sessions under the new name");
  check(!$("#sessionPane").textContent.includes("No sessions"), "its sessions are still shown");

  // a stale confirmation: the server says the name means another directory now (409)
  releaseDelete = null;
  deleteStatus = 409;
  $(".project[data-name='-here'] .pdel").click();
  await until(() => $("#pdConfirm"), "third preview");
  const loadsBefore = listLoads();
  $("#pdConfirm").click();
  await until(() => releaseDelete, "the refused request");
  releaseDelete();
  await until(() => $("#pdStatus").textContent.startsWith("Failed: 409"), "the refusal");
  check($("#pdStatus").textContent.includes("open this dialog again"), "it says to reopen the dialog");
  check(listLoads() === loadsBefore + 1, "the project list was reloaded");
  check($("#pdConfirm").disabled, "the stale dialog can't send again");
  $("#pdConfirm").click();
  await tick();
  check(calls.filter((c) => c.startsWith("POST")).length === 2, "...and doesn't");
  $("#pdCancel").click();
  check(!$(".modal-overlay"), "cancel closes it");

  // the doc editor saves to the project it was opened for, naming the directory it showed
  $(".pnav-btn[data-view='claude']").click();
  await until(() => $("#cmEditor"), "the doc editor");
  check($("#cmEditor").value === "doc of /new/here", "the editor shows the project's doc");
  $("#cmEditor").value = "edited";
  $("#cmSave").click();
  await until(() => docPuts.length === 1, "the save");
  check(docPuts[0].name === "-here" && docPuts[0].body.expect_cwd === "/new/here"
        && docPuts[0].body.content === "edited", "the save names the directory the editor showed");
  const savedStatus = $("#cmStatus");
  $(".pnav-btn[data-view='sessions']").click();  // move on before the save's answer is handled
  await until(() => savedStatus.textContent === "saved ✓", "the save's answer");
  check(true, "a save that finishes after the view changed doesn't break");

  // a doc that arrives after the user moved on is dropped, not shown under another view
  let releaseDoc;
  holdDoc = new Promise((r) => { releaseDoc = r; });
  $(".pnav-btn[data-view='claude']").click();
  await until(() => calls.filter((c) => c === "GET /api/projects/-here/doc?agent=claude").length === 2,
              "the held doc request");
  $(".pnav-btn[data-view='sessions']").click();
  await until(() => $("#sessionPane").textContent.includes("kept session"), "the sessions view");
  holdDoc = null;
  releaseDoc();
  await tick();
  await tick();
  check(!$("#cmEditor") && $("#sessionPane").textContent.includes("kept session"),
        "the late doc didn't replace the sessions view");

  // a view still loading when its project is renamed is asked for again under the new name
  let releaseRenamed;
  holdDoc = new Promise((r) => { releaseRenamed = r; });
  $(".pnav-btn[data-view='claude']").click();
  await until(() => calls.filter((c) => c === "GET /api/projects/-here/doc?agent=claude").length === 3,
              "the doc request held while the project is renamed");
  projects.claude = [{ ...projects.claude[0], name: "-here~bbbbbbbbbbbb" }];
  await w.__loadProjects();
  holdDoc = null;
  releaseRenamed();
  await until(() => $("#cmEditor"), "the view under the new name");
  check(calls.includes("GET /api/projects/-here~bbbbbbbbbbbb/doc?agent=claude")
        && $("#cmEditor").value === "doc of /new/here", "it loaded under the new name, not stuck loading");
  projects.claude = [{ ...projects.claude[0], name: "-here" }];
  await w.__loadProjects();

  // a superseded answer never renders, not even once the name it was for comes back
  // (hashed → plain → hashed), so it can't replace a draft
  const H = "-here~cccccccccccc";
  projects.claude = [{ ...projects.claude[0], name: H }];
  await w.__loadProjects();
  let releaseOld;
  gates[H] = new Promise((r) => { releaseOld = r; });
  $(".pnav-btn[data-view='claude']").click();  // asked for under H, held
  await until(() => calls.includes(`GET /api/projects/${H}/doc?agent=claude`), "the held request under H");
  projects.claude = [{ ...projects.claude[0], name: "-here" }];
  await w.__loadProjects();  // followed to the plain name: asked again, answered at once
  await until(() => calls.filter((c) => c === "GET /api/projects/-here/doc?agent=claude").length === 4,
              "the request under the plain name");
  await until(() => $("#cmEditor") && $("#cmEditor").value === "doc of /new/here", "its editor");
  const draftEditor = $("#cmEditor");
  draftEditor.value = "UNSAVED DRAFT";
  projects.claude = [{ ...projects.claude[0], name: H }];
  await w.__loadProjects();  // H again; nothing in flight for this view, so nothing is reloaded
  check($("#cmEditor") === draftEditor && draftEditor.value === "UNSAVED DRAFT",
        "the draft survives the name coming back");
  delete gates[H];
  releaseOld();  // the request made under H long ago answers now
  await tick();
  await tick();
  check($("#cmEditor") === draftEditor && draftEditor.value === "UNSAVED DRAFT",
        "the superseded answer didn't replace the draft");
  projects.claude = [{ ...projects.claude[0], name: "-here" }];
  await w.__loadProjects();

  // the open project's name comes to mean another directory, and its own is gone:
  // let go of it (an editor open on it must never write into the other directory)
  $(".pnav-btn[data-view='claude']").click();
  await until(() => $("#cmEditor"), "the doc editor again");
  // first a plain rename (same directory): the view follows, the editor stays open and
  // still saves to the name it was opened under; the server refuses, the text stays
  projects.claude = [{ ...projects.claude[0], name: "-here~aaaaaaaaaaaa" }];
  await w.__loadProjects();
  check(w.__state().activeProject === "-here~aaaaaaaaaaaa" && $("#cmEditor"), "renamed: followed, editor kept");
  $("#cmEditor").value = "late edit";
  $("#cmSave").click();
  await until(() => $("#cmStatus").textContent.startsWith("failed: 409"), "the refused save");
  check(docPuts[1].name === "-here" && docPuts[1].body.expect_cwd === "/new/here",
        "the save went to the name and directory the editor was opened for");
  check($("#cmEditor").value === "late edit", "the unsaved text is still there");
  projects.claude = [{ ...projects.claude[0], name: "-here" }];
  await w.__loadProjects();
  $(".pnav-btn[data-view='claude']").click();
  await until(() => $("#cmEditor") && $("#cmEditor").value === "doc of /new/here", "the reopened editor");
  projects.claude = [{ ...projects.claude[0], cwd: "/other/dir", display_path: "/other/dir" }];
  await w.__loadProjects();
  check(w.__state().activeProject === null && !$("#cmEditor"), "the view is closed, not moved along");
  check(row("-here") && !row("-here").classList.contains("active"), "the other directory isn't opened");

  // switching agents closes an open dialog: it can never act on another agent's project
  releaseDelete = null;
  $(".project[data-name='-here'] .pdel").click();
  await until(() => $("#pdConfirm"), "fourth preview");
  set($("#agentSel"), "value", "codex", "change");
  await tick();
  check(!$(".modal-overlay"), "agent switch closed the dialog");
  check(!calls.some((c) => c.startsWith("POST") && c.includes("agent=codex")), "nothing was sent for codex");
  console.log("ALL UI CHECKS PASSED");
})().catch((e) => { console.error(e.message); process.exit(1); });
