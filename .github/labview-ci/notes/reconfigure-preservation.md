Reconfigure now changes only settings explicitly requested by the user instead of
regenerating the complete client manifest. Separate VIPC and Dragon declarations,
explicit false dependency policies, operating systems, provenance and unrecognized
client extensions remain intact. Blank or unchanged requests keep the original file
byte-for-byte, including comments. Actual updates can normalize YAML formatting.
Malformed manifests or incompatible mapping paths fail before the configuration is
written. Existing client workers and dependency-management choices do not change.
