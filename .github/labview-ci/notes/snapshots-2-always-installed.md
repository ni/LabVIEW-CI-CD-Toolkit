VI Snapshots 2.0 is now installed in every repository and can no longer be turned
off. The VI Browser always offers a 2.0 view, so a repository that skipped the
capability had no `vi-snapshots-json.yml` / `vi-snapshots-json-windows.yml` to run
and the viewer's "Run the 2.0 snapshot" button failed with a misleading token
error. The omission also perpetuated itself: reinstalls and updates reuse the
activity list recorded in `.github/labview-ci.yml`, so a repository set up before
the capability existed never gained it. Installs and updates now add it back
automatically, the configurator shows it as "Always installed" rather than an
option, and the retired `build-toimages-image.yml` workflow — which outlived the
render engine it built and failed on every push — is deleted from repositories
that still carry it.
