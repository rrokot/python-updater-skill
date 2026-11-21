<#
.SYNOPSIS
    Update Python to the latest official stable release and rebuild the project venv.

.PARAMETER Project
    Project directory whose venv is rebuilt. Defaults to the current directory.

.PARAMETER WhatIf
    Dry run: report what would be installed, removed and rebuilt, and change nothing.
    Read-only queries (winget list, pymanager list --online) still run.

.EXAMPLE
    .\update_python.ps1

.EXAMPLE
    .\update_python.ps1 -Project ..\service

.EXAMPLE
    .\update_python.ps1 -WhatIf
#>
#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess)]
param(
    [Alias('p')][string]$Project = (Get-Location).Path
)

$ErrorActionPreference = 'Stop'
$DryRun = [bool]$WhatIfPreference

$PyManagerWingetId = 'Python.PythonInstallManager'
$LegacyLauncherWingetId = 'Python.Launcher'
$WingetInstallOrUpgradeFlags = @('--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity')
$WingetUninstallFlags = @('--accept-source-agreements', '--disable-interactivity', '--silent')

function Write-Step([string]$Message) {
    Write-Host "[update-python] $Message"
}

function Write-Warn([string]$Message) {
    Write-Host "[update-python] WARNING: $Message" -ForegroundColor Yellow
}

function Invoke-NativeCommand([string[]]$Command, [switch]$IgnoreExit, [switch]$RunDuringDryRun) {
    $commandLine = $Command -join ' '
    if ($DryRun -and -not $RunDuringDryRun) {
        Write-Step "would run: $commandLine"
        return
    }
    Write-Step "$ $commandLine"
    $executable = $Command[0]
    $arguments = if ($Command.Count -gt 1) { $Command[1..($Command.Count - 1)] } else { @() }
    & $executable @arguments
    $exitCode = $LASTEXITCODE
    if (-not $IgnoreExit -and $exitCode -ne 0) {
        throw "command failed ($exitCode): $commandLine"
    }
}

function Get-PyManagerVersions([switch]$Online) {
    $listArgs = if ($Online) { @('list', '--online', '-f=json') } else { @('list', '-f=json') }
    $catalogOutput = (& pymanager @listArgs) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "pymanager $($listArgs -join ' ') failed" }
    $catalogJsonStart = $catalogOutput.IndexOf('{')
    if ($catalogJsonStart -lt 0) { return @() }
    return ($catalogOutput.Substring($catalogJsonStart) | ConvertFrom-Json).versions
}

function Get-InstalledCPythonVersions {
    return @(Get-PyManagerVersions) | Where-Object { $_.company -eq 'PythonCore' -and -not $_.unmanaged }
}

function Get-InstalledPythonExecutable([string]$Version) {
    return (Get-InstalledCPythonVersions | Where-Object { $_.'sort-version' -eq $Version } | Select-Object -First 1).executable
}

function Test-PathInsideDirectory([string]$Path, [string]$Directory) {
    if (-not $Path) { return $false }
    $directoryPrefix = $Directory.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    return $Path.StartsWith($directoryPrefix, [StringComparison]::OrdinalIgnoreCase)
}

function Get-VenvBaseInstall([string]$PythonExecutable) {
    $scriptsDirectory = Split-Path $PythonExecutable -Parent
    $venvDirectory = Split-Path $scriptsDirectory -Parent
    $venvConfig = Join-Path $venvDirectory 'pyvenv.cfg'
    if (-not (Test-Path -LiteralPath $venvConfig)) { return }
    $homeSetting = Get-Content -LiteralPath $venvConfig |
        Where-Object { $_ -match '^\s*home\s*=' } |
        Select-Object -First 1
    if ($homeSetting) {
        return ($homeSetting -replace '^\s*home\s*=\s*', '').Trim()
    }
}

function Get-PythonProcessesUsingInstall([string]$InstallDirectory) {
    foreach ($process in Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'") {
        $pythonExecutable = $process.ExecutablePath
        if (-not $pythonExecutable) { continue }
        if (-not (Test-PathInsideDirectory $pythonExecutable $InstallDirectory)) {
            $venvBaseInstall = Get-VenvBaseInstall $pythonExecutable
            if (-not $venvBaseInstall) { continue }
            $venvUsesInstallRoot = $venvBaseInstall.TrimEnd('\', '/') -eq $InstallDirectory.TrimEnd('\', '/')
            if (-not $venvUsesInstallRoot -and -not (Test-PathInsideDirectory $venvBaseInstall $InstallDirectory)) { continue }
        }
        [pscustomobject]@{
            ProcessId   = $process.ProcessId
            Path        = $pythonExecutable
            CommandLine = $process.CommandLine
        }
    }
}

function Assert-PythonInstallCanBeReplaced([string]$InstallDirectory, [string]$TargetVersion) {
    $blockingProcesses = @(Get-PythonProcessesUsingInstall $InstallDirectory)
    if (-not $blockingProcesses) { return }

    Write-Warn "Python $TargetVersion replaces $InstallDirectory, and these processes are holding it open:"
    foreach ($process in $blockingProcesses) {
        $description = if ($process.CommandLine) { $process.CommandLine } else { $process.Path }
        Write-Host ("  PID {0,-8} {1}" -f $process.ProcessId, $description)
    }
    Write-Warn 'the list can be incomplete — elevated processes and other-bitness ones are not always visible.'
    if (-not $DryRun) { throw 'stop the processes above, then re-run this script' }
    Write-Warn 'a real run would stop here until they are closed'
}

function Clear-ProcessVenvActivation {
    if (-not $env:VIRTUAL_ENV) { return }
    $venvScriptsDirectory = (Join-Path $env:VIRTUAL_ENV 'Scripts').TrimEnd('\')
    $env:PATH = ($env:PATH -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -ne $venvScriptsDirectory }) -join ';'
    Write-Step "ignoring activated venv: $env:VIRTUAL_ENV"
    $env:VIRTUAL_ENV = $null
}

function Initialize-PyManager {
    if (-not (Get-Command pymanager -ErrorAction SilentlyContinue)) {
        if ($DryRun) {
            throw 'pymanager is not installed — a real run installs it via winget; the dry run cannot go further without it'
        }
        Write-Step 'pymanager not found — installing via winget'
        Invoke-NativeCommand (@('winget', 'install', '--id', $PyManagerWingetId, '-e') + $WingetInstallOrUpgradeFlags) -IgnoreExit
        if (-not (Get-Command pymanager -ErrorAction SilentlyContinue)) {
            throw 'pymanager installed but not on PATH — open a new terminal and re-run'
        }
    }

    Invoke-NativeCommand (@('winget', 'upgrade', '--id', $PyManagerWingetId, '-e') + $WingetInstallOrUpgradeFlags) -IgnoreExit

    $listed = & winget list --id $LegacyLauncherWingetId -e
    if ($listed -match [regex]::Escape($LegacyLauncherWingetId)) {
        Write-Step 'removing legacy Python Launcher'
        Invoke-NativeCommand (@('winget', 'uninstall', '--id', $LegacyLauncherWingetId, '-e') + $WingetUninstallFlags) -IgnoreExit
        if (-not $DryRun -and (& winget list --id $LegacyLauncherWingetId -e) -match [regex]::Escape($LegacyLauncherWingetId)) {
            Write-Warn "legacy Python Launcher still installed — remove it manually: winget uninstall --id $LegacyLauncherWingetId -e"
        }
    }
}

function Get-StableCPythonVersions {
    $stableReleases = @(Get-PyManagerVersions -Online) | Where-Object {
        $_.company -eq 'PythonCore' -and
        $_.tag -notmatch '^\d+\.\d+t' -and
        $_.'sort-version' -match '^\d+\.\d+\.\d+$'
    }
    if (-not $stableReleases) { throw 'no stable CPython found in the pymanager online catalog' }
    return @($stableReleases | ForEach-Object { $_.'sort-version' } | Sort-Object { [version]$_ } -Unique)
}

function Get-NewestInstalledPythonExecutable([string]$MinorTag) {
    $installedVersions = @(Get-InstalledCPythonVersions)
    if ($MinorTag) {
        $installedVersions = @($installedVersions | Where-Object { $_.'sort-version' -like "$MinorTag.*" })
    }
    return ($installedVersions | Sort-Object { [version]$_.'sort-version' } | Select-Object -Last 1).executable
}

function Install-CPython([string]$Version) {
    $installedExecutable = Get-InstalledPythonExecutable $Version
    if ($installedExecutable) {
        Write-Step "already installed: $installedExecutable"
        return $installedExecutable
    }

    $targetMinorTag = ($Version -split '\.')[0..1] -join '.'
    $replacedExecutable = Get-NewestInstalledPythonExecutable -MinorTag $targetMinorTag
    if ($replacedExecutable) {
        $replacedInstallDirectory = Split-Path $replacedExecutable -Parent
        Assert-PythonInstallCanBeReplaced $replacedInstallDirectory $Version
    }

    Write-Step "installing Python $Version"
    Invoke-NativeCommand @('pymanager', 'install', $Version, '-y') | Out-Host
    if ($DryRun) { return }
    $installedExecutable = Get-InstalledPythonExecutable $Version
    if (-not $installedExecutable) { throw "Python $Version installed but not listed by pymanager" }
    return $installedExecutable
}

function Resolve-ProjectPythonVersion([string]$PythonExecutable, [string]$ProjectPath, [string[]]$CandidateVersions) {
    $versionResolver = Join-Path $PSScriptRoot 'project_python.py'
    $resolverOutput = & $PythonExecutable $versionResolver -p $ProjectPath @CandidateVersions
    $exitCode = $LASTEXITCODE
    $noCompatibleRelease = 2
    if ($exitCode -eq $noCompatibleRelease) { return }
    if ($exitCode -ne 0) { throw "cannot read the project's requires-python (exit $exitCode)" }
    return ([string]($resolverOutput | Select-Object -Last 1)).Trim()
}

try {
    $projectPath = (Resolve-Path -LiteralPath $Project).Path
    if ($DryRun) { Write-Step 'DRY RUN — nothing will be changed' }

    Clear-ProcessVenvActivation
    Initialize-PyManager

    $stableVersions = @(Get-StableCPythonVersions)
    $osDefaultVersion = $stableVersions[-1]
    Write-Step "latest stable: Python $osDefaultVersion"
    $osDefaultExecutable = Install-CPython $osDefaultVersion

    $venvTargetVersion = $osDefaultVersion
    $venvTargetExecutable = $osDefaultExecutable
    $resolverExecutable = if ($osDefaultExecutable) { $osDefaultExecutable } else { Get-NewestInstalledPythonExecutable }
    if ($resolverExecutable) {
        $compatibleVersion = Resolve-ProjectPythonVersion $resolverExecutable $projectPath $stableVersions
        if ($compatibleVersion -and $compatibleVersion -ne $osDefaultVersion) {
            Write-Step "venv target: Python $compatibleVersion (requires-python excludes $osDefaultVersion)"
            $venvTargetVersion = $compatibleVersion
            $venvTargetExecutable = Install-CPython $compatibleVersion
        }
    }

    $projectUpdater = Join-Path $PSScriptRoot 'update_python.py'
    if (-not $DryRun) {
        Invoke-NativeCommand @($venvTargetExecutable, $projectUpdater, '-p', $projectPath, '--os-default', $osDefaultVersion)
    }
    else {
        $previewExecutable = $venvTargetExecutable
        if (-not $previewExecutable) {
            $venvMinorTag = ($venvTargetVersion -split '\.')[0..1] -join '.'
            $previewExecutable = Get-NewestInstalledPythonExecutable -MinorTag $venvMinorTag
        }
        if (-not $previewExecutable) { $previewExecutable = Get-NewestInstalledPythonExecutable }
        if ($previewExecutable) {
            $previewCommand = @(
                $previewExecutable, $projectUpdater, '-p', $projectPath,
                '--dry-run', '--target', $venvTargetVersion, '--os-default', $osDefaultVersion
            )
            Invoke-NativeCommand $previewCommand -RunDuringDryRun
        }
        else {
            Write-Step 'no managed Python installed yet — phase 2 (OS default, venv) cannot be previewed'
        }
    }
}
catch {
    Write-Warn $_.Exception.Message
    exit 1
}
