# Ensure the Docker engine is up before a workflow uses it.
#
# The GitHub-hosted windows-2022 runner pool is currently intermittent: some VMs
# start a job before the Docker engine is running, or with the "docker" service
# not yet registered (see actions/runner-images#14252 and #13888). Failing on the
# first `docker` call turns that transient runner hiccup into a red build, so this
# script waits for the engine to respond -- starting, or if necessary registering,
# the service first -- and only fails (with an actionable message) if Docker never
# becomes available. On a healthy runner it returns within a second.
$ErrorActionPreference = 'Continue'

function Test-DockerUp {
    docker version --format '{{.Server.Version}}' 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

$deadlineSeconds = 180
$deadline = (Get-Date).AddSeconds($deadlineSeconds)
$up = $false

while ((Get-Date) -lt $deadline) {
    if (Test-DockerUp) { $up = $true; break }

    $svc = Get-Service -Name docker -ErrorAction SilentlyContinue
    if ($null -eq $svc) {
        # The service is not registered on this VM yet; register dockerd if present.
        if (Get-Command dockerd -ErrorAction SilentlyContinue) {
            Write-Host 'Docker service not registered on this runner; registering dockerd...'
            & dockerd --register-service 2>$null
        }
        $svc = Get-Service -Name docker -ErrorAction SilentlyContinue
    }
    if ($svc -and $svc.Status -ne 'Running') {
        Write-Host "Starting the docker service (current status: $($svc.Status))..."
        try { Start-Service docker -ErrorAction Stop } catch { Write-Host "Start-Service docker failed: $($_.Exception.Message)" }
    }
    Start-Sleep -Seconds 5
}

if (-not $up) {
    throw "Docker engine did not become available on this windows-2022 runner within $deadlineSeconds seconds. GitHub's hosted Windows runner pool is currently, intermittently, starting VMs without a running Docker daemon (actions/runner-images#14252, #13888). Re-run this job to land on a healthy runner, or use a self-hosted Windows runner with Docker (Windows containers)."
}

docker version
Write-Host 'Docker engine is ready.'
