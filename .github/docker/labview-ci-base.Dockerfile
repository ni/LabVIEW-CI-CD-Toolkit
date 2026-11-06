# escape=`
# =============================================================================
# LabVIEW CI Windows base image
# =============================================================================
# Source-owned LCWC base layer for expensive Windows LabVIEW CI tooling. Client
# repositories copy/tag this image into their own GHCR worker package when they
# have no repo-specific VIPC dependencies, or build only a thin VIPC layer from
# it when they do.
# =============================================================================
# The NI container release is ALWAYS pinned and passed explicitly by
# build-labview-image.yml from the catalog's containers[] entry (tag, NIPM feed,
# and VIPM installer move together as one qualified triple). Never float on
# latest-windows: NI repointed it from 2026 Q1 to 2026 Q3 on 2026-07-27 without
# notice, so a floating pin makes NI's schedule an unreviewed production change
# (and disguised the Aug 2026 bake outage's real cause - the baked
# LV_RTE_HEADLESS below - as a version mismatch for weeks). The default here
# only covers ad-hoc local builds.
ARG LV_CONTAINER_TAG=2026q1patch2-windows
FROM nationalinstruments/labview:${LV_CONTAINER_TAG}

SHELL ["powershell", "-NoLogo", "-NoProfile", "-Command", "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue';"]

ARG NIPM_FEED_NAME=LV2026
ARG NIPM_FEED_URL=https://download.ni.com/support/nipkg/products/ni-l/ni-labview-2026/26.1/released
ARG VIA_SUPPORT_PACKAGE=ni-viawin-labview-support

RUN if (-not (Get-Command nipkg -ErrorAction SilentlyContinue)) { throw 'nipkg was not found in the LabVIEW base image.' }; `
    nipkg feed-add --name=$env:NIPM_FEED_NAME $env:NIPM_FEED_URL; `
    nipkg update; `
    nipkg install --accept-eulas -y $env:VIA_SUPPORT_PACKAGE; `
    if (Test-Path 'C:\ProgramData\National Instruments\NI Package Manager\cache') { `
      Remove-Item -Path 'C:\ProgramData\National Instruments\NI Package Manager\cache\*' -Force -Recurse -ErrorAction SilentlyContinue `
    }

ARG UTF_FEED_NAME=LVUTF
ARG UTF_FEED_URL=https://download.ni.com/support/nipkg/products/ni-l/ni-labview-unit-test-framework-toolkit/25.1/released
ARG UTF_FEED_CRITICAL_NAME=LVUTFcritical
ARG UTF_FEED_CRITICAL_URL=https://download.ni.com/support/nipkg/products/ni-l/ni-labview-unit-test-framework-toolkit/25.1/released-critical
ARG UTF_SUPPORT_PACKAGE=ni-utf-labview-support
ARG UTF_PACKAGE=
RUN $ErrorActionPreference = 'Continue'; `
    Write-Host "Adding NI Unit Test Framework toolkit feed: $env:UTF_FEED_URL"; `
    nipkg feed-add --name=$env:UTF_FEED_NAME $env:UTF_FEED_URL; `
    if ($env:UTF_FEED_CRITICAL_URL) { `
      Write-Host "Adding NI Unit Test Framework toolkit feed (critical): $env:UTF_FEED_CRITICAL_URL"; `
      nipkg feed-add --name=$env:UTF_FEED_CRITICAL_NAME $env:UTF_FEED_CRITICAL_URL `
    }; `
    nipkg update; `
    $pkg = if ($env:UTF_PACKAGE) { $env:UTF_PACKAGE } else { $env:UTF_SUPPORT_PACKAGE }; `
    Write-Host "Installing NI Unit Test Framework package: $pkg"; `
    nipkg install --accept-eulas -y $pkg; `
    if ($LASTEXITCODE -ne 0) { `
      Write-Host "::warning::UTF package '$pkg' did not install (exit $LASTEXITCODE); searching the feed for an alternative."; `
      $alt = @(nipkg list-available 2>$null) | Where-Object { $_ -match '(?i)(utf-labview-support|unit.?test.?framework)' } | ForEach-Object { (([string]$_).Trim() -split '\s+')[0] } | Select-Object -First 1; `
      if ($alt) { Write-Host "Retrying with discovered package: $alt"; nipkg install --accept-eulas -y $alt } `
    }; `
    if ($LASTEXITCODE -ne 0) { `
      Write-Host "::warning::NI Unit Test Framework could not be installed; the unit-test runner will report 'no tests found'. Pin the name with the UTF_PACKAGE build-arg and rebuild." `
    } elseif (Test-Path 'C:\ProgramData\National Instruments\NI Package Manager\cache') { `
      Remove-Item -Path 'C:\ProgramData\National Instruments\NI Package Manager\cache\*' -Force -Recurse -ErrorAction SilentlyContinue `
    }

ARG VIPM_INSTALLER_URL=https://traffic.libsyn.com/secure/jkinc/vipm-26.3.4056-windows-setup.exe
COPY .github/labview/vipm/install-vipc.ps1 C:/ci-bootstrap/install-vipc.ps1
RUN & C:\ci-bootstrap\install-vipc.ps1 -BootstrapOnly; `
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; `
    Remove-Item -LiteralPath C:\ci-bootstrap\install-vipc.ps1 -Force

ARG GIT_INSTALLER_URL=https://github.com/git-for-windows/git/releases/download/v2.54.0.windows.1/Git-2.54.0-64-bit.exe
ARG GIT_MINGIT_URL=https://github.com/git-for-windows/git/releases/download/v2.54.0.windows.1/MinGit-2.54.0-64-bit.zip
RUN $ErrorActionPreference = 'Continue'; `
    function Add-MachinePath([string] $dir) { `
      $mp = [Environment]::GetEnvironmentVariable('Path','Machine'); `
      if ($mp -notlike ('*' + $dir + '*')) { [Environment]::SetEnvironmentVariable('Path', $dir + ';' + $mp, 'Machine') }; `
      $env:Path = $dir + ';' + $env:Path `
    }; `
    function Test-GitOk { try { $v = & git --version 2>$null; return ($LASTEXITCODE -eq 0 -and $v) } catch { return $false } }; `
    $ok = $false; `
    if (Get-Command winget -ErrorAction SilentlyContinue) { `
      Write-Host 'Installing Git via winget (Git.Git) ...'; `
      try { `
        & winget install -e --id Git.Git --accept-source-agreements --accept-package-agreements --silent --disable-interactivity; `
        Add-MachinePath 'C:\Program Files\Git\cmd'; `
        $ok = Test-GitOk `
      } catch { Write-Host ('winget Git install failed: ' + $_.Exception.Message) } `
    } else { Write-Host 'winget is not available in this base image; using the Git-for-Windows installer instead.' }; `
    if (-not $ok) { `
      $exe = Join-Path $env:TEMP 'git-setup.exe'; `
      try { `
        Write-Host "Downloading Git for Windows from $env:GIT_INSTALLER_URL"; `
        Invoke-WebRequest -Uri $env:GIT_INSTALLER_URL -OutFile $exe -UseBasicParsing; `
        Write-Host ('Downloaded {0:N1} MB; installing silently ...' -f ((Get-Item $exe).Length / 1MB)); `
        Start-Process -FilePath $exe -ArgumentList '/VERYSILENT','/NORESTART','/SP-','/SUPPRESSMSGBOXES','/NOCANCEL' -Wait; `
        Add-MachinePath 'C:\Program Files\Git\cmd'; `
        $ok = Test-GitOk `
      } catch { Write-Host ('Git-for-Windows installer failed: ' + $_.Exception.Message) } finally { Remove-Item $exe -Force -ErrorAction SilentlyContinue } `
    }; `
    if (-not $ok) { `
      $gitZip = Join-Path $env:TEMP 'mingit.zip'; `
      try { `
        Write-Host "Falling back to portable MinGit from $env:GIT_MINGIT_URL"; `
        Invoke-WebRequest -Uri $env:GIT_MINGIT_URL -OutFile $gitZip -UseBasicParsing; `
        Expand-Archive -Path $gitZip -DestinationPath 'C:\git' -Force; `
        Add-MachinePath 'C:\git\cmd'; `
        $ok = Test-GitOk `
      } catch { Write-Host ('MinGit fallback failed: ' + $_.Exception.Message) } finally { Remove-Item $gitZip -Force -ErrorAction SilentlyContinue } `
    }; `
    if ($ok) { Write-Host ('Installed Git: ' + (& git --version)) } `
    else { Write-Host '::warning::No Git could be installed; VIPM Community Edition cannot verify repository visibility.' }

LABEL com.cotc.ci-base.kind=labview-ci `
      com.cotc.ci-base.platform=windows

# NI's LabVIEW container only licenses the headless automation path
# (LabVIEWCLI -Headless). Tools that launch LabVIEW.exe DIRECTLY -- e.g. g-cli
# (used by Antidoc) -- otherwise come up at the activation wizard and hang. NI's
# global override makes EVERY LabVIEW launch headless and activation-free, so
# direct-launch tools work too. See ni/labview-for-containers docs
# (Headless LabVIEW -> "Setting Headless Mode as default"; FAQ 11).
#
# CAUTION: "EVERY LabVIEW launch" includes LabVIEW-runtime APPLICATIONS. The
# VIPM Desktop engine is one, and under this global default it never completes
# the startup handshake the vipm CLI waits on -- baking this variable in
# (2026-07-22) silently broke every downstream dependency bake at 'wait for
# VIPM startup' until install-vipc.ps1 learned to clear it for its own process
# tree. Any future tool that drives VIPM (or another LabVIEW-built app that
# must show as started) needs the same treatment.
ENV LV_RTE_HEADLESS=1
