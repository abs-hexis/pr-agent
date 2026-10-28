## Change

<!-- What changes for the athlete, coach or system, and why. Link the Jira ticket. The branch name carries the HEX key. -->

## Tier

<!-- One of: 0 (cosmetic, ≤20 lines, no behaviour change) · 1 (contained behaviour) · 2 (flow or experience) · 3a (data, contract, billing, auth) · 3b (pipeline or infra).
     Declare the lowest honest tier. The check raises it from the changed paths and never lowers it. -->

1

## Release note

<!-- One sentence a coach or athlete would understand, or the single word `internal`. This line is copied into the next release's notes when the PR merges. -->

## Deploy actions

<!-- Everything production needs besides the code, or the single word `none`. One line each:
     migration <name> · backfill <script> · Infisical key <NAME> (which environments) · PostHog flag <KEY> (payload, when to flip) ·
     localisation strings (count, published?) · store build (new runtime?) · Typesense schema/backfill · engine pin · vendor config (Stripe portal, Garmin endpoints, Clerk, Resend) · infra (pulumi) -->

none

## Cross-repo dependencies

<!-- Hexis is a polyrepo. List every other repository this change needs or that needs this change, one row per PR.
     Repository: hexis-backend · hexis-engines-api · hexis-expo-next · coach-desktop-next · b2b-portal · foods-dashboard · hexis-infra · hexis-localization
     PR: the link, or `unopened` with the ticket if it does not exist yet.
     Order: `before` (that PR must be in production first) · `after` (this PR must be in production first) · `together` (same release window).
     Write a single `none` row when this repository ships alone. -->

| Repository | PR | Order | Why |
| --- | --- | --- | --- |
| none | | | |

## Evidence

<!-- Links, not sentences. Required by tier:
     all tiers: the green CI run for this head.
     0: before/after screenshots.
     1+: recording or screenshots on a preview or dev build, with the SHA/build tested; API changes: request/response in a code block.
     2+: the whole journey incl. empty, error and loading states.
     3a: migration replay result, schema/contract compatibility statement.
     3b: dry run or preview output. -->

## Operational evidence

<!-- How we will know this works in production, and how we would know it stopped. A few lines:
     outcome and silent-failure mode · existing or new signal (log, metric, alert, dashboard link) · who looks, when (verification window) · recovery (flag off, revert, fix forward).
     Tier 0 may write `existing coverage`. -->
