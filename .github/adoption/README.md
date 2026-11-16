# Repository adoption dashboard

The canonical toolkit publishes at
[Adoption Observatory](https://ni.github.io/LabVIEW-CI-CD-Toolkit/adoption/).
This is separate from the CI results dashboard; the configurator and CLI
installer do not install it by default and it does not send client telemetry
to the toolkit.

## Opt in while setting up a public GitHub repository

1. Set up the toolkit as usual. Confirm GitHub Pages publishes from the root of
   the repository's `gh-pages` branch. This add-on overlays only `adoption/`.
2. Copy [the caller](../../examples/adoption-metrics-caller.yml) to
   `.github/workflows/adoption-metrics.yml` and replace both
   `TOOLKIT_COMMIT_SHA` placeholders with the same full commit SHA of a toolkit
   revision containing this workflow.
3. Set the Actions repository variable `LABVIEW_ADOPTION_PUBLISH` to `true`
   (or use `gh variable set LABVIEW_ADOPTION_PUBLISH --body true`). The page and
   its backing data will be public.
4. To archive aggregate daily views and clones, add an Actions repository
   secret named `ADOPTION_TRAFFIC_TOKEN`. Use a fine-grained GitHub token
   restricted to the destination repository with **Administration: Read**
   access, or an equivalent GitHub App installation token supplied by your
   automation. Do not paste any token into the workflow or into an issue. Public
   history still refreshes when the secret is absent or traffic access fails.
5. Merge the caller, run **Publish adoption dashboard** manually for the first
   snapshot, then open `/adoption/` under the existing Pages URL. The caller
   refreshes daily. Removing the caller or opt-in variable stops the updates;
   removing previously published data from Pages and branch history is a separate
   operation.

GitHub Pages must already be active. GitLab and other hosting modes are outside
this add-on's scope.

## What the numbers mean

- Repository traffic comes from the
  [GitHub traffic API](https://docs.github.com/en/rest/metrics/traffic).
  The seed preserves what was still available on November 16, 2026:
  November 1–14 UTC. Future runs retain older days and refresh overlapping
  observations, leaving unobserved dates blank. Deduplicated rolling uniques
  are shown separately; daily uniques are not added across days.
- Stars and forks are reconstructed from current stargazers and listed forks.
  The timeline omits unstars, deleted forks, and other inaccessible records.
  Releases and merged PRs use their GitHub publication and merge dates.
- The source-only public client series starts with confirmed rows from
  historical `clients.json` commits on `gh-pages`, deduplicated by UTC day.
  New observations count indexed public repositories carrying the
  `labview-ci` topic whose public catalog references the canonical or former
  toolkit slug. Forks of the toolkit itself do not count. These are discovery
  snapshots; they include demo and scratch projects and can fluctuate with the
  index. They cannot reveal private installs or all active use.
- Asset downloads are cumulative counts on still-published uploaded files.
  Source archive and container downloads are not included. This repo had no
  uploaded release assets at backfill.

## Collector and recovery

The public snapshot is `gh-pages:adoption/data.json`. When launching the
canonical dashboard the workflow uses `seed.json` if no published snapshot
exists. If history is lost, recover the latest valid `adoption/data.json` from
the Pages branch's Git history and restore it before the next refresh. A failed
traffic or client scan leaves previous observations intact. The canonical workflow
fails after publication when either traffic endpoint cannot be refreshed, so the
outage is visible in Actions. The base GitHub API
queries and archive load must succeed for a snapshot to publish.

Run local contract checks with:

```sh
python3 -m unittest discover -s .github/adoption -p 'test_*.py'
node --check .github/adoption/site/dashboard.js
```
