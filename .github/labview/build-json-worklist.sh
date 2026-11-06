#!/usr/bin/env bash
#
# build-json-worklist.sh - enumerate which VIs still need a position-aware
# <blob>.json for the in-place VI Browser.
#
# PUBLIC scaffolding: contains NO LabVIEW/NI code. It only lists work by mirroring
# the exact content-addressing the Windows HTML pipeline uses (the git blob SHA
# from `git ls-tree`, stored under by-blob/<ab>/<blob>). The actual rendering is
# done by a separate PRIVATE image (see .github/workflows/vi-snapshots-json.yml).
#
# Usage:
#   build-json-worklist.sh <workspace> <sha> <existing-by-blob-dir> <out-tsv> [json-suffix]
#     <workspace>            repo worktree root
#     <sha>                  commit to enumerate (e.g. the pushed SHA, or HEAD)
#     <existing-by-blob-dir> already-deployed by-blob store, to skip done blobs
#                            (may be missing/empty - then everything is queued)
#     <out-tsv>              worklist output: lines of "<blob>\t<relpath>"
#     [json-suffix]          output suffix to consider rendered (default .json;
#                            Windows uses .windows.json beside Linux .json)
#
# Mirrors Get-VimapForSha in build-snapshots.ps1: same `git ls-tree -r` source,
# same blob field, same path filter. Restricted to *.vi (toimages renders block
# diagrams; .ctl typedefs have none).
set -euo pipefail

ws=${1:?workspace}
sha=${2:?sha}
existing=${3:-}
out=${4:?out-tsv}
json_suffix=${5:-.json}

mkdir -p "$(dirname "$out")"
: > "$out"

total=0
queued=0
while IFS=$'\t' read -r meta path; do
  case "$path" in
    *.vi) ;;
    *) continue ;;
  esac
  case "$path" in
    .github/*|ci-out/*|build/*) continue ;;
  esac
  blob=$(awk '{print $3}' <<<"$meta")
  [ -n "$blob" ] || continue
  total=$((total + 1))
  if [ -n "$existing" ] && [ -f "$existing/${blob:0:2}/$blob$json_suffix" ]; then
    continue
  fi
  printf '%s\t%s\n' "$blob" "$path" >> "$out"
  queued=$((queued + 1))
done < <(git -C "$ws" -c core.quotePath=false ls-tree -r "$sha")

echo "json worklist: $queued of $total .vi(s) need JSON -> $out"
