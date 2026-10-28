'use strict';

// Jira adapter for the PR contract. Enabled only when JIRA_EMAIL and JIRA_API_TOKEN are present.
// Two writes, both idempotent:
//   on merge to the mainline:   fixVersion "<app> next" on the ticket + one marked comment with the contract
//   on release to production:   rename "<app> next" to "<app> <date>", release it, create the next "<app> next",
//                               comment "released" on every ticket the draft carried
// Pure helpers (names, comment text, ADF) are exported for tests; network calls go through client().

const COMMENT_MARKER = 'pr-contract';

function nextVersionName(prefix) {
  return `${prefix} next`;
}

function releasedVersionName(prefix, date) {
  return `${prefix} ${date}`;
}

function mergedCommentText(contract, repo) {
  const deps = (contract.dependencies || [])
    .map((d) => `${d.repository} ${d.pr === 'unopened' ? 'unopened' : `#${d.pr.number}`} (${d.order}${d.state ? `, ${d.state}` : ''})`)
    .join('; ');
  const lines = [
    `Merged to ${repo.split('/')[1]} mainline: PR #${contract.number} (tier ${contract.tier}).`,
    `Release note: ${contract.releaseNote || 'internal'}`,
    `Deploy actions: ${(contract.deployActions || 'none').replace(/\s*\n\s*/g, ' / ')}`,
    `Cross-repo dependencies: ${deps || 'none'}`,
  ];
  if (contract.mobileDelivery) lines.push(`Mobile delivery: ${contract.mobileDelivery}`);
  lines.push(`Operational evidence: ${(contract.operationalEvidence || '').replace(/\s*\n\s*/g, ' / ').slice(0, 400)}`);
  return lines;
}

function releasedCommentText(repo, versionName, releaseUrl) {
  return [`Released in ${versionName} (${repo.split('/')[1]}).`, `Release notes: ${releaseUrl}`];
}

// Atlassian Document Format: one paragraph per line, the first line carries the marker as plain text so we can find it again.
function adfDocument(lines, marker) {
  const paragraphs = lines.map((text) => ({ type: 'paragraph', content: [{ type: 'text', text }] }));
  if (marker) paragraphs.unshift({ type: 'paragraph', content: [{ type: 'text', text: `[${marker}]`, marks: [{ type: 'code' }] }] });
  return { type: 'doc', version: 1, content: paragraphs };
}

function adfText(doc) {
  const out = [];
  (function walk(node) {
    if (!node) return;
    if (node.type === 'text' && node.text) out.push(node.text);
    (node.content || []).forEach(walk);
  })(doc);
  return out.join('\n');
}

function client({ baseUrl, email, token }) {
  if (!baseUrl || !email || !token) throw new Error('Jira needs baseUrl, email and token');
  const auth = Buffer.from(`${email}:${token}`).toString('base64');

  async function request(method, path, body) {
    const response = await fetch(`${baseUrl}${path}`, {
      method,
      headers: { Authorization: `Basic ${auth}`, Accept: 'application/json', 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (response.status === 204) return null;
    const text = await response.text();
    const json = text ? JSON.parse(text) : null;
    if (!response.ok) throw new Error(`Jira ${method} ${path}: ${response.status} ${text.slice(0, 300)}`);
    return json;
  }

  return {
    listVersions: (project) => request('GET', `/rest/api/3/project/${project}/versions`),
    createVersion: (project, name) => request('POST', '/rest/api/3/version', { name, project }),
    updateVersion: (id, fields) => request('PUT', `/rest/api/3/version/${id}`, fields),
    addFixVersion: (issueKey, versionName) => request('PUT', `/rest/api/3/issue/${issueKey}`, { update: { fixVersions: [{ add: { name: versionName } }] } }),
    listComments: (issueKey) => request('GET', `/rest/api/3/issue/${issueKey}/comment?maxResults=100`),
    addComment: (issueKey, body) => request('POST', `/rest/api/3/issue/${issueKey}/comment`, { body }),
    updateComment: (issueKey, id, body) => request('PUT', `/rest/api/3/issue/${issueKey}/comment/${id}`, { body }),
    getIssue: (issueKey) => request('GET', `/rest/api/3/issue/${issueKey}?fields=status,summary,fixVersions`),
  };
}

async function ensureVersion(jira, project, name) {
  const versions = await jira.listVersions(project);
  const existing = versions.find((v) => v.name === name);
  return existing || jira.createVersion(project, name);
}

async function upsertMarkedComment(jira, issueKey, lines, marker) {
  const body = adfDocument(lines, marker);
  const existing = (await jira.listComments(issueKey)).comments.find((c) => adfText(c.body).includes(`[${marker}]`));
  if (existing) return jira.updateComment(issueKey, existing.id, body);
  return jira.addComment(issueKey, body);
}

// Called when a PR merges to the mainline.
async function recordMerge(jira, { project, prefix, contract, repo }) {
  if (!contract.ticket) return { skipped: 'no ticket' };
  const version = await ensureVersion(jira, project, nextVersionName(prefix));
  await jira.addFixVersion(contract.ticket, version.name);
  await upsertMarkedComment(jira, contract.ticket, mergedCommentText(contract, repo), `${COMMENT_MARKER} ${repo.split('/')[1]}`);
  return { version: version.name, ticket: contract.ticket };
}

// Called when a release PR merges to production.
async function recordRelease(jira, { project, prefix, date, tickets, repo, releaseUrl }) {
  const versions = await jira.listVersions(project);
  const next = versions.find((v) => v.name === nextVersionName(prefix));
  const releasedName = releasedVersionName(prefix, date);
  if (next) {
    await jira.updateVersion(next.id, { name: releasedName, released: true, releaseDate: date });
  } else {
    await jira.createVersion(project, releasedName).then((v) => jira.updateVersion(v.id, { released: true, releaseDate: date }));
  }
  await ensureVersion(jira, project, nextVersionName(prefix));
  for (const ticket of tickets) {
    await jira.addFixVersion(ticket, releasedName).catch(() => {});
    await upsertMarkedComment(jira, ticket, releasedCommentText(repo, releasedName, releaseUrl), `${COMMENT_MARKER} released ${repo.split('/')[1]}`).catch(() => {});
  }
  return { version: releasedName, tickets: tickets.length };
}

async function ticketStatuses(jira, tickets) {
  const out = {};
  for (const key of tickets) {
    try {
      const issue = await jira.getIssue(key);
      out[key] = { status: issue.fields.status?.name || '', summary: issue.fields.summary || '', fixVersions: (issue.fields.fixVersions || []).map((v) => v.name) };
    } catch (error) {
      out[key] = { status: 'lookup failed', summary: '', fixVersions: [] };
    }
  }
  return out;
}

module.exports = { client, nextVersionName, releasedVersionName, mergedCommentText, releasedCommentText, adfDocument, adfText, recordMerge, recordRelease, ticketStatuses, COMMENT_MARKER };
