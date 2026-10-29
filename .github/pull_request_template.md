## Features and fixes

<!-- Explain what users can do now, or what failed before and works now.
Lead with capabilities and benefits, not agent roles or release mechanics.
Use an outcome-focused title; use "Document" for documentation-only features. -->

## Release impact

| Field | Required information |
|---|---|
| Target version | `vMAJOR.MINOR.PATCH` (projected), or the exact CI-prepared version and linked proposal |
| Update type | Major / minor / incremental (patch), reason, and matching `release:*` label |
| Published baseline | Link the published release used to calculate the target |
| Availability | Projected / prepared, not published / published with first containing release link |
| Client update | Exact immutable tooling version once published; verify chosen channel includes it and confirm installed version |

<!-- Refresh before merge. After publication, record the verified first containing
release and immutable tag link here. Merge order can change a projected number.
Do not claim a pending release is installable or a channel has been promoted.
For skip-release PRs, state no release from this PR / containing version unassigned.
For promotions, distinguish the promoted version from the metadata release. -->

## Scope and compatibility

<!-- Describe what this PR actually changes, affected platforms, and limitations.
If needed, separate already-landed release context here and link related PRs.
Do not present code already on the base branch as new implementation in this PR.
Keep coordination or release details secondary. Delete inapplicable prompts. -->

## Validation

<!-- List checks and results. Link relevant CI runs and PRs. Explicitly identify
failed, pending, skipped, or environment-limited checks; do not imply they passed. -->

## Author checklist

- [ ] Title and opening summary explain concrete changes and user benefits.
- [ ] Claims match the base-to-head diff; already-landed work is labeled separately.
- [ ] Release impact names the target version, update type/label, baseline, availability, and client update guidance.
- [ ] Compatibility limits and verification gaps are explicit; PR references link directly.
- [ ] Required documentation, mirrored assets, and release-note fragment are updated, or not applicable.
- [ ] Catalog/source validation passes; catalog versions are left to CI.
