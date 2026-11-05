Windows workers now install supported exact VIPM dependencies declared in
monitored Dragon files through the verified VIPM installer instead of silently
continuing when an assumed Dragon CLI is unavailable. Trusted host-side TOML
parsing binds normalized requests to their source files, including Dragon-only
projects. Missing or unsupported declarations, conflicting pins, failed installs,
and missing or wrong-version packages in the final requested LabVIEW inventory
fail the worker build. Later optional tooling cannot silently replace a required
Dragon version. Nonempty NIPM/feed declarations and legacy custom Dragon hooks
are explicitly rejected rather than reported as installed; Windows runtime
qualification still requires a real bake and final inventory.

GitHub monitor selections now use shared safe YAML semantics in both the push
gate and staging, including quoted Windows values, flow/block platform lists,
quoted paths and comments; invalid selection types fail explicitly. Composite
worker-image and GitLab Windows builds stage the same validated target plan and
use the shared installer once, including Dragon-only projects, without a
separate Dragon CLI. Required build/staging errors stop publication.

Compatibility break / major-release candidate from 4.x: independent Dragon CLI,
custom install-dragon.ps1 hooks, and unsupported NIPM/feed/project settings are
not preserved. Migrate to supported exact VIPM declarations before updating.
Do not classify this retirement as a backward-compatible patch unless the
previous compatibility is restored. Publication and real runtime qualification
remain pending.
