'use strict';

// Thin GitHub REST adapter. Everything the action needs from GitHub goes through here so the rules
// in contract.cjs and the ledger in ledger.cjs stay free of network calls.

const API = process.env.GITHUB_API_URL || 'https://api.github.com';

function client(token) {
  if (!token) throw new Error('GITHUB_TOKEN is required');

  async function request(method, path, body) {
    const response = await fetch(`${API}${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
        'User-Agent': 'hexis-pr-contract',
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (response.status === 204) return null;
    const text = await response.text();
    const json = text ? JSON.parse(text) : null;
    if (!response.ok) {
      const message = json && json.message ? json.message : text;
      throw new Error(`${method} ${path} failed: ${response.status} ${message}`);
    }
    return json;
  }

  async function paginate(path) {
    const items = [];
    for (let page = 1; page < 50; page += 1) {
      const separator = path.includes('?') ? '&' : '?';
      const batch = await request('GET', `${path}${separator}per_page=100&page=${page}`);
      items.push(...batch);
      if (batch.length < 100) break;
    }
    return items;
  }

  return {
    getPullRequest: (repo, number) => request('GET', `/repos/${repo}/pulls/${number}`),
    listPullRequestFiles: (repo, number) => paginate(`/repos/${repo}/pulls/${number}/files`),
    listIssueComments: (repo, number) => paginate(`/repos/${repo}/issues/${number}/comments`),
    createIssueComment: (repo, number, body) => request('POST', `/repos/${repo}/issues/${number}/comments`, { body }),
    updateIssueComment: (repo, id, body) => request('PATCH', `/repos/${repo}/issues/comments/${id}`, { body }),
    listIssueLabels: (repo, number) => paginate(`/repos/${repo}/issues/${number}/labels`),
    setIssueLabels: (repo, number, labels) => request('PUT', `/repos/${repo}/issues/${number}/labels`, { labels }),
    ensureLabel: async (repo, name, color, description) => {
      try {
        await request('GET', `/repos/${repo}/labels/${encodeURIComponent(name)}`);
      } catch {
        await request('POST', `/repos/${repo}/labels`, { name, color, description });
      }
    },
    listReleases: (repo) => paginate(`/repos/${repo}/releases`),
    createRelease: (repo, payload) => request('POST', `/repos/${repo}/releases`, payload),
    updateRelease: (repo, id, payload) => request('PATCH', `/repos/${repo}/releases/${id}`, payload),
  };
}

const LABEL_COLOURS = {
  'tier:0': 'c2e0c6',
  'tier:1': 'bfd4f2',
  'tier:2': 'fbca04',
  'tier:3a': 'd93f0b',
  'tier:3b': 'b60205',
};

async function upsertStickyComment(gh, repo, number, body, marker) {
  const comments = await gh.listIssueComments(repo, number);
  const existing = comments.find((c) => typeof c.body === 'string' && c.body.includes(marker));
  if (existing) return gh.updateIssueComment(repo, existing.id, body);
  return gh.createIssueComment(repo, number, body);
}

// Replaces only the labels this action owns (tier:* and needs:*); leaves human labels alone.
async function applyOwnedLabels(gh, repo, number, labels) {
  const current = (await gh.listIssueLabels(repo, number)).map((l) => l.name);
  const foreign = current.filter((l) => !l.startsWith('tier:') && !l.startsWith('needs:'));
  for (const label of labels) {
    const colour = LABEL_COLOURS[label] || 'ededed';
    const description = label.startsWith('tier:') ? 'PR contract tier, derived from the diff' : 'Deploy action implied by the diff';
    await gh.ensureLabel(repo, label, colour, description);
  }
  await gh.setIssueLabels(repo, number, [...foreign, ...labels]);
}

module.exports = { client, upsertStickyComment, applyOwnedLabels };
