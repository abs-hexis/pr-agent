#!/usr/bin/env node
'use strict';

// Read-only replay: what would the PR contract have said about recently merged PRs?
// Uses the local `gh` CLI for API access. Writes nothing to GitHub.
//   node pr-contract/scripts/replay.cjs abs-hexis/hexis-backend 15

const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const contract = require('../action/contract.cjs');

const config = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'config', 'profiles.json'), 'utf8'));

function gh(args) {
  return JSON.parse(execFileSync('gh', args, { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 }));
}

function main() {
  const repo = process.argv[2];
  const limit = Number(process.argv[3] || 15);
  const profileName = config.repositories[repo];
  if (!profileName) throw new Error(`no profile for ${repo}`);
  const profile = config.profiles[profileName];
  const prs = gh(['pr', 'list', '--repo', repo, '--state', 'merged', '--base', profile.mainline, '--limit', String(limit), '--json', 'number']);
  const rows = [];
  const findingCounts = {};
  for (const { number } of prs) {
    const pr = gh(['api', `repos/${repo}/pulls/${number}`]);
    const files = gh(['api', '--paginate', `repos/${repo}/pulls/${number}/files`]);
    const result = contract.evaluate({ pr, files, config, profileName });
    if (result.skipped.length) {
      rows.push({ number, title: pr.title, skipped: result.skipped.join(',') });
      continue;
    }
    for (const f of result.findings) {
      const key = f.split(/[`:.]/)[0].trim().slice(0, 60);
      findingCounts[key] = (findingCounts[key] || 0) + 1;
    }
    rows.push({ number, title: pr.title.slice(0, 60), declared: result.contract.declaredTier, derived: result.contract.derivedTier, tier: result.tier, needs: Object.keys(result.needs).join(','), findings: result.findings.length });
  }
  console.log(`\n${repo} (${profileName}) — last ${rows.length} merged PRs into ${profile.mainline}`);
  console.table(rows);
  console.log('Most common findings:');
  for (const [k, v] of Object.entries(findingCounts).sort((a, b) => b[1] - a[1]).slice(0, 8)) console.log(`  ${v}× ${k}`);
}

main();
