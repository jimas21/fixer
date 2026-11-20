# Fixer Desktop uninstaller (11-20, the owner: "no backup should be made or are needed. It should be clean. Wipe").
#
#   powershell -ExecutionPolicy Bypass -File C:\FixerDesktop\uninstall.ps1
#
# Removes everything Fixer Desktop put on this computer and keeps nothing: the tray and server are stopped, the
# "start with Windows" shortcut is removed, and the install folder is deleted with all of it inside: the models,
# your styles and settings, the phone pairings, ComfyUI, llama.cpp and Python's libraries. Things other programs
# share (uv itself, uv's download cache and its Python) are left alone.
#
# The phone's "Remove Fixer everywhere" runs this through a one-off scheduled task (outside the server, which it
# stops), from a copy in TEMP (it deletes the folder it would otherwise live in).
#
# -HomeDir (or FIXER_HOME) picks the install; -Wait waits that many seconds first (the phone's request gets its
# answer). FIXER_NO_STARTUP=1 leaves the Startup shortcut alone (test installs).

param(
    [string]$HomeDir = $(if ($env:FIXER_HOME) { $env:FIXER_HOME } else { "C:\FixerDesktop" }),
    [int]$Wait = 0,
    [string]$Task = ""
)

$ErrorActionPreference = "Continue"
$log = Join-Path $env:TEMP "fixer-uninstall.log"
function Say($t) { $line = "$(Get-Date -Format s) $t"; Write-Host $line; Add-Content -Path $log -Value $line }

Say "Removing Fixer Desktop from $HomeDir"
if ($Wait -gt 0) { Start-Sleep -Seconds $Wait }

# Never anything but a Fixer install: the folder must hold its server and its control script.
$home_ok = (Test-Path (Join-Path $HomeDir "fixer_desktop.py")) -and (Test-Path (Join-Path $HomeDir "fixerctl.py"))
$resolved = (Resolve-Path $HomeDir -ErrorAction SilentlyContinue).Path
if (-not $home_ok -or -not $resolved -or $resolved.Length -lt 6) {
    Say "Not a Fixer Desktop install: nothing removed."
    exit 1
}

# 1. Stop everything (the control script ends the tray, the server, the chat model and ComfyUI of THIS install).
$py = Join-Path $HomeDir "venv\Scripts\python.exe"
if (Test-Path $py) {
    $env:FIXER_HOME = $HomeDir
    & $py (Join-Path $HomeDir "fixerctl.py") stop 2>&1 | ForEach-Object { Say "   $_" }
}

# 2. The Startup shortcut, only when it points into this install.
if ($env:FIXER_NO_STARTUP -ne "1") {
    $lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "Fixer Desktop.lnk"
    if (Test-Path $lnk) {
        $target = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk).Arguments
        if ($target -like "*$HomeDir*") { Remove-Item $lnk -Force; Say "Removed the Startup shortcut" }
    }
}

# 3. The install folder, everything in it. Files a dying process still holds are retried for a minute.
for ($i = 1; $i -le 12 -and (Test-Path $HomeDir); $i++) {
    Remove-Item -LiteralPath $HomeDir -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $HomeDir) { Start-Sleep -Seconds 5 }
}
if (Test-Path $HomeDir) {
    $left = (Get-ChildItem $HomeDir -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object).Count
    Say "Couldn't remove everything: $left files still in $HomeDir (something still has them open). Run this again after a restart."
} else {
    Say "Removed $HomeDir"
}

# 4. Its own leftovers: temporary download folders, and the one-off task that ran this.
Get-ChildItem $env:TEMP -Directory -Filter "fixer-*" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
if ($Task) { schtasks /Delete /TN $Task /F 2>$null | Out-Null }
Say "Fixer Desktop is gone from this computer."
