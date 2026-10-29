# Copilot / Contributor Instructions — LabVIEW-CI-with-Containers

This repository is a portable, container-based CI/CD pipeline for LabVIEW on
GitHub Actions. The notes below are **rules**, not suggestions — follow them on
every change.

## Rule 1 — Keep the Documentation current (MANDATORY)

There is a long-form developer reference at `.github/pages/documentation.html`
(served at `…/documentation.html`, a sibling of the FAQ). It explains how the
whole stack works for expert LabVIEW developers who want to evaluate, extend, or
adapt it.

**Whenever you add a capability, change how a runner/container is built, learn
how to make something work (for example, getting VIPM dependency baking
working), change a report data contract, alter the catalog model, or otherwise
change the inner workings of the system, you MUST update
`documentation.html` in the same change** so the documentation never drifts from
reality.

Concretely, when your change touches any of these, update the matching section
of `documentation.html`:

- A new action/capability under `actions/` → add it to the "Capabilities in
  depth" section and the architecture overview.
- A change to how worker container images are built or how headless LabVIEW is
  launched → update "Worker container images" and "Container lifecycle".
- A new or changed dependency-install mechanism (VIPM, package baking, etc.) →
  update "VIPM dependency baking".
- A change to a report's JSON/HTML output → update "Report data contracts".
- A change to the catalog (`catalog`/configurator entries) → update "The
  catalog" and "Client discovery".
- A change to versioning, distribution, or the installer → update
  "Installation / distribution" and "Versioning / updates".

If you discover the documentation is already out of date, fix it as part of your
change rather than leaving it stale.

## Rule 2 — Pages exist in two synchronized copies

Every site page/asset exists **identically in two locations** and BOTH must be
edited identically in the same change:

- `.github/pages/<file>` — the canonical source site.
- `actions/dashboard/<file>` — the copy bundled with the `actions/dashboard`
  composite action and staged to `gh-pages` by `dashboard.py`.

This applies to `documentation.html`, `faq.html`, `lvci-header.js`,
`vi-browser.html`, and the other shared page assets. After editing, verify the
two copies are byte-identical (e.g. compare MD5 hashes).

When you add a **new** bundled page, also:

1. Add it to the `_stage()` staging list in `actions/dashboard/dashboard.py`.
2. Add its `.github/pages/<file>` path to the trigger `paths:` in
   `.github/workflows/dashboard-pages.yml`.
3. Wire it into the shared nav in `lvci-header.js` (both copies) if it should
   appear in a menu.

## Rule 3 — Cross-links

- The FAQ (`faq.html`) links to the Documentation, and the Documentation links
  back to the FAQ. Keep both links working.
- The top-level `README.md` links to the live Documentation page. Keep it valid.

## Rule 4 — PRs explain features and fixes first

When creating or updating a PR, follow the
[user-outcome PR policy](CONTRIBUTING.md#describe-pull-requests-in-terms-of-user-outcomes)
and [PR template](pull_request_template.md). Titles and opening summaries explain
concrete capabilities, fixes, and user benefits, not agent coordination or release
mechanics. Match claims to the actual PR diff, separate already-landed features
from new changes, disclose validation gaps, and link every PR reference.

## Rule 5 — Every PR identifies the containing release

Follow the [release-impact policy](CONTRIBUTING.md#every-pr-identifies-its-release-version-and-update-type).
After the feature summary, state the explicit target version, update type
(major, minor, or incremental/patch), matching bump label, published baseline,
availability, and exact client update guidance. Mark estimates as projected;
refresh before merge and confirm the first containing immutable release after
publication. A successful merge or workflow does not prove availability, and
publication does not imply beta/stable promotion. CI still owns versioning.
