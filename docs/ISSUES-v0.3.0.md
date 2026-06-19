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

## 2. Codex / Copilot formats remain UNVERIFIED (no local data)

**Severity:** Medium — adapters ship but are unproven against real data.

`~/.codex` on this machine has only `config.toml` + `tmp/` (no `sessions/`), and
`~/.copilot` has only `ide/`. The Codex adapter assumes Claude-identical turn-per-line
records (`type` in user/assistant, `message.content` blocks, `cwd`, `gitBranch`).
Real Codex rollout files are known in the wild to use a different envelope
(e.g. `{"type":"message","role":...,"content":[{"type":"input_text"|"output_text"}]}`
or `response_item` wrappers). **If real Codex data appears, validate the adapter against
it** — it may need the same kind of rewrite Gemini needed. Same caveat for Copilot.
Until then both are "empty-but-valid" which is the agreed v0.3.0 scope.

## 3. Gemini `<session_context>` prelude is noisy as a "first prompt"

Minor. The first real record is a large synthetic `<session_context>` block. The
revised adapter excludes the prelude `$set.messages` turn from the displayed/`first_prompt`
selection so the session list shows the user's actual first message. If preludes still
leak into search, consider filtering turns whose text starts with `<session_context>`.
