// Thin octokit wrappers for everything the bot does on GitHub, kept free of
// Probot so the runner and handlers stay testable with fakes.

// The open PR by the bot for this issue, identified by branch-name
// convention (branch = `${branchPrefix}/issue-<n>-<slug>`).
export async function findBotPr(octokit, { owner, repo, issueNumber, branchPrefix }) {
  const { data } = await octokit.rest.pulls.list({ owner, repo, state: 'open', per_page: 100 });
  const prefix = `${branchPrefix}/issue-${issueNumber}-`;
  return data.find((pr) => pr.head.ref.startsWith(prefix)) || null;
}

export async function listComments(octokit, { owner, repo, issueNumber }) {
  const { data } = await octokit.rest.issues.listComments({ owner, repo, issue_number: issueNumber, per_page: 100 });
  return data.map((c) => ({ user: c.user.login, body: c.body || '' }));
}

export async function addComment(octokit, { owner, repo, issueNumber, body }) {
  return octokit.rest.issues.createComment({ owner, repo, issue_number: issueNumber, body });
}

export async function createPr(octokit, { owner, repo, title, head, base, body }) {
  const { data } = await octokit.rest.pulls.create({ owner, repo, title, head, base, body });
  return data;
}

// Creates the label if it does not exist yet.
export async function ensureLabel(octokit, { owner, repo, name }) {
  try {
    await octokit.rest.issues.getLabel({ owner, repo, name });
  } catch (err) {
    if (err.status !== 404) throw err;
    await octokit.rest.issues.createLabel({ owner, repo, name, color: 'bfd4f2' });
  }
}

export async function addLabels(octokit, { owner, repo, issueNumber, labels }) {
  return octokit.rest.issues.addLabels({ owner, repo, issue_number: issueNumber, labels });
}

export async function removeLabel(octokit, { owner, repo, issueNumber, name }) {
  try {
    await octokit.rest.issues.removeLabel({ owner, repo, issue_number: issueNumber, name });
  } catch (err) {
    if (err.status !== 404) throw err; // label already gone is fine
  }
}

export async function deleteBranch(octokit, { owner, repo, branch }) {
  try {
    await octokit.rest.git.deleteRef({ owner, repo, ref: `heads/${branch}` });
  } catch (err) {
    if (err.status !== 422) throw err; // 422 = branch does not exist
  }
}
