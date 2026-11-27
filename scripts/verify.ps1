<#
.SYNOPSIS
    Run FlagRewind local lint (ruff) and test suite (pytest) and save the real output
    to docs/verification/latest.md.
#>

$ErrorActionPreference = "Continue"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $RepoRoot
try {
    $RuffExe = Join-Path $RepoRoot ".venv\Scripts\ruff.exe"
    $PytestExe = Join-Path $RepoRoot ".venv\Scripts\pytest.exe"
    $PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"

    if (-not (Test-Path $RuffExe)) { $RuffExe = "ruff" }
    if (-not (Test-Path $PytestExe)) { $PytestExe = "pytest" }
    if (-not (Test-Path $PythonExe)) { $PythonExe = "python" }

    $OutDir = Join-Path $RepoRoot "docs\verification"
    if (-not (Test-Path $OutDir)) {
        New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
    }
    $OutFile = Join-Path $OutDir "latest.md"

    $TimestampUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $PrevNoColor = $env:NO_COLOR
    $env:NO_COLOR = "1"

    try {
        $PyVersion = (
            & $PythonExe --version 2>&1 |
                ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { $_ } }
        ) -join "`n"
        $PyVersion = $PyVersion.Trim()

        $RuffLines = & $RuffExe check . 2>&1 |
            ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { $_ } }
        $RuffExit = $LASTEXITCODE
        $RuffOutput = ($RuffLines -join "`n").TrimEnd()

        $PytestLines = & $PytestExe --tb=short 2>&1 |
            ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { $_ } }
        $PytestExit = $LASTEXITCODE
        $PytestOutput = ($PytestLines -join "`n").TrimEnd()
    }
    finally {
        if ($null -eq $PrevNoColor) {
            Remove-Item Env:NO_COLOR -ErrorAction SilentlyContinue
        } else {
            $env:NO_COLOR = $PrevNoColor
        }
    }

    $OverallStatus = if ($RuffExit -eq 0 -and $PytestExit -eq 0) { "PASS" } else { "FAIL" }

    $Lines = @(
        "# FlagRewind Local Verification Output"
        ""
        "> **Verification Status:** local run; GitHub Actions blocked by an account billing hold."
        "> *(No CI badge is displayed here until GitHub Actions workflows run cleanly on GitHub.)*"
        ""
        "- **Generated (UTC):** ``$TimestampUtc``"
        "- **Command:** ``powershell -ExecutionPolicy Bypass -File scripts/verify.ps1``"
        "- **Python:** ``$PyVersion``"
        "- **Overall Result:** **$OverallStatus** (``ruff`` exit ``$RuffExit``, ``pytest`` exit ``$PytestExit``)"
        ""
        "## 1. Ruff Lint (``ruff check .``)"
        ""
        "- **Exit Code:** ``$RuffExit``"
        ""
        '```text'
        $RuffOutput
        '```'
        ""
        "## 2. Pytest Suite (``pytest --tb=short``)"
        ""
        "- **Exit Code:** ``$PytestExit``"
        ""
        '```text'
        $PytestOutput
        '```'
    )

    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($OutFile, $Lines, $Utf8NoBom)

    Write-Host "Verification complete ($OverallStatus). Saved real output to docs/verification/latest.md"
    if ($OverallStatus -ne "PASS") {
        exit 1
    }
    exit 0
}
finally {
    Pop-Location
}
