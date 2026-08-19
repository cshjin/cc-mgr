// Git operations on the bot's own fresh clone. The clone carries no
// credentials; push() receives a one-shot token from the caller and embeds
// it only in that single push URL — it is never written to disk.
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
const MAX_BUFFER = 16 * 1024 * 1024;

function run(cwd, args) {
  return execFileAsync('git', args, { cwd, maxBuffer: MAX_BUFFER });
}

export async function cloneShallow(url, dir) {
  return run(undefined, ['clone', '--depth', '1', url, dir]);
}

export async function checkoutNewBranch(cwd, branch) {
  return run(cwd, ['checkout', '-b', branch]);
}

export async function commitAll(cwd, message, authorName, authorEmail) {
  await run(cwd, ['add', '-A']);
  return run(cwd, [
    '-c', `user.name=${authorName}`,
    '-c', `user.email=${authorEmail}`,
    'commit', '-m', message,
  ]);
}

export async function hasDiff(cwd) {
  const { stdout } = await run(cwd, ['status', '--porcelain']);
  return stdout.trim().length > 0;
}

// Changed files including untracked ones; resolves "old -> new" rename lines.
export async function changedFiles(cwd) {
  const { stdout } = await run(cwd, ['status', '--porcelain']);
  return stdout
    .split('\n')
    .map((line) => {
      const rest = line.slice(3).trim();
      const arrow = rest.lastIndexOf(' -> ');
      return arrow === -1 ? rest : rest.slice(arrow + 4);
    })
    .filter(Boolean);
}

export function pushUrl(remoteUrl, token) {
  return remoteUrl.startsWith('https://')
    ? remoteUrl.replace('https://', `https://x-access-token:${token}@`)
    : remoteUrl;
}

export async function push(cwd, remoteUrl, token, branch) {
  return run(cwd, ['push', pushUrl(remoteUrl, token), branch]);
}
