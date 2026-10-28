'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const contract = require('../action/contract.cjs');
const ledger = require('../action/ledger.cjs');

const config = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'config', 'profiles.json'), 'utf8'));
const template = fs.readFileSync(path.join(__dirname, '..', 'templates', 'pull_request_template.md'), 'utf8');
const backendTemplate = fs.readFileSync(path.join(__dirname, '..', 'templates', 'hexis-backend.md'), 'utf8');
const mobileExtra = '## Mobile delivery\n\nOTA\n\n## E2E tests\n\n- [ ] Listed the tests\n';
const jira = require('../action/jira.cjs');
const windowLib = require('../action/window.cjs');

const RUN = 'https://github.com/abs-hexis/hexis-backend/actions/runs/40482633156';
const SHOT = 'https://github.com/user-attachments/assets/3f1c2d9e-before.png';
const NONE_TABLE = '| Repository | PR | Order | Why |\n| --- | --- | --- | --- |\n| none | | | |';
const BACKEND_DEP = '| Repository | PR | Order | Why |\n| --- | --- | --- | --- |\n| hexis-backend | https://github.com/abs-hexis/hexis-backend/pull/559 | before | totalActivityHours field |';

function pr({ number = 559, title = 'feat(HEX-2550): add totalActivityHours', branch = 'HEX-2550-total-activity-hours', body, base = 'development', author = 'Pariitosh', repo = 'abs-hexis/hexis-backend', merged = null }) {
  return {
    number,
    title,
    body,
    merged_at: merged,
    user: { login: author },
    head: { ref: branch, sha: 'abc123' },
    base: { ref: base, repo: { full_name: repo } },
  };
}

function file(filename, additions = 5, deletions = 1, patch = '') {
  return { filename, additions, deletions, patch };
}

function body(fields) {
  const defaults = {
    Change: 'Weekly training hours stored as a number. HEX-2550.',
    Tier: '1',
    'Release note': 'Weekly training hours are saved as a number instead of a band.',
    'Deploy actions': 'none',
    'Client compatibility': 'no API change',
    'Cross-repo dependencies': NONE_TABLE,
    Evidence: `${RUN}\n${SHOT}`,
    'Operational evidence': 'Outcome: profile save returns the hours. Signal: existing GraphQL error rate dashboard. Owner: author, 3 days. Recovery: revert.',
  };
  const merged = { ...defaults, ...fields };
  return Object.entries(merged).map(([k, v]) => `## ${k}\n\n${v}\n`).join('\n');
}

test('template sections parse and the untouched template fails as empty, not as missing', () => {
  const sections = contract.parseSections(template);
  for (const name of contract.REQUIRED_SECTIONS) assert.ok(name.toLowerCase() in sections, `section ${name}`);
  const result = contract.evaluate({ pr: pr({ body: backendTemplate }), files: [file('src/lib/activity-volume.ts')], config, profileName: 'backend' });
  assert.ok(!result.findings.some((f) => f.includes('Missing section')), result.findings.join('\n'));
  assert.ok(result.findings.some((f) => f.startsWith('Change section is empty')));
  assert.ok(result.findings.some((f) => f.startsWith('Release note is empty')));
  // The template's own `none` dependency row is valid as shipped.
  assert.ok(!result.findings.some((f) => f.includes('Cross-repo dependencies')));
});

test('a complete tier 1 backend PR with no deploy needs passes', () => {
  const result = contract.evaluate({ pr: pr({ body: body({}) }), files: [file('src/lib/activity-volume.ts')], config, profileName: 'backend' });
  assert.deepEqual(result.findings, []);
  assert.equal(result.tier, '1');
  assert.deepEqual(result.labels, ['tier:1']);
  assert.equal(result.contract.ticket, 'HEX-2550');
  assert.deepEqual(result.contract.dependencies, []);
});

test('a prisma migration raises the tier to 3a and demands a migration line in Deploy actions', () => {
  const files = [file('prisma/migrations/20261021062523_add_total_activity_hours/migration.sql'), file('prisma/schema.prisma'), file('src/lib/activity-volume.ts')];
  const result = contract.evaluate({ pr: pr({ body: body({}) }), files, config, profileName: 'backend' });
  assert.equal(result.tier, '3a');
  assert.ok(result.findings.some((f) => f.includes('raised to 3a')));
  assert.ok(result.findings.some((f) => f.includes('does not mention a migration step')));
  assert.ok(result.labels.includes('needs:migration'));
  assert.ok(result.labels.includes('needs:schema'));
});

test('declaring the migration and the tier clears the findings', () => {
  const files = [file('prisma/migrations/20261021062523_add_total_activity_hours/migration.sql'), file('prisma/schema.prisma')];
  const result = contract.evaluate({
    pr: pr({ body: body({ Tier: '3a', 'Deploy actions': 'migration 20261021062523_add_total_activity_hours (additive). Backfill script scripts/backfill-activity-hours.ts before the onboarding flag flips.' }) }),
    files,
    config,
    profileName: 'backend',
  });
  assert.deepEqual(result.findings, []);
  assert.equal(result.tier, '3a');
});

test('a new process.env key implies a config step even without a config file change', () => {
  const files = [file('src/lib/ai-trace/index.ts', 40, 0, '+  if (process.env.AI_TRACE_ENABLED === "true") {')];
  const result = contract.evaluate({ pr: pr({ body: body({}) }), files, config, profileName: 'backend' });
  assert.ok(result.findings.some((f) => f.includes('env AI_TRACE_ENABLED') && f.includes('config step')));
  const ok = contract.evaluate({ pr: pr({ body: body({ 'Deploy actions': 'Infisical key AI_TRACE_ENABLED=true in dev first, then prod' }) }), files, config, profileName: 'backend' });
  assert.ok(!ok.findings.some((f) => f.includes('config step')));
});

test('cross-repo dependency rows are parsed, validated and carried into the contract', () => {
  const parsed = contract.parseDependencies(BACKEND_DEP, 'hexis-expo-next');
  assert.deepEqual(parsed.findings, []);
  assert.equal(parsed.rows.length, 1);
  assert.equal(parsed.rows[0].repository, 'hexis-backend');
  assert.equal(parsed.rows[0].pr.number, 559);
  assert.equal(parsed.rows[0].order, 'before');

  const unopened = contract.parseDependencies('| Repository | PR | Order | Why |\n| --- | --- | --- | --- |\n| coach-desktop-next | unopened HEX-2550 | together | profile hours UI |', 'hexis-backend');
  assert.deepEqual(unopened.findings, []);
  assert.equal(unopened.rows[0].pr, 'unopened');

  const bad = contract.parseDependencies('| Repository | PR | Order | Why |\n| --- | --- | --- | --- |\n| hexis-web | https://github.com/abs-hexis/hexis-web/pull/1 | soon | |\n| hexis-backend | #559 | before | |', 'hexis-expo-next');
  assert.ok(bad.findings.some((f) => f.includes('not a production repository')));
  assert.ok(bad.findings.some((f) => f.includes('needs a PR link')));

  const self = contract.parseDependencies(BACKEND_DEP, 'hexis-backend');
  assert.ok(self.findings.some((f) => f.includes('not this one')));

  const empty = contract.parseDependencies('| Repository | PR | Order | Why |\n| --- | --- | --- | --- |', 'hexis-backend');
  assert.ok(empty.findings.some((f) => f.includes('no rows')));
});

test('a mobile GraphQL change with a none dependency row must name the backend PR', () => {
  const files = [file('src/graphql/user.graphql'), file('src/screens/profile/hours.tsx')];
  const base = body({ Tier: '3a' }) + '\n' + mobileExtra;
  const result = contract.evaluate({ pr: pr({ body: base, repo: 'abs-hexis/hexis-expo-next' }), files, config, profileName: 'mobile' });
  assert.ok(result.findings.some((f) => f.includes('no hexis-backend dependency is declared')));
  const ok = contract.evaluate({ pr: pr({ body: body({ Tier: '3a', 'Cross-repo dependencies': BACKEND_DEP }) + '\n' + mobileExtra, repo: 'abs-hexis/hexis-expo-next' }), files, config, profileName: 'mobile' });
  assert.ok(!ok.findings.some((f) => f.includes('hexis-backend dependency')), ok.findings.join('\n'));
  assert.equal(ok.contract.dependencies[0].repository, 'hexis-backend');
});

test('mobile native changes force a store build declaration', () => {
  const files = [file('app.config.ts', 3, 1), file('src/app/index.tsx')];
  const base = body({ Tier: '2' }) + '\n' + mobileExtra;
  const result = contract.evaluate({ pr: pr({ body: base, repo: 'abs-hexis/hexis-expo-next' }), files, config, profileName: 'mobile' });
  assert.ok(result.findings.some((f) => f.includes('Mobile delivery says OTA')));
  assert.ok(result.labels.includes('needs:store-build'));
  const fixed = base.replace('\nOTA\n', '\nStore build: runtime 3.16.3, TestFlight build pending\n');
  const ok = contract.evaluate({ pr: pr({ body: fixed.replace('## Deploy actions\n\nnone', '## Deploy actions\n\nstore build 3.16.3, new runtime'), repo: 'abs-hexis/hexis-expo-next' }), files, config, profileName: 'mobile' });
  assert.ok(!ok.findings.some((f) => f.includes('Mobile delivery')), ok.findings.join('\n'));
});

test('tier 0 needs screenshots and stays tier 0 only under the line cap on cosmetic paths', () => {
  const small = [file('docs/README.md', 4, 2)];
  const result = contract.evaluate({ pr: pr({ body: body({ Tier: '0', 'Release note': 'internal', Evidence: RUN, 'Operational evidence': 'existing coverage' }) }), files: small, config, profileName: 'backend' });
  assert.equal(result.tier, '0');
  assert.ok(result.findings.some((f) => f.includes('Tier 0 needs before/after screenshots')));
  const big = [file('docs/README.md', 40, 2)];
  const raised = contract.evaluate({ pr: pr({ body: body({ Tier: '0', Evidence: `${RUN}\n${SHOT}` }) }), files: big, config, profileName: 'backend' });
  assert.equal(raised.tier, '1');
  assert.ok(raised.findings.some((f) => f.includes('exceed the Tier 0 cap')));
});

test('existing coverage is rejected above tier 0', () => {
  const result = contract.evaluate({ pr: pr({ body: body({ 'Operational evidence': 'existing coverage' }) }), files: [file('src/x.ts')], config, profileName: 'backend' });
  assert.ok(result.findings.some((f) => f.includes('only accepted at Tier 0')));
});

test('evidence without a run link or visual is called out', () => {
  const result = contract.evaluate({ pr: pr({ body: body({ Evidence: 'Tested locally, all good.' }) }), files: [file('src/x.ts')], config, profileName: 'backend' });
  assert.ok(result.findings.some((f) => f.includes('no link')));
  const transcript = contract.evaluate({ pr: pr({ body: body({ Evidence: `${RUN}\n\n\`\`\`json\n{"data":{"user":{"totalActivityHours":7}}}\n\`\`\`` }) }), files: [file('src/x.ts')], config, profileName: 'backend' });
  assert.deepEqual(transcript.findings, []);
});

test('missing HEX key is a finding', () => {
  const result = contract.evaluate({ pr: pr({ body: body({}), branch: 'fix-things', title: 'fix things' }), files: [file('src/x.ts')], config, profileName: 'backend' });
  assert.ok(result.findings.some((f) => f.includes('No HEX-')));
});

test('release PRs and bot PRs are skipped, not failed', () => {
  const release = contract.evaluate({ pr: pr({ body: '', branch: 'HEX-2572-production-release-20261029', base: 'production' }), files: [file('src/x.ts')], config, profileName: 'backend' });
  assert.deepEqual(release.findings, []);
  assert.ok(release.skipped.includes('release pull request'));
  const bot = contract.evaluate({ pr: pr({ body: '', author: 'dependabot[bot]' }), files: [file('package.json')], config, profileName: 'backend' });
  assert.ok(bot.skipped.length);
});

test('workflow and infra paths derive tier 3b', () => {
  const result = contract.evaluate({ pr: pr({ body: body({ Tier: '3b', 'Deploy actions': 'workflow change only, no production step' }) }), files: [file('.github/workflows/test.yml')], config, profileName: 'backend' });
  assert.equal(result.tier, '3b');
  assert.ok(result.labels.includes('needs:pipeline'));
  assert.deepEqual(result.findings, []);
});

test('engine changes in the engines repo need the pin step', () => {
  const result = contract.evaluate({ pr: pr({ body: body({ Tier: '3a' }), repo: 'abs-hexis/hexis-engines-api' }), files: [file('engines/carb_ranges.py')], config, profileName: 'engines' });
  assert.ok(result.findings.some((f) => f.includes('engine-pin step')));
});

test('the ledger accrues, replaces by PR number, and renders release notes with cross-repo dependencies', () => {
  const first = contract.evaluate({ pr: pr({ body: body({}), merged: '2026-10-22T08:00:00Z' }), files: [file('src/lib/activity-volume.ts')], config, profileName: 'backend' }).contract;
  const second = contract.evaluate({
    pr: pr({ number: 622, title: 'feat(HEX-2550): save weekly training hours', branch: 'HEX-2550-hours', repo: 'abs-hexis/hexis-expo-next', body: body({ Tier: '2', 'Release note': 'Training hours are a number in onboarding.', 'Cross-repo dependencies': BACKEND_DEP, 'Deploy actions': 'none' }) + '\n' + mobileExtra, merged: '2026-10-22T09:00:00Z' }),
    files: [file('src/screens/onboarding/hours.tsx')],
    config,
    profileName: 'mobile',
  }).contract;
  second.dependencies[0].state = 'merged into development 2026-10-22';
  let state = ledger.readLedger('');
  state = ledger.upsertEntry(state, first, '2026-10-22T09:00:00Z');
  state = ledger.upsertEntry(state, second, '2026-10-22T09:30:00Z');
  state = ledger.upsertEntry(state, { ...first, releaseNote: 'Weekly training hours are saved as a number.' }, '2026-10-22T10:00:00Z');
  assert.equal(state.entries.length, 2);
  assert.equal(state.startedAt, '2026-10-22T09:00:00Z');
  const rendered = ledger.renderBody(state, { repo: 'abs-hexis/hexis-expo-next', mainline: 'development' });
  assert.match(rendered, /Weekly training hours are saved as a number/);
  assert.match(rendered, /\| #622 HEX-2550 \| hexis-backend \| \[#559\]/);
  assert.match(rendered, /Release order:\*\* hexis-backend must reach production before/);
  assert.match(rendered, /HEX-2550/);
  const roundTrip = ledger.readLedger(rendered);
  assert.equal(roundTrip.entries.length, 2);
  assert.equal(roundTrip.entries[1].dependencies[0].pr.number, 559);
});

test('the sticky comment lists findings, the derived tier and dependency states', () => {
  const result = contract.evaluate({ pr: pr({ body: body({ Tier: '1', 'Cross-repo dependencies': BACKEND_DEP }), repo: 'abs-hexis/coach-desktop-next' }), files: [file('src/graphql/x.graphql')], config, profileName: 'web' });
  result.contract.dependencies[0].state = 'open against development';
  const text = contract.renderComment({ result, profileName: 'web', templateUrl: 'https://example.test' });
  assert.match(text, /Tier \*\*3a\*\*/);
  assert.match(text, /Fix before merge/);
  assert.match(text, /hexis-backend \[#559\]/);
  assert.match(text, /open against development/);
});

test('every per-repository template carries the required sections and its profile extras', () => {
  for (const [profileName, profile] of Object.entries(config.profiles)) {
    if (!profile.template) continue;
    const text = fs.readFileSync(path.join(__dirname, '..', 'templates', profile.template), 'utf8');
    const sections = contract.parseSections(text);
    for (const name of contract.REQUIRED_SECTIONS.concat(profile.extraSections || [])) {
      assert.ok(name.toLowerCase() in sections, `${profile.template} lacks ## ${name}`);
    }
  }
});

test('backend API-surface changes need a real compatibility statement', () => {
  const files = [file('src/schema/user/user.resolvers.ts'), file('types-package/src/index.ts')];
  const compatBody = (text) => body({ 'Client compatibility': text });
  const noChange = contract.evaluate({ pr: pr({ body: compatBody('no API change') }), files, config, profileName: 'backend' });
  assert.ok(noChange.findings.some((f) => f.includes('says no API change')));
  const additive = contract.evaluate({ pr: pr({ body: compatBody('Additive: new nullable field User.totalActivityHours. Production coach (184 ops) and mobile (90 ops) validate. Types package 1.251.0.') }), files, config, profileName: 'backend' });
  assert.ok(!additive.findings.some((f) => f.includes('Client compatibility')), additive.findings.join('\n'));
  const withoutSection = body({}).replace(/## Client compatibility\n\nno API change\n/, '');
  const missing = contract.evaluate({ pr: pr({ body: withoutSection }), files: [file('src/lib/x.ts')], config, profileName: 'backend' });
  assert.ok(missing.findings.some((f) => f.includes('Missing section `## Client compatibility`')));
});

test('engine changes need a CHANGELOG entry, an Engine release statement, a cutover note and a carrying PR', () => {
  const enginesBody = (extra) => body({ Tier: '3a', Evidence: 'https://github.com/abs-hexis/hexis-engines-api/actions/runs/40482633156\n\n```\n312 passed in 41.2s\n```', 'Engine release': 'CHANGELOG yes · carb ranges change · comparison attached · science review no', ...extra });
  const noChangelog = contract.evaluate({ pr: pr({ body: enginesBody({}), repo: 'abs-hexis/hexis-engines-api' }), files: [file('engines/carb_ranges.py'), file('engines/calculation_bundles.py')], config, profileName: 'engines' });
  assert.ok(noChangelog.findings.some((f) => f.includes('CHANGELOG.txt')));
  assert.ok(noChangelog.findings.some((f) => f.includes('cutover date')));
  assert.ok(noChangelog.findings.some((f) => f.includes('carrying PR')));
  const good = contract.evaluate({
    pr: pr({
      body: enginesBody({
        'Deploy actions': 'engine pin via backend release PR; cutover bundle_20261029 switches /day to the continuous model',
        'Cross-repo dependencies': '| Repository | PR | Order | Why |\n| --- | --- | --- | --- |\n| hexis-backend | unopened HEX-2545 | together | pin |',
      }),
      repo: 'abs-hexis/hexis-engines-api',
    }),
    files: [file('engines/carb_ranges.py'), file('engines/calculation_bundles.py'), file('CHANGELOG.txt')],
    config,
    profileName: 'engines',
  });
  assert.deepEqual(good.findings, [], good.findings.join('\n'));
  assert.equal(good.contract.engineRelease.startsWith('CHANGELOG yes'), true);
});

test('jira helpers name versions and render a marked comment', () => {
  assert.equal(jira.nextVersionName('backend'), 'backend next');
  assert.equal(jira.releasedVersionName('mobile', '2026-11-05'), 'mobile 2026-11-05');
  const result = contract.evaluate({ pr: pr({ body: body({ 'Cross-repo dependencies': BACKEND_DEP }), repo: 'abs-hexis/coach-desktop-next' }), files: [file('src/app/x.tsx')], config, profileName: 'web' });
  const lines = jira.mergedCommentText(result.contract, 'abs-hexis/coach-desktop-next');
  assert.match(lines[0], /Merged to coach-desktop-next mainline: PR #559 \(tier 2\)/);
  assert.match(lines.join('\n'), /hexis-backend #559 \(before\)/);
  const doc = jira.adfDocument(lines, 'pr-contract coach-desktop-next');
  assert.equal(doc.type, 'doc');
  assert.match(jira.adfText(doc), /\[pr-contract coach-desktop-next\]/);
});

test('the release window joins drafts, orders repositories and lists tickets', () => {
  const backend = contract.evaluate({ pr: pr({ body: body({ Tier: '3a', 'Deploy actions': 'migration x (additive)' }), merged: '2026-10-22T08:00:00Z' }), files: [file('prisma/migrations/x/migration.sql'), file('prisma/schema.prisma')], config, profileName: 'backend' }).contract;
  const mobile = contract.evaluate({ pr: pr({ number: 622, branch: 'HEX-2550-hours', repo: 'abs-hexis/hexis-expo-next', body: body({ Tier: '2', 'Release note': 'Training hours are a number.', 'Cross-repo dependencies': BACKEND_DEP }) + '\n' + mobileExtra.replace('OTA', 'Store build 3.16.3'), merged: '2026-10-22T09:00:00Z' }), files: [file('src/screens/onboarding/x.tsx')], config, profileName: 'mobile' }).contract;
  const drafts = [
    { repo: 'abs-hexis/hexis-expo-next', mainline: 'development', draftUrl: 'https://github.com/abs-hexis/hexis-expo-next/releases/1', ledger: { startedAt: '2026-10-22', entries: [mobile] } },
    { repo: 'abs-hexis/hexis-backend', mainline: 'development', draftUrl: 'https://github.com/abs-hexis/hexis-backend/releases/2', ledger: { startedAt: '2026-10-22', entries: [backend] } },
    { repo: 'abs-hexis/hexis-infra', mainline: 'main', draftUrl: null, ledger: { startedAt: null, entries: [] } },
  ];
  const text = windowLib.renderWindow(drafts, { jiraStatuses: { 'HEX-2550': { status: 'IN DEV', summary: 'Add totalActivityHours' } }, generatedAt: '2026-10-28T10:00:00Z' });
  assert.match(text, /\| hexis-expo-next \| 1 \| 1 \| 2×1 \|  \| yes \|/);
  assert.match(text, /\*\*Deploy first:\*\* hexis-backend/);
  assert.match(text, /\| \[HEX-2550\]\(https:\/\/hexisapp.atlassian.net\/browse\/HEX-2550\) \| IN DEV \|/);
  assert.match(text, /\*\*Not QA Done:\*\* HEX-2550/);
  assert.match(text, /### hexis-expo-next\n- Training hours are a number/);
  assert.match(text, /hexis-infra \| 0 \| 0/);
});
