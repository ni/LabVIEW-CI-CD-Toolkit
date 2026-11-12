Linux workers now use the VIPM 26.3.2 package-format compatibility update and apply monitored Dragon VIPM declarations alongside VIPCs. Compact VIPC exports are parsed correctly. Final verification includes toolkit packages as well as project pins, so incomplete installations cannot be published as successful.

Dependency reporting recognizes verified Linux VIPM Dragon packages, respects the selected Windows worker, and retains Linux native-package inventory. Native validation includes the example Dragon file and Windows Antidoc dependencies as well as the example VIPC and toolkit baseline.

Windows also requires the complete effective tooling baseline in its final inventory. The native VIPM validation feeds actual installed inventories through the dashboard and dependency-page functions and rejects any remaining package, VIPC, Dragon, or platform warning. A fresh scratch-client installation exercises the distributed staging and LVPM planning helpers.

A requested Linux dependency rebuild also verifies baseline tooling when the client has no project dependencies; it no longer substitutes an uninventoried registry copy for that rebuild. Initial worker seeding remains a separate operation.

Windows builds forward the client repository URL into the VIPM installation layer instead of silently falling back to the toolkit repository context.

Windows native-tool coverage now reads installed NI packages instead of available feed packages. VIPM presence is established by its verified CLI; Dragon declarations no longer incorrectly require a separate Dragon application. Rebuild older workers to refresh native-package evidence.
