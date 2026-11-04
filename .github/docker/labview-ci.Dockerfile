# escape=`
# =============================================================================
# LabVIEW CI final worker image
# =============================================================================
# Starts from the public LCWC base image, then applies only this repository's
# VIPC/required Dragon layer and worker-version labels. Repositories with neither
# declaration type can simply tag/push the base image as their own worker.
# =============================================================================
ARG LCWC_BASE_IMAGE=ghcr.io/elijah286/labview-ci-with-containers-labview-base:2026
FROM ${LCWC_BASE_IMAGE}

SHELL ["powershell", "-NoLogo", "-NoProfile", "-Command", "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue';"]

# Worker version: a short content hash of the build inputs (this Dockerfile +
# install-vipc.ps1 + normalized Dragon plan/sources + applied *.vipc), computed by the build workflow and
# passed in here. It is stamped into the image (env + label) so any CI job can
# read back exactly which worker it pulled and link to that worker's manifest on
# the dashboard. Defaults to 'dev' for local/ad-hoc builds.
ARG CI_WORKER_VERSION=dev
ARG LABVIEW_VERSION=2026
ARG LABVIEW_BITNESS=64

# VIPC automation assets. install-vipc.ps1 plus any *.vipc are staged here; the
# build workflow also copies repo-root *.vipc (e.g. "COTC Dependencies.vipc")
# into .github/labview/vipm/ before the build, so "a repo that features a .vipc"
# gets that configuration baked into the Windows worker automatically. With no
# VIPC or Dragon sources staged the VIPM hook below is a no-op.
COPY .github/labview/vipm/ C:/vipm/

RUN $vipcFiles = Get-ChildItem -Path 'C:\vipm' -Filter '*.vipc' -Recurse -ErrorAction SilentlyContinue; `
    $dragonFiles = Get-ChildItem -Path 'C:\vipm' -Filter '*.dragon' -Recurse -ErrorAction SilentlyContinue; `
    if ($vipcFiles -or $dragonFiles) { `
      if (Test-Path 'C:\vipm\install-vipc.ps1') { `
        Write-Host 'Required VIPC/Dragon declarations detected. Running C:\vipm\install-vipc.ps1 ...'; `
        powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File 'C:\vipm\install-vipc.ps1'; `
        if ($LASTEXITCODE -ne 0) { throw ('Required VIPM dependency bake failed with exit ' + $LASTEXITCODE) } `
      } else { `
        throw 'Dependency declarations were detected in C:\vipm but install-vipc.ps1 was not provided.' `
      } `
    } else { `
      Write-Host 'No VIPC or Dragon dependencies were provided. Skipping VIPM install hook.' `
    }

# Stamp the worker version so any consuming CI job can read it back from the
# pulled image (docker inspect / env) and link the dashboard to this worker's
# published manifest. ENV survives into `docker run`; LABEL is queryable without
# starting a container.
ENV CI_WORKER_VERSION=${CI_WORKER_VERSION}
LABEL com.cotc.ci-worker.version=${CI_WORKER_VERSION} `
      com.cotc.ci-worker.platform=windows
