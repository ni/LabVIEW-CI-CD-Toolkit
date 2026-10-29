VI snapshots now use a single rich interactive code view across configuration,
generation, and browsing, while historical population respects the selected
revisions and highlights gaps in snapshot coverage. Linux VIDiff renders LabVIEW
XML comparisons as static HTML to avoid native HTML-report crashes, preserving
change descriptions and available before/after screenshots. Missing screenshots
are identified explicitly; native interactive sub-VI hotspots are not included.
The source Dependencies page now matches the bundled dashboard's recovery
guidance for workflow-dispatch 404 errors, including workflows that exist but
have not been registered by GitHub Actions. Shared dashboard assets are checked
for byte-for-byte parity to prevent the two views from drifting apart.
