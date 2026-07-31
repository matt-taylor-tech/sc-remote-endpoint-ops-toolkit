#!/usr/bin/env python3
"""Read-only diagnostic playbooks over ScreenConnect (screenconnect plugin).

Curated, consistent troubleshooting checks that run a guest machine through a
known set of read-only queries and return a clean report. Wraps sc.py; targets
a machine by serial, machine name, or sessionID (same resolution as sc.py).

Usage:
  python3 diag.py <check> <serial|name|sessionID> [--days N] [--provider NAME] [--timeout S]

Checks (all READ-ONLY):
  eventlogs  Error/warning triage grouped by source over N days (default 7).
             With --provider NAME, drill into one source's recent messages.
  sound      Audio services, sound devices, unhealthy audio endpoints, recent audio errors.
  onedrive   OneDrive process/version, per-user account config, recent OneDrive errors, disk free.
  network    Up adapters, IP/DNS config, recent NIC/Wi-Fi driver errors.
  health     Uptime, disk free, memory + commit + top consumers, pending-reboot flags.
  memory     Deep memory: free/commit/pagefile, 14d low-memory event count, and the
             top processes by grouped working set (catches multi-process hogs like Chrome).
  drivers    Devices in error state now (with problem codes) + Dell Command Update
             scan of pending driver/BIOS/firmware updates. Dell-only; slow (~60-90s).
  battery    Battery report + health: live charge/status, design vs full-charge
             capacity (% of design), and powercfg cycle count. Laptops only.

Remediation is intentionally NOT here. Fixes (restart Audiosrv, onedrive /reset,
dcu-cli /applyUpdates, etc.) are state-changing and run via `sc.py run` only
after the operator confirms the exact command. See SKILL.md.

Note: commands run as the ScreenConnect agent (SYSTEM). Per-user state (OneDrive)
is read from the loaded user hives under HKEY_USERS and C:\\Users\\*, best-effort.
"""
import os
import subprocess
import sys

SC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sc.py")


def ps_eventlogs(days, provider):
    if provider:
        return (
            f"$d=(Get-Date).AddDays(-{days});"
            f"\"=== {provider} events (last {days}d) ===\";"
            "Get-WinEvent -FilterHashtable @{LogName='System','Application'; Level=1,2,3; StartTime=$d} -EA SilentlyContinue |"
            f" Where-Object {{$_.ProviderName -eq '{provider}'}} |"
            " Select -First 20 TimeCreated,Id,LevelDisplayName,@{n='Msg';e={($_.Message -split \"`r?`n\")[0]}} |"
            " Format-Table -Auto -Wrap"
        )
    return (
        f"$d=(Get-Date).AddDays(-{days});"
        "$ev=Get-WinEvent -FilterHashtable @{LogName='System','Application'; Level=1,2,3; StartTime=$d} -EA SilentlyContinue;"
        f"\"Total error/warning events (last {days}d): \" + $ev.Count;"
        "\"--- top sources (Provider / EventID); DCOM 10016/10010 are usually benign noise ---\";"
        "$ev | Group-Object ProviderName,Id | Sort Count -Descending |"
        " Select -First 15 @{n='Count';e={$_.Count}},@{n='Source';e={$_.Name}} | Format-Table -Auto"
    )


def ps_sound():
    return (
        "\"=== Audio services ===\";"
        "Get-Service Audiosrv,AudioEndpointBuilder -EA SilentlyContinue | Select Name,Status,StartType | Format-Table -Auto;"
        "\"=== Sound devices (WMI) ===\";"
        "Get-CimInstance Win32_SoundDevice -EA SilentlyContinue | Select Name,Status | Format-Table -Auto;"
        "\"=== Audio endpoints / media not OK ===\";"
        "Get-PnpDevice -Class AudioEndpoint,Media -EA SilentlyContinue | Where-Object {$_.Status -ne 'OK'} | Select Status,Class,FriendlyName | Format-Table -Auto;"
        "\"=== Recent audio-related errors/warnings (7d) ===\";"
        "Get-WinEvent -FilterHashtable @{LogName='System'; Level=1,2,3; StartTime=(Get-Date).AddDays(-7)} -EA SilentlyContinue |"
        " Where-Object {$_.ProviderName -match 'Audio|HdAudio|IntcAzAudAddService|Kernel-PnP'} |"
        " Select -First 10 TimeCreated,ProviderName,Id,@{n='Msg';e={($_.Message -split \"`r?`n\")[0]}} | Format-Table -Auto -Wrap"
    )


# SharePoint/Teams libraries synced with the OneDrive **Sync button** land in a
# tenant-named top-level profile folder (e.g. C:\Users\<u>\<Your Org Name>) - SEPARATE from the
# personal "OneDrive - <org>" folder and consuming real local disk. "Add shortcut to OneDrive"
# libraries instead live INSIDE "OneDrive - <org>" and honor Files On-Demand, so the mere
# presence of a top-level org-named folder is the Sync-button signal. Surfaced in disk-space
# (health) and onedrive triage so the agent steers users to Add shortcut + Files On-Demand.
#
# CUSTOMIZE FOR YOUR TENANT: list the exact top-level folder name(s) your OneDrive Sync
# button creates (usually your SharePoint/Teams site or org display name). Leave empty to
# skip this check entirely.
_ORG_SYNC_FOLDERS = ""
_PS_SYNCED_LIBS = (
    "\"=== SharePoint/Teams synced libraries (Sync button vs Add shortcut) ===\";"
    "$orgs=@(" + (_ORG_SYNC_FOLDERS if _ORG_SYNC_FOLDERS.strip() else "") + ");"
    "$hits=@();"
    "Get-ChildItem C:\\Users -Directory -EA SilentlyContinue |"
    " Where-Object { $_.Name -notin @('Public','Default','Default User','defaultuser0','All Users') } |"
    " ForEach-Object { $u=$_; foreach($o in $orgs){ if(Test-Path (Join-Path $u.FullName $o)){ $hits += \"$($u.Name)\\$o\" } } };"
    "if($hits){"
    "  \"  SYNC-BUTTON libraries (local copies consuming disk): \" + ($hits -join ', ');"
    "  \"  >> These are SharePoint/Teams libraries synced via the OneDrive 'Sync' button. Best practice is\";"
    "  \"     'Add shortcut to OneDrive' (Files On-Demand) - files stay online-only until opened, reclaiming local space.\";"
    "  \"  >> Remediation: in OneDrive settings stop syncing each library, then re-add it from the SharePoint/Teams\";"
    "  \"     library with 'Add shortcut to OneDrive'; confirm Files On-Demand is on.\";"
    "} else { \"  none found (libraries use Add shortcut / Files On-Demand, or none synced)\" };"
    "$fod=(Get-ItemProperty 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\OneDrive' -Name FilesOnDemandEnabled -EA SilentlyContinue).FilesOnDemandEnabled;"
    "\"  Files On-Demand policy: \" + $(if($fod -eq 1){'ON'}elseif($fod -eq 0){'OFF (should be ON)'}else{'not set (OneDrive default: on)'})"
)


def ps_onedrive():
    return (
        "\"=== OneDrive process ===\";"
        "Get-Process OneDrive -EA SilentlyContinue | Select Id,@{n='MemMB';e={[math]::Round($_.WS/1MB)}} | Format-Table -Auto;"
        "\"=== OneDrive.exe version(s) ===\";"
        "@('C:\\Program Files\\Microsoft OneDrive\\OneDrive.exe','C:\\Program Files (x86)\\Microsoft OneDrive\\OneDrive.exe') +"
        " @(Get-ChildItem 'C:\\Users\\*\\AppData\\Local\\Microsoft\\OneDrive\\OneDrive.exe' -EA SilentlyContinue | ForEach-Object {$_.FullName}) |"
        " ForEach-Object { if(Test-Path $_){ $_ + ' -> ' + (Get-Item $_).VersionInfo.ProductVersion } };"
        "\"=== OneDrive accounts (per-user hives) ===\";"
        "Get-ChildItem 'Registry::HKEY_USERS' -EA SilentlyContinue | ForEach-Object {"
        " $base=\"Registry::$($_.PSChildName)\\Software\\Microsoft\\OneDrive\\Accounts\";"
        " if(Test-Path $base){ Get-ChildItem $base -EA SilentlyContinue | ForEach-Object {"
        " $a=Get-ItemProperty $_.PSPath -EA SilentlyContinue;"
        " if($a.UserEmail -or $a.UserFolder){ \"$($_.PSChildName): $($a.UserEmail)  folder=$($a.UserFolder)\" } } } };"
        "\"=== Recent OneDrive errors (7d) ===\";"
        "Get-WinEvent -FilterHashtable @{LogName='Application'; Level=1,2,3; StartTime=(Get-Date).AddDays(-7)} -EA SilentlyContinue |"
        " Where-Object {$_.ProviderName -match 'OneDrive' -or $_.Message -match 'OneDrive'} |"
        " Select -First 8 TimeCreated,Id,@{n='Msg';e={($_.Message -split \"`r?`n\")[0]}} | Format-Table -Auto -Wrap;"
        "\"=== Disk free (C:) ===\";"
        "Get-CimInstance Win32_LogicalDisk -Filter \"DeviceID='C:'\" | Select @{n='FreeGB';e={[math]::Round($_.FreeSpace/1GB)}},@{n='SizeGB';e={[math]::Round($_.Size/1GB)}} | Format-Table -Auto;"
        + _PS_SYNCED_LIBS
    )


def ps_network():
    return (
        "\"=== Adapters up ===\";"
        "Get-NetAdapter -EA SilentlyContinue | Where-Object Status -eq 'Up' | Select Name,InterfaceDescription,LinkSpeed | Format-Table -Auto;"
        "$vpn=Get-NetAdapter -EA SilentlyContinue | Where-Object { $_.Status -eq 'Up' -and ($_.InterfaceDescription -match 'VPN|GlobalProtect|AnyConnect|WireGuard|OpenVPN|TAP|Fortinet|ZScaler|Tailscale' -or $_.Name -match 'VPN') }; "
        "\"VPN adapter active: \" + ($(if ($vpn) { ($vpn.InterfaceDescription -join ', ') } else { 'none detected' }));"
        "\"=== Wi-Fi (current SSID / signal) ===\";"
        "$wifi = netsh wlan show interfaces 2>$null | Select-String '^\\s*(SSID|Signal|State|Channel|Radio type)\\s*:'; if ($wifi) { $wifi | ForEach-Object { $_.Line.Trim() } } else { 'no Wi-Fi interface (wired only)' };"
        "\"=== IP / DNS ===\";"
        "Get-NetIPConfiguration -EA SilentlyContinue | Select InterfaceAlias,@{n='IPv4';e={$_.IPv4Address.IPAddress}},@{n='GW';e={$_.IPv4DefaultGateway.NextHop}},@{n='DNS';e={$_.DNSServer.ServerAddresses -join ','}} | Format-Table -Auto;"
        "\"=== Recent NIC/Wi-Fi driver errors (7d) ===\";"
        "Get-WinEvent -FilterHashtable @{LogName='System'; Level=1,2,3; StartTime=(Get-Date).AddDays(-7)} -EA SilentlyContinue |"
        " Where-Object {$_.ProviderName -match 'e1d|Netwtw|NDIS|Tcpip|Dhcp'} |"
        " Group-Object ProviderName | Sort Count -Descending | Select Count,Name | Format-Table -Auto"
    )


def ps_drivers():
    return (
        "\"=== Devices in error/problem state NOW ===\";"
        "Get-PnpDevice -PresentOnly | Where-Object { $_.Status -ne 'OK' } | "
        "Select-Object Status,Class,FriendlyName,@{n='Code';e={(Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_ProblemCode -EA SilentlyContinue).Data}},InstanceId | "
        "Format-Table -Auto | Out-String -Width 130;"
        "\"=== Dock / USB component firmware (REV = bcdDevice) ===\";"
        "Get-PnpDevice -PresentOnly -EA SilentlyContinue | Where-Object { $_.InstanceId -match '^USB' } | ForEach-Object { $hw=(Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_HardwareIds -EA SilentlyContinue).Data | Select-Object -First 1; if ($hw -match 'REV_') { '{0,-40} {1}' -f $_.FriendlyName, $hw } } | Sort-Object -Unique | Select-Object -First 20;"
        "\"=== Battery (laptops) ===\";"
        "$b=Get-CimInstance Win32_Battery -EA SilentlyContinue; "
        "if ($b) { $fc=(Get-CimInstance -Namespace root/wmi -Class BatteryFullChargedCapacity -EA SilentlyContinue).FullChargedCapacity; $ds=(Get-CimInstance -Namespace root/wmi -Class BatteryStaticData -EA SilentlyContinue).DesignedCapacity; if ($fc -and $ds) { \"  Battery health: \" + [math]::Round(100*$fc/$ds) + '% of design (' + $fc + '/' + $ds + ' mWh)' } else { '  Battery present; capacity detail unavailable' } } else { '  No battery (desktop)' };"
        "\"=== Dell Command Update scan (pending driver/BIOS/firmware) ===\";"
        "$dcu='C:\\Program Files\\Dell\\CommandUpdate\\dcu-cli.exe';"
        "if (Test-Path $dcu) { & $dcu /scan -silent 2>&1 | Select-String -Pattern 'Driver|BIOS|Firmware|Chipset|Application|--|update' | Select-Object -First 25 | ForEach-Object { $_.Line.Trim() } } "
        "else { 'Dell Command Update (dcu-cli) not installed - driver scan skipped (non-Dell or DCU absent).' }"
    )


def ps_battery():
    return (
        "$b=Get-CimInstance Win32_Battery -EA SilentlyContinue;"
        "if (-not $b) { 'No battery detected (desktop or VM) - battery report N/A.' } else {"
        "  \"=== Battery summary ===\";"
        "  foreach ($x in $b) {"
        "    \"  Name: \" + $x.Name + '  (' + $x.DeviceID + ')';"
        "    $st=switch ($x.BatteryStatus) { 1 {'Discharging (on battery)'} 2 {'AC power'} 3 {'Fully charged'} 4 {'Low'} 5 {'Critical'} 6 {'Charging'} 7 {'Charging (high)'} 8 {'Charging (low)'} 9 {'Charging (critical)'} 11 {'Partially charged'} default {'Unknown'} };"
        "    \"  Charge: \" + $x.EstimatedChargeRemaining + '%   Status: ' + $st;"
        "    if ($x.EstimatedRunTime -and $x.EstimatedRunTime -lt 71582788) { \"  Est. runtime on battery: \" + $x.EstimatedRunTime + ' min' };"
        "  }"
        "  $bs=Get-CimInstance -Namespace root/wmi -Class BatteryStatus -EA SilentlyContinue | Select-Object -First 1;"
        "  if ($bs) { \"  AC connected: \" + [bool]$bs.PowerOnline + '   Charging: ' + [bool]$bs.Charging + '   Discharging: ' + [bool]$bs.Discharging + '   ChargeRate: ' + $bs.ChargeRate + ' mW';"
        "    if ($bs.PowerOnline -and -not $bs.Charging -and -not $bs.Discharging -and $x.EstimatedChargeRemaining -lt 99) { '  >> Plugged in but NOT charging - check the AC adapter (wattage/recognition), battery health below, and the battery connection.' } };"
        "  \"=== Health (live WMI) ===\";"
        "  $fc=(Get-CimInstance -Namespace root/wmi -Class BatteryFullChargedCapacity -EA SilentlyContinue).FullChargedCapacity;"
        "  $ds=(Get-CimInstance -Namespace root/wmi -Class BatteryStaticData -EA SilentlyContinue).DesignedCapacity;"
        "  if ($fc -and $ds) { \"  Health: \" + [math]::Round(100*$fc/$ds) + '% of design  (full-charge ' + $fc + ' / design ' + $ds + ' mWh)' } else { '  Capacity detail unavailable from WMI.' };"
        "  \"=== powercfg /batteryreport ===\";"
        "  $rpt=Join-Path $env:TEMP 'agent-batteryreport.html';"
        "  powercfg /batteryreport /output $rpt > $null 2>&1;"
        "  if (Test-Path $rpt) {"
        "    $t=((Get-Content $rpt -Raw) -replace '<[^>]+>',' ' -replace '&nbsp;',' ' -replace '\\s+',' ');"
        "    foreach ($k in 'DESIGN CAPACITY','FULL CHARGE CAPACITY','CYCLE COUNT') { if ($t -match ([regex]::Escape($k)+'\\s+([0-9][0-9,]*(?:\\s*mWh)?)')) { '  ' + $k + ': ' + $matches[1].Trim() } };"
        "    Remove-Item $rpt -Force -EA SilentlyContinue;"
        "  } else { '  powercfg battery report could not be generated.' };"
        "  '';"
        "  'NOTE: if the laptop will NOT power on, the fault is pre-boot and not readable here - read the chassis amber/white flash code and look it up in KB Dell Latitude Flash Codes (21000039674).'"
        "}"
    )


def ps_memory():
    return (
        "$os=Get-CimInstance Win32_OperatingSystem;"
        "\"RAM free/total MB: \" + [math]::Round($os.FreePhysicalMemory/1KB) + ' / ' + [math]::Round($os.TotalVisibleMemorySize/1KB);"
        "$cl=(Get-Counter '\\Memory\\Commit Limit' -EA SilentlyContinue).CounterSamples.CookedValue;"
        "$cb=(Get-Counter '\\Memory\\Committed Bytes' -EA SilentlyContinue).CounterSamples.CookedValue;"
        "if($cl){\"Commit MB: \" + [math]::Round($cb/1MB) + ' / ' + [math]::Round($cl/1MB) + ' limit (' + [math]::Round(100*$cb/$cl) + '%)'};"
        "$pf=Get-CimInstance Win32_PageFileUsage -EA SilentlyContinue;"
        "if($pf){\"PageFile MB: \" + $pf.CurrentUsage + ' used / ' + $pf.AllocatedBaseSize + ' alloc, peak ' + $pf.PeakUsage};"
        "$n=(Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Resource-Exhaustion-Detector'; StartTime=(Get-Date).AddDays(-14)} -EA SilentlyContinue | Measure-Object).Count;"
        "\"Low-virtual-memory events (14d): \" + $n;"
        "\"=== Top memory (grouped working set MB; sum across all child processes) ===\";"
        "Get-Process | Group-Object ProcessName | ForEach-Object { [PSCustomObject]@{Name=$_.Name; MB=[math]::Round((($_.Group|Measure-Object WorkingSet64 -Sum).Sum)/1MB); Procs=$_.Count} } | Sort-Object MB -Descending | Select-Object -First 12 | Format-Table -Auto"
    )


def ps_health():
    return (
        "$os=Get-CimInstance Win32_OperatingSystem;"
        "$up=(Get-Date) - $os.LastBootUpTime;"
        "\"Uptime: \" + $up.Days + ' days ' + $up.Hours + ' hours ' + $up.Minutes + ' minutes';"
        "\"=== Disks ===\";"
        "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Select DeviceID,@{n='FreeGB';e={[math]::Round($_.FreeSpace/1GB)}},@{n='SizeGB';e={[math]::Round($_.Size/1GB)}} | Format-Table -Auto;"
        "\"Memory free/total MB: \" + [math]::Round($os.FreePhysicalMemory/1KB) + ' / ' + [math]::Round($os.TotalVisibleMemorySize/1KB);"
        "$cl=(Get-Counter '\\Memory\\Commit Limit' -EA SilentlyContinue).CounterSamples.CookedValue;"
        "$cb=(Get-Counter '\\Memory\\Committed Bytes' -EA SilentlyContinue).CounterSamples.CookedValue;"
        "if($cl){\"Commit MB: \" + [math]::Round($cb/1MB) + ' / ' + [math]::Round($cl/1MB) + ' limit'};"
        "\"=== Top memory (grouped working set MB) ===\";"
        "Get-Process | Group-Object ProcessName | ForEach-Object { [PSCustomObject]@{Name=$_.Name; MB=[math]::Round((($_.Group|Measure-Object WorkingSet64 -Sum).Sum)/1MB); Procs=$_.Count} } | Sort-Object MB -Descending | Select-Object -First 6 | Format-Table -Auto;"
        "\"=== Disk health ===\";"
        "Get-PhysicalDisk | Select FriendlyName,MediaType,HealthStatus,@{n='Wear%';e={($_ | Get-StorageReliabilityCounter -EA SilentlyContinue).Wear}} | Format-Table -Auto;"
        "\"=== Pending reboot flags ===\";"
        "\"  CBS: \" + (Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Component Based Servicing\\RebootPending');"
        "\"  WindowsUpdate: \" + (Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\WindowsUpdate\\Auto Update\\RebootRequired');"
        "try { $wu=(New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher().Search('IsInstalled=0 and IsHidden=0').Updates.Count; \"  Pending Windows Updates: \" + $wu } catch { '  Pending Windows Updates: (unavailable)' };"
        + _PS_SYNCED_LIBS
    )





BUILDERS = {
    "eventlogs": ("eventlogs", 40),
    "sound": (ps_sound, 38),
    "onedrive": (ps_onedrive, 40),
    "network": (ps_network, 38),
    "health": (ps_health, 35),
    "memory": (ps_memory, 35),
    "drivers": (ps_drivers, 90),
    "battery": (ps_battery, 60),
}


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        sys.exit(__doc__)
    check, target = args[0], args[1]
    days, provider, timeout, ticket, dry = 7, None, None, None, False
    i = 2
    while i < len(args):
        if args[i] == "--days":
            days = int(args[i + 1]); i += 2
        elif args[i] == "--provider":
            provider = args[i + 1]; i += 2
        elif args[i] == "--timeout":
            timeout = int(args[i + 1]); i += 2
        elif args[i] == "--ticket":
            ticket = args[i + 1]; i += 2
        elif args[i] == "--dry-run-note":
            dry = True; i += 1
        else:
            i += 1
    if check not in BUILDERS:
        sys.exit("unknown check '" + check + "'. Options: " + ", ".join(BUILDERS))
    if check == "eventlogs":
        ps = ps_eventlogs(days, provider); default_to = 40
    else:
        builder, default_to = BUILDERS[check]
        ps = builder()
    to = timeout or default_to
    r = subprocess.run(["python3", SC, "run", target, ps, "--shell", "powershell", "--timeout", str(to)],
                       capture_output=True, text=True, timeout=to + 20)
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit(r.returncode)


if __name__ == "__main__":
    main()
