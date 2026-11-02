LUnit tests now run in Linux workers as well as Windows, with per-platform reports
and dashboard Run controls on GitHub and a native GitLab job. Linux runs retain
framework autodetection, respect explicitly disabled tools, and fail visibly for
missing tooling, runner errors, empty results or all-skipped tests. Report generation
failures cannot publish partial output or produce a passing status. Other frameworks
are identified as unsupported on Linux. The bundled LUnit CLI is upgraded to
1.7.1.32; rebuild shared and client Linux workers before use. Linux VIPM installs
also preserve package revision suffixes so pinned dependencies resolve correctly.
