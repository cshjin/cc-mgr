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

// Changed files including untracked ones. Uses --porcelain -z (NUL-separated
// records, no C-style quoting) so non-ASCII/quoted filenames survive intact;
// rename records are "R  <dest>\0<src>\0" — the source path is skipped.
export async function changedFiles(cwd) {
  const { stdout } = await run(cwd, ['status', '--porcelain', '-z']);
  const files = [];
  const fields = stdout.split('\0');
  for (let i = 0; i < fields.length; i++) {
    const f = fields[i];
    if (f.length < 3 || f[2] !== ' ') continue; // not a record header
    files.push(f.slice(3));
    if (f[0] === 'R' || f[0] === 'C') i++; // skip the rename/copy source path
  }
  return files;
}

export function pushUrl(remoteUrl, token) {
  return remoteUrl.startsWith('https://')
    ? remoteUrl.replace('https://', `https://x-access-token:${token}@`)
    : remoteUrl;
}

// Pushes using the one-shot token embedded only in the push URL. On failure
// rethrows an error built from stderr only — execFile's default error
// message echoes the full command line, which would leak the token into logs.
export async function push(cwd, remoteUrl, token, branch) {
  try {
    return await run(cwd, ['push', pushUrl(remoteUrl, token), branch]);
  } catch (err) {
    const clean = new Error(String(err.stderr).slice(-2000) || 'git push failed');
    throw clean;
  }
}
