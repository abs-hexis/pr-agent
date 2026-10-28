## Change

<!-- What changes in the recommendations, and why, with the scientific source or spreadsheet version it traces to. Link the Jira ticket. The branch name carries the HEX key.
     Engine facts that matter at release: a merge to development moves the backend's python-engine pin on development automatically (update-submodule workflow); production only moves when a backend release PR carries the pin.
     There is no pytest in CI yet, so the test run is evidence you attach. CHANGELOG.txt must gain an [UNRELEASED] entry. -->

## Tier

<!-- 0 cosmetic (docs only) · 1 contained (a helper, a bound, a log line) · 3a data/contract (anything under engines/, services.py, calculation bundles: these change athlete recommendations) · 3b pipeline (.github, Dockerfile).
     Declare the lowest honest tier. The check raises it from the changed paths and never lowers it. -->

3a

## Release note

<!-- One sentence a coach or athlete would understand ("Carb ranges now follow weekly training hours"), or the single word `internal`. This is also your CHANGELOG line. -->

## Deploy actions

<!-- Everything production needs besides the code, or the single word `none`. One line each. Engine checklist:
     engine pin — this change reaches production only through a backend release PR that moves the submodule; name the backend PR or `unopened`.
     cutover — any date-gated calculation bundle (calculation_bundles.py): the date, what switches on, and whether /athlete switches earlier than /day and /livegraph.
     backend inputs — new or renamed request fields the backend must send (totalActivityHours, elevationGain); name the backend PR.
     goldens — which functional cases were recaptured and why only the expected fields moved. -->

none

## Engine release

<!-- Required. State: CHANGELOG.txt entry added under [UNRELEASED] (yes/no) · recommendation outputs that change (carb ranges, macros, TDEE, meal timing, none) · reference-athlete comparison attached (link) · science review needed before release (yes/no, who) · allergen or compliance owner involved (yes/no). -->

## Cross-repo dependencies

<!-- Hexis is a polyrepo. The backend pin PR is always a dependency unless this is docs only.
     Repository: hexis-backend · hexis-expo-next · coach-desktop-next
     PR: the link, or `unopened` with the ticket. Order: `before` (that PR must be in production first) · `after` · `together`.
     A single `none` row is accepted only for Tier 0. -->

| Repository    | PR       | Order    | Why                                            |
| ------------- | -------- | -------- | ---------------------------------------------- |
| hexis-backend | unopened | together | backend release PR moves the python-engine pin |

## Evidence

<!-- Links and the pytest output, not sentences.
     all tiers: the `test-sync` run for this head and the pytest run (`PYTHONPATH=. pytest tests -q`) pasted in a code block with its pass/fail counts.
     3a: the functional cases added or recaptured, and the before/after diff for the reference athletes (which fields moved, by how much). -->

## Operational evidence

<!-- How we will know the new outputs are right in production and how we would know they are wrong. A few lines:
     outcome (which numbers change for whom) · signal (engine error logs in the backend, a comparison query on recommendation outputs for the reference athletes, coach/support reports) · who looks and when (the first day after the backend release) · recovery (backend re-pins the previous engine sha; cutover dates can be moved forward). -->
