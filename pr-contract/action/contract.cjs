'use strict';

// Pure rules for the PR contract. No network, no GitHub API here, so every rule is unit-testable.
// The composite action (run.cjs) fetches the PR and changed files, then calls these functions.

const TIER_ORDER = ['0', '1', '2', '3a', '3b'];

const REQUIRED_SECTIONS = [
  'Change',
  'Tier',
  'Release note',
  'Deploy actions',
  'Cross-repo dependencies',
  'Evidence',
  'Operational evidence',
];

// The repositories that reach production. A dependency row must name one of these.
const KNOWN_REPOSITORIES = [
  'hexis-backend',
  'hexis-engines-api',
  'hexis-expo-next',
  'coach-desktop-next',
  'b2b-portal',
  'foods-dashboard',
  'hexis-infra',
  'hexis-localization',
];

const DEPENDENCY_ORDERS = ['before', 'after', 'together'];

// Parses the Cross-repo dependencies table. Returns { none, rows, findings }.
// A row is { repository, pr: {url, number} | 'unopened', order, why }.
function parseDependencies(text, ownRepository) {
  const findings = [];
  const rows = [];
  if (isEmpty(text)) return { none: false, rows, findings: ['Cross-repo dependencies is empty. Fill the table or write a single `none` row.'] };

  const lines = text.split('\n').map((l) => l.trim()).filter((l) => l.startsWith('|'));
  const dataLines = lines.filter((l) => !/^\|\s*-{3,}/.test(l) && !/^\|\s*Repository\s*\|/i.test(l));
  if (dataLines.length === 0 && isNone(text)) return { none: true, rows, findings };

  for (const line of dataLines) {
    const cells = line.split('|').slice(1, -1).map((c) => c.trim());
    const [repository = '', pr = '', order = '', why = ''] = cells;
    if (isNone(repository)) continue;
    if (!repository) continue;
    const repoName = repository.replace(/^abs-hexis\//, '').replace(/`/g, '');
    if (!KNOWN_REPOSITORIES.includes(repoName)) {
      findings.push(`Cross-repo dependency names \`${repository}\`, which is not a production repository (${KNOWN_REPOSITORIES.join(', ')}).`);
      continue;
    }
    if (ownRepository && repoName === ownRepository) {
      findings.push('Cross-repo dependencies must name other repositories, not this one.');
      continue;
    }
    const prMatch = pr.match(/github\.com\/abs-hexis\/([\w.-]+)\/pull\/(\d+)/);
    let prRef;
    if (prMatch) {
      if (prMatch[1] !== repoName) findings.push(`Dependency row for ${repoName} links a PR in ${prMatch[1]}.`);
      prRef = { url: prMatch[0].startsWith('http') ? prMatch[0] : `https://${prMatch[0]}`, number: Number(prMatch[2]), repository: prMatch[1] };
    } else if (/unopened/i.test(pr)) {
      prRef = 'unopened';
    } else {
      findings.push(`Dependency row for ${repoName} needs a PR link or \`unopened\` with the ticket.`);
      continue;
    }
    const orderWord = order.toLowerCase().replace(/[`*]/g, '');
    if (!DEPENDENCY_ORDERS.includes(orderWord)) {
      findings.push(`Dependency row for ${repoName} needs an order: before, after or together.`);
      continue;
    }
    rows.push({ repository: repoName, pr: prRef, order: orderWord, why });
  }
  if (rows.length === 0 && findings.length === 0) {
    const sawNone = dataLines.some((l) => isNone(l.split('|')[1] || ''));
    if (!sawNone) findings.push('Cross-repo dependencies has no rows. Add one per dependent PR or a single `none` row.');
    return { none: sawNone, rows, findings };
  }
  return { none: false, rows, findings };
}

const NONE_WORDS = new Set(['none', 'n/a', 'na', 'nothing', '-']);

function stripHtmlComments(text) {
  return text.replace(/<!--[\s\S]*?-->/g, '');
}

// Splits a PR body into {heading: body} using H2 headings. Headings are matched case-insensitively
// so "## Release Note" and "## Release note" are the same section.
function parseSections(body) {
  const sections = {};
  const lines = stripHtmlComments(body || '').split(/\r?\n/);
  let current = null;
  for (const line of lines) {
    const heading = line.match(/^##\s+(.+?)\s*$/);
    if (heading) {
      current = heading[1].trim().toLowerCase();
      sections[current] = [];
      continue;
    }
    if (current) sections[current].push(line);
  }
  const result = {};
  for (const [key, value] of Object.entries(sections)) {
    result[key] = value.join('\n').trim();
  }
  return result;
}

function sectionText(sections, name) {
  const text = sections[name.toLowerCase()];
  return text === undefined ? null : text;
}

function isNone(text) {
  return NONE_WORDS.has((text || '').trim().toLowerCase().replace(/[`*_.]/g, ''));
}

function isEmpty(text) {
  if (text === null || text === undefined) return true;
  // Unticked checkboxes and bare labels from the template do not count as content.
  const stripped = text
    .replace(/^- \[ \].*$/gm, '')
    .replace(/^(Affected areas considered|Run link and tested commit):\s*$/gm, '')
    .trim();
  return stripped.length === 0;
}

function parseDeclaredTier(text) {
  if (!text) return null;
  const match = text.match(/\b(0|1|2|3a|3b)\b/i);
  return match ? match[1].toLowerCase() : null;
}

function tierRank(tier) {
  return TIER_ORDER.indexOf(tier);
}

function higherTier(a, b) {
  return tierRank(a) >= tierRank(b) ? a : b;
}

// Rules are anchored at the repository root. A rule like ".md" (leading dot, no slash) matches a file
// extension anywhere; every other rule must be the start of the path, so "scripts/" never matches
// ".github/scripts/". "." matches everything (used by the infra profile).
function pathMatches(filename, prefixes) {
  return prefixes.some((prefix) => {
    if (prefix === '.') return true;
    if (prefix.startsWith('.') && !prefix.includes('/')) return filename.endsWith(prefix);
    return filename.startsWith(prefix);
  });
}

// Tier derived from the diff alone. The author's declaration can only raise it further.
function deriveTierFromFiles(files, profile, tierZeroMaxChangedLines) {
  const names = files.map((f) => f.filename);
  const changedLines = files.reduce((sum, f) => sum + (f.additions || 0) + (f.deletions || 0), 0);
  const reasons = [];
  let tier = '0';

  const allCosmetic = names.length > 0 && names.every((n) => pathMatches(n, profile.cosmeticOnly || []));
  if (!allCosmetic) {
    tier = '1';
    reasons.push('changes outside cosmetic paths');
  }
  if (changedLines > tierZeroMaxChangedLines && tier === '0') {
    tier = '1';
    reasons.push(`${changedLines} changed lines exceed the Tier 0 cap of ${tierZeroMaxChangedLines}`);
  }
  const hit2 = names.filter((n) => pathMatches(n, profile.tier2 || []));
  if (hit2.length) {
    tier = higherTier(tier, '2');
    reasons.push(`flow paths: ${hit2.slice(0, 3).join(', ')}`);
  }
  const hit3a = names.filter((n) => pathMatches(n, profile.tier3a || []));
  if (hit3a.length) {
    tier = higherTier(tier, '3a');
    reasons.push(`data/contract paths: ${hit3a.slice(0, 3).join(', ')}`);
  }
  const hit3b = names.filter((n) => pathMatches(n, profile.tier3b || []));
  if (hit3b.length) {
    tier = higherTier(tier, '3b');
    reasons.push(`pipeline/infra paths: ${hit3b.slice(0, 3).join(', ')}`);
  }
  return { tier, reasons, changedLines };
}

const ENV_KEY_PATTERN = /^\+.*process\.env\.([A-Z][A-Z0-9_]{3,})/gm;

// Deploy actions the diff implies. Each key maps to the words we expect in the Deploy actions section.
const NEED_KEYWORDS = {
  migration: ['migration', 'migrate'],
  schema: ['migration', 'schema'],
  'search-schema': ['typesense', 'search schema', 'backfill'],
  'engine-pin': ['engine', 'pin', 'submodule'],
  config: ['infisical', 'env', 'key', 'secret', 'variable'],
  flag: ['flag', 'posthog'],
  strings: ['string', 'localis', 'localiz', 'translation', 'portal'],
  'store-build': ['store', 'testflight', 'build', 'runtime', 'ota'],
  pipeline: ['workflow', 'pipeline', 'ci', 'action'],
  infra: ['pulumi', 'infra', 'terraform', 'gcp'],
  dependencies: ['dependenc', 'upgrade', 'bump', 'version', 'node'],
};

function deriveNeedsFromFiles(files, profile) {
  const needs = new Map();
  for (const [need, prefixes] of Object.entries(profile.needs || {})) {
    const hits = files.map((f) => f.filename).filter((n) => pathMatches(n, prefixes));
    if (hits.length) needs.set(need, hits.slice(0, 5));
  }
  // New environment variables read in added lines imply a config step even when no config file changed.
  const newEnvKeys = new Set();
  for (const file of files) {
    if (!file.patch) continue;
    for (const match of file.patch.matchAll(ENV_KEY_PATTERN)) newEnvKeys.add(match[1]);
  }
  if (newEnvKeys.size) {
    const existing = needs.get('config') || [];
    needs.set('config', existing.concat([...newEnvKeys].map((k) => `env ${k}`)).slice(0, 8));
  }
  return needs;
}

function deployActionsCover(text, need) {
  const lower = (text || '').toLowerCase();
  return (NEED_KEYWORDS[need] || [need]).some((word) => lower.includes(word));
}

function extractUrls(text) {
  return [...(text || '').matchAll(/https?:\/\/[^\s)>\]"']+/g)].map((m) => m[0]);
}

function hostOf(url) {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch {
    return '';
  }
}

function evidenceFindings(evidenceText, tier, evidenceHosts, repoFullName) {
  const findings = [];
  const urls = extractUrls(evidenceText);
  const allowed = urls.filter((u) => evidenceHosts.some((h) => hostOf(u) === h || hostOf(u).endsWith(`.${h}`)));
  if (allowed.length === 0) {
    findings.push('Evidence has no link to a CI run, build, preview, recording or screenshot.');
    return findings;
  }
  const hasRunOrBuild = allowed.some(
    (u) => /\/actions\/runs\/\d+/.test(u) || hostOf(u) === 'expo.dev' || hostOf(u).endsWith('vercel.app') || u.includes('/checks'),
  );
  if (!hasRunOrBuild) {
    findings.push('Evidence needs the CI run, EAS build or preview deployment for this head.');
  }
  const hasVisual = allowed.some(
    (u) =>
      /\.(png|jpe?g|gif|mp4|mov|webm)(\?|$)/i.test(u) ||
      hostOf(u).includes('githubusercontent.com') ||
      u.includes('github.com/user-attachments') ||
      hostOf(u).endsWith('loom.com') ||
      hostOf(u) === 'drive.google.com',
  );
  const hasTranscript = /```/.test(evidenceText || '');
  if (tier === '0' && !hasVisual) {
    findings.push('Tier 0 needs before/after screenshots.');
  }
  if (tier !== '0' && !hasVisual && !hasTranscript) {
    findings.push('Tier 1+ needs a recording or screenshots on a preview/dev build, or a request/response transcript in a code block.');
  }
  if (repoFullName && allowed.some((u) => u.includes('github.com') && !u.includes(repoFullName) && /\/actions\/runs\//.test(u))) {
    findings.push('A linked CI run belongs to another repository.');
  }
  return findings;
}

function releaseNoteFindings(text) {
  if (isEmpty(text)) return ['Release note is empty. Write one user-facing sentence or `internal`.'];
  if (isNone(text) || text.trim().toLowerCase() === 'internal') return [];
  const firstLine = text.trim().split('\n')[0];
  if (firstLine.length > 220) return ['Release note is longer than one sentence. Keep it under 220 characters.'];
  return [];
}

function hexKey(branch, title) {
  const match = `${branch || ''} ${title || ''}`.match(/HEX-\d{1,6}/i);
  return match ? match[0].toUpperCase() : null;
}

function isReleasePullRequest(pr, config, profile) {
  if (pr.base && pr.base.ref === profile.releaseBranch && profile.releaseBranch !== profile.mainline) return true;
  const haystack = `${pr.head?.ref || ''} ${pr.title || ''}`;
  return (config.releaseBranchPatterns || []).some((p) => haystack.toLowerCase().includes(p.toLowerCase()));
}

function isBotAuthor(pr, config) {
  const login = pr.user?.login || '';
  return (config.botAuthors || []).includes(login) || login.endsWith('[bot]');
}

// The whole evaluation. Returns labels to apply, findings that fail the check, and the extracted
// contract that the release-notes accrual stores.
function evaluate({ pr, files, config, profileName }) {
  const profile = config.profiles[profileName];
  if (!profile) throw new Error(`Unknown profile ${profileName}`);

  const skipped = [];
  if (isBotAuthor(pr, config)) skipped.push(`bot author ${pr.user?.login}`);
  if (isReleasePullRequest(pr, config, profile)) skipped.push('release pull request');
  if (skipped.length) {
    return { skipped, findings: [], labels: [], contract: null, tier: null, needs: {} };
  }

  const sections = parseSections(pr.body || '');
  const findings = [];

  for (const name of REQUIRED_SECTIONS.concat(profile.extraSections || [])) {
    if (sectionText(sections, name) === null) findings.push(`Missing section \`## ${name}\`. Start from the PR template.`);
  }

  const key = hexKey(pr.head?.ref, pr.title);
  if (!key) findings.push('No HEX-<n> key in the branch name or title.');

  const declared = parseDeclaredTier(sectionText(sections, 'Tier'));
  if (!declared) findings.push('Tier must be one of 0, 1, 2, 3a, 3b.');

  const derived = deriveTierFromFiles(files, profile, config.tierZeroMaxChangedLines);
  const tier = declared ? higherTier(declared, derived.tier) : derived.tier;
  if (declared && tierRank(derived.tier) > tierRank(declared)) {
    findings.push(`Declared tier ${declared} is raised to ${derived.tier}: ${derived.reasons.join('; ')}. Update the Tier section and the evidence it requires.`);
  }

  if (isEmpty(sectionText(sections, 'Change'))) findings.push('Change section is empty.');
  findings.push(...releaseNoteFindings(sectionText(sections, 'Release note')));

  const deploy = sectionText(sections, 'Deploy actions');
  if (isEmpty(deploy)) findings.push('Deploy actions is empty. List every production step or write `none`.');
  const needs = deriveNeedsFromFiles(files, profile);
  const needsObject = Object.fromEntries(needs);
  if (!isEmpty(deploy)) {
    for (const [need, hits] of needs) {
      if (need === 'dependencies') continue; // informational: labelled, never fails
      if (isNone(deploy) || !deployActionsCover(deploy, need)) {
        findings.push(`The diff touches ${hits.join(', ')} but Deploy actions does not mention a ${need} step.`);
      }
    }
  }

  const ownRepository = (pr.base?.repo?.full_name || '').replace(/^abs-hexis\//, '');
  const dependencies = parseDependencies(sectionText(sections, 'Cross-repo dependencies'), ownRepository);
  findings.push(...dependencies.findings);
  if (profile.graphqlPaths && profile.graphqlPaths.length) {
    const graphqlChanged = files.some((f) => pathMatches(f.filename, profile.graphqlPaths));
    const namesBackend = dependencies.rows.some((r) => r.repository === 'hexis-backend');
    if (graphqlChanged && !namesBackend && dependencies.none) {
      findings.push('GraphQL operations or generated types changed but no hexis-backend dependency is declared. Name the backend PR (order: before) or state why the production schema already has the fields in the Why column.');
    }
  }

  // Backend: a change to the API surface must come with a compatibility statement for the shipped clients.
  if (profile.contractPaths && profile.contractPaths.length) {
    const compat = sectionText(sections, 'Client compatibility');
    const contractChanged = files.filter((f) => pathMatches(f.filename, profile.contractPaths) && !/\.test\.[tj]s$/.test(f.filename));
    if (compat !== null) {
      const saysNoChange = (profile.contractNoChangeWords || []).some((w) => (compat || '').toLowerCase().includes(w));
      if (isEmpty(compat)) findings.push('Client compatibility is empty. State breaking or additive, which production coach/mobile operations still validate, or `no API change`.');
      else if (contractChanged.length && saysNoChange && /no (api|sdl) change/.test(compat.toLowerCase())) {
        findings.push(`API surface files changed (${contractChanged.slice(0, 3).map((f) => f.filename).join(', ')}) but Client compatibility says no API change. State breaking or additive and the types-package version.`);
      }
    }
  }

  // Engines: every change carries a CHANGELOG line, a release statement, and a cutover note when bundles move.
  if (profile.changelog) {
    const changelogTouched = files.some((f) => f.filename === profile.changelog);
    const docsOnly = files.length > 0 && files.every((f) => pathMatches(f.filename, profile.cosmeticOnly || []));
    if (!changelogTouched && !docsOnly) findings.push(`\`${profile.changelog}\` has no entry for this change. Add one under [UNRELEASED].`);
    const release = sectionText(sections, 'Engine release');
    if (release !== null && isEmpty(release)) findings.push('Engine release is empty. State the CHANGELOG entry, which recommendation outputs change, the reference-athlete comparison and whether science review is needed.');
    const cutoverTouched = files.some((f) => pathMatches(f.filename, profile.cutoverPaths || []));
    if (cutoverTouched && !/cutover|bundle_20\d{6}|date/i.test(`${deploy || ''} ${release || ''}`)) {
      findings.push('calculation_bundles.py changed but no cutover date is declared in Deploy actions or Engine release.');
    }
  }
  if (profile.dependencyRequiredAboveTier !== undefined && dependencies.none && tierRank(tier) > tierRank(profile.dependencyRequiredAboveTier)) {
    findings.push(`Tier ${tier} changes in this repository reach production through another repository. Name the carrying PR (hexis-backend pin) or \`unopened\` with the ticket.`);
  }

  const evidence = sectionText(sections, 'Evidence');
  if (isEmpty(evidence)) findings.push('Evidence is empty.');
  else findings.push(...evidenceFindings(evidence, tier, config.evidenceHosts, pr.base?.repo?.full_name));

  const operational = sectionText(sections, 'Operational evidence');
  if (isEmpty(operational)) {
    findings.push('Operational evidence is empty. Say how we would see this failing in production, or `existing coverage` at Tier 0.');
  } else if (tier !== '0' && operational.trim().toLowerCase() === 'existing coverage') {
    findings.push('`existing coverage` is only accepted at Tier 0. Name the signal and the responder.');
  }

  if (sectionText(sections, 'Mobile delivery') !== null) {
    const delivery = (sectionText(sections, 'Mobile delivery') || '').toLowerCase();
    const storeBuildImplied = needs.has('store-build');
    if (storeBuildImplied && !delivery.includes('store')) {
      findings.push(`Native/runtime files changed (${(needs.get('store-build') || []).join(', ')}) but Mobile delivery says OTA. Declare a store build with the runtime version.`);
    }
    if (!delivery.includes('ota') && !delivery.includes('store')) findings.push('Mobile delivery must say OTA or Store build.');
  }

  const labels = [`tier:${tier}`, ...[...needs.keys()].map((n) => `needs:${n}`)];

  const contract = {
    number: pr.number,
    title: pr.title,
    ticket: key,
    author: pr.user?.login || null,
    headSha: pr.head?.sha || null,
    mergedAt: pr.merged_at || null,
    tier,
    declaredTier: declared,
    derivedTier: derived.tier,
    releaseNote: (sectionText(sections, 'Release note') || '').trim(),
    deployActions: (deploy || '').trim(),
    dependencies: dependencies.rows,
    operationalEvidence: (operational || '').trim(),
    evidenceLinks: extractUrls(evidence),
    needs: needsObject,
    mobileDelivery: sectionText(sections, 'Mobile delivery') !== null ? (sectionText(sections, 'Mobile delivery') || '').trim().split('\n')[0] : null,
    clientCompatibility: sectionText(sections, 'Client compatibility') !== null ? (sectionText(sections, 'Client compatibility') || '').trim() : null,
    engineRelease: sectionText(sections, 'Engine release') !== null ? (sectionText(sections, 'Engine release') || '').trim() : null,
    repository: pr.base?.repo?.full_name || null,
  };

  return { skipped, findings, labels, contract, tier, needs: needsObject, derived };
}

function renderComment({ result, profileName, templateUrl }) {
  const lines = ['<!-- pr-contract -->', '## PR contract'];
  if (result.skipped.length) {
    lines.push(`Skipped: ${result.skipped.join(', ')}.`);
    return lines.join('\n');
  }
  lines.push(`Tier **${result.tier}** (declared ${result.contract.declaredTier ?? 'none'}, derived ${result.contract.derivedTier}) · profile \`${profileName}\``);
  const needs = Object.entries(result.needs);
  lines.push(needs.length ? `Deploy actions implied by the diff: ${needs.map(([n]) => `\`${n}\``).join(', ')}` : 'No deploy actions implied by the diff.');
  const deps = result.contract.dependencies || [];
  if (deps.length) {
    lines.push('', '**Cross-repo dependencies**');
    for (const d of deps) {
      const ref = d.pr === 'unopened' ? 'unopened' : `[#${d.pr.number}](${d.pr.url})`;
      const state = d.state ? ` · ${d.state}` : '';
      lines.push(`- ${d.repository} ${ref} · ${d.order}${state}${d.why ? ` · ${d.why}` : ''}`);
    }
  }
  if (result.findings.length) {
    lines.push('', '**Fix before merge**');
    for (const f of result.findings) lines.push(`- ${f}`);
  } else {
    lines.push('', 'All sections present. This PR will be added to the next release notes when it merges.');
  }
  if (templateUrl) lines.push('', `Template and rules: ${templateUrl}`);
  return lines.join('\n');
}

module.exports = {
  REQUIRED_SECTIONS,
  KNOWN_REPOSITORIES,
  TIER_ORDER,
  parseDependencies,
  parseSections,
  parseDeclaredTier,
  deriveTierFromFiles,
  deriveNeedsFromFiles,
  deployActionsCover,
  evidenceFindings,
  releaseNoteFindings,
  hexKey,
  isReleasePullRequest,
  isBotAuthor,
  evaluate,
  renderComment,
  higherTier,
  isNone,
  isEmpty,
};
