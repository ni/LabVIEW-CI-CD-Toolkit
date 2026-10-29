#!/usr/bin/env bash
# =============================================================================
# LabVIEW CI - Debug Session (container side)
# =============================================================================
# Runs INSIDE the Linux worker container (started by debug-session.yml). Brings
# up a graphical desktop in a virtual framebuffer, launches the LabVIEW IDE UI,
# and serves it over noVNC on port 6080 (the host tunnels that out via Cloudflare).
# An on-screen terminal shows an operations menu so the user can open the project
# and run CI operations live from inside the session (run-debug-actions.sh).
#
# Everything is best-effort: this is an interactive debugging aid, not CI, so a
# missing tool logs a warning rather than failing hard. Pure ASCII.
# =============================================================================
set -u

WS=/workspace
export DISPLAY=:99
export HOME="${HOME:-/root}"
ACTIONS="${ACTIONS:-}"
MINUTES="${MINUTES:-45}"
VNC_PW="${VNC_PW:-changeme}"
OPEN_PROJECT="${OPEN_PROJECT:-}"

log() { echo "[lvci-debug] $*"; }

# --- 1. Install the desktop + VNC tooling (best-effort) ----------------------
# The worker image ships Xvfb (for headless snapshot renders) but not a window manager
# or VNC bridge, so add them on demand. Only debug sessions pay this cost.
if ! command -v x11vnc >/dev/null 2>&1 || ! command -v websockify >/dev/null 2>&1; then
  log "Installing desktop + VNC tools (fluxbox, x11vnc, novnc, websockify, xterm)..."
  export DEBIAN_FRONTEND=noninteractive
  SUDO=""; [ "$(id -u)" != "0" ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
  $SUDO apt-get update -y >/tmp/apt.log 2>&1 || log "apt-get update failed (see /tmp/apt.log)"
  $SUDO apt-get install -y --no-install-recommends \
    xvfb fluxbox x11vnc novnc websockify xterm openssl xauth x11-utils x11-xserver-utils \
    fonts-dejavu-core fonts-liberation fonts-noto-core xfonts-base xfonts-75dpi xfonts-100dpi fontconfig \
    >>/tmp/apt.log 2>&1 || log "apt-get install had errors (see /tmp/apt.log)"
  $SUDO fc-cache -f >>/tmp/apt.log 2>&1 || true
fi

# --- 2. Virtual display + window manager -------------------------------------
if ! xdpyinfo -display :99 >/dev/null 2>&1; then
  log "Starting Xvfb on :99"
  Xvfb :99 -screen 0 1680x1010x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
  sleep 2
fi
# Make the freshly installed X core fonts visible even if Xvfb was already up, so
# LabVIEW's UI fonts (its Helvetica/Courier/Times aliases) resolve and buttons and
# text render instead of appearing blank.
xset +fp /usr/share/fonts/X11/misc,/usr/share/fonts/X11/75dpi,/usr/share/fonts/X11/100dpi 2>/dev/null || true
xset fp rehash 2>/dev/null || true
# Focus-follows-mouse + focus new windows so the on-screen terminal actually
# receives the ENTER keystroke (fluxbox defaults to click-to-focus, which made
# pressing ENTER over VNC seem to do nothing).
mkdir -p "$HOME/.fluxbox"
cat > "$HOME/.fluxbox/init" <<'FBINIT'
session.screen0.focusModel: MouseFocus
session.screen0.focusNewWindows: true
session.screen0.autoRaise: true
session.screen0.workspaces: 1
FBINIT
fluxbox >/tmp/fluxbox.log 2>&1 &
sleep 1

# --- 3. VNC server + noVNC web bridge ----------------------------------------
NOVNC_WEB=""
for d in /usr/share/novnc /usr/share/webapps/novnc /usr/lib/novnc; do
  [ -f "$d/vnc.html" ] && NOVNC_WEB="$d" && break
done
[ -z "$NOVNC_WEB" ] && log "noVNC web root not found; the browser client may 404." && NOVNC_WEB=/usr/share/novnc

x11vnc -storepasswd "$VNC_PW" /tmp/.vncpw >/dev/null 2>&1
log "Starting x11vnc on :99 (port 5900)"
x11vnc -display :99 -rfbauth /tmp/.vncpw -forever -shared -noxdamage -rfbport 5900 -bg -o /tmp/x11vnc.log

log "Starting websockify/noVNC on 6080 (web root: $NOVNC_WEB)"
websockify --web="$NOVNC_WEB" 6080 localhost:5900 >/tmp/websockify.log 2>&1 &
sleep 1

# --- 4. Launch the LabVIEW IDE UI --------------------------------------------
# Always locate the LabVIEW project so the on-screen menu's "Open the project"
# option works. Only auto-open it at launch when OPEN_PROJECT is set (the
# dashboard's "Open the project" action); otherwise LabVIEW opens empty and the
# user drives everything from the operations menu.
PROJ="$(find "$WS" -maxdepth 4 -iname '*.lvproj' 2>/dev/null | sort | head -1)"
OPENPROJ=""
if [ "$OPEN_PROJECT" = "true" ] || [ "$OPEN_PROJECT" = "1" ]; then
  if [ -n "$PROJ" ]; then OPENPROJ="$PROJ"; log "Will open project: $PROJ"; else log "OPEN_PROJECT set but no .lvproj found under $WS"; fi
fi
LV="$(command -v labview 2>/dev/null || true)"
[ -z "$LV" ] && LV="$(ls -1 /usr/local/natinst/LabVIEW-*/labview 2>/dev/null | sort -V | tail -1 || true)"
if [ -n "$LV" ]; then
  log "Launching LabVIEW: $LV ${OPENPROJ:+with $OPENPROJ}"
  if [ -n "$OPENPROJ" ]; then ( cd "$WS" && "$LV" "$OPENPROJ" >/tmp/labview.log 2>&1 & ); else ( cd "$WS" && "$LV" >/tmp/labview.log 2>&1 & ); fi
else
  log "Could not find the labview binary; open it from the on-screen terminal."
fi

# --- 5. On-screen operations menu --------------------------------------------
# Instead of pre-selecting activities in the dashboard, the user picks what to
# run from an interactive menu INSIDE the session, once LabVIEW is logged in /
# activated. The first option opens the project; the rest run CI operations with
# the same entrypoints the Linux workflows use, streaming output to this window
# so each step can be watched live. It is a run-and-observe loop: run one, come
# back, run another. This keeps the whole handshake inside the session the user
# is already driving (no extra network channel needed).
#
# Config values (LV/PROJ/WS/ACTIONS/MINUTES) are baked in from the host shell;
# everything the menu reads at runtime is escaped (\$) so it stays literal.
cat > /tmp/lvci-prompt.sh <<PROMPT
#!/usr/bin/env bash
LV="${LV}"
PROJ="${PROJ}"
WS="${WS}"
ACTIONS="${ACTIONS}"
MINUTES="${MINUTES}"
RUNNER="\${WS}/.github/labview/debug/run-debug-actions.sh"

show_menu() {
  echo "=================================================================="
  echo " LabVIEW CI - Debug Session"
  echo "=================================================================="
  echo
  echo " Log into / activate LabVIEW in the window that opened, then pick an"
  echo " operation to run and watch it execute in the live UI:"
  echo
  echo "   1) Open the project in LabVIEW"
  echo "   2) Mass Compile"
  echo "   3) Build all binaries"
  echo "   4) VIDiff                       (run from the LabVIEW UI)"
  echo "   5) VI Analyzer                  (run from the LabVIEW UI)"
  echo "   6) VI Browser snapshots     (run from the LabVIEW UI)"
  echo "   7) Unit tests                   (run from the LabVIEW UI)"
  echo
  echo "   q) Quit this menu (keep the desktop until the session ends)"
  echo
  echo " (End the session anytime from the dashboard's 'End session' button,"
  echo "  or it ends automatically after \${MINUTES} minutes.)"
  echo
}

open_project() {
  if [ -z "\${PROJ}" ]; then echo "No .lvproj was found in the checkout."; return; fi
  if [ -z "\${LV}" ]; then echo "The labview binary was not found."; return; fi
  echo "Opening \${PROJ} ..."
  ( cd "\${WS}" && "\${LV}" "\${PROJ}" >/tmp/labview-open.log 2>&1 & )
  echo "Requested LabVIEW to open the project (watch the IDE window)."
}

# If activities were pre-selected in the dashboard, run them first, then drop
# into the menu so more operations can be chosen.
if [ -n "\${ACTIONS}" ]; then
  echo "Running the activities selected in the dashboard: \${ACTIONS}"
  bash "\${RUNNER}" \${ACTIONS}
  echo
  printf "Press ENTER to open the operations menu... "
  read -r _
fi

while true; do
  show_menu
  printf "Select an option: "
  read -r choice
  case "\${choice}" in
    1) open_project ;;
    2) bash "\${RUNNER}" masscompile ;;
    3) bash "\${RUNNER}" builds ;;
    4) bash "\${RUNNER}" vidiff ;;
    5) bash "\${RUNNER}" vi-analyzer ;;
    6) bash "\${RUNNER}" snapshots2 ;;
    7) bash "\${RUNNER}" unit-tests ;;
    q|Q) echo "Menu closed. The desktop stays up until the session ends."; break ;;
    "") ;;
    *) echo "Unknown option: '\${choice}'" ;;
  esac
  echo
  printf "Press ENTER to return to the menu... "
  read -r _
done

# Keep this window open until the session is torn down.
while true; do sleep 3600; done
PROMPT
chmod +x /tmp/lvci-prompt.sh
xterm -geometry 104x34+40+40 -T "LabVIEW CI Debug - operations menu" -e bash /tmp/lvci-prompt.sh &

log "Debug desktop is up. Holding for ${MINUTES} minutes."
# --- 6. Hold the session, then exit so the host tears down -------------------
trap 'log "Received stop signal; exiting."; exit 0' TERM INT
sleep "$(( MINUTES * 60 ))" &
wait $!
log "Session time elapsed; exiting."
