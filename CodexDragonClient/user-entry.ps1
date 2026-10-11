# Portable Windows entry point. No registry, account or global policy changes.
[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop')][string]$Action = 'Start',
    [switch]$Check
)
$ErrorActionPreference = 'Stop'

function Test-PythonCandidate {
    param([string]$Executable, [string[]]$Prefix = @())
    $probe = 'import sys; assert sys.version_info >= (3, 10); print(sys.executable)'
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Executable
    $info.Arguments = (($Prefix -join ' ') + ' -c "' + $probe + '"').Trim()
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    try {
        if (-not $process.Start()) { return $null }
        if (-not $process.WaitForExit(5000)) {
            $process.Kill()
            return $null
        }
        $resolved = $process.StandardOutput.ReadToEnd().Trim()
        if ($process.ExitCode -eq 0 -and (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            return $resolved
        }
    } catch { return $null }
    finally { $process.Dispose() }
    return $null
}

function Find-Python {
    $candidates = @()
    foreach ($commandName in @('py.exe', 'python.exe', 'python3.exe')) {
        foreach ($command in @(Get-Command $commandName -CommandType Application -All -ErrorAction SilentlyContinue)) {
            $prefix = @()
            if ($commandName -eq 'py.exe') { $prefix = @('-3') }
            $candidates += [pscustomobject]@{ Executable = $command.Source; Prefix = $prefix }
        }
    }
    if ($env:USERPROFILE) {
        $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
        if (Test-Path -LiteralPath $bundled -PathType Leaf) {
            $candidates += [pscustomobject]@{ Executable = $bundled; Prefix = @() }
        }
    }
    foreach ($candidate in $candidates) {
        $python = Test-PythonCandidate -Executable $candidate.Executable -Prefix $candidate.Prefix
        if ($python) { return $python }
    }
    throw 'Python 3.10 or newer was not found. Install Python from python.org with its launcher or PATH option enabled, then try again.'
}

try {
    $python = Find-Python
    if ($Check) {
        Write-Output $python
        exit 0
    }
    $entry = Join-Path $PSScriptRoot 'launcher.py'
    if ($Action -eq 'Stop') { & $python $entry --stop }
    else { & $python $entry }
    exit $LASTEXITCODE
} catch {
    if ($Check) { Write-Error $_.Exception.Message }
    else {
        Add-Type -AssemblyName System.Windows.Forms
        [void][System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Codex Dragon Atelier', 'OK', 'Error')
    }
    exit 1
}
