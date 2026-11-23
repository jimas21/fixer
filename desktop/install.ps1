# Fixer Desktop installer (11-20). One line in PowerShell:
#
#   irm https://raw.githubusercontent.com/jimas21/fixer/main/desktop/install.ps1 | iex
#
# or run it from a checkout:  powershell -ExecutionPolicy Bypass -File desktop\install.ps1
#
# Installs into C:\FixerDesktop (FIXER_HOME to change): Python 3.12 and the server's libraries exactly as locked in
# requirements-lock.txt, llama.cpp b12443 (CUDA) for chat, ComfyUI v0.45.5 with its own libraries (comfyui-lock.txt)
# for pictures, video and songs, the server files, and the tray at login. Re-running it repairs or updates; finished
# parts are skipped. No models are downloaded here: the phone fetches the ones you choose.
# Your styles (recipes.json) are private and not in the repo: copy yours into the install folder to have them.
#
# Options (environment variables): FIXER_HOME, FIXER_REF (git branch or tag, default main), FIXER_NO_STARTUP=1 (no
# tray at login), FIXER_NO_START=1 (don't start it at the end).

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"                  # Invoke-WebRequest is many times slower with the bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$HomeDir = if ($env:FIXER_HOME) { $env:FIXER_HOME } else { "C:\FixerDesktop" }
$Ref = if ($env:FIXER_REF) { $env:FIXER_REF } else { "main" }
$Raw = "https://raw.githubusercontent.com/jimas21/fixer/$Ref/desktop"
$LlamaTag = "b12443"
$ComfyTag = "v0.45.5"
# SHA-256 of the three archives below, recorded 2026-11-22. They belong to the tags above: change a tag, change its hash.
$LlamaSha = "a12df512f52da4bfe9aeb2916e17ed1ebe9f3ea3078e319b108f9bb18f03bb2b"
$CudartSha = "c2bf4cc202bfbaaa7a0b75f5082457c3811326947bb216059b72839d844e9cba"
$ComfySha = "7f2807f842de6a0da8d31d42214feb0adcb0a7f20e7cff458efd21d9871d8346"
$Torch = @("torch==2.11.0", "torchvision==0.26.0")
$TorchIndex = "https://download.pytorch.org/whl/cu128"
$Files = @("fixer_desktop.py", "fixer_secure.py", "comfy_guard.py", "tray.py", "taehv.py", "pair.py", "fixerctl.py", "client_test.py",
           "trace_test.py", "uninstall.ps1", "requirements.txt", "requirements-lock.txt", "comfyui-lock.txt",
           "prompt_toggles.json", "phone_config.json", "local.example.json")

function Step($t) { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Done($t) { Write-Host "   $t" -ForegroundColor DarkGray }

function Get-File($url, $dest, $sha = "") {
    for ($i = 1; $i -le 3; $i++) {
        try { Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -Headers @{ "User-Agent" = "FixerInstaller/1" }; break }
        catch { if ($i -eq 3) { throw "Couldn't download $url : $($_.Exception.Message)" }; Start-Sleep -Seconds (3 * $i) }
    }
    if ($sha) {                                        # a file that is not the one recorded here is never unpacked or run
        $got = (Get-FileHash -Path $dest -Algorithm SHA256).Hash
        if ($got -ne $sha) {
            Remove-Item $dest -Force -ErrorAction SilentlyContinue
            throw "The download of $url is not the file this installer expects (SHA-256 $got). Nothing was installed from it."
        }
    }
}

function Expand-Into($zip, $dest) {
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Expand-Archive -Path $zip -DestinationPath $dest -Force
}

Write-Host "Fixer Desktop installer -> $HomeDir" -ForegroundColor White

Step "Checking this computer"
try { $gpu = (& nvidia-smi --query-gpu=name,memory.total --format=csv,noheader) 2>$null } catch { $gpu = $null }
if ($gpu) { Done "Graphics card: $gpu" } else { Write-Warning "No NVIDIA graphics card found. Chat and pictures need one." }
$free = [math]::Round((Get-PSDrive ($HomeDir.Substring(0, 1))).Free / 1GB)
Done "Free space on $($HomeDir.Substring(0, 2)): $free GB (the install is about 25 GB, models extra)"
New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null

Step "uv (Python installer)"
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    $uv = (Get-Command uv -ErrorAction Stop).Source
}
Done $uv

Step "Server files"
$here = if ($PSScriptRoot) { $PSScriptRoot } else { "" }
$local = $here -and (Test-Path (Join-Path $here "fixer_desktop.py"))
foreach ($f in $Files) {
    $dest = Join-Path $HomeDir $f
    if ($f -in @("phone_config.json", "prompt_toggles.json") -and (Test-Path $dest)) { continue }   # yours win
    if ($local) { Copy-Item (Join-Path $here $f) $dest -Force } else { Get-File "$Raw/$f" $dest }
}
Done ($(if ($local) { "copied from $here" } else { "downloaded from GitHub ($Ref)" }))

Step "Python and the server's libraries"
$py = Join-Path $HomeDir "venv\Scripts\python.exe"
if (-not (Test-Path $py)) { & $uv venv --python 3.12 (Join-Path $HomeDir "venv"); if ($LASTEXITCODE) { throw "venv failed" } }
& $uv pip install --python $py @Torch --index-url $TorchIndex
if ($LASTEXITCODE) { throw "torch install failed" }
& $uv pip install --python $py -r (Join-Path $HomeDir "requirements-lock.txt")
if ($LASTEXITCODE) { throw "library install failed" }
Done "venv ready"

Step "llama.cpp $LlamaTag (chat)"
$llama = Join-Path $HomeDir "llama"
if (-not (Test-Path (Join-Path $llama "llama-server.exe"))) {
    $tmp = Join-Path $env:TEMP "fixer-llama"
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $rel = "https://github.com/ggml-org/llama.cpp/releases/download/$LlamaTag"
    Get-File "$rel/llama-$LlamaTag-bin-win-cuda-13.5-x64.zip" "$tmp\llama.zip" $LlamaSha
    Get-File "$rel/cudart-llama-bin-win-cuda-13.5-x64.zip" "$tmp\cudart.zip" $CudartSha
    Expand-Into "$tmp\llama.zip" $llama
    Expand-Into "$tmp\cudart.zip" $llama
    Remove-Item $tmp -Recurse -Force
}
Done "llama-server ready"

Step "ComfyUI $ComfyTag (pictures, video, songs)"
$comfy = Join-Path $HomeDir "comfyui"
if (-not (Test-Path (Join-Path $comfy "main.py"))) {
    $tmp = Join-Path $env:TEMP "fixer-comfy"
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    Get-File "https://github.com/comfyanonymous/ComfyUI/archive/refs/tags/$ComfyTag.zip" "$tmp\comfy.zip" $ComfySha
    Expand-Into "$tmp\comfy.zip" $tmp
    $inner = Get-ChildItem $tmp -Directory | Where-Object { $_.Name -like "ComfyUI-*" } | Select-Object -First 1
    Move-Item $inner.FullName $comfy
    Remove-Item $tmp -Recurse -Force
}
$cpy = Join-Path $comfy ".venv\Scripts\python.exe"
if (-not (Test-Path $cpy)) { & $uv venv --python 3.12 (Join-Path $comfy ".venv"); if ($LASTEXITCODE) { throw "ComfyUI venv failed" } }
& $uv pip install --python $cpy @Torch "torchaudio==2.11.0" --index-url $TorchIndex
if ($LASTEXITCODE) { throw "ComfyUI torch install failed" }
& $uv pip install --python $cpy -r (Join-Path $HomeDir "comfyui-lock.txt")
if ($LASTEXITCODE) { throw "ComfyUI library install failed" }
Done "ComfyUI ready"

if ($env:FIXER_NO_STARTUP -ne "1") {
    Step "Start with Windows"
    $lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "Fixer Desktop.lnk"
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $s.TargetPath = Join-Path $HomeDir "venv\Scripts\pythonw.exe"
    $s.Arguments = '"' + (Join-Path $HomeDir "tray.py") + '"'
    $s.WorkingDirectory = $HomeDir
    $s.Description = "Fixer Desktop tray"
    $s.Save()
    Done $lnk
}

Step "Checking the install"
$env:FIXER_HOME = $HomeDir
$bad = 1
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"                       # Windows PowerShell 5.1 turns a library's stderr warning into an error
Push-Location $HomeDir
try {
    # from the install folder, so the server file itself is imported too, along with the libraries it and the tray need
    & $py -c "import sys; sys.path.insert(0, '.'); import aiohttp, cryptography, PIL, torch, yt_dlp, av, diffusers, transformers, pystray, fixer_desktop; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"
    $bad = $LASTEXITCODE
}
finally { Pop-Location; $ErrorActionPreference = $prevEap }
if ($bad) { throw "The install didn't check out." }

if ($env:FIXER_NO_START -ne "1") {
    Step "Starting Fixer Desktop"
    $env:FIXER_HOME = $HomeDir
    & $py (Join-Path $HomeDir "fixerctl.py") start
}

Write-Host ""
Write-Host "Fixer Desktop is installed." -ForegroundColor Green
Write-Host "To pair your iPhone: right-click the Fixer icon by the clock -> Pair a phone, and scan the code in the app."
