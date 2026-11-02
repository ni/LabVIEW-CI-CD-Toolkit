Release proposals now describe the prepared features and fixes in their titles,
with the version as supporting context and an explicit release-impact section.
Reused bot proposals refresh their descriptions, and Reconfigure proposals name
the actual changed settings or Linux worker activation/reset. Known generic
process-only titles fail the PR gate, including after title edits. The same
content-first policy applies to human and automated PRs; missing meaningful release
notes stop generation rather than producing a placeholder headline.
