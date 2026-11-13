#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ruby-full x11-utils imagemagick
rm -rf /var/lib/apt/lists/*
gem install --no-document asciidoctor:2.0.26 asciidoctor-pdf:2.3.24 asciidoctor-kroki:0.10.2 asciimath:2.0.6
asciidoctor --version
asciidoctor-pdf --version
gem which asciidoctor-kroki
gem which asciimath
cat > /usr/local/bin/cmd <<'PYPROBE'
#!/usr/bin/python3
import json, os, pathlib, sys
with open('/evidence/cmd-invocation.jsonl', 'a') as out:
    out.write(json.dumps({'args': sys.argv, 'cwd': os.getcwd()}) + '\n')
args = sys.argv[1:]
if not args or args.pop(0).lower() != '/c' or not args:
    sys.exit('Unsupported AsciiDoc command wrapper invocation')
if args[0] not in ('asciidoctor', 'asciidoctor-pdf') and args not in (
        ['gem', 'which', 'asciidoctor-kroki'], ['gem', 'which', 'asciimath'], ['gem', 'outdated']):
    sys.exit('Unsupported AsciiDoc command: ' + repr(args))
if args == ['gem', 'outdated']:
    args += ['--local', '--clear-sources']
os.execvp(args[0], args)
PYPROBE
chmod +x /usr/local/bin/cmd
unset LV_RTE_HEADLESS
export DISPLAY=:99
Xvfb "$DISPLAY" -screen 0 1440x1000x24 -ac +extension GLX +render -noreset >/evidence/xvfb.log 2>&1 &
mkdir -p /tmp/natinst
printf '1\n' > /tmp/natinst/LVContainer.txt
cp /input/.github/labview/lvpm/adapter.py /tmp/diagnostic-adapter.py
python3 - <<'PY'
from pathlib import Path
p=Path('/tmp/diagnostic-adapter.py')
s=p.read_text().replace('"LV_RTE_HEADLESS": "1", ', '')
p.write_text(s)
PY
python3 /tmp/diagnostic-adapter.py plan --repo /input --out /tmp/diagnostic-plan
printf 'started\n' > /evidence/install-started
python3 -u /tmp/diagnostic-adapter.py install --plan /tmp/diagnostic-plan/requirements.json --out /opt/lvci/lvpm > /evidence/install.log 2>&1 &
installer=$!
for i in $(seq 1 100); do
  sleep 10
  date -u >> /evidence/windows.txt
  xwininfo -root -tree >> /evidence/windows.txt 2>&1 || true
  ps -eo pid,ppid,stat,args > /evidence/processes.txt
  if (( i % 3 == 0 )); then
    import -window root "/evidence/screen-$i.png" 2>/evidence/screenshot-error.txt || true
  fi
  if ! kill -0 "$installer" 2>/dev/null; then break; fi
done
set +e
wait "$installer"
result=$?
printf '%s\n' "$result" > /evidence/install-exit-code
find /root/natinst -maxdepth 5 -name '*.conf' -exec sh -c 'echo "$1"; cat "$1"' _ {} \; > /evidence/labview-preferences.txt
exit "$result"
