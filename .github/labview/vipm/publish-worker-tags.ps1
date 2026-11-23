param(
    [Parameter(Mandatory = $true)] [string] $Immutable,
    [Parameter(Mandatory = $true)] [string[]] $References
)
$ErrorActionPreference = 'Stop'
$repository = $Immutable -replace ':[^/:]+$', ''
$digest = & crane digest $Immutable
if ($LASTEXITCODE -ne 0 -or ([string]$digest).Trim() -notmatch '^sha256:[a-f0-9]{64}$') {
    throw "Verified immutable image is not readable: $Immutable"
}
$source = "${repository}@$(([string]$digest).Trim())"
$previous = @{}
foreach ($reference in ($References | Select-Object -Unique)) {
    if ($reference -eq $Immutable) { continue }
    if (($reference -replace ':[^/:]+$', '') -cne $repository -or
        ($reference -split ':')[-1] -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$') {
        throw "Invalid worker alias: $reference"
    }
    $preference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $oldDigest = & crane digest $reference 2>&1
        $oldExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $preference }
    $oldText = ($oldDigest | Out-String).Trim()
    if ($oldExit -eq 0 -and $oldText -match '^sha256:[a-f0-9]{64}$') {
        $previous[$reference] = $oldText
    } elseif ($oldExit -ne 0 -and $oldText -match 'MANIFEST_UNKNOWN|manifest unknown|NAME_UNKNOWN') {
        $previous[$reference] = $null
    } else {
        throw "Cannot safely snapshot active alias '$reference': $oldText"
    }
}
$attempted = @()
try {
    foreach ($reference in $previous.Keys | Sort-Object) {
        $attempted += $reference
        & crane tag $source (($reference -split ':')[-1])
        if ($LASTEXITCODE -ne 0) { throw "Worker alias publication failed: $reference" }
        $published = & crane digest $reference
        if ($LASTEXITCODE -ne 0 -or ([string]$published).Trim() -cne ([string]$digest).Trim()) {
            throw "Worker alias digest verification failed: $reference"
        }
    }
} catch {
    $failure = $_
    foreach ($reference in $attempted) {
        if ($previous[$reference]) {
            & crane tag "${repository}@$($previous[$reference])" (($reference -split ':')[-1])
            if ($LASTEXITCODE -ne 0) { Write-Warning "ROLLBACK FAILED for $reference; restore $($previous[$reference]) manually." }
        } else {
            Write-Warning "New alias '$reference' may remain published; no prior digest existed to restore."
        }
    }
    throw $failure
}