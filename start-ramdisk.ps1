# 64 MB ImDisk RAM volume mounted *inside* Equalizer APO's config folder.
# APO's ReadDirectoryChangesW only watches that folder — a drive letter R:
# is invisible to audiodg (session 0) and does not fire the watcher.
$ErrorActionPreference = "Continue"
$imdisk = Join-Path $env:SystemRoot "System32\imdisk.exe"
$apo = "C:\Program Files\EqualizerAPO\config"
$ram = Join-Path $apo "ram"
$seed = Join-Path $PSScriptRoot "presets\peace-ram-seed.txt"
$diskPeace = Join-Path $apo "peace.txt"

if (-not (Test-Path $imdisk)) {
    Write-Error "imdisk.exe not found"
    exit 1
}

function Test-FolderMount([string]$path) {
    if (-not (Test-Path $path)) { return $false }
    $a = [int](Get-Item $path -Force).Attributes
    return [bool]($a -band [IO.FileAttributes]::ReparsePoint)
}

$mounted = Test-FolderMount $ram
# Stale junction to a dead ImDisk0 looks mounted but is unwritable.
if ($mounted) {
    try {
        $probe = Join-Path $ram "_probe.tmp"
        Set-Content -Path $probe -Value "ok" -ErrorAction Stop
        Remove-Item $probe -Force -ErrorAction SilentlyContinue
    } catch {
        cmd /c "rmdir `"$ram`""
        $mounted = $false
    }
}

if (-not $mounted) {
    if (-not (Test-Path $ram)) {
        New-Item -ItemType Directory -Force -Path $ram | Out-Null
    }
    Get-ChildItem $ram -Force -ErrorAction SilentlyContinue |
        Remove-Item -Force -Recurse -ErrorAction SilentlyContinue
    & $imdisk -a -t vm -s 64M -m $ram -p "/fs:ntfs /q /y /v:Q6TRAM"
    Start-Sleep -Milliseconds 900
}

if (-not (Test-FolderMount $ram) -and -not (Test-Path $ram)) {
    Write-Error "RAM mount failed at $ram"
    exit 1
}

$live = Join-Path $ram "peace.txt"
if (-not (Test-Path $live)) {
    $src = $null
    if (Test-Path $seed) { $src = $seed }
    elseif (Test-Path $diskPeace) { $src = $diskPeace }
    if ($src) { Copy-Item $src $live -Force }
}

if (Test-FolderMount $ram -and (Test-Path $live)) {
    Set-Content -Path (Join-Path $apo "config.txt") -Value "Include: ram\peace.txt`n" -Encoding ascii -NoNewline
} else {
    Set-Content -Path (Join-Path $apo "config.txt") -Value "Include: peace.txt`n" -Encoding ascii -NoNewline
}

exit 0
