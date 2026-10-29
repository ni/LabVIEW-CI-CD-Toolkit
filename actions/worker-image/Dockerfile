# escape=`
# =============================================================================
# LabVIEW CI final worker image
# =============================================================================
# Starts from the public LCWC base image, then applies only this repository's
# optional VIPC layer and worker-version labels. Repositories with no VIPC files
# can skip this build and simply tag/push the base image as their own worker.
# =============================================================================
ARG LCWC_BASE_IMAGE=ghcr.io/elijah286/labview-ci-with-containers-labview-base:2026
FROM ${LCWC_BASE_IMAGE}

SHELL ["powershell", "-NoLogo", "-NoProfile", "-Command", "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue';"]

# Worker version: a short content hash of the build inputs (this Dockerfile +
# install-vipc.ps1 + any applied *.vipc), computed by the build workflow and
# passed in here. It is stamped into the image (env + label) so any CI job can
# read back exactly which worker it pulled and link to that worker's manifest on
# the dashboard. Defaults to 'dev' for local/ad-hoc builds.
ARG CI_WORKER_VERSION=dev

# VIPC automation assets. install-vipc.ps1 plus any *.vipc are staged here; the
# build workflow also copies repo-root *.vipc (e.g. "COTC Dependencies.vipc")
# into .github/labview/vipm/ before the build, so "a repo that features a .vipc"
# gets that configuration baked into the Windows worker automatically. With no
# .vipc staged the VIPM hook below is a no-op.
COPY .github/labview/vipm/ C:/vipm/

# Optional VIPC support hook. If .vipc files exist, an installer script must be
# present so dependencies are handled explicitly.
RUN $vipcFiles = Get-ChildItem -Path 'C:\vipm' -Filter '*.vipc' -Recurse -ErrorAction SilentlyContinue; `
    if ($vipcFiles -and $vipcFiles.Count -gt 0) { `
      if (Test-Path 'C:\vipm\install-vipc.ps1') { `
        Write-Host 'VIPC files detected. Running C:\vipm\install-vipc.ps1 ...'; `
        powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File 'C:\vipm\install-vipc.ps1' `
      } else { `
        throw 'VIPC files were detected in C:\vipm but install-vipc.ps1 was not provided.' `
      } `
    } else { `
      Write-Host 'No VIPC dependencies were provided. Skipping VIPM install hook.' `
    }

# Optional Dragon support hook. Project *.dragon (JKI Dragon / NIPM) files are
# applied with the Dragon CLI that the base image already provides. This is
# BEST-EFFORT: a failure logs a warning but never fails the worker build, because
# the exact headless Dragon apply invocation can vary by environment. A repo can
# stage its own install-dragon.ps1 to override the default `dragon apply` per file.
RUN $dragonFiles = Get-ChildItem -Path 'C:\vipm' -Filter '*.dragon' -Recurse -ErrorAction SilentlyContinue; `
    if ($dragonFiles -and $dragonFiles.Count -gt 0) { `
      if (Test-Path 'C:\vipm\install-dragon.ps1') { `
        Write-Host 'Dragon files detected. Running C:\vipm\install-dragon.ps1 ...'; `
        powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File 'C:\vipm\install-dragon.ps1' `
      } else { `
        foreach ($d in $dragonFiles) { `
          Write-Host ('Applying Dragon dependency file (best-effort): ' + $d.FullName); `
          try { dragon apply $d.FullName } catch { Write-Host ('::warning::Dragon apply failed for ' + $d.Name + ': ' + $_) } `
        } `
      } `
    } else { `
      Write-Host 'No Dragon dependencies were provided. Skipping Dragon install hook.' `
    }

# Stamp the worker version so any consuming CI job can read it back from the
# pulled image (docker inspect / env) and link the dashboard to this worker's
# published manifest. ENV survives into `docker run`; LABEL is queryable without
# starting a container.
ENV CI_WORKER_VERSION=${CI_WORKER_VERSION}
LABEL com.cotc.ci-worker.version=${CI_WORKER_VERSION} `
      com.cotc.ci-worker.platform=windows
