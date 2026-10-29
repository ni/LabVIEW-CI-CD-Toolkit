# Agent coordination

## Release captain

The user has designated the **release captain for CI-CD toolkit** session as
the integration owner. Worker agents implement scoped changes; the captain
reviews, integrates, validates, and publishes them. Follow this division unless
the user explicitly overrides it.

Read [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md) and
[.github/copilot-instructions.md](.github/copilot-instructions.md) before making
changes. Their source-repository safeguards and documentation requirements apply
to all agents.

## Worker agents

- Agree on scope and file ownership with the captain before editing. Prefer
  separate worktrees for concurrent implementation. In a shared checkout, do not
  change branches, stage unrelated files, or overwrite another agent's work.
- Do not merge, push, publish releases, move tags, or promote release channels.
  Leave those actions to the captain unless the user explicitly assigns them.
- Do not commit unless the user or captain explicitly requests it. Keep any
  requested commits limited to the assigned work.
- Keep required documentation and duplicated dashboard assets synchronized.
- Run relevant checks and hand off the result using the template below. Clearly
  distinguish checks that passed, failed, or could not run locally.
- After handoff, do not continue editing the submitted changes until the captain
  coordinates a follow-up.

## Handoff

Provide this information to the captain in the session conversation:

```text
Task and intended behavior:
Session / branch / worktree:
Changed files or explicitly requested commit SHAs:
Dependencies and overlapping work:
Validation commands and results:
Checks requiring Windows, LabVIEW, containers, or GitHub Actions:
Documentation / mirrored assets / release note:
Known risks and recommended integration order:
Ready for captain review: yes / no
```

## Captain integration gate

1. Inventory submitted work and inspect actual Git state; session summaries may
   be stale. Do not mix unrelated changes into an integration.
2. Review each diff for correctness, security, compatibility, scope, and overlap.
   Resolve cross-agent dependencies before integrating.
3. Check documentation and mirrored assets, then run targeted tests and relevant
   repository gates. Run the catalog/source validator for every integration:
   `python3 .github/labview/validate-catalog-source-sync.py`.
4. Record any environment-limited verification. Do not describe unrun checks as
   passing; require appropriate CI evidence before declaring integration verified.
5. Fetch the latest remote state before publishing, reconcile concurrent changes,
   and use the repository's PR and required-review/check process. Do not bypass
   branch protection, force-push shared branches, or overwrite others' work.
6. Publish only reviewed, scoped changes when authorized. Observe the resulting
   CI and automated release; a successful push alone is not a verified release.
7. Report the integrated commits or PR, validation results, release status, and
   unresolved blockers to the user. If remote checks are pending, say so.

## PR communication gate

Follow the user-outcome PR policy in
[.github/CONTRIBUTING.md](.github/CONTRIBUTING.md#describe-pull-requests-in-terms-of-user-outcomes)
and the [PR template](.github/pull_request_template.md) when creating or updating
a PR. Lead with concrete features/fixes and their user benefit, not captain
roles, fleet handoffs, or release mechanics. Check the actual base-to-head diff
and explicitly distinguish new implementation from already-landed release
context. Keep validation limits visible and always link PR references.

Every PR must also follow the
[release-impact policy](.github/CONTRIBUTING.md#every-pr-identifies-its-release-version-and-update-type):
state the explicit target version, major/minor/incremental (patch) classification
and matching label, baseline release, publication status, and client update
guidance. Refresh projections before merge and record the verified first
containing immutable release after publication. Do not equate merge, publication,
and channel promotion. Include this information in captain status reports too.

## Release boundaries

- GitHub is canonical; do not contribute independently to the GitLab mirror.
- Never run the client installer/configurator against this source repo or a fork.
- CI owns catalog versioning and release publication. Do not hand-edit versions
  or manually move release tags to integrate agent work.
- Follow the existing release-note and bump-label conventions. Publication and
  promotion are distinct: do not promote beta or stable without explicit user
  authorization.
- This coordination agreement is not a background monitor. The captain checks
  and integrates work during active turns; recurring automation requires a
  separate user request.
