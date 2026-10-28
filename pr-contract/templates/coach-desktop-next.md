## Change

<!-- What changes for the coach, and why. Link the Jira ticket. The branch name carries the HEX key.
     Coach facts that matter at release: Vercel builds every PR and deploys `production` on merge; codegen runs against the live backend schema at install, so the production build fails until the backend fields exist in production (20 Oct);
     the Localization workflow registers string IDs with the portal; the Playwright suite runs weekly against dev, not per PR. -->

## Tier

<!-- 0 cosmetic (≤20 lines on public assets, styles, translations, e2e, docs) · 1 contained behaviour · 2 flow (src/app routes, components, features, sections) · 3a data/contract (src/graphql, generated types, auth, Stripe, billing, middleware, API routes) · 3b pipeline (.github, vercel.json, next.config).
     Declare the lowest honest tier. The check raises it from the changed paths and never lowers it. -->

1

## Release note

<!-- One sentence a coach would understand, or the single word `internal`. Copied into the next release's notes when the PR merges. -->

## Deploy actions

<!-- Everything production needs besides the code, or the single word `none`. One line each. Coach checklist:
     backend — fields or operations this UI reads that must be in production first (see Cross-repo dependencies); the Vercel production build will fail otherwise.
     localisation strings — IDs added or changed, registered for production.
     PostHog flag <KEY> — payload and when it flips.
     Vercel env — any new environment variable in the Vercel project (production and preview).
     Stripe — Customer Portal or retention configuration this UI expects. -->

none

## Cross-repo dependencies

<!-- Hexis is a polyrepo. One row per PR in another repository that this change needs or that needs this change.
     Repository: hexis-backend · hexis-expo-next · b2b-portal · hexis-localization · hexis-infra
     PR: the link, or `unopened` with the ticket. Order: `before` (that PR must be in production first) · `after` · `together`.
     A GraphQL or generated-types change with a `none` row fails the check: name the backend PR or explain in Why that production already has the fields.
     A single `none` row means coach ships alone. -->

| Repository | PR | Order | Why |
| --- | --- | --- | --- |
| none | | | |

## Evidence

<!-- Links, not sentences.
     all tiers: the green `test` and `lint and format` runs for this head, and the Vercel preview URL for this commit.
     0: before/after screenshots on the preview.
     1+: recording on the Vercel preview against dev, as a coach fixture persona; API changes: the operation and response in a code block.
     2+: the whole journey incl. empty, error and loading states; the Playwright spec that covers it, or why none does.
     3a: the backend PR this depends on and the codegen result against it. -->

## Operational evidence

<!-- How we will know this works in production and how we would know it stopped. A few lines:
     outcome and silent-failure mode · signal (Sentry issue, Vercel runtime log, PostHog event, backend log) · who looks and when (3-day verification window) · recovery (Vercel rollback to the previous deployment, flag off).
     Tier 0 may write `existing coverage`. -->
