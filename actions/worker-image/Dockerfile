# escape=`
# Composite worker layer. Uses the current source installer staged by worker.py.
# Mirrors the source worker's C:/vipm contract; Dragon is required here, never best-effort.
ARG LCWC_BASE_IMAGE
FROM ${LCWC_BASE_IMAGE}
SHELL ["powershell", "-NoLogo", "-NoProfile", "-Command", "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue';"]
ARG CI_WORKER_VERSION=dev
ARG LABVIEW_VERSION=2026
ARG VIPM_PUBLIC_REPO_URL=https://github.com/ni/LabVIEW-CI-CD-Toolkit.git
ARG LVCI_UNIT_TESTS_INSTALLED=true
ENV LABVIEW_VERSION=${LABVIEW_VERSION} `
    VIPM_PUBLIC_REPO_URL=${VIPM_PUBLIC_REPO_URL}
COPY .github/labview/vipm/ C:/vipm/
RUN if ($env:LVCI_UNIT_TESTS_INSTALLED -eq 'false') { `
      $env:VIPM_REQUIRED_PACKAGES = '-'; $env:VIPM_EARLY_PACKAGES = '-' `
    }; `
    $vipcs = @(Get-ChildItem -LiteralPath 'C:\vipm' -File | Where-Object { $_.Extension -eq '.vipc' }); `
    if ($vipcs.Count -gt 0) { `
      powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File C:\vipm\install-vipc.ps1; `
      if ($LASTEXITCODE -ne 0) { throw ('Required VIPM bake failed: ' + $LASTEXITCODE) } `
    }
RUN $dragons = @(Get-ChildItem -LiteralPath 'C:\vipm' -File | Where-Object { $_.Extension -eq '.dragon' }); `
    if ($dragons.Count -gt 0) { `
      $cli = Get-Command dragon -ErrorAction Stop; `
      foreach ($d in $dragons) { `
        & $cli.Source apply $d.FullName; `
        if ($LASTEXITCODE -ne 0) { throw ('Required Dragon bake failed: ' + $LASTEXITCODE) } `
      } `
    }
ENV CI_WORKER_VERSION=${CI_WORKER_VERSION}
LABEL com.cotc.ci-worker.version=${CI_WORKER_VERSION} `
      com.cotc.ci-worker.platform=windows
