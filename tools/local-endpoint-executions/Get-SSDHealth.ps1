<#
Get-SSDHealth.ps1

Purpose:
  Shows a compact local SSD health summary using smartctl from smartmontools.

Created by:
  Avraham Makovsky

License:
  MIT

Requirements:
  - Windows
  - smartmontools installed
  - smartctl.exe available in PATH or under:
    C:\Program Files\smartmontools\bin\smartctl.exe

Notes:
  SMART health interpretation depends on drive model, protocol, bridge adapter,
  and which vendor attributes are exposed.
#>

param(
    [string]$SmartCtlPath = "",

    [switch]$Json
)

function Resolve-SmartCtl {
    param([string]$PathFromUser)

    if ($PathFromUser -and (Test-Path -LiteralPath $PathFromUser)) {
        return $PathFromUser
    }

    $fromCommand = Get-Command smartctl.exe -ErrorAction SilentlyContinue
    if ($fromCommand) {
        return $fromCommand.Source
    }

    $defaultPath = Join-Path $env:ProgramFiles "smartmontools\bin\smartctl.exe"
    if (Test-Path -LiteralPath $defaultPath) {
        return $defaultPath
    }

    throw "smartctl.exe was not found. Install smartmontools or pass -SmartCtlPath."
}

function Get-DeviceCandidates {
    param(
        [string]$Device,
        [string]$DetectedType
    )

    $candidates = New-Object System.Collections.Generic.List[string]

    if ($DetectedType) {
        [void]$candidates.Add($DetectedType)
    }

    # USB/NVMe adapters expose SMART through different smartctl -d modes.
    if ($Device -match '^/dev/nvme\d+$') {
        [void]$candidates.Add("nvme")
    } else {
        foreach ($type in @(
            "sntjmicron,1",
            "sntjmicron,0x1",
            "sntjmicron,2",
            "sntjmicron,0x2",
            "sntjmicron",
            "sntrealtek",
            "sntasmedia",
            "sat,12",
            "sat,16",
            "sat",
            "scsi"
        )) {
            [void]$candidates.Add($type)
        }
    }

    $seen = @{}
    $result = @()

    foreach ($candidate in $candidates) {
        if ($candidate -and -not $seen.ContainsKey($candidate)) {
            $seen[$candidate] = $true
            $result += $candidate
        }
    }

    return $result
}

function Get-HealthFromSmartJson {
    param($SmartJson)

    $protocol = $SmartJson.protocol
    $percentageUsed = $SmartJson.nvme_smart_health_information_log.percentage_used

    if ($null -ne $percentageUsed) {
        $used = 0
        if ([int]::TryParse([string]$percentageUsed, [ref]$used)) {
            if ($used -ge 0 -and $used -le 100) {
                return [pscustomobject]@{
                    HealthPercent = 100 - $used
                    Method        = "NVMe percentage used: $used%"
                    Protocol      = if ($protocol) { $protocol } else { "NVMe" }
                }
            }
        }
    }

    $table = $SmartJson.ata_smart_attributes.table

    if ($table) {
        $lifeNames = @(
            "Percent_Lifetime_Remain",
            "Percent_Life_Remaining",
            "SSD_Life_Left",
            "Remaining_Life",
            "Media_Wearout_Indicator"
        )

        $row = $table | Where-Object { $lifeNames -contains $_.name } | Select-Object -First 1

        if ($row) {
            $value = $row.raw.value
            if ($null -eq $value -or $value -lt 0 -or $value -gt 100) {
                $value = $row.value
            }

            $parsed = 0
            if ([int]::TryParse([string]$value, [ref]$parsed)) {
                if ($parsed -ge 0 -and $parsed -le 100) {
                    return [pscustomobject]@{
                        HealthPercent = $parsed
                        Method        = $row.name
                        Protocol      = if ($protocol) { $protocol } else { "ATA/SATA" }
                    }
                }
            }
        }

        $wear = $table | Where-Object { $_.name -match "Wear_Leveling_Count" } | Select-Object -First 1
        if ($wear) {
            $parsedWear = 0
            if ([int]::TryParse([string]$wear.value, [ref]$parsedWear)) {
                if ($parsedWear -ge 1 -and $parsedWear -le 100) {
                    return [pscustomobject]@{
                        HealthPercent = $parsedWear
                        Method        = "Wear_Leveling_Count normalized value"
                        Protocol      = if ($protocol) { $protocol } else { "ATA/SATA" }
                    }
                }
            }
        }
    }

    return [pscustomobject]@{
        HealthPercent = $null
        Method        = ""
        Protocol      = $protocol
    }
}

function Get-HealthBar {
    param($HealthPercent)

    if ($null -eq $HealthPercent) {
        return "[??????????]"
    }

    $value = [Math]::Max(0, [Math]::Min(100, [int]$HealthPercent))
    $segments = [int][Math]::Round($value / 10.0)
    return "[{0}{1}]" -f ("#" * $segments), ("." * (10 - $segments))
}

function Get-SmartDevices {
    param([string]$SmartCtl)

    $scanOutput = & $SmartCtl --scan-open 2>$null

    if (-not $scanOutput) {
        throw "smartctl --scan-open did not return devices."
    }

    $devices = @()

    foreach ($line in $scanOutput) {
        if ($line -match '^(?<device>/dev/nvme\d+)(?:\s+-d\s+(?<type>\S+))?') {
            $devices += [pscustomobject]@{ Device = $matches["device"]; Type = $matches["type"] }
            continue
        }

        if ($line -match '^(?<device>\\\\\.\\PhysicalDrive\d+)\s+-d\s+(?<type>\S+)') {
            $devices += [pscustomobject]@{ Device = $matches["device"]; Type = $matches["type"] }
            continue
        }

        if ($line -match '^(?<device>/dev/sd\w+)\s+-d\s+(?<type>\S+)') {
            $devices += [pscustomobject]@{ Device = $matches["device"]; Type = $matches["type"] }
            continue
        }
    }

    return $devices
}

function Get-SsdHealthRows {
    param([string]$SmartCtl)

    $devices = Get-SmartDevices -SmartCtl $SmartCtl
    $rows = @()

    foreach ($device in $devices) {
        $jsonObject = $null
        $workedType = ""
        $healthInfo = $null

        foreach ($candidateType in (Get-DeviceCandidates -Device $device.Device -DetectedType $device.Type)) {
            $jsonText = & $SmartCtl -a -j -d $candidateType $device.Device 2>$null

            if (-not $jsonText) {
                continue
            }

            $joined = ($jsonText -join "`n").Trim()

            if (-not $joined.StartsWith("{")) {
                continue
            }

            try {
                $jsonObject = $joined | ConvertFrom-Json
            } catch {
                $jsonObject = $null
            }

            if ($jsonObject) {
                $workedType = $candidateType
                $healthInfo = Get-HealthFromSmartJson -SmartJson $jsonObject

                if ($null -ne $healthInfo.HealthPercent) {
                    break
                }
            }
        }

        $rows += [pscustomobject]@{
            Device        = $device.Device
            DetectedType  = $workedType
            Protocol      = if ($healthInfo) { $healthInfo.Protocol } else { "" }
            Model         = if ($jsonObject.model_name) { $jsonObject.model_name } else { $jsonObject.device_model }
            Serial        = if ($jsonObject.serial_number) { $jsonObject.serial_number } else { "" }
            HealthPercent = if ($healthInfo) { $healthInfo.HealthPercent } else { $null }
            HealthBar     = if ($healthInfo) { Get-HealthBar -HealthPercent $healthInfo.HealthPercent } else { Get-HealthBar -HealthPercent $null }
            Method        = if ($healthInfo) { $healthInfo.Method } else { "" }
        }
    }

    return $rows
}

try {
    $smartctl = Resolve-SmartCtl -PathFromUser $SmartCtlPath
    $rows = Get-SsdHealthRows -SmartCtl $smartctl

    if ($Json) {
        $rows | ConvertTo-Json -Depth 4
        exit 0
    }

    if (-not $rows) {
        Write-Host "No SSD/drive information returned."
        exit 1
    }

    $rows |
        Select-Object Device, DetectedType, Protocol, Model, HealthPercent, HealthBar, Method |
        Format-Table -AutoSize

    Write-Host ""
    Write-Host "Note: Health interpretation depends on drive model, protocol, and SMART attribute support."

} catch {
    Write-Error $_.Exception.Message
    exit 1
}
