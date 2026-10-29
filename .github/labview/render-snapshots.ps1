param(
    [string]$WorkspaceRoot = 'C:\workspace',
    [string]$OpsDir = 'C:\ops',
    [string]$OutByBlobDir = 'C:\out\by-blob',
    [string]$WorkListPath = 'C:\out\worklist.tsv',
    [string]$LabVIEWPath = '',
    [string]$EmitFramesJson = 'true',
    [string]$RenderBinDir = 'C:\renderbin'
)

$ErrorActionPreference = 'Stop'
if ($EmitFramesJson -eq 'false') { exit 0 }
if (-not (Test-Path $WorkListPath)) { exit 0 }
if (@(Get-Content $WorkListPath | Where-Object { $_.Trim() }).Count -eq 0) { exit 0 }
$renderRoot = Join-Path $env:TEMP ('lvci-frames-' + [System.Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $renderRoot, $OutByBlobDir | Out-Null
try {
    & (Join-Path $OpsDir 'toimages\windows-render.ps1') `
        -Workspace $WorkspaceRoot `
        -Worklist $WorkListPath `
        -OutByBlob $renderRoot `
        -Lvctl (Join-Path $RenderBinDir 'lvctl.exe') `
        -Runner (Join-Path $RenderBinDir 'runner.exe') `
        -LabVIEWPath $LabVIEWPath
    $renderExit = $LASTEXITCODE
    Get-ChildItem $renderRoot -Recurse -File -Filter '*.json' | ForEach-Object {
        $blob = $_.BaseName
        if ($blob -notmatch '^[0-9a-f]{40}$') { return }
        $destination = Join-Path $OutByBlobDir $blob.Substring(0, 2)
        New-Item -ItemType Directory -Force -Path $destination | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $destination ($blob + '.windows.json')) -Force
    }
} finally {
    Remove-Item $renderRoot -Recurse -Force -ErrorAction SilentlyContinue
}
exit $renderExit
