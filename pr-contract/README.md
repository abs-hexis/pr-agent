# PR contract

Every pull request into a mainline declares what production needs from it and which other repositories it
depends on. The `PR contract` check validates the declaration against the diff and labels the PR. When the PR
merges, its contract is added to the draft GitHub Release "Next release" of that repository and, when Jira
credentials are present, to the ticket as fixVersion `<app> next` plus a comment. Merging a release PR
publishes the draft under the date and releases the Jira version. A scheduled job joins every repository's
draft into one "Release window" issue here.

Home: this repository (`hexis-pr-agent`), folder `pr-contract/`, branch `hexis-main`. Application repositories
reference `abs-hexis/pr-agent/pr-contract/action@hexis-main` (public repository, no access setting needed) and
keep their template in their own `.github/pull_request_template.md` on the mainline branch, because GitHub reads
the template from the PR's base branch.

## What each repository installs

| Repository | Template | Mainline → release | Deploys how | Extra section | Checks that exist today |
| --- | --- | --- | --- | --- | --- |
| hexis-backend | `templates/hexis-backend.md` | development → production | push to branch builds and deploys; Prisma paths run Sync Database Schema; Typesense files run Sync Typesense Schema; every merge publishes the types package | Client compatibility | test, Branch name, Jira ticket exists |
| hexis-expo-next | `templates/hexis-expo-next.md` (keeps the E2E section) | development → production | EAS build on push; OTA by manual dispatch from production; codegen from the live dev schema at install | Mobile delivery, E2E tests | Static checks, Unit tests, Expo Doctor, PR tests declared |
| coach-desktop-next | `templates/coach-desktop-next.md` | development → production | Vercel deploys production; semantic-release on development (currently red); Localization registers string IDs | none | test, lint and format, Vercel preview |
| hexis-engines-api | `templates/hexis-engines-api.md` | development → production (via backend pin) | merge to development moves the backend dev pin automatically; production pin moves in the backend release PR; no pytest in CI | Engine release | test-sync |
| b2b-portal, foods-dashboard | `templates/coach-desktop-next.md` (web profile) | development → production | Vercel | none | Vercel preview |
| hexis-infra | `templates/pull_request_template.md` | main | pulumi via deploy-infra | none | checks |
| hexis-localization | `templates/pull_request_template.md` | production | build-deploy on push | none | ci |

Each repository adds two workflows from `workflows/`: `pr-contract.yml` (the check) and `release-notes.yml`
(accrue on merge to mainline, publish on merge to production).

## Repository-specific rules (from `config/profiles.json`)

- **Backend.** `Client compatibility` is required. If `src/schema/`, `src/api/routes/`, `types-package/`, `codegen.ts` or the GraphQL export change, it must say breaking or additive and the types-package version, not `no API change`. Migrations, schema, Typesense, engine pin, new `process.env` reads and pipeline files each demand a line in Deploy actions.
- **Mobile.** `Mobile delivery` is required. `app.config.ts`, `eas.json`, `ios/`, `android/`, `patches/` or `package.json` changes force a store build. `src/translations/` demands a strings line. A `src/graphql/` change with a `none` dependency row must name the backend PR.
- **Coach and web.** `src/graphql/` or `src/generated/` changes with a `none` dependency row must name the backend PR. Translations demand a strings line.
- **Engines.** `CHANGELOG.txt` must change unless the PR is docs only. `Engine release` is required. A `calculation_bundles.py` change must declare the cutover date. Anything above Tier 0 must name the carrying backend PR or `unopened`.

## Layout

| Path | Purpose |
| --- | --- |
| `templates/*.md` | Per-repository templates. `pull_request_template.md` is the generic one for infra and localization. |
| `config/profiles.json` | Path rules per profile, extra sections, Jira version prefixes, evidence hosts, bot exemptions. The only file to edit when rules change. |
| `action/contract.cjs` | Pure rules: sections, dependency table, tier and needs, repository-specific findings. |
| `action/ledger.cjs` | The draft-release ledger: accrue, render, round-trip. |
| `action/jira.cjs` | Jira versions and comments. Enabled by `JIRA_EMAIL` and `JIRA_API_TOKEN`. |
| `action/window.cjs` | The cross-repository release window. |
| `action/github.cjs` | REST adapter. |
| `action/run.cjs` + `action/action.yml` | Composite action, modes `check`, `accrue`, `publish`, `window`. |
| `workflows/` | The two files each application repository copies. |
| `../.github/workflows/release-window.yaml` | Renders the Release window issue here every two hours. |
| `scripts/replay.cjs` | Read-only replay against merged PRs. |
| `tests/contract.test.cjs` | `node --test pr-contract/tests/contract.test.cjs` |

## Jira

- On merge to the mainline: fixVersion `<app> next` (backend, engines, mobile, coach, infra, localization) is created if missing and added to the ticket; one comment marked `[pr-contract <repo>]` carries tier, release note, deploy actions, dependencies and operational evidence. Re-runs update the same comment.
- On merge to production: `<app> next` is renamed `<app> YYYY-MM-DD` and released, a new `<app> next` is created, and every ticket in the draft gets the released version and a `[pr-contract released <repo>]` comment with the release-notes link.
- Needs two organisation secrets, `JIRA_EMAIL` and `JIRA_API_TOKEN`, for an Atlassian account that can edit HEX issues and project versions. Without them the action logs a notice and skips Jira.

## Release window

`Release window` is one issue in this repository, regenerated every two hours and on demand: PRs, tickets, tier mix and
needs per repository; the declared cross-repo dependencies with their current state and the resulting deploy
order; every deploy action; tickets with their Jira status and the "Not QA Done" list; what to watch after deploy;
the user-facing release-notes draft. It needs the `CI_APP_ID` variable and `CI_APP_PRIVATE_KEY` secret of the
hexis-ci-automation GitHub App (the pair hexis-engines-api already uses), with the app installed on the eight
production repositories and this one.

## Rules in one screen

1. Seven sections are required: Change, Tier, Release note, Deploy actions, Cross-repo dependencies, Evidence, Operational evidence, plus the profile's extra sections.
2. The tier is declared by the author and raised by the diff. It is never lowered. Tier 0 needs at most 20 changed lines on cosmetic paths and before/after screenshots.
3. Deploy actions must mention every step the diff implies. `none` is accepted only when nothing is implied.
4. Cross-repo dependencies is a table: repository, PR link or `unopened`, order (`before`, `after`, `together`), why. The check looks each PR up and shows its state.
5. Evidence must link the CI run or build for this head, plus a recording or screenshot on a preview or dev build, or a request/response transcript in a code block.
6. Operational evidence names the signal, the responder and the recovery. `existing coverage` is accepted at Tier 0 only.
7. Release PRs and bot PRs are skipped, never failed.

## Installing in a repository

1. Add `.github/pull_request_template.md` from `templates/` to the mainline branch.
2. Add `.github/workflows/pr-contract.yml` and `.github/workflows/release-notes.yml` from `workflows/`.
3. After three green runs, make `PR contract` a required check on the mainline branch.

## Changing the rules

Edit `config/profiles.json`, add or adjust a test, run `scripts/replay.cjs` against the last 15 merged PRs of the
affected repository, open a PR here. Application repositories pick the change up on their next PR because they
reference `@hexis-main`.
