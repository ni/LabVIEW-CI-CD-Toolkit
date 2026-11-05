Selecting all four committed VIPCs and the example Dragon file no longer fails
Linux dependency planning because of stale experimental Antidoc CLI metadata.
The experimental declaration now uses the authoritative Antidoc capability's
existing exact version, 3.4.0.126, with its authentic package spec and icon.
All selected sources and 35 package roots remain required; conflicting exact
pins still fail rather than choosing a newer version automatically.

Offline regression tests cover the combined declarations, archive integrity and
conflict rejection. A real package installation, Linux worker bake and final
runtime inventory remain separate validation requirements.
