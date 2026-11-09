Windows and Linux dependency builds now distinguish requested packages from actual installed inventory for the configured LabVIEW target. Dependency coverage requires the exact pinned version; declarations, seed-image assumptions, and different installed versions no longer produce false satisfaction. Required project installs fail when target-bound inventory cannot verify their packages.

Worker inputs can come from an explicitly selected committed ancestor revision while tooling stays at reviewed HEAD. Safe staging records configuration and dependency hashes. Separately tagged Linux workers no longer change the default Pages pointer. Linux-only installations receive the inventory/staging helper through the normal tooling payload.

The Dependencies page preserves revision-aware comparisons and LVPM digest-pinned candidate verification. The tooling source now explains why client Save is disabled on both platforms; client monitoring changes still require reviewing and merging the generated configuration proposal.

Local regression fixtures do not qualify real LabVIEW/VIPM installations, Docker builds, registry publication, or runtime activities. Those require native CI evidence before production use.

A dedicated read-only validation workflow builds the real example VIPC on Windows x64 and Linux x64 using VIPM and LVPM. It retains build and installed-inventory evidence without publishing images or changing active workers. Its results qualify only the recorded fixture and validation scope.