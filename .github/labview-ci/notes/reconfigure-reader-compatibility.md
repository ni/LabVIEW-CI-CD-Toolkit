Reconfigure keeps changed manifests readable by existing Configure and Dependencies
pages and dashboard readers, retaining dependency-list indentation, path-first records,
double-quoted numeric strings and inline OS lists without line-wrapping scalar values.
Explicit none selections remain configured empty lists instead of restoring automatic
dependency discovery. The Dependencies page no longer
skips adjacent VIPC, Dragon or action sections. Updates to a YAML-aliased mapping no
longer change unrelated client fields that share that mapping. No-op requests still
preserve the original bytes. Existing values in a previously normalized manifest
are not lost; after updating tooling, an intentional change through the GitHub
Reconfigure workflow regenerates compatible formatting.
