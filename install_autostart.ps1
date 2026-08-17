# Register the watcher as a Scheduled Task so it starts at logon and keeps
# running without a terminal window open.
#
#     powershell -ExecutionPolicy Bypass -File install_autostart.ps1
#     powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Remove
#
# Why a Scheduled Task rather than a Windows service: the watcher drives a real
# Chrome profile that belongs to your user account. A service running as
# SYSTEM would have a different profile and would not be logged in to Amazon.

param(
    [switch]$Remove,
    [string]$TaskName = "AmazonShiftWatcher",
    [string]$Config = "config.yaml"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat = Join-Path $root "run_watcher.bat"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        "Removed scheduled task '$TaskName'."
    } else {
        "No scheduled task named '$TaskName'."
    }
    return
}

if (-not (Test-Path $bat)) { throw "run_watcher.bat not found next to this script" }

$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"$bat`" $Config" -WorkingDirectory $root

# At logon rather than at boot: the browser profile holding your Amazon login
# lives in your user account, so there is nothing to run before you log in.
$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)   # 0 = never kill it

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Watches hiring.amazon.ca for shifts and holds them." | Out-Null

@"
Registered '$TaskName'.

  Start now :  Start-ScheduledTask -TaskName $TaskName
  Stop      :  Stop-ScheduledTask  -TaskName $TaskName
  Status    :  Get-ScheduledTask   -TaskName $TaskName | Get-ScheduledTaskInfo
  Remove    :  powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Remove
  Logs      :  logs\watcher.log

IMPORTANT: sleep still stops it. A sleeping laptop catches nothing, and these
postings last about a minute. Set the machine to never sleep while plugged in:
  powercfg /change standby-timeout-ac 0
"@
