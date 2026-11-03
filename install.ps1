# zephvox installer for Windows. Also updates an existing install.
#
#   powershell -ExecutionPolicy ByPass -c "iex (irm https://raw.githubusercontent.com/dipumiah/zephvox/main/install.ps1 | Out-String)"
#
# Environment: ZEPHVOX_DIR (default %LOCALAPPDATA%\zephvox), ZEPHVOX_VERSION (a release tag;
# default latest), ZEPHVOX_SETUP_ARGS (passed to `zephvox setup`, e.g. "--profile cpu --yes").
#
# What it does: installs uv if needed, downloads zephvox into one folder, installs the small core,
# then hands over to `zephvox setup`, which detects your hardware and installs the rest.

& {
    $ErrorActionPreference = "Stop"
    $ProgressPreference = "SilentlyContinue"  # Invoke-WebRequest is many times slower with it
    $Repo = "dipumiah/zephvox"
    $Old = Join-Path $env:LOCALAPPDATA "speakcheck"   # an install made under the app's old name is updated in place
    $New = Join-Path $env:LOCALAPPDATA "zephvox"
    $Dir = if ($env:ZEPHVOX_DIR) { $env:ZEPHVOX_DIR } elseif ($env:SPEAKCHECK_DIR) { $env:SPEAKCHECK_DIR } `
           elseif ((Test-Path $Old) -and -not (Test-Path $New)) { $Old } else { $New }

    if (Test-Path (Join-Path $Dir ".git")) { throw "$Dir is a git checkout: update it with git pull instead" }

    # 1. uv, which installs Python and every package
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) {
        $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
        if (-not (Test-Path $uv)) {
            Write-Host "Installing uv (Python package manager)..."
            powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
            if (-not (Test-Path $uv)) { throw "uv installation failed" }
        }
    }

    # 2. zephvox itself: the latest release, or main while there is none
    $Version = $env:ZEPHVOX_VERSION
    if (-not $Version) {
        try { $Version = (Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest").tag_name } catch { $Version = $null }
    }
    if ($Version) {
        $Url = "https://github.com/$Repo/archive/refs/tags/$Version.zip"
    } else {
        $Version = "main"
        $Url = "https://github.com/$Repo/archive/refs/heads/main.zip"
    }
    Write-Host "Downloading zephvox $Version into $Dir..."
    $Tmp = Join-Path ([IO.Path]::GetTempPath()) ("zephvox-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $Tmp | Out-Null
    try {
        $Zip = Join-Path $Tmp "source.zip"
        Invoke-WebRequest $Url -OutFile $Zip -UseBasicParsing
        Expand-Archive $Zip -DestinationPath $Tmp
        $Src = Get-ChildItem $Tmp -Directory | Select-Object -First 1
        New-Item -ItemType Directory -Force -Path $Dir | Out-Null
        # Replace what the release contains; config.toml, .env and .venv aren't in it, so they stay.
        Get-ChildItem $Src.FullName -Force | ForEach-Object {
            $Target = Join-Path $Dir $_.Name
            if (Test-Path $Target) { Remove-Item $Target -Recurse -Force }
            Move-Item $_.FullName $Target
        }
    } finally {
        Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    # 3. the core (a few MB), then the setup wizard for everything else
    Push-Location $Dir
    try {
        & $uv sync --no-dev --frozen --quiet
        if ($LASTEXITCODE -ne 0) { throw "installing zephvox's core failed" }
        Write-Host "Starting setup..."
        $SetupArgs = if ($env:ZEPHVOX_SETUP_ARGS) { $env:ZEPHVOX_SETUP_ARGS -split " " } else { @() }
        & $uv run --no-sync python -m zephvox.cli setup --installed-version $Version @SetupArgs
    } finally {
        Pop-Location
    }
}
