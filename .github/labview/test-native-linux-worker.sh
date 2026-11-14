#!/usr/bin/env bash
set -euo pipefail

mode=${1:-${NATIVE_LINUX_INSTALLER:-vipm}}
if [[ $# -gt 1 || ! "$mode" =~ ^(vipm|lvpm)$ ]]; then
    printf 'Usage: bash %s [vipm|lvpm]\n' "$0" >&2
    exit 2
fi
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
out="$root/ci-out/native-linux-$mode"
mkdir -p "$out"

run_validation() (
    set -euo pipefail
    temporary=''
    container=''
    cleanup() {
        result=$?
        trap - EXIT
        if [[ -n "$container" ]]; then
            docker rm -f "$container" >/dev/null 2>&1 || true
        fi
        if [[ -n "$temporary" ]]; then
            rm -rf -- "$temporary"
        fi
        printf 'Native Linux %s validation exit code: %s\n' "$mode" "$result"
        exit "$result"
    }
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    cd "$root"
    rm -f "$out/passed.json"
    printf 'Native Linux %s validation started: %s\n' "$mode" "$(date -u +%FT%TZ)"
    [[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || {
        printf 'Requires a native Linux x86_64 host; emulation is not qualification.\n' >&2
        exit 1
    }
    source /etc/os-release
    [[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || {
        printf 'Requires Ubuntu 24.04.\n' >&2
        exit 1
    }
    for executable in docker python3 git tar df sha256sum; do
        command -v "$executable" >/dev/null
    done
    docker version
    docker buildx version
    docker info --format '{{json .}}' > "$out/docker-info.json"
    engine_os=$(docker info --format '{{.OSType}}')
    engine_arch=$(docker info --format '{{.Architecture}}')
    [[ "$engine_os" == linux && "$engine_arch" =~ ^(x86_64|amd64)$ ]] || {
        printf 'Docker engine must be linux/x86_64, got %s/%s.\n' "$engine_os" "$engine_arch" >&2
        exit 1
    }
    docker_root=$(docker info --format '{{.DockerRootDir}}')
    [[ -d "$docker_root" ]] || { printf 'Docker engine storage is not local.\n' >&2; exit 1; }
    df -Pk "$root" "$docker_root" | tee "$out/host-disk.txt"
    for location in "$root" "$docker_root"; do
        available=$(df -Pk "$location" | awk 'END {print $4}')
        [[ "$available" =~ ^[0-9]+$ && "$available" -ge 20971520 ]] || {
            printf 'At least 20 GiB free required at %s.\n' "$location" >&2
            exit 1
        }
    done
    revision=$(git rev-parse HEAD)
    git status --short > "$out/source-status.txt"
    temporary=$(mktemp -d)
    context="$temporary/context"
    mkdir -p "$context"
    tar -cf - .github example | tar -xf - -C "$context"
    python3 - "$context" "$out" "$revision" "$mode" <<'PY'
import hashlib
import json
from pathlib import Path
import re
import sys
import yaml

root, output = map(Path, sys.argv[1:3])
revision, installer = sys.argv[3:5]
catalog = json.loads((root / '.github/labview-ci/catalog.json').read_text())['containers']
release = next(entry for entry in catalog['supported'] if entry['id'] == catalog['default'])
base = release['linuxTag']
year = str(release['labviewYear'])
if (release['status'] != 'qualified' or not re.fullmatch(r'20\d{2}', year)
        or not re.fullmatch(r'nationalinstruments/labview:[a-z0-9.-]+-linux', base)
        or ':latest' in base):
    raise SystemExit('A qualified catalog Linux release is required')
config_path = root / '.github/labview-ci.yml'
original_config = hashlib.sha256(config_path.read_bytes()).hexdigest()
selected_vipcs = ['.github/labview/antidoc/antidoc.vipc', '.github/labview/experimental.vipc',
                  'example/Dependencies.vipc']
fixture = {'schemaVersion': 1, 'config': {'labviewVersion': year, 'os': ['linux'],
           'container': {'vipc': [{'path': path, 'monitor': True,
                                   'monitorOn': ['linux']} for path in selected_vipcs], 'dragon': [{'path': 'example/Example.dragon',
           'monitor': True, 'monitorOn': ['linux']}]}}}
config_path.write_text(yaml.safe_dump(fixture, sort_keys=False))
(output / 'fixture-config.yml').write_bytes(config_path.read_bytes())
paths = sorted(path for path in root.rglob('*') if path.is_file())
hashes = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
          for path in paths}
evidence = {'source_revision': revision, 'installer': installer, 'base_image': base,
            'labview_version': year, 'original_config_sha256': original_config,
            'fixture_config_sha256': hashes['.github/labview-ci.yml'],
            'context_files_sha256': hashes,
            'context_sha256': hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()}
(output / 'source-evidence.json').write_text(json.dumps(evidence, indent=2) + '\n')
(output / 'base-image.txt').write_text(base + '\n')
(output / 'labview-year.txt').write_text(year + '\n')
PY
    base=$(<"$out/base-image.txt")
    year=$(<"$out/labview-year.txt")
    docker buildx imagetools inspect "$base" > "$out/base-inspect.txt"
    base_digest=$(docker buildx imagetools inspect "$base" --format '{{.Manifest.Digest}}')
    [[ "$base_digest" =~ ^sha256:[0-9a-f]{64}$ ]]
    printf '%s@%s\n' "$base" "$base_digest" > "$out/base-image-pinned.txt"
    image="lvci-native-linux-$mode:$(date -u +%Y%m%d%H%M%S)-${revision:0:12}-$$"
    printf '%s\n' "$image" > "$out/local-image.txt"
    helper="$context/.github/labview/vipm/linux-worker.py"
    build_args=(--platform linux/amd64 --progress plain --load
        --metadata-file "$out/build-metadata.json" --tag "$image"
        --build-arg "LABVIEW_VERSION=$year" --build-arg "CI_WORKER_VERSION=native-$revision")
    if [[ "$mode" == vipm ]]; then
        python3 "$helper" stage --root "$context" \
            --destination "$context/.github/labview/vipm" --dependency-sha "$revision" \
            --output "$out/dependency-inputs.json"
        cmp "$root/.github/labview/vipm/ci-tooling.vipc" "$context/.github/labview/vipm/ci-tooling.vipc"
        build_args+=(--file "$context/.github/docker/labview-ci-linux.Dockerfile"
            --build-arg "LV_CONTAINER_TAG=${base#nationalinstruments/labview:}@$base_digest")
    else
        python3 "$helper" plan --root "$context" --output "$out/safe-plan.json"
        python3 "$context/.github/labview/lvpm/adapter.py" plan --repo "$context" \
            --labview-version "$year" --out "$context/ci-out/lvpm-plan"
        python3 - "$out/safe-plan.json" "$context/ci-out/lvpm-plan/requirements.json" <<'PY'
import json
from pathlib import Path
import sys
if json.loads(Path(sys.argv[1]).read_text()) != json.loads(Path(sys.argv[2]).read_text()):
    raise SystemExit('LVPM staged plan differs from safe source plan')
PY
        cp "$context/ci-out/lvpm-plan/requirements.json" "$out/requirements.json"
        build_args+=(--file "$context/.github/docker/labview-ci-lvpm.Dockerfile"
            --build-arg "LV_BASE_IMAGE=$base@$base_digest")
    fi
    python3 - "$context" "$out" <<'PY'
import hashlib
import json
from pathlib import Path
import sys
root, output = map(Path, sys.argv[1:])
hashes = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
          for path in sorted(root.rglob('*')) if path.is_file()}
(output / 'staged-context-sha256.json').write_text(json.dumps(hashes, indent=2) + '\n')
PY
    docker buildx build "${build_args[@]}" "$context"
    docker image inspect "$image" > "$out/image-inspect.json"
    [[ "$(docker image inspect "$image" --format '{{.Os}}/{{.Architecture}}')" == linux/amd64 ]]
    docker run --rm --network none --entrypoint /usr/bin/dpkg-query "$image" -W '-f=${Package} ${Version}\n' > "$out/dpkg-list.txt"
    docker run --rm --network none --entrypoint /usr/bin/dpkg-query "$image" \
        -W '-f=${db:Status-Status}\t${Package}\t${Version}\n' > "$out/dpkg-status.tsv"
    container=$(docker create --network none --entrypoint /bin/sh "$image" -c true)
    if [[ "$mode" == vipm ]]; then
        docker cp "$container:/opt/lvci/vipm/observed-inventory.json" "$out/observed-inventory.json"
        python3 "$helper" validate --year "$year" --required \
            --vipc-dir "$context/.github/labview/vipm" --output "$out/observed-inventory.json"
        docker run --rm --network none --entrypoint python3 "$image" \
            /opt/lvci/vipm/linux-worker.py validate --year "$year" --required \
            --vipc-dir /opt/lvci/vipc --output /opt/lvci/vipm/observed-inventory.json
        manifest_args=()
        for vipc in "$context/.github/labview/vipm/"*.vipc; do
            manifest_args+=(--vipc "$vipc")
        done
        python3 "$context/.github/labview/build-worker-manifest.py" \
            --platform linux --version "native-$revision" --labview-version "$year" \
            --labview-bitness 64 --base-image "$base" --base-digest "$base_digest" \
            --image-ref "$image" --git-sha "$revision" --dependency-sha "$revision" \
            --observed-inventory "$out/observed-inventory.json" --dpkg-list "$out/dpkg-list.txt" \
            --out-dir "$out/manifest" "${manifest_args[@]}"
    else
        docker cp "$container:/opt/lvci/lvpm/inventory.json" "$out/inventory.json"
        docker run --rm --interactive --network none --entrypoint python3 \
            --mount "type=bind,src=$context/.github/labview/lvpm,dst=/native-validation,readonly" \
            "$image" - <<'PY' | tee "$out/offline-verification.json"
import json
from pathlib import Path
import sys
sys.path.insert(0, '/native-validation')
import worker
sys.path.insert(0, '/opt/lvci/lvpm')
import adapter
inventory = json.loads(Path('/opt/lvci/lvpm/inventory.json').read_text())
plan = json.loads(Path('/opt/lvci/lvpm/requirements.json').read_text())
worker.verify_packages(inventory, plan)
plan['labview_target'] = inventory['labview_target']
actual = adapter.verify_inventory(plan, inventory['packages'], Path(
    f'/var/lib/lvpm/LabVIEW-{plan["labview_version"]}-64bit/installed'))
if actual != inventory['packages']:
    raise SystemExit('Offline installed package files differ from captured inventory')
print(json.dumps({'offline_verification': 'passed', 'labview_target': inventory['labview_target']}))
PY
    fi
    docker run --rm --network none --entrypoint /bin/sh "$image" -ec '
        test "$(uname -m)" = x86_64
        test -x /app/lvctl
        test -x /app/runner
        test -x /usr/local/bin/toimages-entrypoint.sh
        sha256sum /app/lvctl /app/runner /usr/local/bin/toimages-entrypoint.sh
    ' | tee "$out/render-engine-sha256.txt"
    python3 - "$out" "$mode" "$context" <<'PY'
import json
from pathlib import Path
import sys
output, mode = Path(sys.argv[1]), sys.argv[2]
image = json.loads((output / 'image-inspect.json').read_text())[0]
if image['Os'] != 'linux' or image['Architecture'] != 'amd64':
    raise SystemExit('Built image is not linux/amd64')
if mode == 'vipm':
    manifest = json.loads((output / 'manifest/manifest.json').read_text())
    if manifest['schema'] != 3 or not manifest['vipm_inventory']['target_verified']:
        raise SystemExit('Schema-3 observed manifest is required')
else:
    sys.path.insert(0, str(Path(sys.argv[3]) / '.github/labview/lvpm'))
    import worker
    worker.verify_image_platform(image, json.loads((output / 'inventory.json').read_text()))
evidence = json.loads((output / 'source-evidence.json').read_text())
import hashlib
evidence['staged_context_sha256'] = hashlib.sha256(
    (output / 'staged-context-sha256.json').read_bytes()).hexdigest()
evidence.update({'status': 'passed', 'image_id': image['Id'],
                 'base_image_pinned': (output / 'base-image-pinned.txt').read_text().strip(),
                 'scope': 'native dependency build and offline inventory/file verification',
                 'activity_checks': 'not_run'})
(output / 'passed.json').write_text(json.dumps(evidence, indent=2) + '\n')
PY
)

run_validation 2>&1 | tee "$out/validation.log"