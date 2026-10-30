# Run once after discord.local.json is configured. Requires no administrator rights.
$ErrorActionPreference = 'Stop'
$taskName = 'WTStat Discord Bot'
$launcher = Join-Path $PSScriptRoot 'start_discord_bot.bat'
if (-not (Test-Path -LiteralPath $launcher)) { throw "找不到 $launcher" }
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'discord.local.json')) -and -not $env:DISCORD_BOT_TOKEN) {
    throw '请先创建 discord.local.json 并填写 Bot Token。'
}
$action = New-ScheduledTaskAction -Execute $env:ComSpec -Argument ('/c "' + $launcher + '"') -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "已创建登录时自动启动任务：$taskName"
Write-Host '现在可用 start_discord_bot.bat 启动；下次登录 Windows 时任务会自动启动。'
