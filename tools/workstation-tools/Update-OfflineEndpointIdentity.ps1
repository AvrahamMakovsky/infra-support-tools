<#
Update-OfflineEndpointIdentity.ps1

Purpose:
  Updates an offline Windows installation identity in two places:
  1. hostname-related values in the offline Windows SYSTEM registry hive
  2. an optional small hostname file on the EFI System Partition

Created by:
  Avraham Makovsky

License:
  MIT

Why it exists:
  Some lab or deployment workflows keep a small endpoint identity file on the
  EFI System Partition so pre-boot, PXE, rebuild, or imaging tooling can read
  the intended host name even when another OS partition is being rebuilt or
  replaced.

Scope:
  - Generic public version.
  - No company domain.
  - No fixed lab naming scheme.
  - No external system integration.
  - Explicit paths only.

Run from:
  Administrator PowerShell on a maintenance workstation.

Use only on:
  Offline Windows installations and disks you are authorized to maintain.
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(?!-)(?!.*--)[A-Za-z0-9-]{1,15}(?<!-)$')]
    [string]$ComputerName,

    [string]$WindowsRoot = "",

    [string]$EfiRoot = "",

    [string]$EfiHostFile = "EFI\EndpointIdentity\hostname.txt",

    [switch]$NoBackup
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}

function Resolve-FullPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
    return [System.IO.Path]::GetFullPath($resolved.ProviderPath).TrimEnd('\')
}

function Test-SamePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Left,

        [Parameter(Mandatory = $true)]
        [string]$Right
    )

    $leftFull = [System.IO.Path]::GetFullPath($Left).TrimEnd('\')
    $rightFull = [System.IO.Path]::GetFullPath($Right).TrimEnd('\')

    return $leftFull.Equals($rightFull, [System.StringComparison]::OrdinalIgnoreCase)
}

function Set-RegistryStringIfKeyExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [string]$Value,

        [Parameter(Mandatory = $true)]
        [System.Collections.Generic.List[string]]$UpdatedValues
    )

    if (Test-Path -LiteralPath $Path) {
        New-ItemProperty `
            -LiteralPath $Path `
            -Name $Name `
            -Value $Value `
            -PropertyType String `
            -Force | Out-Null

        [void]$UpdatedValues.Add("$Path :: $Name")
    }
}

function Update-OfflineWindowsRegistry {
    param(
        [Parameter(Mandatory = $true)]
        [string]$OfflineWindowsRoot,

        [Parameter(Mandatory = $true)]
        [string]$NewComputerName,

        [switch]$SkipBackup
    )

    $windowsRootResolved = Resolve-FullPath -Path $OfflineWindowsRoot
    $liveWindowsRoot = Resolve-FullPath -Path $env:WINDIR

    if (Test-SamePath -Left $windowsRootResolved -Right $liveWindowsRoot) {
        throw "Refusing to update the currently running Windows installation. Provide an offline Windows root path."
    }

    $systemHive = Join-Path $windowsRootResolved "System32\config\SYSTEM"

    if (-not (Test-Path -LiteralPath $systemHive)) {
        throw "SYSTEM hive was not found: $systemHive"
    }

    if (-not $PSCmdlet.ShouldProcess($systemHive, "Update offline Windows hostname registry values")) {
        return
    }

    if (-not $SkipBackup) {
        $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
        $backupPath = "$systemHive.bak_$stamp"
        Copy-Item -LiteralPath $systemHive -Destination $backupPath -Force
        Write-Host "Backup created: $backupPath"
    }

    $mountName = "OfflineEndpointIdentity_$PID"
    $mountKey = "HKLM\$mountName"
    $registryRoot = "Registry::HKEY_LOCAL_MACHINE\$mountName"
    $updatedValues = New-Object System.Collections.Generic.List[string]
    $loaded = $false

    try {
        & reg.exe load $mountKey $systemHive | Out-Null

        if ($LASTEXITCODE -ne 0) {
            throw "reg.exe load failed with exit code $LASTEXITCODE"
        }

        $loaded = $true

        $controlSets = @(
            Get-ChildItem -LiteralPath $registryRoot -ErrorAction Stop |
                Where-Object { $_.PSChildName -match '^ControlSet\d{3}$' }
        )

        if (-not $controlSets) {
            throw "No ControlSet### keys were found in the offline SYSTEM hive."
        }

        foreach ($controlSet in $controlSets) {
            $base = $controlSet.PSPath

            Set-RegistryStringIfKeyExists `
                -Path (Join-Path $base "Control\ComputerName\ComputerName") `
                -Name "ComputerName" `
                -Value $NewComputerName `
                -UpdatedValues $updatedValues

            Set-RegistryStringIfKeyExists `
                -Path (Join-Path $base "Control\ComputerName\ActiveComputerName") `
                -Name "ComputerName" `
                -Value $NewComputerName `
                -UpdatedValues $updatedValues

            Set-RegistryStringIfKeyExists `
                -Path (Join-Path $base "Services\Tcpip\Parameters") `
                -Name "Hostname" `
                -Value $NewComputerName `
                -UpdatedValues $updatedValues

            Set-RegistryStringIfKeyExists `
                -Path (Join-Path $base "Services\Tcpip\Parameters") `
                -Name "NV Hostname" `
                -Value $NewComputerName `
                -UpdatedValues $updatedValues

            Set-RegistryStringIfKeyExists `
                -Path (Join-Path $base "Services\Tcpip6\Parameters") `
                -Name "Hostname" `
                -Value $NewComputerName `
                -UpdatedValues $updatedValues

            Set-RegistryStringIfKeyExists `
                -Path (Join-Path $base "Services\Tcpip6\Parameters") `
                -Name "NV Hostname" `
                -Value $NewComputerName `
                -UpdatedValues $updatedValues
        }

        Write-Host ""
        Write-Host "Updated registry values:"
        foreach ($item in $updatedValues) {
            Write-Host "  - $item"
        }

        if ($updatedValues.Count -eq 0) {
            Write-Host "  No matching registry values were updated."
        }
    }
    finally {
        if ($loaded) {
            [GC]::Collect()
            Start-Sleep -Milliseconds 300
            & reg.exe unload $mountKey | Out-Null

            if ($LASTEXITCODE -ne 0) {
                Write-Warning "reg.exe unload returned exit code $LASTEXITCODE. Check whether the hive is still mounted at $mountKey."
            }
        }
    }
}

function Update-EfiHostnameFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$EfiRootPath,

        [Parameter(Mandatory = $true)]
        [string]$RelativeHostFile,

        [Parameter(Mandatory = $true)]
        [string]$NewComputerName
    )

    $efiRootResolved = Resolve-FullPath -Path $EfiRootPath
    $cleanRelativePath = $RelativeHostFile.TrimStart('\', '/')

    if ([System.IO.Path]::IsPathRooted($cleanRelativePath)) {
        throw "EfiHostFile must be a relative path inside the EFI partition."
    }

    $targetFile = Join-Path $efiRootResolved $cleanRelativePath
    $targetDirectory = Split-Path -Parent $targetFile

    if (-not $PSCmdlet.ShouldProcess($targetFile, "Write EFI hostname file")) {
        return
    }

    New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
    Set-Content -LiteralPath $targetFile -Value $NewComputerName -Encoding ASCII

    Write-Host "EFI hostname file written: $targetFile"
}

if (-not (Test-IsAdministrator)) {
    throw "Run PowerShell as Administrator."
}

$normalizedComputerName = $ComputerName.ToUpperInvariant()

if ([string]::IsNullOrWhiteSpace($WindowsRoot) -and [string]::IsNullOrWhiteSpace($EfiRoot)) {
    throw "Provide at least one target: -WindowsRoot and/or -EfiRoot."
}

Write-Host "Offline endpoint identity update"
Write-Host "Computer name: $normalizedComputerName"

if (-not [string]::IsNullOrWhiteSpace($WindowsRoot)) {
    Update-OfflineWindowsRegistry `
        -OfflineWindowsRoot $WindowsRoot `
        -NewComputerName $normalizedComputerName `
        -SkipBackup:$NoBackup
}

if (-not [string]::IsNullOrWhiteSpace($EfiRoot)) {
    Update-EfiHostnameFile `
        -EfiRootPath $EfiRoot `
        -RelativeHostFile $EfiHostFile `
        -NewComputerName $normalizedComputerName
}

Write-Host ""
Write-Host "Done."
