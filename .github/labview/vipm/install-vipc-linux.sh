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
  find /usr/local/natinst /usr /opt -type f -name labview -perm -111 2>/dev/null | head -n 1
}

start_labview() {
  local labview_bin
  labview_bin="$(find_labview || true)"
  if [ -z "$labview_bin" ]; then
    echo "LabVIEW executable was not found; VIPM may fail to apply packages." >&2
    return 0
  fi
  if ! pgrep -f "$labview_bin" >/dev/null 2>&1; then
    echo "Starting LabVIEW headless: $labview_bin"
    "$labview_bin" --headless >/tmp/labview-headless.log 2>&1 &
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

vipc_package_specs() {
  local vipc_file="$1"
  unzip -p "$vipc_file" config.xml | awk '
    /<Package([[:space:]>])/ { in_package = 1; next }
    /<\/Package>/ { in_package = 0; next }
    in_package && /<Name>/ {
      sub(/.*<Name>/, "")
      sub(/<\/Name>.*/, "")
      gsub(/^[[:space:]]+|[[:space:]]+$/, "")
      if (length) print
    }
  '
}

package_install_spec() {
  local package_name="$1"
  if [[ "$package_name" =~ ^(.+)-([0-9]+(\.[0-9]+)+)(-[0-9]+)?$ ]]; then
    # Keep a package revision suffix (e.g. jki_rsc_toolkits_palette-1.1-1 ->
    # @1.1-1): VIPM only knows the full version and rejects the bare 1.1.
    printf '%s@%s%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[4]}"
  else
    printf '%s\n' "$package_name"
  fi
}

install_package_specs() {
  local result
  if "$VIPM_BIN" --show-progress --verbose --labview-version "$LABVIEW_VERSION" --labview-bitness 64 install "$@"; then
    return 0
  else
    result=$?
  fi
  if [ "$result" -eq 2 ]; then
    echo "VIPM rejected global LabVIEW flags; retrying against the active target."
    "$VIPM_BIN" --show-progress --verbose install "$@"
    return $?
  fi
  return "$result"
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
"$VIPM_BIN" --version || true
setup_display

vipc_files=()
while IFS= read -r vipc_file; do
  vipc_files+=("$vipc_file")
done < <(find "$VIPC_DIR" -maxdepth 1 -type f -iname '*.vipc' | sort)
if [ "${#vipc_files[@]}" -eq 0 ]; then
  echo "No VIPC files found in $VIPC_DIR; nothing to apply."
  exit 0
fi

if [ -n "${VIPM_SERIAL_NUMBER:-}" ]; then
  echo "Activating VIPM Pro license for ${VIPM_FULL_NAME:-VIPM user}..."
  activation_args=(--serial-number "$VIPM_SERIAL_NUMBER")
  if [ -n "${VIPM_FULL_NAME:-}" ]; then activation_args+=(--name "$VIPM_FULL_NAME"); fi
  if [ -n "${VIPM_EMAIL:-}" ]; then activation_args+=(--email "$VIPM_EMAIL"); fi
  "$VIPM_BIN" activate "${activation_args[@]}" || \
  "$VIPM_BIN" activate --serial-number "$VIPM_SERIAL_NUMBER" --full-name "${VIPM_FULL_NAME:-VIPM user}" --email "${VIPM_EMAIL:-}" || \
  "$VIPM_BIN" license activate "${activation_args[@]}" || \
  echo "VIPM activation command was not accepted; continuing with the installed license state."
fi

start_labview
echo "Refreshing VIPM package sources..."
if refresh_output=$("$VIPM_BIN" refresh --force 2>&1); then
  printf '%s\n' "$refresh_output"
else
  refresh_result=$?
  printf '%s\n' "$refresh_output"
  if printf '%s\n' "$refresh_output" | grep -Fqi 'wait for VIPM startup'; then
    echo "VIPM's desktop engine did not finish starting; aborting before package installs repeat the same timeout." >&2
    exit "$refresh_result"
  fi
  echo "VIPM package-source refresh failed (exit $refresh_result); continuing with version-pinned installs." >&2
fi

for vipc_file in "${vipc_files[@]}"; do
  echo "Applying VIPC: $vipc_file"
  package_specs=()
  while IFS= read -r package_name; do
    package_specs+=("$(package_install_spec "$package_name")")
  done < <(vipc_package_specs "$vipc_file")
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

"$VIPM_BIN" list --installed || true
echo "All VIPC files applied successfully."
