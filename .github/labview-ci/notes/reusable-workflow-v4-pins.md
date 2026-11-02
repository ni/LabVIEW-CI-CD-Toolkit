The reusable workflow now loads configuration from the canonical
ni/LabVIEW-CI-CD-Toolkit repository at v4 and checks out capability tooling from
that same repository. Missing or empty source.ref values, including a missing
manifest, default to the v4 stable major alias instead of v1-era tooling.
Explicit capability pins remain unchanged; select an immutable release tag
for reproducible tooling. The config reader independently follows v4, and
publication does not promote the stable alias.
