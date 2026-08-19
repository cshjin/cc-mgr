// Probot app entry: webhook → checks → enqueue. The agentic work lives in
// runner.js; the handlers are factored out so they are testable without a
// running Probot server.
import { Queue } from './queue.js';
import { runJob } from './runner.js';
import * as gh from './github-helpers.js';
import { defaultConfig as config } from './config.js';

export function createHandlers({ config, queue, log = () => {} }) {
  async function handleIssueEvent({ payload, octokit, appOctokit }) {
    const issue = payload.issue;
    if (issue.pull_request) return; // PRs fire issue events too
    const labels = (issue.labels || []).map((l) => l.name);
    if (!labels.includes(config.triggerLabel)) return;

    const allowed = config.allowedAuthors.length > 0
      ? config.allowedAuthors
      : [payload.repository.owner.login];
    if (!allowed.includes(issue.user.login)) {
      log(`ignoring #${issue.number}: author @${issue.user.login} not in ALLOWED_AUTHORS`);
      return;
    }

    // One-shot push token from the app-level octokit; the agent never sees it.
    const { data } = await appOctokit.rest.apps.createInstallationAccessToken({
      installation_id: payload.installation.id,
    });
    const job = {
      owner: payload.repository.owner.login,
      repo: payload.repository.name,
      issueNumber: issue.number,
      title: issue.title,
      body: issue.body || '',
      author: issue.user.login,
      cloneUrl: payload.repository.clone_url,
      defaultBranch: payload.repository.default_branch,
      installationToken: data.token,
    };

    const id = `${job.owner}/${job.repo}#${job.issueNumber}`;
    if (!queue.enqueue(id, () => runJob({ job, config, octokit, log }))) return;

    log(`enqueued ${id}`);
    if (!config.dryRun) {
      await gh.addComment(octokit, { ...job, body: "👷 Working on this — I'll open a PR with the fix shortly." });
    }
  }

  return { handleIssueEvent };
}

// Startup reconciliation: re-enqueue trigger-labeled issues left over from a
// crash (only repos listed in REPOS). The runner's own dedup skips anything
// that already has an open bot PR.
export async function reconcile({ config, queue, log = () => {}, appOctokit, getInstallationOctokit }) {
  for (const repoSpec of config.repos) {
    const [owner, repo] = repoSpec.split('/');
    try {
      const { data: repoData } = await appOctokit.rest.repos.get({ owner, repo });
      const { data: installation } = await appOctokit.rest.apps.getRepoInstallation({ owner, repo });
      const octokit = await getInstallationOctokit(installation.id);
      const { data: issues } = await octokit.rest.issues.listForRepo({
        owner, repo, state: 'open', labels: config.triggerLabel, per_page: 100,
      });
      for (const issue of issues) {
        if (issue.pull_request) continue;
        const { data } = await appOctokit.rest.apps.createInstallationAccessToken({ installation_id: installation.id });
        const job = {
          owner, repo, issueNumber: issue.number, title: issue.title, body: issue.body || '',
          author: issue.user.login, cloneUrl: repoData.clone_url, defaultBranch: repoData.default_branch,
          installationToken: data.token,
        };
        const id = `${owner}/${repo}#${issue.number}`;
        if (!queue.enqueue(id, () => runJob({ job, config, octokit, log }))) continue;
        log(`reconcile: enqueued ${id}`);
      }
    } catch (err) {
      log(`reconcile: ${repoSpec}: ${err.message}`);
    }
  }
}

export default (app) => {
  const queue = new Queue(async (job) => job());
  queue.on('error', (id, err) => app.log.error(`job ${id}: ${err.stack || err}`));
  const handlers = createHandlers({ config, queue, log: (m) => app.log.info(m) });

  app.on(['issues.opened', 'issues.labeled'], async (context) => {
    const appOctokit = await app.auth(); // app-level (JWT) octokit
    await handlers.handleIssueEvent({
      payload: context.payload,
      octokit: context.octokit, // installation-bound
      appOctokit,
    });
  });

  // Fire-and-forget startup reconciliation, deferred until the server is up.
  setTimeout(() => {
    (async () => {
      const appOctokit = await app.auth();
      await reconcile({
        config,
        queue,
        log: (m) => app.log.info(m),
        appOctokit,
        getInstallationOctokit: (id) => app.auth(id),
      });
    })().catch((err) => app.log.error(`reconcile: ${err.stack || err}`));
  }, 10 * 1000);
};
