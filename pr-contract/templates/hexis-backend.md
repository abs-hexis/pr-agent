## Change

<!-- What changes for the athlete, coach or system, and why. Link the Jira ticket. The branch name carries the HEX key.
     Backend facts that matter at release: the branch deploys on push (Build and Deploy App), Prisma paths trigger Sync Database Schema,
     Typesense schema files trigger Sync Typesense Schema, and every merge publishes the types package consumed by coach and mobile. -->

## Tier

<!-- 0 cosmetic (≤20 lines, no behaviour) · 1 contained behaviour · 2 flow (integrations sync, jobs, workers, search) · 3a data/contract (prisma, billing, Stripe, auth, Clerk, integrations core, engine pin, GraphQL export) · 3b pipeline (.github, Dockerfile, deploy scripts).
     Declare the lowest honest tier. The check raises it from the changed paths and never lowers it. -->

1

## Release note

<!-- One sentence a coach or athlete would understand, or the single word `internal`. Copied into the next release's notes when the PR merges. -->

## Deploy actions

<!-- Everything production needs besides the code, or the single word `none`. One line each. Backend checklist:
     migration <name> — additive or contract step? reversible? replayed on a disposable Postgres? Remember Sync Database Schema freezes writes for the migration in EU, US and AU.
     backfill <script> — when it runs (before the flag flips? after deploy?) and its post-condition.
     Infisical key <NAME> — which environments (dev, prod EU/US/AU); the 20 Oct FIT bucket defect was a prod key pointing at a dev identity.
     PostHog flag <KEY> — payload and when it flips; name the client it gates.
     Typesense schema/backfill — the 22 Sep outage was a backfill that only ever ran on dev.
     engine pin — python-engine submodule moves from <sha> to <sha>; which engine PRs that carries; any date-gated bundle.
     vendor config — Stripe portal/retention settings, Garmin endpoints (PING vs push), Clerk, Resend, HubSpot.
     jobs — new or changed BullMQ/cron jobs and their alert coverage. -->

none

## Client compatibility

<!-- Required when the GraphQL schema, resolvers' return shapes, REST routes or the types package change. Otherwise write `no API change`.
     State: breaking or additive · which production coach/mobile operations still validate · types-package version consumers must bump to · whether mobile needs this in production before an OTA (mobile and coach codegen from the live dev schema at install time). -->

no API change

## Cross-repo dependencies

<!-- Hexis is a polyrepo. One row per PR in another repository that needs this change or that this change needs.
     Repository: hexis-engines-api · hexis-expo-next · coach-desktop-next · b2b-portal · foods-dashboard · hexis-infra · hexis-localization
     PR: the link, or `unopened` with the ticket. Order: `before` (that PR must be in production first) · `after` (this PR first) · `together`.
     A single `none` row means the backend ships alone. -->

| Repository | PR  | Order | Why |
| ---------- | --- | ----- | --- |
| none       |     |       |     |

## Evidence

<!-- Links, not sentences.
     all tiers: the green `test` run for this head.
     1+: the changed GraphQL operation or REST call run against dev or a preview, request and response in a code block; recording for anything user-visible.
     2+: the whole affected flow (sync, job, search) incl. failure cases and the Day/Meal state after it.
     3a: `prisma migrate deploy` replay on an empty Postgres with `prisma migrate diff` clean; the compatibility statement above; billing: the Stripe test-mode run.
     3b: the dry run or preview output. -->

## Operational evidence

<!-- How we will know this works in production and how we would know it stopped. A few lines:
     outcome and silent-failure mode · signal (Loki query, Grafana panel, alert rule, Sentry issue) · who looks and when (3-day verification window) · recovery (flag off, revert, fix forward).
     Tier 0 may write `existing coverage`. -->
