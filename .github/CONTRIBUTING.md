# Contributing to LabVIEW CI

This repository is the **tooling source**, not a client. Every repo listed on the
dashboard's Clients page installs from here, so a few things behave differently
than they would in a normal project.

## This repo is not a place to install the pipeline

The **LabVIEW CI configurator** (the "Integrate this CI pipeline" page) is a
*client installer*. It vendors a tooling payload pinned to a released version and
deletes files it does not recognise as current tooling.

This repository carries release machinery that no client has — `release.yml`,
`promote-release.py`, `discover-clients.yml`, the configurator pages themselves.
So running the configurator against this repo **or a fork of it** deletes that
machinery and overwrites `catalog.json` with an older version.

That is not hypothetical. On 2026-07-30 an install pinned at v4.11.10 landed on a
fork, downgraded the catalog from 4.12.4, removed 48 source-only files, and
stopped all client releases for two weeks.

The configurator now refuses this, and `catalog-source-sync` fails the PR if the
source-only files go missing. **To test the pipeline, install it into a separate
scratch repository.**

## Describe pull requests in terms of user outcomes

PR titles and opening summaries must explain the features, fixes, or capabilities
being delivered and why they matter to toolkit users. Do not lead with agent
roles, handoffs, coordination, integration chores, or release mechanics. PR titles
can become release notes, so they must make sense without session context.

- Start the title with the concrete change. For documentation-only changes, say
  "Document" rather than implying the documented feature is implemented here.
- Open the body with user-visible behavior and benefits, grouped by capability.
  Explain the previous problem and the new behavior for fixes.
- Distinguish changes in the actual PR diff from features already on the base
  branch or in another PR. Put related release context in a separate section;
  do not claim those implementations are introduced by this PR.
- Put tests, evidence, compatibility limits, and unresolved validation gaps after
  the feature/fix summary. Keep coordination policies and release mechanics in
  a short supporting section when relevant, never as the headline.
- Describe genuine process-only changes honestly; do not invent product
  features to make them sound user-facing.
- Use direct clickable links whenever referencing a PR in descriptions, comments,
  or user-facing status reports.

For example:

- Avoid: "Release: integrate fleet handoffs and enforce dashboard mirror parity".
- Prefer: "Fix dependency dispatch guidance and document snapshot and VIDiff
  improvements". Explain which dashboard behavior changes in this PR and which
  snapshot/VIDiff capabilities are already implemented and being documented.

Use the [PR template](pull_request_template.md). Before opening or updating a PR,
compare its title and summary with the actual base-to-head diff. A reviewer must
be able to identify the user benefit, the exact scope, and any verification gaps
without reading agent handoffs or commit history. Apply the same user-outcome
standard to release-note fragments.

## Every PR identifies its release version and update type

Every PR, including documentation, maintenance, and bot-generated PRs, must have
a **Release impact** section immediately after its feature/fix summary. Authors
and reviewers must fill in missing bot metadata before approval. Include:

- **Target version:** an explicit `vMAJOR.MINOR.PATCH`, marked **projected** until
  CI prepares it. Link the latest published release used as the baseline and any
  matching release-preparation PR. A projection is not a reservation: merge order
  and concurrent publications can change it. Refresh it before merge.
- **Update type:** **major**, **minor**, or **incremental (patch)**, with a reason.
  Major means an incompatible change; minor means a backward-compatible new
  capability; patch means a backward-compatible fix or docs/maintenance update.
  Apply the matching `release:major`, `release:minor`, or `release:patch` label;
  the workflow also defaults an unlabeled PR to patch.
- **Availability:** **projected**, **prepared / not published**, or **published**.
  A merged feature PR or green workflow is not proof of publication. Once CI
  prepares a version, link that proposal; once it publishes, update the feature
  PR with the exact first containing release and its immutable tag/release link.
  Verify that the release actually contains the PR's changes before confirming.
- **Client update guidance:** give the exact immutable tooling version to select
  once published. State separately whether the client's chosen `dev`, `beta`,
  `stable`, or rolling alias resolves to a release containing the fix. Never
  imply that publication promotes a channel, or that updating any channel
  automatically includes it. Check the installed tooling version after updating.

Keep the feature/fix headline first; do not substitute a version-only title.
Compute projected targets from published tags and the intended bump, never by
hand-editing the catalog. For an explicitly skipped release, state **no release
from this PR** and **containing version unassigned** rather than promising a
version; record the actual containing release when it becomes available.
Promotion-only PRs must identify the already-published version being promoted,
the channel, and any separate metadata-release version; they are not new fixes.

For example: **Target: v4.18.7 (projected), incremental (patch)**; **Availability:
not published**; **Client update: select v4.18.7 after publication; beta/stable
inclusion is not implied**. Replace these values with the PR's verified facts.

## Never edit `catalog.json`

`.github/labview-ci/catalog.json` → `version` is the version of record: the
release workflow reads it to cut the `v<MAJOR>.<MINOR>.<PATCH>` tag and force-move
the `v<MAJOR>` alias every client pins to. It is release machinery, not a config
file, and **CI owns it**. You should never have it open.

Every merge to main is published automatically. CI derives the next version from
the highest published **tag** — never from the file — so a stale or conflicted
catalog cannot roll the version backwards. That's deliberate: on 2026-07-30 a
hand-edited copy took the version from 4.12.4 to 4.11.10 and stalled every client
release for two weeks.

What you supply instead:

**A release note.** Drop a `.md` file in `.github/labview-ci/notes/` describing
your change in prose, as a client would read it. One file per PR, so two PRs can
never conflict. CI folds pending fragments into the release and deletes them. See
[the notes README](labview-ci/notes/README.md). Forget one and the release still
happens, falling back to your PR title.

**A bump level.** As required by the release-impact policy, label the PR
`release:patch` for an incremental fix, documentation, or maintenance update,
`release:minor` for a new capability, or `release:major` for a breaking change.
The workflow retains its patch default for legacy unlabeled PRs. Major moves
`@v<major>`, so it should be rare.

To land something without publishing, put `[skip release]` in the merge commit
message — but use it sparingly. Everything being published is what makes
retroactive promotion to beta and stable work.

## Channels, not version numbers, carry risk

Publishing is automatic and unconditional. Deciding a version is *good* is a
separate, manual, retroactive act: `promote-release.yml` moves the rolling
`stable` and `beta` tags to an already-published release. Nothing is rebuilt.

The immutable version identifies the released contents; the selected channel
decides which version a client is offered. Record both when explaining whether
an update will deliver a particular fix.

## Reverts

If a PR is reverted, do **not** bring it back by merging main into your fork and
re-merging your branch — git treats the reverted commits as already merged, so
they silently return. Revert the revert (`git revert <revert-sha>`) on a fresh
branch, fix the actual problem, and open a new PR.

This is how the 2026-07-30 downgrade came back seven minutes after it was fixed.

## Repository settings this relies on

The guards are only as good as the branch rules that enforce them. On `main`,
under **Settings › Branches › Branch protection rules**:

- Require a pull request before merging
- Require review from Code Owners (activates `.github/CODEOWNERS`)
- Require status checks to pass → **`Catalog Source Sync`**

Without the required status check, `catalog-source-sync` reports a failure that
nothing acts on.
