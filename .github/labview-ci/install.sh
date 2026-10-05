#!/usr/bin/env bash
#
# install.sh - bootstrap the LabVIEW CI installer (curl | bash entry point).
#
# Fetches the tooling (unless run from a checkout) and hands off to install.py,
# which does the actual catalog-driven copy. This wrapper only locates Python,
# acquires the source, and forwards your flags.
#
# Usage (from the root of the repo you want to add CI to):
#
#   curl -fsSL https://raw.githubusercontent.com/ni/LabVIEW-CI-CD-Toolkit/main/.github/labview-ci/install.sh \
#     | bash -s -- --activities masscompile,vi-analyzer,vidiff,dashboard \
#                  --os windows,linux --labview-version 2026
#
# All flags after `--` are passed through to install.py (run with --help to see
# them). Bootstrap-only flags handled here:
#   --source-host github|gitlab  distribution to fetch from (default github)
#   --source-repo OWNER/NAME     tooling repo to fetch from (default below)
#   --source-ref  REF            branch/tag/sha of the tooling repo (default main)
#   --source-gitlab-url URL      GitLab host for --source-host gitlab (default https://gitlab.com)
#   --source      DIR            use a local tooling checkout instead of fetching
#
set -euo pipefail

SOURCE_HOST="github"
SOURCE_REPO="ni/LabVIEW-CI-CD-Toolkit"
SOURCE_REF="main"
SOURCE_GITLAB_URL="https://gitlab.com"
SRC_DIR=""
EXPLICIT_REPO=0
EXPLICIT_REF=0
EXPLICIT_SOURCE=0
IS_UPDATE=0
PASS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --source-host)       SOURCE_HOST="$2"; EXPLICIT_SOURCE=1; shift 2 ;;
    --source-repo)       SOURCE_REPO="$2"; EXPLICIT_REPO=1; EXPLICIT_SOURCE=1; shift 2 ;;
    --source-ref)        SOURCE_REF="$2"; EXPLICIT_REF=1; EXPLICIT_SOURCE=1; shift 2 ;;
    --source-gitlab-url) SOURCE_GITLAB_URL="$2"; EXPLICIT_SOURCE=1; shift 2 ;;
    --source)            SRC_DIR="$2"; EXPLICIT_SOURCE=1; shift 2 ;;
    --update)            IS_UPDATE=1; PASS+=("$1"); shift ;;
    *)                   PASS+=("$1"); shift ;;
  esac
done

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "ERROR: Python 3 is required but was not found on PATH." >&2
  exit 1
fi

TARGET="$PWD"

read_manifest_distribution() {
  local manifest="$1"
  "$PY" - "$manifest" <<'PY'
import re
import sys

values = {}
in_source = False
in_distribution = False
for line in open(sys.argv[1], encoding="utf-8"):
    if re.match(r"^source:\s*$", line):
        in_source = True
        in_distribution = False
        continue
    if line and not line[0].isspace():
        in_source = False
        in_distribution = False
        continue
    if in_source and re.match(r"^\s{2}distribution:\s*$", line):
        in_distribution = True
        continue
    if in_distribution:
        match = re.match(r"^\s{4}(host|repo|ref|url):\s*(\S.*?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2).strip("'\"")
print("|".join(values.get(key, "") for key in ("host", "repo", "ref", "url")))
PY
}

urlencode() {
  "$PY" - "$1" <<'PY'
import sys
import urllib.parse

print(urllib.parse.quote(sys.argv[1], safe=""))
PY
}

source_pointer_url() {
  case "$SOURCE_HOST" in
    github)
      printf 'https://raw.githubusercontent.com/%s/%s/.github/labview-ci/source.json\n' "$SOURCE_REPO" "$SOURCE_REF"
      ;;
    gitlab)
      printf '%s/api/v4/projects/%s/repository/files/%s/raw?ref=%s\n' \
        "${SOURCE_GITLAB_URL%/}" "$(urlencode "$SOURCE_REPO")" \
        "$(urlencode '.github/labview-ci/source.json')" "$(urlencode "$SOURCE_REF")"
      ;;
  esac
}

parse_pointer_distribution() {
  "$PY" -c '
import json
import sys

host = sys.argv[1]
try:
    pointer = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
entry = (pointer.get("distributions") or {}).get(host)
if not isinstance(entry, dict) and host == "github":
    entry = pointer
if not isinstance(entry, dict):
    raise SystemExit(0)
print("|".join(str(entry.get(key) or "") for key in ("repo", "ref", "url")))
' "$SOURCE_HOST"
}

if [ "$IS_UPDATE" = 1 ] && [ "$EXPLICIT_SOURCE" = 0 ] && [ -z "$SRC_DIR" ]; then
  manifest_distribution="$(read_manifest_distribution "$TARGET/.github/labview-ci.yml" 2>/dev/null || true)"
  if [ -n "$manifest_distribution" ]; then
    IFS='|' read -r stored_host stored_repo stored_ref stored_url <<EOF
$manifest_distribution
EOF
    [ -z "$stored_host" ] || SOURCE_HOST="$stored_host"
    [ -z "$stored_repo" ] || SOURCE_REPO="$stored_repo"
    [ -z "$stored_ref" ] || SOURCE_REF="$stored_ref"
    [ -z "$stored_url" ] || SOURCE_GITLAB_URL="$stored_url"
  fi
fi

case "$SOURCE_HOST" in
  github|gitlab) ;;
  *) echo "ERROR: --source-host must be github or gitlab." >&2; exit 1 ;;
esac

if [ "$SOURCE_HOST" = gitlab ] && [ "$EXPLICIT_REPO" = 0 ] && [ "$SOURCE_REPO" = "ni/LabVIEW-CI-CD-Toolkit" ]; then
  SOURCE_REPO="elijah286/ci-for-labview"
fi

if [ -z "$SRC_DIR" ]; then
  if [ -f ".github/labview-ci/install.py" ] && [ "$IS_UPDATE" = 0 ]; then
    # Running from a checkout that already contains the tooling.
    SRC_DIR="$PWD"
  else
    # Relocation pointer: if the source repo names a different official home in
    # .github/labview-ci/source.json, follow it (unless --source-repo was given)
    # so installs land on the current repo. install.py records the FETCHED
    # catalog's source.repo, so the new client polls the new home from then on.
    if [ "$EXPLICIT_REPO" = 0 ]; then
      MOVED="$(curl -fsSL "$(source_pointer_url)" 2>/dev/null | parse_pointer_distribution 2>/dev/null || true)"
      if [ -n "$MOVED" ]; then
        IFS='|' read -r moved_repo moved_ref moved_url <<EOF
$MOVED
EOF
        if [ -n "$moved_repo" ] && [ "$(printf %s "$moved_repo" | tr "[:upper:]" "[:lower:]")" != "$(printf %s "$SOURCE_REPO" | tr "[:upper:]" "[:lower:]")" ]; then
          echo "LabVIEW CI tooling has moved to ${moved_repo}; installing from there ..."
          SOURCE_REPO="$moved_repo"
        fi
        if [ -n "$moved_ref" ] && [ "$EXPLICIT_REF" = 0 ]; then SOURCE_REF="$moved_ref"; fi
        if [ -n "$moved_url" ] && [ "$SOURCE_HOST" = gitlab ]; then SOURCE_GITLAB_URL="$moved_url"; fi
      fi
    fi
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    case "$SOURCE_HOST" in
      github)
        # Bare ref form so --source-ref accepts a branch, a release tag (e.g. v1.2.0),
        # or a commit SHA; codeload resolves all three.
        URL="https://codeload.github.com/${SOURCE_REPO}/tar.gz/${SOURCE_REF}"
        ;;
      gitlab)
        URL="${SOURCE_GITLAB_URL%/}/api/v4/projects/$(urlencode "$SOURCE_REPO")/repository/archive.tar.gz?sha=$(urlencode "$SOURCE_REF")"
        ;;
    esac
    echo "Fetching LabVIEW CI tooling from ${SOURCE_HOST}:${SOURCE_REPO}@${SOURCE_REF} ..."
    if ! curl -fsSL "$URL" | tar -xz -C "$TMP"; then
      echo "ERROR: failed to download tooling from $URL" >&2
      exit 1
    fi
    SRC_DIR="$TMP/$(ls "$TMP" | head -1)"
  fi
fi

if [ ! -f "$SRC_DIR/.github/labview-ci/install.py" ]; then
  echo "ERROR: tooling not found under $SRC_DIR (.github/labview-ci/install.py missing)." >&2
  exit 1
fi

if [ "$SOURCE_HOST" = gitlab ]; then
  SOURCE_DISTRIBUTION_URL="$SOURCE_GITLAB_URL"
else
  SOURCE_DISTRIBUTION_URL="https://github.com"
fi

exec "$PY" "$SRC_DIR/.github/labview-ci/install.py" \
  --source "$SRC_DIR" --target "$TARGET" \
  --source-distribution "$SOURCE_HOST" \
  --source-distribution-repo "$SOURCE_REPO" \
  --source-distribution-ref "$SOURCE_REF" \
  --source-distribution-url "$SOURCE_DISTRIBUTION_URL" \
  "${PASS[@]}"
