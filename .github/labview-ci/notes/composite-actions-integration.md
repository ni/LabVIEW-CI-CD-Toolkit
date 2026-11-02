Existing GitHub workflows can build or reuse a Windows LabVIEW worker and run one
unit-test framework through standalone composite actions, without vendoring CI
scripts or installing client configuration. Worker reuse hashes the resolved base
digest, complete dependency context and build settings, and fails on registry or
required dependency errors. Unit-test reports use the current runner and reject
missing results, runner errors and empty or all-skipped runs, with hosted contract
and Windows PowerShell regression coverage.
