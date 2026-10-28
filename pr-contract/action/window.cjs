'use strict';

// The release window: one page that joins the "Next release" drafts of every production repository.
// Rendered from the ledgers, so it needs no typing; it is written to one GitHub issue (and can be mirrored).

const ledgerLib = require('./ledger.cjs');

const ISSUE_TITLE = 'Release window';
const ISSUE_MARKER = '<!-- release-window -->';

// Repositories that others declare as "before" sort first.
function orderOf(repo, drafts) {
  const before = new Set();
  for (const draft of drafts) {
    for (const entry of draft.ledger.entries) {
      for (const dep of entry.dependencies || []) if (dep.order === 'before') before.add(dep.repository);
    }
  }
  return before.has(repo.split('/')[1]) ? 0 : 1;
}

function renderWindow(drafts, { jiraStatuses = {}, generatedAt }) {
  // drafts: [{ repo, mainline, draftUrl, ledger }]
  const lines = [ISSUE_MARKER, `# Release window`, '', `Generated ${generatedAt} from the draft "Next release" of each repository. Edit a PR body, not this page.`, ''];

  const withEntries = drafts.filter((d) => d.ledger.entries.length);
  const ordered = [...withEntries].sort((a, b) => orderOf(a.repo, withEntries) - orderOf(b.repo, withEntries) || a.repo.localeCompare(b.repo));

  lines.push('## What is waiting, per repository');
  lines.push('');
  lines.push('| Repository | PRs | Tickets | Tier mix | Needs | Store build | Draft |');
  lines.push('| --- | --- | --- | --- | --- | --- | --- |');
  for (const d of drafts) {
    const entries = d.ledger.entries;
    const tickets = [...new Set(entries.map((e) => e.ticket).filter(Boolean))];
    const mix = {};
    for (const e of entries) mix[e.tier] = (mix[e.tier] || 0) + 1;
    const needs = {};
    for (const e of entries) for (const n of Object.keys(e.needs || {})) needs[n] = (needs[n] || 0) + 1;
    const store = entries.some((e) => (e.mobileDelivery || '').toLowerCase().includes('store')) ? 'yes' : '';
    lines.push(`| ${d.repo.split('/')[1]} | ${entries.length} | ${tickets.length} | ${Object.entries(mix).map(([t, c]) => `${t}×${c}`).join(' ') || ''} | ${Object.entries(needs).map(([n, c]) => `${n}×${c}`).join(' ') || ''} | ${store} | ${d.draftUrl ? `[draft](${d.draftUrl})` : 'none yet'} |`);
  }
  lines.push('');

  lines.push('## Release order from declared dependencies');
  lines.push('');
  const deps = [];
  for (const d of withEntries) {
    for (const e of d.ledger.entries) {
      for (const dep of e.dependencies || []) {
        deps.push({ from: d.repo.split('/')[1], fromNumber: e.number, ticket: e.ticket, repository: dep.repository, theirPr: dep.pr, order: dep.order, state: dep.state, why: dep.why });
      }
    }
  }
  if (!deps.length) lines.push('No cross-repo dependencies declared. Repositories can release independently.');
  else {
    lines.push('| Repository | PR | Needs | Their PR | Order | State | Why |');
    lines.push('| --- | --- | --- | --- | --- | --- | --- |');
    for (const dep of deps) {
      const ref = dep.theirPr === 'unopened' ? 'unopened' : dep.theirPr && dep.theirPr.number ? `[#${dep.theirPr.number}](${dep.theirPr.url})` : '';
      lines.push(`| ${dep.from} | #${dep.fromNumber} ${dep.ticket || ''} | ${dep.repository} | ${ref} | ${dep.order} | ${dep.state || ''} | ${(dep.why || '').replace(/\|/g, '/')} |`);
    }
    const first = [...new Set(deps.filter((d) => d.order === 'before').map((d) => d.repository))];
    if (first.length) lines.push('', `**Deploy first:** ${first.join(', ')}.`);
    const unopened = deps.filter((d) => d.theirPr === 'unopened');
    if (unopened.length) lines.push(`**Still unopened:** ${unopened.map((d) => `${d.repository} for ${d.from} ${d.ticket || ''}`).join('; ')}.`);
  }
  lines.push('');

  lines.push('## Deploy actions across repositories');
  lines.push('');
  const actions = [];
  for (const d of withEntries) for (const e of d.ledger.entries) if (e.deployActions && !/^(none|n\/a)$/i.test(e.deployActions.trim())) actions.push({ repo: d.repo.split('/')[1], e });
  if (!actions.length) lines.push('None declared.');
  else {
    lines.push('| Repository | PR | Ticket | Tier | Actions |');
    lines.push('| --- | --- | --- | --- | --- |');
    for (const { repo, e } of actions) lines.push(`| ${repo} | #${e.number} | ${e.ticket || ''} | ${e.tier} | ${e.deployActions.replace(/\|/g, '/').replace(/\n+/g, '<br>')} |`);
  }
  lines.push('');

  lines.push('## Tickets and QA status');
  lines.push('');
  const allTickets = [...new Set(withEntries.flatMap((d) => d.ledger.entries.map((e) => e.ticket)).filter(Boolean))].sort();
  if (!allTickets.length) lines.push('No tickets.');
  else {
    lines.push('| Ticket | Status | Summary | Repositories |');
    lines.push('| --- | --- | --- | --- |');
    for (const t of allTickets) {
      const repos = withEntries.filter((d) => d.ledger.entries.some((e) => e.ticket === t)).map((d) => d.repo.split('/')[1]);
      const s = jiraStatuses[t];
      lines.push(`| [${t}](https://hexisapp.atlassian.net/browse/${t}) | ${s ? s.status : 'unknown'} | ${s ? s.summary.replace(/\|/g, '/') : ''} | ${repos.join(', ')} |`);
    }
    const notDone = allTickets.filter((t) => jiraStatuses[t] && !/qa done|done/i.test(jiraStatuses[t].status));
    if (Object.keys(jiraStatuses).length) lines.push('', `**Not QA Done:** ${notDone.length ? notDone.join(', ') : 'none'}.`);
  }
  lines.push('');

  lines.push('## Watch after deploy');
  lines.push('');
  const watch = withEntries.flatMap((d) => d.ledger.entries.filter((e) => e.operationalEvidence && e.operationalEvidence.toLowerCase() !== 'existing coverage').map((e) => ({ repo: d.repo.split('/')[1], e })));
  if (!watch.length) lines.push('- existing coverage only');
  for (const { repo, e } of watch) lines.push(`- ${repo} #${e.number} ${e.ticket || ''}: ${e.operationalEvidence.replace(/\n+/g, ' ').slice(0, 240)}`);
  lines.push('');
  lines.push('## Release notes draft');
  lines.push('');
  for (const d of withEntries) {
    const userFacing = d.ledger.entries.filter((e) => e.releaseNote && !/^(internal|none)$/i.test(e.releaseNote.trim()));
    if (!userFacing.length) continue;
    lines.push(`### ${d.repo.split('/')[1]}`);
    for (const e of userFacing) lines.push(`- ${e.releaseNote.split('\n')[0]} (${e.ticket || ''})`);
    lines.push('');
  }
  return lines.join('\n');
}

async function collectDrafts(gh, repos, config) {
  const drafts = [];
  for (const repo of repos) {
    const profile = config.profiles[config.repositories[repo]];
    let ledger = ledgerLib.readLedger('');
    let draftUrl = null;
    try {
      const releases = await gh.listReleases(repo);
      const draft = releases.find((r) => r.draft && r.name === ledgerLib.DRAFT_NAME);
      if (draft) {
        ledger = ledgerLib.readLedger(draft.body);
        draftUrl = draft.html_url;
      }
    } catch (error) {
      ledger = ledgerLib.readLedger('');
      draftUrl = null;
    }
    drafts.push({ repo, mainline: profile ? profile.mainline : 'development', draftUrl, ledger });
  }
  return drafts;
}

module.exports = { renderWindow, collectDrafts, ISSUE_TITLE, ISSUE_MARKER };
