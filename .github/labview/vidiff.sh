#!/usr/bin/env bash
# =============================================================================
# vidiff.sh — VIDiff comparison reports (Linux container)
# =============================================================================
# Usage:
#   CHANGED_FILES="path/to/a.vi\npath/to/b.vi" \
#   bash /workspace/.github/labview/vidiff.sh \
#       /workspace-base    # BaseDir
#       /workspace         # HeadDir
#       /report            # ReportDir
# =============================================================================
set -euo pipefail

BASE_DIR="${1:-/workspace-base}"
HEAD_DIR="${2:-/workspace}"
REPORT_DIR="${3:-/report}"
# Directory containing the PrintToSingleFileHtml operation. Defaults to the head
# checkout, but the backfill orchestrator passes a stable ops mount because old
# commits' worktrees predate the CI scripts.
OPS_DIR="${4:-${HEAD_DIR}/.github/labview}"

# LabVIEWCLI is on PATH in the NI Linux container
LABVIEWCLI="LabVIEWCLI"
# Discover labviewprofull dynamically (year varies by image tag)
LABVIEW_EXE=$(find /usr/local/natinst -name "labviewprofull" 2>/dev/null | head -1)
if [ -z "$LABVIEW_EXE" ]; then echo "ERROR: labviewprofull not found" >&2; exit 1; fi
echo "Using LabVIEW: $LABVIEW_EXE"
PRINT_TO_HTML_OP="$OPS_DIR"

mkdir -p "$REPORT_DIR"

# ── Magic-byte check for real LabVIEW files ──────────────────────────────────
# LabVIEW VIs have LVIN or LVCC at byte offset 8 (per NI's container examples)
is_labview_file() {
    local f="$1"
    [ -f "$f" ] || return 1
    local magic
    magic=$(dd if="$f" bs=1 skip=8 count=4 2>/dev/null)
    [[ "$magic" == "LVIN" || "$magic" == "LVCC" ]]
}

reset_labview_session() {
    local status=0
    pkill -KILL -x "${LABVIEW_EXE##*/}" || status=$?
    if [ "$status" -ne 0 ] && [ "$status" -ne 1 ]; then
        echo "  ERROR: Could not reset the container's LabVIEW session (pkill exit ${status})." >&2
        return 1
    fi
    sleep 1
}

run_labview_operation() {
    local report_path="$1"
    shift
    local attempt log_path status
    for attempt in 1 2; do
        log_path="${OUT_DIR}/cli-attempt-${attempt}.log"
        rm -f "$report_path" "${OUT_DIR}/index.html"
        if "$LABVIEWCLI" "$@" >"$log_path" 2>&1; then
            cat "$log_path"
            if [ -s "$report_path" ]; then
                return 0
            fi
            echo "  ERROR: LabVIEWCLI succeeded but produced no report for ${REL_PATH}." >&2
            return 1
        else
            status=$?
        fi
        cat "$log_path"
        if ! grep -Eq '^[[:space:]]*Error code[[:space:]]*:[[:space:]]*(66|-350000)[[:space:]]*$' "$log_path"; then
            return "$status"
        fi
        echo "  LabVIEW session failed for ${REL_PATH}; resetting the container's LabVIEW process."
        reset_labview_session || return 1
        if [ "$attempt" -eq 1 ]; then
            echo "  Retrying ${REL_PATH} once in a fresh LabVIEW session."
        fi
    done
    return "$status"
}

# ── Parse changed files ──────────────────────────────────────────────────────
IFS=$'\n' read -r -d '' -a FILES <<< "${CHANGED_FILES:-}" || true
VI_FILES=()
for f in "${FILES[@]}"; do
    f="${f#/}"   # strip leading slash
    [[ "$f" =~ \.(vi|ctl)$ ]] && VI_FILES+=("$f")
done

if [ "${#VI_FILES[@]}" -eq 0 ]; then
    echo "No .vi/.ctl files changed — nothing to diff."
    exit 0
fi

PROCESSED=0; ERRORS=0
PROCESSED_ENTRIES=()   # "TYPE|SAFE_NAME|REL_PATH" per successfully-processed file

for REL_PATH in "${VI_FILES[@]}"; do
    BASE_PATH="${BASE_DIR}/${REL_PATH}"
    HEAD_PATH="${HEAD_DIR}/${REL_PATH}"
    SAFE_NAME="${REL_PATH//[\/]/-}"
    SAFE_NAME="${SAFE_NAME//[^a-zA-Z0-9._-]/_}"
    OUT_DIR="${REPORT_DIR}/${SAFE_NAME}"
    mkdir -p "$OUT_DIR"

    BASE_EXISTS=false; HEAD_EXISTS=false
    is_labview_file "$BASE_PATH" && BASE_EXISTS=true
    is_labview_file "$HEAD_PATH" && HEAD_EXISTS=true

    echo "── ${REL_PATH} (base=${BASE_EXISTS} head=${HEAD_EXISTS})"

    TYPE=""
    if $BASE_EXISTS && $HEAD_EXISTS; then
        TYPE="modified"
        run_labview_operation "${OUT_DIR}/comparison.xml" \
            -OperationName    CreateComparisonReport \
            -LabVIEWPath      "$LABVIEW_EXE" \
            -VI1              "$BASE_PATH" \
            -VI2              "$HEAD_PATH" \
            -ReportType       XML \
            -ReportPath       "${OUT_DIR}/comparison.xml" \
            -LogToConsole     TRUE \
            -Headless || { echo "  ERROR: CreateComparisonReport failed"; ERRORS=$((ERRORS+1)); continue; }
        python3 "${OPS_DIR}/build-vidiff-report.py" "${OUT_DIR}/comparison.xml" "${OUT_DIR}/index.html" || {
            echo "  ERROR: Could not render comparison XML"; ERRORS=$((ERRORS+1)); continue;
        }

    elif $HEAD_EXISTS; then
        TYPE="added"
        run_labview_operation "${OUT_DIR}/index.html" \
            -OperationName                PrintToSingleFileHtml \
            -AdditionalOperationDirectory "$PRINT_TO_HTML_OP" \
            -LabVIEWPath                  "$LABVIEW_EXE" \
            -VI                           "$HEAD_PATH" \
            -OutputPath                   "${OUT_DIR}/index.html" \
            -o -c \
            -LogToConsole                 TRUE \
            -Headless || { echo "  ERROR: PrintToSingleFileHtml (added) failed"; ERRORS=$((ERRORS+1)); continue; }

    elif $BASE_EXISTS; then
        TYPE="deleted"
        run_labview_operation "${OUT_DIR}/index.html" \
            -OperationName                PrintToSingleFileHtml \
            -AdditionalOperationDirectory "$PRINT_TO_HTML_OP" \
            -LabVIEWPath                  "$LABVIEW_EXE" \
            -VI                           "$BASE_PATH" \
            -OutputPath                   "${OUT_DIR}/index.html" \
            -o -c \
            -LogToConsole                 TRUE \
            -Headless || { echo "  ERROR: PrintToSingleFileHtml (deleted) failed"; ERRORS=$((ERRORS+1)); continue; }
    else
        echo "  Skipping — not a valid LabVIEW binary"
        continue
    fi

    PROCESSED=$((PROCESSED+1))
    PROCESSED_ENTRIES+=("${TYPE}|${SAFE_NAME}|${REL_PATH}")
done

echo ""
echo "=== VIDiff complete: ${PROCESSED} processed, ${ERRORS} errors ==="

# ── Machine-readable manifest (consumed by the VI Browser to flag changed VIs) ─
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
{
  echo '{'
  echo '  "platform": "linux",'
  echo '  "files": ['
  n=$PROCESSED; i=0
  for entry in ${PROCESSED_ENTRIES[@]+"${PROCESSED_ENTRIES[@]}"}; do
    i=$((i+1))
    e_type="${entry%%|*}"; e_rest="${entry#*|}"
    e_safe="${e_rest%%|*}"; e_rel="${e_rest#*|}"
    [ "$i" -lt "$n" ] && sep="," || sep=""
    printf '    {"file": "%s", "type": "%s", "report": "%s/index.html"}%s\n' \
      "$(json_escape "$e_rel")" "$e_type" "$(json_escape "$e_safe")" "$sep"
  done
  echo '  ]'
  echo '}'
} > "${REPORT_DIR}/changes.json"

# ── Human-facing index page (system light/dark theme + change-type labels) ────
{
cat << 'HTML'
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VIDiff — LabVIEW-CI-with-Containers</title>
<style>
  :root{--bg:#0d1117;--surface:#161b22;--border:#30363d;--fg:#e6edf3;--fg-muted:#8b949e;--link:#58a6ff;--row:#21262d;--hover:#1c2128}
  @media(prefers-color-scheme:light){:root{--bg:#fff;--surface:#f6f8fa;--border:#d0d7de;--fg:#1f2328;--fg-muted:#57606a;--link:#0969da;--row:#eaeef2;--hover:#f3f4f6}}
  *{box-sizing:border-box}
  body{margin:0;padding:24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--fg)}
  h1{font-size:1.3em;margin:0 0 4px}
  .sub{color:var(--fg-muted);font-size:.85em;margin-bottom:18px}
  table{border-collapse:collapse;width:100%;background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden}
  th{text-align:left;padding:9px 12px;border-bottom:1px solid var(--border);color:var(--fg-muted);font-size:.74em;text-transform:uppercase;letter-spacing:.04em}
  td{padding:9px 12px;border-bottom:1px solid var(--row);font-size:.9em;vertical-align:middle}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:var(--hover)}
  a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
  .badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:.7em;font-weight:600;color:#fff;text-transform:uppercase;letter-spacing:.03em}
  .modified{background:#9a6700}.added{background:#1a7f37}.deleted{background:#cf222e}
  .file{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.84em;word-break:break-all}
  .note{color:var(--fg-muted);font-size:.8em;margin-top:14px}
</style>
</head>
<body>
<h1>VIDiff — LabVIEW-CI-with-Containers</h1>
HTML
echo "<div class=\"sub\">${PROCESSED} file(s) compared &nbsp;|&nbsp; ${ERRORS} error(s)</div>"
if [ "$PROCESSED" -eq 0 ]; then
  echo "<p class=\"note\">No comparable VI changes in this revision.</p>"
else
  echo "<table><thead><tr><th>Change</th><th>VI</th><th>Report</th></tr></thead><tbody>"
  for entry in ${PROCESSED_ENTRIES[@]+"${PROCESSED_ENTRIES[@]}"}; do
    e_type="${entry%%|*}"; e_rest="${entry#*|}"; e_safe="${e_rest%%|*}"; e_rel="${e_rest#*|}"
    if [ "$e_type" = "modified" ]; then linktext="View diff report"; else linktext="View snapshot"; fi
    esc_rel="$(printf '%s' "$e_rel" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')"
    echo "<tr><td><span class=\"badge ${e_type}\">${e_type}</span></td><td class=\"file\">${esc_rel}</td><td><a href=\"${e_safe}/index.html\">${linktext} &rarr;</a></td></tr>"
  done
  echo "</tbody></table>"
  echo "<p class=\"note\"><strong>modified</strong> VIs show a true side-by-side diff. <strong>added</strong> / <strong>deleted</strong> VIs have no counterpart to compare, so a single-version snapshot is shown.</p>"
fi
echo "</body></html>"
} > "${REPORT_DIR}/index.html"

[ "$ERRORS" -gt 0 ] && exit 1
exit 0
