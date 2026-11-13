// Drives the real frontend (index.html + app.js) in jsdom with a stubbed fetch:
// the project-delete dialog, the missing-directory filter, and agent binding.
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
const projects = {
  claude: [
    { name: "-gone", display_path: "/old/SwarmZoo_paper", cwd: "/old/SwarmZoo_paper", session_count: 2,
      mtime: 1700000000, cwd_missing: true, has_memory: true },
    { name: "-here", display_path: "/new/here", cwd: "/new/here", session_count: 1, mtime: 1700000100 },
  ],
  codex: [{ name: "-here", display_path: "/codex/here", cwd: "/codex/here", session_count: 5, mtime: 1 }],
};
let releaseDelete;
w.fetch = async (u, opts = {}) => {
  const url = new w.URL(String(u), "http://localhost:8765");
  calls.push(`${opts.method || "GET"} ${url.pathname}?agent=${url.searchParams.get("agent")}`);
  const json = (obj, status = 200) => ({ ok: status < 400, status, statusText: "", json: async () => obj });
  if (url.pathname === "/api/agents") return json([
    { agent_id: "claude", label: "Claude", doc_filename: "CLAUDE.md", has_memory: true, has_tasks: true,
      can_edit_doc: true, can_delete: true, can_export: true, delete_note: "claude note", has_data: true },
    { agent_id: "codex", label: "Codex", doc_filename: "AGENTS.md", has_memory: false, has_tasks: false,
      can_edit_doc: true, can_delete: true, can_export: true, delete_note: "codex note", has_data: true }]);
  if (url.pathname === "/api/projects") return json(projects[url.searchParams.get("agent")] || []);
  if (url.pathname.endsWith("/delete-preview"))
    return json({ sessions: 2, subagents: 1, hidden: 1, memory_files: 3, newest: 1700000000, outside: 0 });
  if (url.pathname.endsWith("/delete")) {
    await new Promise((r) => { releaseDelete = r; });
    return json({ sessions: ["a", "b", "c"], trash: ["/t/x"], exported: ["/e/a.md"], kept: [],
                  errors: {}, warnings: [], project_removed: true });
  }
  return json({});
};
w.localStorage.setItem("cc_agent", "claude");
w.eval(fs.readFileSync(root + "/app.js", "utf8") + "\n;window.__state = () => state;");

const $ = (s) => w.document.querySelector(s);
const tick = () => new Promise((r) => setTimeout(r, 20));
async function until(fn, what) {
  for (let i = 0; i < 200; i++) { if (fn()) return; await tick(); }
  throw new Error("timed out waiting for " + what);
}
function check(cond, what) { if (!cond) throw new Error("FAILED: " + what); console.log("ok -", what); }
function set(el, prop, value, ev) { el[prop] = value; el.dispatchEvent(new w.Event(ev, { bubbles: true })); }

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

  // project delete dialog
  const before = w.__state().activeProject;
  $(".project[data-name='-gone'] .pdel").click();
  check(w.__state().activeProject === before, "the delete button doesn't open the project");
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
  releaseDelete();
  await until(() => !$(".modal-overlay"), "dialog closes");
  check(calls.includes("POST /api/projects/-gone/delete?agent=claude"), "deleted with the agent it previewed");
  check(calls.includes("GET /api/projects/-gone/delete-preview?agent=claude"), "previewed with that agent");
  check($(".flash") && $(".flash").textContent.startsWith("Removed 3 sessions (reversible: /t/x)."), "result message");

  // switching agents closes an open dialog: it can never act on another agent's project
  releaseDelete = null;
  $(".project[data-name='-here'] .pdel").click();
  await until(() => $("#pdConfirm"), "second preview");
  set($("#agentSel"), "value", "codex", "change");
  await tick();
  check(!$(".modal-overlay"), "agent switch closed the dialog");
  check(!calls.some((c) => c.startsWith("POST") && c.includes("agent=codex")), "nothing was sent for codex");
  console.log("ALL UI CHECKS PASSED");
})().catch((e) => { console.error(e.message); process.exit(1); });
