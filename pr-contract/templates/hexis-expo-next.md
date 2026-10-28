## Change

<!-- What changes for the athlete, and why. Link the Jira ticket. The branch name carries the HEX key.
     Mobile facts that matter at release: codegen runs against the live dev backend schema at install, so a backend change must be on dev before this PR builds and in production before it ships;
     JavaScript-only changes ship by OTA on the current runtime; any native, Expo SDK, patch or version change needs a store build and store review (~24 h). -->

## Tier

<!-- 0 cosmetic (≤20 lines on translations, assets, Maestro flows, docs) · 1 contained behaviour · 2 flow (src/app routes, shared components, onboarding, food-log, home, navigation) · 3a data/contract (src/graphql, auth, billing, instrumentation) · 3b pipeline (.github, .eas, eas.json, scripts).
     Declare the lowest honest tier. The check raises it from the changed paths and never lowers it. -->

1

## Release note

<!-- One sentence an athlete would understand, or the single word `internal`. Copied into the next release's notes when the PR merges. -->

## Deploy actions

<!-- Everything production needs besides the code, or the single word `none`. One line each. Mobile checklist:
     localisation strings — how many IDs added or changed, published to the portal for production? (the build gate fails on unknown IDs).
     PostHog flag <KEY> — payload keyed by APP_ENV, when it flips in production, what backend must be live first.
     store build — new runtime version, native module, patch or Expo SDK change; TestFlight/internal build exists?
     OTA — update branch and message; runtime unchanged.
     backend — fields this app reads that must exist in the production schema (see Cross-repo dependencies). -->

none

## Mobile delivery

<!-- Exactly one of:
     OTA — runtime version unchanged, JavaScript only. Name the EAS update branch or dev build you tested on.
     Store build — app.config/version, native module, patch, Expo SDK or iOS/Android change. Name the runtime version and whether a TestFlight or internal build exists. -->

OTA

## Cross-repo dependencies

<!-- Hexis is a polyrepo. One row per PR in another repository that this change needs or that needs this change.
     Repository: hexis-backend · hexis-engines-api · coach-desktop-next · hexis-localization · hexis-infra
     PR: the link, or `unopened` with the ticket. Order: `before` (that PR must be in production first) · `after` · `together`.
     A GraphQL operation change with a `none` row fails the check: name the backend PR or explain in Why that production already has the fields.
     A single `none` row means the app ships alone. -->

| Repository | PR | Order | Why |
| --- | --- | --- | --- |
| none | | | |

## Evidence

<!-- Links, not sentences.
     all tiers: the green CI run for this head (Static checks, Unit tests, PR tests declared).
     0: before/after screenshots.
     1+: recording on a dev build or EAS update branch, with the build or update group tested; the Maestro PR-selection run for this head.
     2+: the whole journey incl. empty, error and loading states, on iOS and Android where behaviour differs.
     3a: the backend PR this depends on and proof the operation validates against it.
     3b: the EAS build IDs for both platforms. -->

## E2E tests

<!-- Required when app code in src/ changed (generated GraphQL, unit tests, snapshots and Markdown do not count). -->

- [ ] Listed the tests in `.maestro/pr-selection/<key>.json` (`HEX-123.json`, or the whole branch name for a `HEX-000` branch).
- [ ] Passing PR review run linked below, for the current PR head.

Affected areas considered:

Run link and tested commit:

<!-- Reviewer: check the test list covers the change and the run tested the current commit before merging. -->

## Operational evidence

<!-- How we will know this works in production and how we would know it stopped. A few lines:
     outcome and silent-failure mode · signal (Sentry issue, PostHog event, backend log the screen triggers) · who looks and when (3-day verification window; name the app versions still in use) · recovery (flag off, OTA republish of the previous group; a store build is fix-forward).
     Tier 0 may write `existing coverage`. -->
