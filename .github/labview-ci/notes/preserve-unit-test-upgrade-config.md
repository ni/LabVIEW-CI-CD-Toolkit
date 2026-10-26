Tooling upgrades now retain existing project configuration blocks, including
native Caraya commands, test locations, worker dependency selections, build
settings, and concurrency. Installer-owned version, branch, and OS fields still
refresh, and an explicit image-name override takes precedence. This fixes an
upgrade that could silently remove enabled test runners. Restore already-lost
configuration from the last known-good manifest before upgrading; the installer
cannot recover missing settings. Regression coverage runs when the installer
or unit-test runtime changes.
Configure Pipeline now also retains a supplied native runner command, including
quoted Windows paths, and upgrades preserve its legacy top-level unitTests block.