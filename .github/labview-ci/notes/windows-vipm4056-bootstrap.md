Windows dependency builds now use the official VIPM 2026 Q3 f2/build 4056
installer, whose vendor release notes identify the legacy OpenG package-file
metadata fix. The base image and dependency hook verify the actual selected CLI
and Desktop signatures and minimum versions, upgrade older or missing pairs,
and fail closed on unsupported binaries, failed or reboot-requiring installs,
and timeouts. Custom installer URLs remain authoritative and cannot silently
bypass the supported floor or downgrade a newer installation. Bootstrap-only
base builds avoid package and LabVIEW side effects, and immutable base cache
keys include the shared bootstrap bytes. Real Windows bake validation of
required Antidoc/palette and the final target inventory remains necessary;
static payload inspection and mocked tests do not establish runtime repair.
