Protected-branch releases and promotions now recover when an older prepared
catalog branch already exists for the same version. A non-fast-forward rejection
uses a separate branch keyed to the new prepared commit, preserving the original
branch and its review history without a force-push. Other push failures retain
their Git diagnostics and still stop publication. Regression checks now run in
the Catalog Source Sync pull-request gate. Review and merge only the current
catalog proposal; close older proposals once their release notes are accounted for.