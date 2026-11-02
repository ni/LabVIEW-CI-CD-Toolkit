The Dependencies page now offers an experimental LVPM alternative without replacing
VIPM or changing Windows workers. Review the selected declarations, build a separate
Linux candidate, inspect package-file verification, and explicitly request activation
through a Reconfigure pull request. Existing per-action worker overrides remain in place.
Reconfigure changes only explicitly requested settings, preserving separate VIPC/Dragon
declarations, dependency-policy values, platforms, provenance and other client fields.
Activation or reset no longer regenerates unrelated configuration defaults.
Build controls require the Linux custom-image workflow to be installed; unavailable or
unknown workflow access blocks dispatch with guidance rather than attempting a fallback.

LVPM is consumed from pinned upstream source, not copied into the toolkit. The adapter
rejects conflicting pins, unsupported dependencies, missing transitive packages, and
incomplete installs. Candidates are digest-pinned, leave production tags untouched, and
are checked for dependency freshness before Linux execution. Resetting the Linux default
returns to the existing VIPM selection without changing Windows or per-action overrides.
Declared embedded VIP subpackages are included in the exact dependency closure. Original
archives remain available with their hashes; separately hashed installation copies
normalize legacy ZIP/spec filename encoding and select only applicable LabVIEW-version
file groups, recording excluded groups. A strict local-only dry run must verify the
complete closure before installation; discovery warnings never count as a verified install.
Exact root versions are fetched independently of transitive resolution and checked before
installation. The frozen local closure is then installed without further dependency
resolution, preventing upstream's transitive-first traversal from upgrading declared pins.
System-package paths remain as declared, so package-file evidence is not a claim that
Windows-shaped CLI integrations function on Linux.
The first integration supports Linux x86-64 only: installation checks the host and binds
explicit architecture evidence to the digest-pinned image. Missing or mismatched evidence
requires rebuilding and verifying a candidate.
Local freshness is bound to the inspected revision and exact selection. Changed inputs
remain pending even when package pins match; unavailable checks show unknown instead of
green satisfaction. Local freshness is not registry, provenance, or runtime verification.
Dragon declarations are inspected only with parser code shipped in the trusted dashboard
action repository, never Python code from the inspected client. Missing parsers or TOML
backends, missing monitored files, and malformed declarations now stay unknown/pending
instead of appearing installed. Unmonitored files do not create pending work, and existing
measured Windows package statuses are retained. Update tooling or repair declarations and
regenerate the dashboard to restore inspection; rebuilding an image alone does not fix it.
Dragon package status now honors each file's monitored platforms: Linux-only declarations
can be satisfied by exact VIPM name/version matches in the selected, locally current LVPM
inventory without requiring Windows evidence. Stale/unknown evidence and unsupported
native/NIPM dependencies remain unknown/pending. Mixed-platform declarations are satisfied
only when every applicable platform is satisfied; Windows-only inference is unchanged.
Linux satisfaction remains package-file evidence, not proof of working runtime activities.

This is experimental package-file verification, not proof that the project's CI activities
passed. LVPM cannot yet report a hook VI's internal error output, and this first integration
does not support private feeds, NIPM installation, or developer virtual environments.
Projects with conflicting tooling/project pins must reconcile those declarations before
building a candidate. Linux container and GitHub Actions validation is required before
relying on a candidate in production.
