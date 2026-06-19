# v0.3.0 implementation issues & findings

Running log of problems found during multi-agent implementation. Review later.

## 1. Gemini real transcript format differs from the design spec (FIXED in T5-revised)

**Severity:** Critical — would have shown empty/near-empty Gemini conversations.

**What the spec/fixture assumed:** conversation messages live inside event records
`{"$set": {"messages": [ {id,type,content:[{text}]}, ... ]}}`, and the parser folds
to the *last* `$set.messages` array.

**What real `~/.gemini/tmp/<project>/chats/session-*.jsonl` actually contains**
(verified against `lumina-core/session-2026-06-01T12-57-c9883b19.jsonl`, 220 KB, 34 lines):

- Line 0: session meta `{sessionId, projectHash, startTime, lastUpdated, kind}`.
- Line 1: ONE `$set` whose `messages[]` holds only the `<session_context>` prelude
  (a single synthetic user turn). `messages[]` length is always 1 here.
- Remaining lines alternate between:
  - `{"$set": {"lastUpdated": "..."}}` — bookkeeping, no messages.
  - **Bare top-level turn records**: `{id, timestamp, type, content, thoughts?,
    tokens?, model?, toolCalls?}`. These are the real conversation, appended one per
    line. They are NOT wrapped in `$set`/`messages`.

**Turn record specifics:**
- `type` is `"user"` or `"gemini"` (not `"assistant"`).
- `content` is polymorphic:
  - gemini answers: a plain **string** (often `""` when the turn is only a tool call).
  - user prompts: a list `[{"text": "..."}]`.
  - tool results: a list `[{"functionResponse": {...}}]` (these are `type:"user"`).
- `thoughts`: list of `{subject, description}` (model reasoning) — usually present on
  gemini turns; not shown in the viewer but useful for search.
- `toolCalls`: list of `{id, name, args, result:[{functionResponse:{...}}]}` on gemini
  turns that call tools.

**Consequence of the original parser:** `_fold_messages` returned only the line-1
prelude, so every real Gemini session rendered as a single `<session_context>` turn
and the entire conversation was dropped. Search would index nothing useful.

**Fix applied (T5-revised):** the Gemini parser now reads BOTH shapes:
1. the line-1 `$set.messages` prelude (skipped from display by default — it's the
   synthetic context block), and
2. bare top-level turn records, in file order.
`content` is normalized via a helper that handles str / `[{text}]` / `[{functionResponse}]`,
maps `type:"gemini"` → assistant, and surfaces tool calls as `tool_use` blocks and
function responses as `tool_result` blocks. The synthetic test fixture in
`tests/conftest.py` was rewritten to mirror this real layout.

## 2. Codex real transcript format differs from the design spec (FIXED in T6-revised)

**Severity:** Critical — would have shown empty Codex conversations + "unknown" project.

Real Codex data WAS found at `$CODEX_HOME=/pscratch/sd/j/jinh/.codex` (the env var
points off the home dir; `~/.codex` itself only has `config.toml` + `tmp/`). Three real
`sessions/YYYY/MM/DD/rollout-*.jsonl` files exist there.

**Real format (verified against
`rollout-2026-06-05T18-26-48-019e9a8a-...jsonl`, 211 lines):**

Every line is an envelope `{timestamp, type, payload}`. `type` is one of:
- `session_meta` — `payload` has `id, timestamp, cwd, originator, cli_version, ...`.
  **This is where cwd comes from** (NOT a per-record `cwd` field).
- `turn_context` — bookkeeping.
- `event_msg` — UI/telemetry events; `payload.type` includes `task_started`,
  `token_count` (carries `info.total_token_usage.{input,output,total}_tokens`), etc.
- `response_item` — the actual conversation. `payload.type` is one of:
  - `message`: `{role: "user"|"assistant"|"developer", content:[{type:"input_text"|
    "output_text", text}]}`. (`developer` = system prelude; user/assistant are real.)
  - `function_call`: `{name, arguments(JSON string), call_id}` → a tool call.
  - `function_call_output`: `{call_id, output(string)}` → a tool result.

**Consequence of the original (Claude-shaped) parser:** it looked for top-level
`type in (user,assistant)` and a per-record `cwd`. Real records have neither, so it
found 0 turns and grouped everything under a `"unknown"` project with empty cwd.

**Fix applied (T6-revised):** the Codex adapter now unwraps the `{type,payload}`
envelope, reads cwd from `session_meta.payload.cwd`, maps `response_item`/`message`
roles (skipping `developer` prelude by default in counts), renders `input_text`/
`output_text` parts, and surfaces `function_call`/`function_call_output` as
tool_use/tool_result blocks. Context/output tokens come from the last `token_count`
event when present. Synthetic fixture rewritten to the real envelope; validated
against the live `$CODEX_HOME` data.

**Copilot still UNVERIFIED** — `~/.copilot` has only `ide/`, no CLI history dir on
this machine. Its adapter remains "empty-but-valid" (agreed v0.3.0 scope). If real
Copilot history appears, validate/rewrite the same way (it likely has its own
envelope, NOT the Claude shape currently assumed).

## 4. Known limits / scaling notes (not bugs)

- **Turn counts are post-filter, not raw line counts.** Both Gemini and Codex drop
  whitespace-only turns, and Codex drops the `developer` prelude. The `message_count`
  shown (and the index `turns` count) reflects displayable turns, not raw records.
- **Codex re-scans `$CODEX_HOME/sessions` per request.** `_project_map()` rglobs all
  rollout files and reads each one's `session_meta` on every `list_projects` /
  `get_conversation` / `list_sessions` call. Fine for a handful of sessions; add a
  per-request cache if codex histories grow large.
- **Codex sessions with no recoverable cwd** collapse into a synthetic `"unknown"`
  project; doc read/edit there is a no-op (cwd is empty).
- **Gemini projects are discovered only under `tmp/`.** A project present only in
  `history/` (no `tmp/<p>/chats/`) won't be listed, even though `_project_cwd` reads
  `history/<p>/.project_root` as a cwd fallback. Matches intended scope (live chats
  live in `tmp/`), noted for completeness.

## 3. Gemini `<session_context>` prelude is noisy as a "first prompt"

Minor. The first real record is a large synthetic `<session_context>` block. The
revised adapter excludes the prelude `$set.messages` turn from the displayed/`first_prompt`
selection so the session list shows the user's actual first message. If preludes still
leak into search, consider filtering turns whose text starts with `<session_context>`.
