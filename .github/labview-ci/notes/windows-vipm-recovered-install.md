Windows worker dependency builds no longer fail solely because an earlier
transitive package-file attempt failed after the requested packages recovered
during local-file retries. The installer verifies every requested package and
version against the target LabVIEW installed inventory before accepting that
recovery; missing required packages or failed verification still block the build.
