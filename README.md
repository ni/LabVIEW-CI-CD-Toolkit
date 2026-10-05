# LabVIEW CI with Containers

**Real CI/CD for LabVIEW — mass compile, VI Analyzer, visual VI diffs, a browsable VI gallery, and a live status dashboard — running natively on GitHub Actions or GitLab CI, in containers, on your own account.**

Push a commit and this pipeline spins up headless LabVIEW inside a throwaway Docker container on your GitHub Actions or GitLab CI runner, runs the code-quality checks you choose, and publishes the results as a polished Pages dashboard. There's no build server to babysit, no license server to wire up, and nothing running on anyone else's infrastructure — every container executes in *your* CI environment, under your account's limits.

<p align="center">
  <a href="https://ni.github.io/LabVIEW-CI-CD-Toolkit/"><img src="https://img.shields.io/badge/View%20the%20Live%20Dashboard-1f6feb?style=for-the-badge&logo=githubpages&logoColor=white" alt="View the live LabVIEW CI dashboard" height="42"></a>
  &nbsp;&nbsp;
  <a href="https://ni.github.io/LabVIEW-CI-CD-Toolkit/integrate.html"><img src="https://img.shields.io/badge/Apply%20to%20New%20Repo-238636?style=for-the-badge&logo=github&logoColor=white" alt="Apply LabVIEW CI to a new repository" height="42"></a>
</p>

<p align="center">
  <a href="https://ni.github.io/LabVIEW-CI-CD-Toolkit/documentation.html">Documentation</a> &nbsp;·&nbsp;
  <a href="https://ni.github.io/LabVIEW-CI-CD-Toolkit/faq.html">FAQ</a> &nbsp;·&nbsp;
  <a href="example/README.md">Example project</a>
</p>

> **Apply to New Repo** is the same green button you'll find on every dashboard — it's
> the one-click installer for your own repository. The buttons go live once GitHub Pages
> is enabled (Settings ▸ Pages ▸ deploy from the `gh-pages` branch) and the
> dashboard/configurator workflows have run.

<p align="center">
  <a href="https://www.youtube.com/watch?v=dXeF1d6HBmg">
    <img src="https://img.youtube.com/vi/dXeF1d6HBmg/maxresdefault.jpg" alt="5-minute overview of LabVIEW CI with Containers" width="640">
  </a>
  <br><em>5-minute overview — click to watch on YouTube</em>
</p>

---

## The problem it solves

LabVIEW projects rarely get the automated quality gates that text-based languages take for granted. There's no `git diff` you can read for a binary `.vi`, no one-command "compile everything and tell me what broke," and standing up LabVIEW on a build server is a licensing-and-setup ordeal. So changes ship without a safety net, broken VIs and missing dependencies surface late, and reviewing changes to VIs is mostly guesswork.

LabVIEW CI closes that gap. It is **portable** (drops into any LabVIEW repository), **self-contained** (LabVIEW runs headless in Docker, so there's no machine to maintain), and **transparent** (every result is a shareable web page). You get the "green check on every commit" workflow the rest of software engineering relies on — for LabVIEW.

## What you get

| Capability | What it does |
|---|---|
| **Mass Compile** | Compiles every VI/CTL and flags broken VIs and missing dependencies — a build check for your whole project, on every commit. |
| **VI Analyzer** | Runs NI's static-analysis suite for correctness, performance, style, and documentation, as a friendly, navigable report. |
| **VIDiff** | Visual, side-by-side front-panel + block-diagram diffs of the VIs each commit changed — code review you can actually see. |
| **VI Browser** | A searchable gallery of every VI's front panel and block diagram, browsable across your commit history. |
| **Unit Tests** | Runs Caraya / VI Tester / NI Unit Test Framework headlessly and merges the results into one report. |
| **Antidoc** | Generates project documentation from the VI hierarchy on every commit. |
| **Status Dashboard** | Aggregates every capability's result for every commit into one live GitHub Pages or GitLab Pages dashboard. |

Enable only the capabilities you want — each runs on its own and writes its own report. The whole system is driven by a single catalog, so adding a new capability is one entry rather than edits scattered across the UI, installer, and dashboard.

## See it live

The dashboard for this repo's own [example project](example/README.md) — a real ~54-VI LabVIEW application with a dozen revisions of genuine history — is published and continuously updated:

**▶ [View the live dashboard](https://ni.github.io/LabVIEW-CI-CD-Toolkit/)** — click any cell to open the underlying Mass Compile, VI Analyzer, VIDiff, or VI Browser report.

## Add it to your repository

The fastest path is the interactive installer — the same **Apply to New Repo** button you'll find on every dashboard:

**➕ [Apply to New Repo](https://ni.github.io/LabVIEW-CI-CD-Toolkit/integrate.html)** — pick your LabVIEW version, platforms, and capabilities; choose GitHub or GitLab; and open a reviewable install pull/merge request with everything wired up. GitHub installs can enable GitHub Pages and add a dashboard badge to your README in the same pull request.

**Private GitHub repositories are supported** — see [installing to a private GitHub repository](.github/labview-ci/README.md#installing-to-a-private-github-repository) for the token setup.

**GitLab projects use native GitLab templates, the project Container Registry, and GitLab Pages.** Use the browser configurator with a GitLab `api` token, or, after the first canonical release synchronizes it, [install from the GitLab mirror](.github/labview-ci/README.md#installing-from-gitlab) from a local checkout. GitHub is the canonical implementation; the GitLab mirror advances only after canonical releases and is not a second contribution branch.

Prefer the command line, or want a thin GitHub reusable-workflow caller? Both are covered in the [installer guide](.github/labview-ci/README.md). The GitHub caller is:

```yaml
# .github/workflows/labview-ci.yml
jobs:
  labview-ci:
    uses: ni/LabVIEW-CI-CD-Toolkit/.github/workflows/labview-ci.reusable.yml@v4
    secrets: inherit
```

### Which tag to pin

Every merge here is published as an immutable `v<major>.<minor>.<patch>` release. Which of
those releases is considered *good* is a separate, later decision, and that's what the moving
tags encode:

| Pin | You get | Moves when |
|---|---|---|
| `@v4` | The latest release blessed as **stable**. Pin this unless you have a reason not to. | A release is promoted to stable |
| `@beta` | The latest release-candidate | A release is promoted to beta |
| `@dev` | Every build, including diagnostics | Every merge |
| `@v4.14` | The newest patch on the 4.14 line | Every patch on that line |
| `@v4.14.1` | Exactly that release, forever | Never |

`@v4` never moves backwards, so an update can't silently downgrade you. Major-version
bumps are reserved for breaking changes — that's the only time you'd move off `@v4`.

GitHub installs can use the dashboard's **Configure** and **Update now** buttons. GitLab installs are updated from a local checkout with the recorded-distribution bootstrapper described in the installer guide.

## How it works on GitHub

```
push / PR ─▶ reusable workflow ─▶ per-capability container jobs
                                    │ pull worker image (built once, cached)
                                    │ docker run --rm  →  headless LabVIEW
                                    │ build report
                                    ▼
                              gh-pages ─▶ GitHub Pages dashboard
```

- **Containers, not a build server.** Worker images bundle LabVIEW (plus your VIPM / `.vipc` dependencies, baked in at build time), are published once to your project registry, and are pulled on demand. GitHub uses GHCR; GitLab uses its project Container Registry. Each job runs in a fresh, throwaway container.
- **Your infrastructure, your control.** Everything runs on GitHub-hosted or self-hosted Actions runners, or GitLab runners under your account. Nothing touches NI's or the author's servers.
- **Catalog-driven and versioned.** A single `catalog.json` is the source of truth for every capability; the configurator and installer both read it. GitHub clients can adopt updates from the dashboard; GitLab clients refresh from the distribution recorded in their manifest.

The [Documentation](https://ni.github.io/LabVIEW-CI-CD-Toolkit/documentation.html) tells the full implementation-level story.

## Documentation & help

- **[Full Documentation](https://ni.github.io/LabVIEW-CI-CD-Toolkit/documentation.html)** — an implementation-level reference for expert LabVIEW developers: how the worker images are built, how runners launch headless LabVIEW, how VIPM dependencies are baked in, the catalog model, the report data contracts, the security boundaries, and how to extend or adapt the system.
- **[FAQ](https://ni.github.io/LabVIEW-CI-CD-Toolkit/faq.html)** — short, practical answers about setup, configuration, and day-to-day operation.
- **[Example project](example/README.md)** — the LabVIEW application this repo runs its own CI on.

## Contributing

Contributions are welcome — issues, fixes, and new capabilities. GitHub is the canonical source; do not contribute independently to the GitLab distribution mirror. The architecture is built to make extension cheap: because the system is catalog-driven, adding a capability is usually a single entry in [`catalog.json`](.github/labview-ci/catalog.json) plus the action that implements it. Start with the [Documentation](https://ni.github.io/LabVIEW-CI-CD-Toolkit/documentation.html) ("Capabilities in depth" and the architecture overview) to see how the pieces fit, then open an issue or pull request.

Pull requests from forks run the same analysis and retain their report artifacts for review, but cannot update this repository's shared GitHub Pages dashboard. That is intentional: fork-provided code never receives permission to write here. After a reviewed PR merges, the `main` pipeline reruns with repository permissions and publishes the canonical report. Collaborators who need pre-merge dashboard publication should use feature branches in this repository rather than forks.

## Versioning & updates

Every change to any part of the stack bumps a version and ships as a release, so installed repositories can see exactly what changed and choose when to adopt it. The [installer guide](.github/labview-ci/README.md) describes the versioning and release model in detail.
