$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Get-ProbeResult([scriptblock]$Collect) {
    try {
        $data = & $Collect
        if ($null -eq $data) {
            return @{status = 'unavailable'}
        }
        return @{status = 'ok'; data = $data}
    } catch [System.Management.Automation.CommandNotFoundException] {
        return @{status = 'unavailable'}
    } catch {
        return @{status = 'failed'}
    }
}

$probes = [ordered]@{}
$os = Get-ProbeResult { Get-CimInstance Win32_OperatingSystem }
$probes.uptime = if ($os.status -eq 'ok') {
    Get-ProbeResult {
        if ($null -ne $os.data.LastBootUpTime) {
            @{seconds = [long]((Get-Date) - $os.data.LastBootUpTime).TotalSeconds}
        }
    }
} else { $os }

$probes.memory = if ($os.status -eq 'ok') {
    Get-ProbeResult {
        if ($os.data.TotalVisibleMemorySize -gt 0 -and $null -ne $os.data.FreePhysicalMemory) {
            @{
                free_bytes = [long]$os.data.FreePhysicalMemory * 1KB
                total_bytes = [long]$os.data.TotalVisibleMemorySize * 1KB
            }
        }
    }
} else { $os }

$probes.disks = Get-ProbeResult {
    $volumes = Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3'
    if ($null -eq $volumes) { return }
    $disks = @($volumes | ForEach-Object {
        @{drive = $_.DeviceID; free_bytes = $_.FreeSpace; total_bytes = $_.Size}
    })
    if ($disks.Count) { @{volumes = $disks} }
}

$probes.commit = Get-ProbeResult {
    $limit = (Get-Counter '\Memory\Commit Limit').CounterSamples.CookedValue
    $used = (Get-Counter '\Memory\Committed Bytes').CounterSamples.CookedValue
    if ($limit -gt 0 -and $null -ne $used) {
        @{used_bytes = [long]$used; limit_bytes = [long]$limit}
    }
}

$probes.top_memory = Get-ProbeResult {
    $processes = @(Get-Process | Group-Object ProcessName | ForEach-Object {
        [pscustomobject]@{
            name = $_.Name
            working_set_bytes = [long](($_.Group | Measure-Object WorkingSet64 -Sum).Sum)
            count = $_.Count
        }
    } | Sort-Object working_set_bytes -Descending | Select-Object -First 6)
    if ($processes.Count) { @{processes = $processes} }
}

$probes.disk_health = Get-ProbeResult {
    $physicalDisks = Get-PhysicalDisk
    if ($null -eq $physicalDisks) { return }
    $devices = @($physicalDisks | ForEach-Object {
        $wear = $_ | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue
        @{
            name = $_.FriendlyName
            media_type = [string]$_.MediaType
            health = [string]$_.HealthStatus
            wear_percent = $wear.Wear
        }
    })
    if ($devices.Count) { @{devices = $devices} }
}

$probes.pending_reboot = Get-ProbeResult {
    @{
        component_servicing = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'
        windows_update = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
    }
}

$probes.pending_updates = Get-ProbeResult {
    $searcher = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher()
    @{count = $searcher.Search('IsInstalled=0 and IsHidden=0').Updates.Count}
}

$report = @{
    collected_at = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffZ', [Globalization.CultureInfo]::InvariantCulture)
    probes = $probes
}
$marker + (ConvertTo-Json -InputObject $report -Depth 6 -Compress)
