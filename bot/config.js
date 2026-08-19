// Bot configuration, loaded from the environment with defaults.
// One frozen object is exported; tests pass a custom env map.
import path from 'node:path';

export function list(value) {
  return (value || '').split(',').map((s) => s.trim()).filter(Boolean);
}

export function load(env = process.env) {
  return Object.freeze({
    triggerLabel: env.TRIGGER_LABEL || 'bot:fix',
    doneLabel: env.DONE_LABEL || 'bot:done',
    failedLabel: env.FAILED_LABEL || 'bot:failed',
    // Empty = only the repo owner may trigger the bot.
    allowedAuthors: list(env.ALLOWED_AUTHORS),
    workdirRoot: path.resolve(env.WORKDIR_ROOT || path.join('..', 'data', 'bot-worktrees')),
    claudeCmd: env.CLAUDE_CMD || 'claude',
    claudeArgs: (env.CLAUDE_ARGS || '').split(/\s+/).filter(Boolean),
    permissionMode: env.CLAUDE_PERMISSION_MODE || 'bypassPermissions',
    claudeEnvExtra: list(env.CLAUDE_ENV_EXTRA),
    agentTimeoutMin: Number(env.AGENT_TIMEOUT_MIN || 30),
    verifyCommands: (env.VERIFY_COMMANDS || '').split('\n').map((s) => s.trim()).filter(Boolean),
    branchPrefix: env.BRANCH_PREFIX || 'bot',
    gitName: env.BOT_GIT_NAME || 'cc-mgr-bot[bot]',
    gitEmail: env.BOT_GIT_EMAIL || 'cc-mgr-bot[bot]@users.noreply.github.com',
    repos: list(env.REPOS), // "owner/repo" list for startup reconciliation
    dryRun: env.DRY_RUN === '1',
  });
}

export const defaultConfig = load();
