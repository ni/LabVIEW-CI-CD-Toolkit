#!/usr/bin/env bash
set -euo pipefail

VIPC_DIR="${VIPC_DIR:-/opt/lvci/vipm}"
LABVIEW_VERSION="${LABVIEW_VERSION:-2026}"
export VIPM_NONINTERACTIVE="${VIPM_NONINTERACTIVE:-1}"
export VIPM_ASSUME_YES="${VIPM_ASSUME_YES:-1}"
export NO_COLOR="${NO_COLOR:-1}"
export VIPM_DEBUG="${VIPM_DEBUG:-1}"
export VIPM_TIMEOUT="${VIPM_TIMEOUT:-900}"
export VIPM_DESKTOP_LIVELINESS_TIMEOUT="${VIPM_DESKTOP_LIVELINESS_TIMEOUT:-900}"
export CI="${CI:-true}"
VIPM_COMMAND_TIMEOUT="${VIPM_COMMAND_TIMEOUT:-1200}"
INVENTORY_DIR="${INVENTORY_DIR:-/opt/lvci/vipm}"
if ! [[ "$LABVIEW_VERSION" =~ ^20[0-9]{2}$ && "$VIPM_COMMAND_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
  echo "Invalid LabVIEW year or VIPM command timeout." >&2
  exit 1
fi

run_vipm() {
  timeout --kill-after=30s "${VIPM_COMMAND_TIMEOUT}s" "$VIPM_BIN" "$@"
}

# The worker runtime needs this default, but VIPM's desktop engine cannot finish
# its startup handshake while it inherits it during the image build.
if [ -n "${LV_RTE_HEADLESS:-}" ]; then
  echo "Clearing LV_RTE_HEADLESS for the VIPM install."
  unset LV_RTE_HEADLESS
fi

setup_display() {
  export DISPLAY="${DISPLAY:-:99}"
  if command -v Xvfb >/dev/null 2>&1 && ! pgrep -x Xvfb >/dev/null 2>&1; then
    Xvfb "$DISPLAY" -screen 0 1280x720x24 -ac +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 &
  fi
  mkdir -p /tmp/natinst
  echo "1" > /tmp/natinst/LVContainer.txt
}

find_labview() {
  local candidate="/usr/local/natinst/LabVIEW-${LABVIEW_VERSION}-64/labview"
  if [ -x "$candidate" ]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  return 1
}

# Wovalab's donation dialog checks this opt-out file in Dflt Data Dir.vi.
# Keep package hooks enabled; only suppress its optional interactive prompt.
prepare_unattended_hooks() {
  local data_dir="${1:-$HOME/natinst/LabVIEW Data}"
  mkdir -p "$data_dir"
  touch "$data_dir/wovalab-post-install-dialog"
  echo "Disabled the optional Wovalab post-install donation prompt: $data_dir"
}

start_labview() {
  local labview_bin
  labview_bin="$(find_labview || true)"
  if [ -z "$labview_bin" ]; then
    echo "Requested 64-bit LabVIEW executable was not found." >&2
    return 1
  fi
  if ! pgrep -f "$labview_bin" >/dev/null 2>&1; then
    echo "Starting LabVIEW headless: $labview_bin"
    "$labview_bin" --headless >/tmp/labview-headless.log 2>&1 &
    LABVIEW_PID=$!
  else
    LABVIEW_PID="$(pgrep -f "$labview_bin" | head -n 1)"
  fi
}

find_vipm() {
  if command -v vipm >/dev/null 2>&1; then
    command -v vipm
    return 0
  fi
  if command -v vipm-cli >/dev/null 2>&1; then
    command -v vipm-cli
    return 0
  fi
  find /usr/local /usr /opt -type f \( -name vipm -o -name vipm-cli \) -perm -111 2>/dev/null | head -n 1
}


install_package_specs() {
  run_vipm --show-progress --verbose --labview-version "$LABVIEW_VERSION" --labview-bitness 64 install "$@"
}

print_install_diagnostics() {
  local result="$1"
  echo "VIPM install failed with exit $result; collecting process and display diagnostics." >&2
  ps -eo pid,ppid,stat,etime,comm | grep -Ei 'labview|vipm|xvfb' || true
  for log_file in /tmp/labview-headless.log /tmp/xvfb.log; do
    if [ -f "$log_file" ]; then
      echo "--- $log_file ---" >&2
      tail -n 200 "$log_file" >&2 || true
    fi
  done
}

VIPM_BIN="$(find_vipm || true)"
if [ -z "$VIPM_BIN" ]; then
  echo "VIPM CLI was not found after installing the native VIPM package." >&2
  exit 1
fi

echo "Using VIPM CLI: $VIPM_BIN"
command -v timeout >/dev/null
run_vipm --version
setup_display

vipc_files=()
while IFS= read -r vipc_file; do
  vipc_files+=("$vipc_file")
done < <(find "$VIPC_DIR" -maxdepth 1 -type f -iname '*.vipc' | sort)
if [ "${#vipc_files[@]}" -eq 0 ]; then
  echo "No VIPC files found in $VIPC_DIR; nothing to apply."
  mkdir -p "$INVENTORY_DIR"
  : > "$INVENTORY_DIR/vipm-list.txt"
  python3 "$(dirname "$0")/linux-worker.py" observe --year "$LABVIEW_VERSION" \
    --raw "$INVENTORY_DIR/vipm-list.txt" --failed --output "$INVENTORY_DIR/observed-inventory.json"
  exit 0
fi

if [ -n "${VIPM_SERIAL_NUMBER:-}" ]; then
  echo "Activating VIPM Pro license for ${VIPM_FULL_NAME:-VIPM user}..."
  activation_args=(--serial-number "$VIPM_SERIAL_NUMBER")
  if [ -n "${VIPM_FULL_NAME:-}" ]; then activation_args+=(--name "$VIPM_FULL_NAME"); fi
  if [ -n "${VIPM_EMAIL:-}" ]; then activation_args+=(--email "$VIPM_EMAIL"); fi
  run_vipm activate "${activation_args[@]}" || \
  run_vipm activate --serial-number "$VIPM_SERIAL_NUMBER" --full-name "${VIPM_FULL_NAME:-VIPM user}" --email "${VIPM_EMAIL:-}" || \
  run_vipm license activate "${activation_args[@]}" || \
  echo "VIPM activation command was not accepted; continuing with the installed license state."
fi

prepare_unattended_hooks
start_labview
echo "Refreshing VIPM package sources..."
if refresh_output=$(run_vipm refresh --force 2>&1); then
  printf '%s\n' "$refresh_output"
else
  refresh_result=$?
  printf '%s\n' "$refresh_output"
  if [ "$refresh_result" -eq 124 ] || [ "$refresh_result" -eq 137 ] || printf '%s\n' "$refresh_output" | grep -Fqi 'wait for VIPM startup'; then
    echo "VIPM's desktop engine did not finish starting; aborting before package installs repeat the same timeout." >&2
    exit "$refresh_result"
  fi
  echo "VIPM package-source refresh failed (exit $refresh_result); continuing with version-pinned installs." >&2
fi

for vipc_file in "${vipc_files[@]}"; do
  echo "Applying VIPC: $vipc_file"
  package_specs=()
  # Parse XML structurally; VIPCs do not require one element per line.
  specs=$(python3 "$(dirname "$0")/linux-worker.py" specs --vipc "$vipc_file")
  while IFS= read -r package_spec; do
    if [ -n "$package_spec" ]; then package_specs+=("$package_spec"); fi
  done <<< "$specs"
  if [ "${#package_specs[@]}" -eq 0 ]; then
    echo "No package names could be read from $vipc_file" >&2
    exit 1
  fi
  printf 'Installing packages: %s\n' "${package_specs[*]}"
  install_result=0
  install_package_specs "${package_specs[@]}" || install_result=$?
  if [ "$install_result" -eq 0 ]; then
    continue
  fi
  print_install_diagnostics "$install_result"
  echo "Failed to apply VIPC: $vipc_file" >&2
  exit "$install_result"
done

if ! kill -0 "$LABVIEW_PID" 2>/dev/null; then
  echo "Initial LabVIEW process exited; verifying installed packages for LabVIEW ${LABVIEW_VERSION} (64-bit)."
fi
mkdir -p "$INVENTORY_DIR"
inventory_args=(--vipc-dir "$VIPC_DIR" --required)
if ! run_vipm --labview-version "$LABVIEW_VERSION" --labview-bitness 64 list --installed > "$INVENTORY_DIR/vipm-list.txt" 2>&1; then
  inventory_args+=(--failed)
fi
inventory_result=0
python3 "$(dirname "$0")/linux-worker.py" observe --year "$LABVIEW_VERSION" \
  --raw "$INVENTORY_DIR/vipm-list.txt" --output "$INVENTORY_DIR/observed-inventory.json" \
  "${inventory_args[@]}" || inventory_result=$?
if [ "$inventory_result" -ne 0 ]; then
  echo "Strict inventory validation failed; captured CLI output follows:" >&2
  cat "$INVENTORY_DIR/vipm-list.txt" >&2
  df -h / /tmp /opt/lvci/vipm >&2
  free -m >&2
  exit "$inventory_result"
fi
cat "$INVENTORY_DIR/vipm-list.txt"
echo "All VIPC files applied successfully."
