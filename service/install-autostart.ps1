# Registers the DocumentReader Document Reader OCR service to start at boot.
# Run once, elevated:  powershell -ExecutionPolicy Bypass -File install-autostart.ps1
# Remove with:         Unregister-ScheduledTask -TaskName "DocReaderOCRService" -Confirm:$false

$python = "<hermes-home>\hermes-agent\venv\Scripts\python.exe"
$script = "<hermes-home>\scripts\anydoc-ocr-viewer\ocr_service.py"
$logDir = "<hermes-home>\scripts\anydoc-ocr-viewer\service"

if (-not (Test-Path $python)) { throw "venv python not found: $python" }
if (-not (Test-Path $script)) { throw "service script not found: $script" }

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-u `"$script`" --port 8899" `
    -WorkingDirectory (Split-Path $script)

$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT30S"   # let the network + tailscale come up first

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest

Register-ScheduledTask -TaskName "DocReaderOCRService" `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force

# hand ownership over: stop any manually-started instance holding the port
$conn = Get-NetTCPConnection -LocalPort 8899 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    Write-Host "Stopping existing instance (PID $($conn.OwningProcess)) so the task owns the service..."
    try { Stop-Process -Id $conn.OwningProcess -Force -ErrorAction Stop } catch {}
    Start-Sleep -Seconds 2
}

Start-ScheduledTask -TaskName "DocReaderOCRService"
Start-Sleep -Seconds 5
$r = try { (Invoke-WebRequest -Uri "http://localhost:8899/api/state" -UseBasicParsing -TimeoutSec 5).StatusCode } catch { "DOWN" }
Write-Host "Task registered. Service check: $r  (200 = running)"
Write-Host "It will now start ~30s after every boot and auto-restart up to 10x if it crashes."
