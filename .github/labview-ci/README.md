# LabVIEW CI — portable CI/CD for any LabVIEW repository

This folder makes a set of LabVIEW CI/CD capabilities **portable** so they can be
installed into any LabVIEW repository — from the command line or from the
**"Integrate this CI pipeline"** button on the CI dashboard.

| Capability | What it does | OS |
|---|---|---|
| **Dashboard** | Dashboard and configurator published through GitHub Pages or GitLab Pages | Linux runner |
| **Mass Compile** | Compiles every VI to catch broken/mutated code; reports % compiled | Windows |
| **VI Analyzer** | Runs the VI Analyzer test suite; native + friendly report | Windows |
| **VIDiff** | Side-by-side visual diff reports per changed VI; PR comments | Windows / Linux |
| **VI Snapshots** | Browseable gallery of every VI's block diagram (the VI Browser) | Windows |
| **Shared image** | Builds the LabVIEW CI container image in the provider's project registry | Windows / Linux |
| **Unit Tests** | *Planned* — placeholder showing how new capabilities slot in | Windows / Linux |

Everything is driven by [`catalog.json`](catalog.json) — a single capability
registry that **both** the configurator UI and the installer read. Adding a new
capability is a one-entry change (see [Adding a capability](#adding-a-capability)).

## How it fits together

```
catalog.json ──────┬──────────────► integrate.html  (the dashboard configurator UI)
 (capability        │                  • pick version / OS / activities
  registry,         │                  • emits an install command + a downloadable bundle
  single source)    │
                    └──────────────► install.py      (the installer brain)
                                       • copies the selected capabilities' files
                                       • rebrands cosmetic strings for the target repo
                                       • writes .github/labview-ci.yml (manifest)
install.sh / install.ps1  ───────────► thin bootstrappers: fetch tooling, find Python, run install.py
```

The design separates three concerns so each is distributed the right way:

- **Heavy, version-pinned** — the multi-GB LabVIEW container image — is built
  **once** and published to the provider's project registry (GHCR or the GitLab
  Container Registry); consumers pull it (they never rebuild it).
- **Logic** — the `.ps1` / `.sh` / `.py` scripts — travels *with* the workflows.
- **Repo-local wiring** — push/PR/`workflow_run`/`status` triggers and the Pages
  publish — must live in each repo, so the installer drops thin copies in.

**Functional wiring adapts at runtime, not at install time.** The image name,
Pages URL, and LabVIEW version derive from the GitHub context and optional
Actions variables, so the same workflow file works unchanged in any repo. The
installer only rewrites *cosmetic* branding (report titles), which keeps installs
robust and upgrades trivial.

## Install from the command line

From the root of the repo you want to add CI to:

```bash
# macOS / Linux / Git Bash
curl -fsSL https://raw.githubusercontent.com/ni/LabVIEW-CI-CD-Toolkit/main/.github/labview-ci/install.sh \
  | bash -s -- --activities masscompile,vi-analyzer,vidiff,dashboard \
               --os windows,linux --labview-version 2026
```

```powershell
# Windows PowerShell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/ni/LabVIEW-CI-CD-Toolkit/main/.github/labview-ci/install.ps1))) `
    --activities masscompile,vi-analyzer,vidiff,dashboard --os windows,linux --labview-version 2026
```

Or, from a checkout of the tooling, drive `install.py` directly:

```bash
python3 .github/labview-ci/install.py --list                 # show capabilities
python3 .github/labview-ci/install.py --target /path/to/repo \
    --activities masscompile,dashboard --os windows --dry-run
```

The installer **only writes files into the working tree** — it never runs
LabVIEW, commits, or pushes. Review the result with `git diff`, then follow the
printed next steps (enable Pages, set Actions permissions, commit & push).

## Installing from GitLab

GitHub is the canonical implementation and release source. The
[`elijah286/ci-for-labview`](https://gitlab.com/elijah286/ci-for-labview) project
is an automated, one-way mirror of released source tags; its `main` branch is a
released snapshot, not a contribution branch. After a canonical release has
been synchronized to that mirror, install a native GitLab pipeline from the
root of a local GitLab checkout with:

```bash
# macOS / Linux / Git Bash
curl -fsSL "https://gitlab.com/api/v4/projects/elijah286%2Fci-for-labview/repository/files/.github%2Flabview-ci%2Finstall.sh/raw?ref=main" \
  | bash -s -- --source-host gitlab \
               --source-repo elijah286/ci-for-labview \
               --source-ref main \
               --provider gitlab \
               --activities masscompile,vi-analyzer,vidiff,dashboard \
               --os windows,linux --labview-version 2026
```

```powershell
# Windows PowerShell
& ([scriptblock]::Create((irm "https://gitlab.com/api/v4/projects/elijah286%2Fci-for-labview/repository/files/.github%2Flabview-ci%2Finstall.ps1/raw?ref=main"))) `
    --source-host gitlab --source-repo elijah286/ci-for-labview --source-ref main `
    --provider gitlab --activities masscompile,vi-analyzer,vidiff,dashboard `
    --os windows,linux --labview-version 2026
```

The bootstrapper downloads the source archive through the GitLab API and writes
its origin into `source.distribution` in the target manifest. A later bootstrap
update therefore refetches from GitLab rather than reusing the target's local
copy of the installer. For a self-hosted GitLab mirror, set
`--source-gitlab-url https://gitlab.example.com` along with its project path.
GitLab Linux jobs require a runner that permits Docker-in-Docker; Windows LabVIEW
jobs require a self-managed Windows runner with Docker. Set the provider's
`LVCI_WINDOWS_RUNNER_TAG` and `LVCI_LINUX_RUNNER_TAG` CI/CD variables when your
registered runner tags differ from their defaults.

## Installing to a private GitHub repository

Private GitHub repositories are supported. The main difference is that GitHub
cannot read or write a private target repository unless you authenticate with a
fine-grained personal access token that has access to that specific repository.
The easiest path is the published
[Apply to New Repo page](https://ni.github.io/LabVIEW-CI-CD-Toolkit/integrate.html),
which walks you through creating that token with the permissions pre-filled and
then opens the install pull request for you.

Before installing, make sure:

1. The target repository already exists.
2. For GitHub repositories, the target repository has at least one commit. A
  README-only initial commit is enough.
3. Your token's **Repository access** includes the private repository. GitHub's
  token page can pre-fill the owner and permissions, but you still choose which
  repositories the token can access.
4. The token has the permissions needed for the install choices you enable:

| Permission | Why the installer may need it |
|---|---|
| Contents: Read and write | Create the install branch and commit workflow/tooling files |
| Pull requests: Read and write | Open the install PR when you review before merging |
| Workflows: Read and write | Add or update files under `.github/workflows/` |
| Actions: Read and write | Dispatch the dashboard publish workflow and later CI backfills |
| Administration: Read and write | Optional: set workflow permissions for the repo |
| Pages: Read and write | Optional: enable GitHub Pages automatically |
| Secrets: Read and write | Optional: store `TOOLING_UPDATE_TOKEN` for one-click future updates |

If you install from a local checkout instead of the browser page, run the
bootstrapper from the private repo's working tree. The installer usually infers
the GitHub repo from `origin`, but you can specify it explicitly when needed:

```bash
curl -fsSL https://raw.githubusercontent.com/ni/LabVIEW-CI-CD-Toolkit/main/.github/labview-ci/install.sh \
  | bash -s -- --repo your-org/your-private-repo \
          --activities masscompile,vi-analyzer,vidiff,dashboard \
          --os windows,linux --labview-version 2026
```

Private repo dashboards use GitHub Pages just like public repo dashboards. On
GitHub Free, Pages for private repositories may be unavailable; GitHub Pro,
Team, or Enterprise is typically required. If Pages cannot be enabled
automatically, the installer still lands the CI tooling and reports the manual
follow-up instead of treating the whole install as failed.

After the install PR is merged, the workflows run inside your repository. Worker
images are published under your repository's GHCR packages, and private
repositories are not listed by the public client discovery page.

### Installer flags

| Flag | Meaning |
|---|---|
| `--activities a,b,c` | Capability ids (default: the recommended set) |
| `--os windows,linux` | Target operating systems |
| `--labview-version` | LabVIEW year (default 2026) |
| `--image-name` | Worker image name override |
| `--repo owner/name` | Target repo (default: inferred from the git remote); use the [Apply to New Repo page](https://ni.github.io/LabVIEW-CI-CD-Toolkit/integrate.html) for a guided browser install |
| `--provider github\|gitlab` | Target CI provider to scaffold (default: GitHub) |
| `--source-host github\|gitlab` | Bootstrap distribution to fetch (default: GitHub) |
| `--source-repo owner/name` | Tooling repository on that distribution |
| `--source-ref ref` | Tooling branch, tag, or commit to fetch |
| `--source-gitlab-url URL` | GitLab host for a GitLab distribution (default: `https://gitlab.com`) |
| `--source DIR` | Use a local tooling checkout rather than downloading an archive |
| `--dry-run` | Show what would change without writing |
| `--force` | Overwrite files that already exist |
| `--update` | Refresh an existing install (bootstrappers refetch its recorded distribution; preserves your config) |
| `--list` | List capabilities and exit |

## Updating an existing install

A copy install is a snapshot. To pull later improvements **without losing your
config**, re-run the bootstrapper (or `install.py`) with `--update`:

```bash
curl -fsSL https://raw.githubusercontent.com/ni/LabVIEW-CI-CD-Toolkit/main/.github/labview-ci/install.sh \
  | bash -s -- --update
```

The bootstrapper reads `.github/labview-ci.yml` to recover what was installed
and, when no source option is supplied, reads `source.distribution` before
fetching a fresh archive. This keeps GitLab-mirror installs on GitLab. The
archive-sourced `install.py --update` refreshes every tooling file and **never
overwrites** the files listed under `userConfig` in [`catalog.json`](catalog.json)
(e.g. your `ci-tooling.packages.json`). A direct `install.py --update` only
updates from the source checkout already on disk. Review with `git diff`, then
commit.

For true *ongoing* updates (auto-PRs via Dependabot), graduate to the standalone
dependency model — see [Going standalone](#going-standalone).

## Reconfiguring a repo

Settings live in `labview-ci.yml` (pipeline) + `ci-tooling.packages.json`
(container dependencies). Change them two ways:

- **Configure tool** ([`../pages/configure.html`](../pages/configure.html), the
  dashboard's **⚙ Configure** button) — edit LabVIEW version, OS, activities,
  runner count, and dependencies/Antidoc; download the files or copy a
  `gh workflow run reconfigure.yml` command.
- **Reconfigure workflow** ([`../workflows/reconfigure.yml`](../workflows/reconfigure.yml))
  — a `workflow_dispatch` form that writes the config and **opens a PR**
  (GitHub-native auth, fully reviewable).

## Going standalone

To let many repos share this tooling and receive updates by version, extract it
into its own repo and reference it by tag (`@v1`) with Dependabot auto-bumps. A
ready-to-extract template + step-by-step guide lives in
[`standalone/`](standalone/README.md).

## Adding a capability

The system is built to scale. To add, say, **Unit Tests**:

1. Add the workflow + script files under `.github/workflows/` and `.github/labview/`.
2. Add (or, for the existing placeholder, edit) one entry in
   [`catalog.json`](catalog.json):

   ```jsonc
   {
     "id": "unit-tests",
     "name": "Unit Tests",
     "summary": "Run Caraya/VI Tester unit tests and publish results.",
     "status": "stable",            // flip from "planned"
     "recommended": true,
     "supportsOs": ["windows", "linux"],
     "requires": [],
     "recommends": ["dashboard"],
     "statusContext": "CI / Unit Tests",
     "files": {
       "any":     ["..."],
       "windows": [".github/workflows/unit-tests-windows.yml", ".github/labview/run-unit-tests.ps1"],
       "linux":   [".github/workflows/unit-tests-linux.yml",  ".github/labview/run-unit-tests.sh"]
     }
   }
   ```

That's it. The capability now appears automatically in the configurator UI and
in `install.py` — no UI code and no installer code to change. `status` controls
presentation: `stable`, `advanced` (opt-in), or `planned` (shown but disabled).

## Files in this folder

| File | Role |
|---|---|
| `catalog.json` | Capability registry + version + userConfig — the single source of truth |
| `install.py` | Installer brain (stdlib-only Python); supports `--update` |
| `install.sh` / `install.ps1` | Bootstrappers for `curl \| bash` / PowerShell |
| `config.example.yml` | Documented configuration/manifest schema |
| `standalone/` | Template + guide for the versioned-dependency (own-repo) model |
| `README.md` | This document |

The configurator UI lives at [`.github/pages/integrate.html`](../pages/integrate.html)
and is published next to the dashboard.
