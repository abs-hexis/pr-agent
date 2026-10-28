# PR contract

Every pull request into a mainline declares what production needs from it and which other repositories it
depends on. A GitHub Action checks the declaration against the diff, labels the PR with its tier and deploy
needs, and, when the PR merges, adds it to the draft GitHub Release "Next release". Merging a release PR
publishes that draft under the date. The release report is written while the work happens.

**Home:** `abs-hexis/pr-agent` (the `hexis-pr-agent` automation repository), folder `pr-contract/`, branch
`hexis-main`. Application repositories reference the action as `abs-hexis/pr-agent/pr-contract/action@hexis-main`.
The repository is public, so no Actions access setting is needed.

**Templates live in each application repository**, on its mainline branch, at `.github/pull_request_template.md`.
GitHub reads the template from the base branch of the PR, so it must be merged into `development` (or `main`,
or `production` for hexis-localization) before it appears. The copies here are the source; a change here is
followed by a PR per repository.

## Layout

| Path | Purpose |
| --- | --- |
| `templates/pull_request_template.md` | The seven sections every PR fills, including the cross-repo dependency table. |
| `templates/mobile-extra.md` | Appended in `hexis-expo-next`: OTA or store build, plus the existing E2E section. |
| `config/profiles.json` | Path rules per repository profile: tier escalation, implied deploy actions, evidence hosts, bot exemptions. The only file to edit when rules change. |
| `action/contract.cjs` | Pure rules: parse sections, dependency table, derive tier and needs, produce findings. Unit-tested. |
| `action/ledger.cjs` | The draft-release ledger: accrue, render, round-trip. Unit-tested. |
| `action/github.cjs` | REST adapter: PR, files, labels, sticky comment, releases. |
| `action/run.cjs` + `action/action.yml` | Composite action with modes `check`, `accrue`, `publish`. `check` also looks up each dependency PR's state. |
| `workflows/pr-contract.yml` | Copied into each repo. Runs `check` on every PR. Becomes a required check. |
| `workflows/release-notes.yml` | Copied into each repo. `accrue` on merge to mainline, `publish` on merge to `production`. |
| `scripts/replay.cjs` | Read-only replay against merged PRs: what the check would have said. |
| `tests/contract.test.cjs` | `node --test pr-contract/tests/contract.test.cjs` |

## Rules in one screen

1. Seven sections are required: Change, Tier, Release note, Deploy actions, Cross-repo dependencies, Evidence, Operational evidence. Mobile adds Mobile delivery.
2. The tier is declared by the author and raised by the diff. It is never lowered. Tier 0 needs at most 20 changed lines on cosmetic paths and before/after screenshots.
3. Deploy actions must mention every step the diff implies: migration, schema, search schema, engine pin, config key (including new `process.env` reads), flag, strings, store build, pipeline, infra. `none` is accepted only when nothing is implied.
4. Cross-repo dependencies is a table: repository (one of the eight production repositories), PR link or `unopened`, order (`before`, `after`, `together`), why. A single `none` row means this repository ships alone. Mobile and web PRs that change GraphQL operations must name the backend PR or say why the production schema already has the fields.
5. Evidence must link the CI run or build for this head, plus a recording or screenshot on a preview or dev build, or a request/response transcript in a code block.
6. Operational evidence names the signal, the responder and the recovery. `existing coverage` is accepted at Tier 0 only.
7. Release PRs and bot PRs are skipped, never failed.

## Installing in a repository

1. Add `.github/pull_request_template.md` from `templates/` (mobile: template + `mobile-extra.md`) to the mainline branch.
2. Add `.github/workflows/pr-contract.yml` and `.github/workflows/release-notes.yml` from `workflows/`.
3. After three green runs, make `PR contract` a required check on the mainline branch.

## Changing the rules

Edit `config/profiles.json`, add or adjust a test, run `scripts/replay.cjs` against the last 15 merged PRs of the
affected repository, open a PR here. Application repositories pick the change up on their next PR because they
reference `@hexis-main`.
