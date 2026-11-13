# Release-note fragments

Drop one `.md` file here per pull request describing what changed, in the voice of
the release notes clients read on the What's New page.

```
.github/labview-ci/notes/sbom-vipm-version-detect.md
```

Name it after the change, not the PR number — a descriptive name is readable in a
diff and still can't collide with anyone else's.

On merge, the Release workflow folds every pending fragment into one catalog
history entry, publishes the version, and deletes the fragments it used. Nothing
here is permanent; the catalog is where notes end up.

## Why fragments instead of editing the catalog

`catalog.json` is the version of record — the release workflow reads it to cut the
tag and move the `v<major>` alias every client pins to. When contributors edited it
directly, two branches touching it conflicted constantly, and on 2026-07-30 a
stale copy silently rolled the version back from 4.12.4 to 4.11.10 and stopped
client releases for two weeks.

One file per PR means two PRs can never conflict, and nothing a contributor writes
can change the version of record.

## Writing a good fragment

Plain prose, no heading, no bullet — it becomes one entry in a list clients read.
Keep it to one to three short, user-focused sentences. Lead with the benefit; put
implementation details and validation logs in the PR, not the update notice.
The page shows at most three brief highlights with full notes collapsed.
Say what changed and what it means for them:

> VI Analyzer reports now embed each VI's rendered front panel and block diagram
> inline, so findings sit beside the actual code rather than a bare list of VI
> names.

Whitespace is collapsed, so wrap however you like. A trailing period is added if
you leave it off.

## Choosing the version bump

Add a label to the PR:

| Label | Effect | Use for |
|---|---|---|
| *(none)* | patch | Fixes, tweaks, docs — the default |
| `release:minor` | minor | A new capability or a user-visible feature |
| `release:major` | major | A breaking change (this moves `@v<major>`, so it's rare) |

## Skipping a release

Put `[skip release]` in the merge commit message. Use it sparingly — every change
being published is what makes retroactive promotion to beta and stable work.

If you forget a fragment entirely, the release still happens and falls back to the
pull request title. That's a worse note, not a failed merge.
