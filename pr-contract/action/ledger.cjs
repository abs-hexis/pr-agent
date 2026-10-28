'use strict';

// The next release accrues inside one draft GitHub Release per repository. Its body is rendered from a
// JSON ledger kept in a fenced block at the bottom, so every merge can regenerate the whole body and
// the release day only has to publish it.

const LEDGER_START = '<!-- pr-contract-ledger -->';
const LEDGER_END = '<!-- /pr-contract-ledger -->';
const DRAFT_NAME = 'Next release';
const DRAFT_TAG = 'next-release';

function readLedger(body) {
  const start = (body || '').indexOf(LEDGER_START);
  const end = (body || '').indexOf(LEDGER_END);
  if (start === -1 || end === -1) return { startedAt: null, entries: [] };
  const raw = body.slice(start + LEDGER_START.length, end).replace(/```json|```/g, '').trim();
  try {
    const parsed = JSON.parse(raw);
    return { startedAt: parsed.startedAt || null, entries: Array.isArray(parsed.entries) ? parsed.entries : [] };
  } catch {
    return { startedAt: null, entries: [] };
  }
}

function upsertEntry(ledger, contract, now) {
  const entries = ledger.entries.filter((e) => e.number !== contract.number);
  entries.push(contract);
  entries.sort((a, b) => a.number - b.number);
  return { startedAt: ledger.startedAt || now, entries };
}

function tierLabel(tier) {
  return { 0: 'cosmetic', 1: 'contained', 2: 'flow', '3a': 'data/contract', '3b': 'pipeline/infra' }[tier] || tier;
}

function isUserFacing(entry) {
  const note = (entry.releaseNote || '').trim().toLowerCase();
  return note && note !== 'internal' && note !== 'none';
}

function renderBody(ledger, { repo, mainline }) {
  const entries = ledger.entries;
  const userFacing = entries.filter(isUserFacing);
  const internal = entries.filter((e) => !isUserFacing(e));
  const withActions = entries.filter((e) => e.deployActions && !/^(none|n\/a)$/i.test(e.deployActions.trim()));
  const withDeps = entries.filter((e) => Array.isArray(e.dependencies) && e.dependencies.length > 0);
  const needCounts = {};
  for (const e of entries) for (const need of Object.keys(e.needs || {})) needCounts[need] = (needCounts[need] || 0) + 1;
  const tickets = [...new Set(entries.map((e) => e.ticket).filter(Boolean))].sort();
  const storeBuild = entries.some((e) => (e.mobileDelivery || '').toLowerCase().includes('store'));

  const lines = [];
  lines.push(`# Next release for \`${repo}\``);
  lines.push('');
  lines.push(`Accruing from \`${mainline}\` since ${ledger.startedAt ? ledger.startedAt.slice(0, 10) : 'now'} · ${entries.length} merged PR${entries.length === 1 ? '' : 's'} · tickets: ${tickets.length}`);
  lines.push('');
  lines.push('## Release notes');
  lines.push('');
  lines.push('### For coaches and athletes');
  if (userFacing.length === 0) lines.push('- nothing user-facing yet');
  for (const e of userFacing) lines.push(`- ${e.releaseNote.split('\n')[0]} (${e.ticket || 'no ticket'}, #${e.number}, tier ${e.tier})`);
  lines.push('');
  lines.push('### Internal');
  if (internal.length === 0) lines.push('- none');
  for (const e of internal) lines.push(`- ${e.title} (${e.ticket || 'no ticket'}, #${e.number}, tier ${e.tier})`);
  lines.push('');
  lines.push('## Deploy actions before or during this release');
  lines.push('');
  if (withActions.length === 0) lines.push('None declared.');
  else {
    lines.push('| PR | Ticket | Tier | Actions |');
    lines.push('| --- | --- | --- | --- |');
    for (const e of withActions) lines.push(`| #${e.number} | ${e.ticket || ''} | ${e.tier} | ${e.deployActions.replace(/\|/g, '/').replace(/\n+/g, '<br>')} |`);
  }
  lines.push('');
  lines.push('## Cross-repo dependencies');
  lines.push('');
  if (withDeps.length === 0) lines.push('No cross-repo dependencies declared. This repository can release alone.');
  else {
    lines.push('| This PR | Repository | Their PR | Order | Why |');
    lines.push('| --- | --- | --- | --- | --- |');
    for (const e of withDeps) {
      for (const d of e.dependencies) {
        const ref = d.pr === 'unopened' ? 'unopened' : `[#${d.pr.number}](${d.pr.url})${d.state ? ` (${d.state})` : ''}`;
        lines.push(`| #${e.number} ${e.ticket || ''} | ${d.repository} | ${ref} | ${d.order} | ${(d.why || '').replace(/\|/g, '/')} |`);
      }
    }
    const before = withDeps.flatMap((e) => e.dependencies).filter((d) => d.order === 'before');
    if (before.length) lines.push('', `**Release order:** ${[...new Set(before.map((d) => d.repository))].join(', ')} must reach production before this repository.`);
  }
  lines.push('');
  lines.push('## What this release needs (derived from the diffs)');
  lines.push('');
  const needLines = Object.entries(needCounts).map(([n, c]) => `${n} ×${c}`);
  lines.push(needLines.length ? `- ${needLines.join(' · ')}` : '- nothing beyond the code');
  if (storeBuild) lines.push('- **mobile: store build required** (a merged PR declared Store build)');
  lines.push('');
  lines.push('## Watch after deploy');
  lines.push('');
  const watch = entries.filter((e) => e.operationalEvidence && e.operationalEvidence.toLowerCase() !== 'existing coverage');
  if (watch.length === 0) lines.push('- existing coverage only');
  for (const e of watch) lines.push(`- #${e.number} ${e.ticket || ''}: ${e.operationalEvidence.replace(/\n+/g, ' ').slice(0, 300)}`);
  lines.push('');
  lines.push('## Tickets for the Jira version');
  lines.push('');
  lines.push(tickets.length ? tickets.join(', ') : 'none');
  lines.push('');
  lines.push('## Tier mix');
  lines.push('');
  const mix = {};
  for (const e of entries) mix[e.tier] = (mix[e.tier] || 0) + 1;
  lines.push(Object.entries(mix).map(([t, c]) => `${t} ${tierLabel(t)} ×${c}`).join(' · ') || 'none');
  lines.push('');
  lines.push(LEDGER_START);
  lines.push('```json');
  lines.push(JSON.stringify({ startedAt: ledger.startedAt, entries }, null, 2));
  lines.push('```');
  lines.push(LEDGER_END);
  return lines.join('\n');
}

function releaseTitleForDate(repo, date) {
  const short = repo.split('/')[1];
  return `${short} ${date}`;
}

module.exports = { readLedger, upsertEntry, renderBody, releaseTitleForDate, DRAFT_NAME, DRAFT_TAG, LEDGER_START, LEDGER_END };
