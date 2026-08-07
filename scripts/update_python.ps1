<#
.SYNOPSIS
    Update Python to the latest official stable release and rebuild the project venv.

.DESCRIPTION
    Entry point of the python-updater skill.

    The interpreter is installed from PowerShell, never from a running Python.
    pymanager replaces an install by deleting its directory, and a script started
    with `py` executes out of that very directory, so the install dies with
    "Unable to remove previous install because files are still in use" — and the
    same goes for any venv built on that install, since it loads its pythonXY.dll.

    Phase 1 (this script): update the install manager, drop the legacy launcher,
    resolve the latest stable release, refuse to continue while any Python process
    runs out of the install that would be replaced, then install.

    Phase 2 (update_python.py, launched with the newly installed interpreter): set
    the OS default and rebuild the project virtualenv. It installs nothing and reads
    its target from the interpreter it is launched with.

.PARAMETER Project
    Project directory whose venv is rebuilt. Defaults to the current directory.

.EXAMPLE
    .\update_python.ps1

.EXAMPLE
    .\update_python.ps1 -Project ..\service
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [Alias('p')][string]$Project = (Get-Location).Path
)

$ErrorActionPreference = 'Stop'

$PyManagerWingetId = 'Python.PythonInstallManager'
$LegacyLauncherWingetId = 'Python.Launcher'
$WingetFlags = @('--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity')

function Write-Step([string]$Message) {
    Write-Host "[update-python] $Message"
}

function Write-Warn([string]$Message) {
    Write-Host "[update-python] WARNING: $Message" -ForegroundColor Yellow
}

function Invoke-Native([string[]]$Command, [switch]$IgnoreExit) {
    # Returns nothing on purpose: a return value would have to be piped away at every
    # call site, and piping a native command sends its output there too.
    Write-Step "$ $($Command -join ' ')"
    $exe = $Command[0]
    $rest = if ($Command.Count -gt 1) { $Command[1..($Command.Count - 1)] } else { @() }
    & $exe @rest
    $code = $LASTEXITCODE
    if (-not $IgnoreExit -and $code -ne 0) {
        throw "command failed ($code): $($Command -join ' ')"
    }
}

function Get-PyManagerVersions([switch]$Online) {
    $listArgs = if ($Online) { @('list', '--online', '-f=json') } else { @('list', '-f=json') }
    $raw = (& pymanager @listArgs) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "pymanager $($listArgs -join ' ') failed" }
    # pymanager prints notices above the JSON — e.g. right after it self-updates.
    $start = $raw.IndexOf('{')
    # No JSON at all is what an empty catalog looks like; only a failed call is an error.
    if ($start -lt 0) { return @() }
    return ($raw.Substring($start) | ConvertFrom-Json).versions
}

function Get-InstalledCore {
    return @(Get-PyManagerVersions) | Where-Object { $_.company -eq 'PythonCore' -and -not $_.unmanaged }
}

function Test-UnderRoot([string]$Path, [string]$Root) {
    # Prefix match with the separator attached, so pythoncore-3.14-64-old is not
    # mistaken for something inside pythoncore-3.14-64.
    if (-not $Path) { return $false }
    $prefix = $Root.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    return $Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Get-BlockingProcess([string]$Root) {
    # Two passes, because neither alone is enough.
    #
    # By loaded module: catches processes that hold the install open under a name of
    # their own — anything embedding the interpreter by loading pythonXY.dll directly.
    # (Console scripts such as pip.exe are not this case: those launchers CreateProcess
    # a child python.exe, which the name pass below is what actually catches.)
    # Reading .Modules throws on processes we cannot open — elevated, or the other
    # bitness — so each process is attempted independently.
    #
    # By name: python.exe/pythonw.exe, including the ones whose modules we just failed
    # to read. A venv's own python.exe names its base install in pyvenv.cfg's `home`.
    $info = @{}
    foreach ($p in Get-CimInstance Win32_Process) { $info[[int]$p.ProcessId] = $p }

    # Keyed by PID as a string: an [ordered] dictionary reads an integer index as a
    # position, not as a key, so integer keys throw once a PID exceeds the count.
    $blockers = [ordered]@{}

    foreach ($proc in Get-Process) {
        $hit = $false
        try {
            foreach ($m in $proc.Modules) {
                if (Test-UnderRoot $m.FileName $Root) { $hit = $true; break }
            }
        }
        catch { continue }  # cannot read this process's modules — elevated, or other bitness
        if (-not $hit) { continue }
        $ci = $info[[int]$proc.Id]
        $blockers["$($proc.Id)"] = [pscustomobject]@{
            ProcessId   = $proc.Id
            Path        = if ($ci) { $ci.ExecutablePath } else { $proc.ProcessName }
            CommandLine = if ($ci) { $ci.CommandLine } else { $null }
        }
    }

    foreach ($p in $info.Values) {
        if ($p.Name -notin @('python.exe', 'pythonw.exe')) { continue }
        if ($blockers.Contains("$($p.ProcessId)")) { continue }
        $exe = $p.ExecutablePath
        if (-not $exe) { continue }
        if (-not (Test-UnderRoot $exe $Root)) {
            $cfg = Join-Path (Split-Path (Split-Path $exe -Parent) -Parent) 'pyvenv.cfg'
            if (-not (Test-Path $cfg)) { continue }
            $line = Get-Content $cfg | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
            if (-not $line) { continue }
            $venvHome = ($line -replace '^\s*home\s*=\s*', '').Trim()
            # `home` is the install root itself, so match it as well as anything under it.
            if ($venvHome.TrimEnd('\', '/') -ne $Root.TrimEnd('\', '/') -and -not (Test-UnderRoot $venvHome $Root)) { continue }
        }
        $blockers["$($p.ProcessId)"] = [pscustomobject]@{
            ProcessId   = $p.ProcessId
            Path        = $exe
            CommandLine = $p.CommandLine
        }
    }

    return @($blockers.Values)
}

function Initialize-PyManager {
    if (-not (Get-Command pymanager -ErrorAction SilentlyContinue)) {
        Write-Step 'pymanager not found — installing via winget'
        Invoke-Native (@('winget', 'install', '--id', $PyManagerWingetId, '-e') + $WingetFlags) -IgnoreExit
        if (-not (Get-Command pymanager -ErrorAction SilentlyContinue)) {
            throw 'pymanager installed but not on PATH — open a new terminal and re-run'
        }
    }

    Invoke-Native (@('winget', 'upgrade', '--id', $PyManagerWingetId, '-e') + $WingetFlags) -IgnoreExit

    $listed = & winget list --id $LegacyLauncherWingetId -e
    if ($listed -match [regex]::Escape($LegacyLauncherWingetId)) {
        Write-Step 'removing legacy Python Launcher'
        Invoke-Native (@('winget', 'uninstall', '--id', $LegacyLauncherWingetId, '-e') + $WingetFlags) -IgnoreExit
    }
}

function Get-LatestStableVersion {
    $stable = @(Get-PyManagerVersions -Online) | Where-Object {
        $_.company -eq 'PythonCore' -and
        $_.tag -notmatch '^\d+\.\d+t' -and
        $_.'sort-version' -match '^\d+\.\d+\.\d+$'
    }
    if (-not $stable) { throw 'no stable CPython found in the pymanager online catalog' }
    return ($stable | Sort-Object { [version]$_.'sort-version' } | Select-Object -Last 1).'sort-version'
}

try {
    $projectPath = (Resolve-Path -LiteralPath $Project).Path

    Initialize-PyManager

    $target = Get-LatestStableVersion
    Write-Step "latest stable: Python $target"

    $installed = @(Get-InstalledCore)
    $exe = ($installed | Where-Object { $_.'sort-version' -eq $target } | Select-Object -First 1).executable

    if ($exe) {
        Write-Step "already installed: $exe"
    }
    else {
        # pymanager keeps one directory per minor tag, so installing a newer patch
        # deletes the install the current minor already lives in.
        $minor = ($target -split '\.')[0..1] -join '.'
        $doomed = ($installed | Where-Object { $_.'sort-version' -like "$minor.*" } | Select-Object -First 1).executable
        if ($doomed) {
            $root = Split-Path $doomed -Parent
            $blockers = @(Get-BlockingProcess $root)
            if ($blockers) {
                Write-Warn "Python $target replaces $root, and these processes are holding it open:"
                foreach ($b in $blockers) {
                    $what = if ($b.CommandLine) { $b.CommandLine } else { $b.Path }
                    Write-Host ("  PID {0,-8} {1}" -f $b.ProcessId, $what)
                }
                Write-Warn 'the list can be incomplete — elevated processes and other-bitness ones are not always visible.'
                throw 'stop the processes above, then re-run this script'
            }
        }

        Write-Step "installing Python $target"
        Invoke-Native @('pymanager', 'install', $target, '-y')
        $exe = (@(Get-InstalledCore) | Where-Object { $_.'sort-version' -eq $target } | Select-Object -First 1).executable
        if (-not $exe) { throw "Python $target installed but not listed by pymanager" }
    }

    # Phase 2 runs on the interpreter that was just installed, so nothing it does can be
    # holding the files of an install still to be replaced. It also reads its target from
    # the interpreter it is launched with, which is why $exe here is not just a convenience.
    $phase2 = Join-Path $PSScriptRoot 'update_python.py'
    Invoke-Native @($exe, $phase2, '-p', $projectPath)
}
catch {
    Write-Warn $_.Exception.Message
    exit 1
}
