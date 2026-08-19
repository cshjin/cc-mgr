// The agentic job: clone → run headless claude → verify → commit → push →
// PR → issue comment. Every GitHub action goes through github-helpers, and
// DRY_RUN turns external side effects into log lines, so runJob is testable
// with fakes and a local git repo. (Task 7 appends verifyChanges/runJob.)

export function slugify(text, maxLen = 40) {
  const slug = String(text || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, maxLen)
    .replace(/-+$/g, '');
  return slug || 'fix';
}

export function buildPrompt({ issueNumber, title, body, author, comments }) {
  const commentText = comments.length
    ? comments.map((c) => `@${c.user}: ${c.body}`).join('\n\n---\n\n')
    : '(none)';
  return `You are an autonomous coding agent fixing a GitHub issue. The repository is checked out in your working directory.

ISSUE #${issueNumber}: ${title}
Author: @${author}

${body || '(no body)'}

EXISTING COMMENTS:
${commentText}

TASK: Fix the issue described above. Rules:
- Make the smallest change that resolves the issue. Do not refactor unrelated code.
- Follow the repository's own conventions; read CLAUDE.md and related docs if present.
- Do not run git commands, do not commit, do not push, do not create PRs — the orchestrator handles all git.
- Do not modify anything outside the repository directory.
- If the issue cannot be fixed, explain clearly why.
- End your reply with a summary of exactly what you changed and why.`;
}

// Parses `claude -p --output-format json` stdout into { summary, isError }.
// Falls back to raw text when stdout is not JSON.
export function parseResult(stdout) {
  const raw = (stdout || '').trim();
  if (!raw) return { summary: '', isError: false };
  try {
    const parsed = JSON.parse(raw);
    const text = typeof parsed.result === 'string'
      ? parsed.result.trim()
      : (typeof parsed.message === 'string' ? parsed.message.trim() : raw);
    return { summary: text, isError: parsed.is_error === true };
  } catch {
    return { summary: raw, isError: false };
  }
}

// Minimal environment for the claude child: only what the CLI needs. The
// bot's own GitHub credentials are never passed through.
export function claudeEnv(config) {
  const env = {
    PATH: process.env.PATH,
    HOME: process.env.HOME,
    LANG: process.env.LANG || 'C.UTF-8',
  };
  for (const [key, value] of Object.entries(process.env)) {
    if (key.startsWith('ANTHROPIC_')) env[key] = value;
  }
  for (const name of config.claudeEnvExtra) {
    if (process.env[name] !== undefined) env[name] = process.env[name];
  }
  return env;
}
