#!/usr/bin/env node
'use strict';

// Entry point for the composite action. Three modes:
//   check    validate an open PR against the contract, label it, post the sticky comment, fail on findings
//   accrue   a PR merged into the mainline: add its contract to the draft "Next release"
//   publish  a release PR merged into the release branch: publish the draft under the date and start a new one
//
// Environment: GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_EVENT_PATH (or PR_NUMBER), PR_CONTRACT_MODE,
// PR_CONTRACT_PROFILE (optional override), PR_CONTRACT_CONFIG (path to profiles.json).

const fs = require('node:fs');
const path = require('node:path');
const contract = require('./contract.cjs');
const ledgerLib = require('./ledger.cjs');
const { client, upsertStickyComment, applyOwnedLabels } = require('./github.cjs');

const MARKER = '<!-- pr-contract -->';

function loadConfig() {
  const configPath = process.env.PR_CONTRACT_CONFIG || path.join(__dirname, '..', 'config', 'profiles.json');
  return JSON.parse(fs.readFileSync(configPath, 'utf8'));
}

function loadEvent() {
  const eventPath = process.env.GITHUB_EVENT_PATH;
  if (!eventPath || !fs.existsSync(eventPath)) return {};
  return JSON.parse(fs.readFileSync(eventPath, 'utf8'));
}

function resolveProfile(config, repo) {
  const override = process.env.PR_CONTRACT_PROFILE;
  if (override) return override;
  const mapped = config.repositories[repo];
  if (!mapped) throw new Error(`No profile mapped for ${repo}; set PR_CONTRACT_PROFILE or add it to profiles.json`);
  return mapped;
}

function writeOutput(name, value) {
  const file = process.env.GITHUB_OUTPUT;
  if (!file) return;
  fs.appendFileSync(file, `${name}<<EOF\n${value}\nEOF\n`);
}

// Looks up each declared dependency PR so the comment and the ledger show whether it is open,
// merged to its mainline, or already in production. A lookup failure is reported, never fatal.
async function enrichDependencies(gh, result) {
  if (!result.contract || !Array.isArray(result.contract.dependencies)) return;
  for (const dep of result.contract.dependencies) {
    if (dep.pr === 'unopened') {
      dep.state = 'unopened';
      continue;
    }
    try {
      const other = await gh.getPullRequest(`abs-hexis/${dep.pr.repository}`, dep.pr.number);
      if (other.merged_at) dep.state = `merged into ${other.base.ref} ${other.merged_at.slice(0, 10)}`;
      else if (other.state === 'closed') dep.state = 'closed without merge';
      else dep.state = `open against ${other.base.ref}`;
      dep.title = other.title;
      if (other.state === 'closed' && !other.merged_at) {
        result.findings.push(`Dependency ${dep.repository}#${dep.pr.number} was closed without merging.`);
      }
    } catch (error) {
      dep.state = `lookup failed: ${error.message}`;
    }
  }
}

async function modeCheck({ gh, repo, number, config, profileName }) {
  const pr = await gh.getPullRequest(repo, number);
  const files = await gh.listPullRequestFiles(repo, number);
  const result = contract.evaluate({ pr, files, config, profileName });
  await enrichDependencies(gh, result);
  const templateUrl = process.env.PR_CONTRACT_TEMPLATE_URL || '';
  const body = contract.renderComment({ result, profileName, templateUrl });

  if (!result.skipped.length) {
    await applyOwnedLabels(gh, repo, number, result.labels);
  }
  await upsertStickyComment(gh, repo, number, body, MARKER);
  writeOutput('tier', result.tier || '');
  writeOutput('contract', JSON.stringify(result.contract || {}));

  if (result.skipped.length) {
    console.log(`::notice::PR contract skipped: ${result.skipped.join(', ')}`);
    return 0;
  }
  for (const finding of result.findings) console.log(`::error title=PR contract::${finding}`);
  console.log(`PR contract: tier ${result.tier}, ${result.findings.length} finding(s)`);
  return result.findings.length ? 1 : 0;
}

async function findDraft(gh, repo) {
  const releases = await gh.listReleases(repo);
  return releases.find((r) => r.draft && r.name === ledgerLib.DRAFT_NAME) || null;
}

async function modeAccrue({ gh, repo, number, config, profileName }) {
  const pr = await gh.getPullRequest(repo, number);
  if (!pr.merged_at) {
    console.log('::notice::PR is not merged; nothing to accrue');
    return 0;
  }
  const profile = config.profiles[profileName];
  if (pr.base.ref !== profile.mainline) {
    console.log(`::notice::merged into ${pr.base.ref}, not the mainline ${profile.mainline}; nothing to accrue`);
    return 0;
  }
  const files = await gh.listPullRequestFiles(repo, number);
  const result = contract.evaluate({ pr, files, config, profileName });
  if (result.skipped.length || !result.contract) {
    console.log(`::notice::not accrued: ${result.skipped.join(', ') || 'no contract'}`);
    return 0;
  }
  await enrichDependencies(gh, result);
  const draft = await findDraft(gh, repo);
  const ledger = ledgerLib.readLedger(draft ? draft.body : '');
  const updated = ledgerLib.upsertEntry(ledger, result.contract, new Date().toISOString());
  const body = ledgerLib.renderBody(updated, { repo, mainline: profile.mainline });
  if (draft) {
    await gh.updateRelease(repo, draft.id, { body });
  } else {
    await gh.createRelease(repo, {
      tag_name: ledgerLib.DRAFT_TAG,
      target_commitish: profile.mainline,
      name: ledgerLib.DRAFT_NAME,
      body,
      draft: true,
      prerelease: false,
    });
  }
  console.log(`Accrued #${number} into "${ledgerLib.DRAFT_NAME}" (${updated.entries.length} entries)`);
  return 0;
}

async function modePublish({ gh, repo, number, config, profileName }) {
  const pr = await gh.getPullRequest(repo, number);
  const profile = config.profiles[profileName];
  if (!pr.merged_at || pr.base.ref !== profile.releaseBranch) {
    console.log('::notice::not a merged release PR; nothing to publish');
    return 0;
  }
  const draft = await findDraft(gh, repo);
  if (!draft) {
    console.log('::warning::no draft "Next release" to publish; release notes were not accrued for this release');
    return 0;
  }
  const date = pr.merged_at.slice(0, 10);
  const ledger = ledgerLib.readLedger(draft.body);
  const body = `${draft.body}\n\nPublished from release PR #${pr.number} (${pr.merge_commit_sha}) on ${pr.merged_at}.`;
  await gh.updateRelease(repo, draft.id, {
    tag_name: `release/${date}`,
    target_commitish: pr.merge_commit_sha,
    name: ledgerLib.releaseTitleForDate(repo, date),
    body,
    draft: false,
  });
  writeOutput('tickets', ledger.entries.map((e) => e.ticket).filter(Boolean).join(','));
  console.log(`Published ${ledgerLib.releaseTitleForDate(repo, date)} with ${ledger.entries.length} PRs`);
  return 0;
}

async function main() {
  const config = loadConfig();
  const repo = process.env.GITHUB_REPOSITORY;
  if (!repo) throw new Error('GITHUB_REPOSITORY is required');
  const event = loadEvent();
  const number = Number(process.env.PR_NUMBER || (event.pull_request && event.pull_request.number));
  if (!number) throw new Error('No pull request number (PR_NUMBER or pull_request event)');
  const profileName = resolveProfile(config, repo);
  const gh = client(process.env.GITHUB_TOKEN);
  const mode = process.env.PR_CONTRACT_MODE || 'check';
  const args = { gh, repo, number, config, profileName };
  const handlers = { check: modeCheck, accrue: modeAccrue, publish: modePublish };
  if (!handlers[mode]) throw new Error(`Unknown mode ${mode}`);
  const exitCode = await handlers[mode](args);
  process.exitCode = exitCode;
}

if (require.main === module) {
  main().catch((error) => {
    console.error(`::error::${error.message}`);
    process.exitCode = 1;
  });
}

module.exports = { modeCheck, modeAccrue, modePublish, resolveProfile };
