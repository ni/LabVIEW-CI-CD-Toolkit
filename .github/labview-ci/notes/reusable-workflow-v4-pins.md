The reusable workflow now loads configuration from the canonical
ni/LabVIEW-CI-CD-Toolkit repository at v4 and checks out capability tooling from
that same repository. Missing or empty source.ref values, including a missing
manifest, default to the v4 major alias that follows stable instead of v1-era
tooling. Only source.ref selects the capability pin: quoted refs are supported,
unrelated config and distribution refs are ignored, and malformed or unsupported
source mappings produce a clear error. Explicit immutable pins fetch the exact
release, including newer published releases outside stable. The config reader
independently follows v4; publication alone does not promote stable or advance
v4 to the new release.
