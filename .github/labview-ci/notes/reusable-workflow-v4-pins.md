The reusable workflow no longer reaches back into the v1 tooling line. It had
kept pinning its config reader to `@v1`, and repos whose install manifest omits
`source.ref` were falling back to `v1` for every capability — so a client on
`@v4` could run v4 workflow logic over v1-era actions, most visibly the older
VI Snapshots renderer without the interactive VI browser. Both now default to
`v4`, matching the alias clients pin.
