param(
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action
)

$ErrorActionPreference = "Stop"
$serverScript = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "server.py"))
$pidFile = Join-Path $PSScriptRoot ".dashboard-server.pid"

function Get-DashboardServerProcesses {
    @(
        Get-CimInstance -ClassName Win32_Process | Where-Object {
            $_.Name -in @("python.exe", "pythonw.exe") -and
            $_.CommandLine -and
            $_.CommandLine.IndexOf($serverScript, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
        }
    )
}

function Get-DashboardServerProcessById([int]$ProcessId) {
    $process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if (
        $process -and
        $process.Name -in @("python.exe", "pythonw.exe") -and
        $process.CommandLine -and
        $process.CommandLine.IndexOf($serverScript, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    ) {
        return $process
    }
    return $null
}

function Set-DashboardServerProcessId([int]$ProcessId) {
    [System.IO.File]::WriteAllText($pidFile, [string]$ProcessId)
}

function Get-TrackedDashboardServer {
    if (Test-Path -LiteralPath $pidFile) {
        $trackedId = 0
        $storedId = [System.IO.File]::ReadAllText($pidFile).Trim()
        if ([int]::TryParse($storedId, [ref]$trackedId)) {
            $process = Get-DashboardServerProcessById $trackedId
            if ($process) {
                return $process
            }
        }
        Remove-Item -LiteralPath $pidFile -Force
    }

    $processes = @(Get-DashboardServerProcesses)
    if ($processes.Count -gt 1) {
        throw "Es laufen mehrere Server aus diesem Ordner. Beende sie jeweils in ihrem Serverfenster mit Strg+C."
    }
    if ($processes.Count -eq 1) {
        Set-DashboardServerProcessId ([int]$processes[0].ProcessId)
        return $processes[0]
    }
    return $null
}

function Get-PythonExecutable {
    $candidates = @()
    $launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($launcher) {
        $resolved = & $launcher.Source -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $resolved) {
            $candidates += ([string]$resolved).Trim()
        }
    }

    $python = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($python) {
        $candidates += $python.Source
    }

    foreach ($candidate in $candidates) {
        $version = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $version) {
            continue
        }
        $parts = ([string]$version).Trim().Split(".")
        if ($parts.Count -eq 2 -and ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10))) {
            return $candidate
        }
    }

    throw "Python 3.10 oder neuer wurde nicht gefunden. Bitte Python installieren und erneut starten."
}

function Start-DashboardServer {
    $running = Get-TrackedDashboardServer
    if ($running) {
        Write-Host "Der Server laeuft bereits (PID $($running.ProcessId))." -ForegroundColor Yellow
        return
    }

    $pythonExecutable = Get-PythonExecutable
    $process = Start-Process `
        -FilePath $pythonExecutable `
        -ArgumentList ('"{0}"' -f $serverScript) `
        -WorkingDirectory $PSScriptRoot `
        -PassThru `
        -WindowStyle Normal
    Set-DashboardServerProcessId ([int]$process.Id)

    Start-Sleep -Milliseconds 800
    $started = Get-DashboardServerProcessById ([int]$process.Id)
    if (-not $started) {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        throw "Der Server wurde gestartet, ist aber sofort wieder beendet worden. Pruefe das Serverfenster auf Fehlermeldungen."
    }
    Write-Host "Server gestartet (PID $($started.ProcessId)). Das Dashboard sollte sich im Browser oeffnen." -ForegroundColor Green
}

function Stop-DashboardServer {
    $running = Get-TrackedDashboardServer
    if (-not $running) {
        Write-Host "Der Server laeuft nicht." -ForegroundColor Yellow
        return
    }

    $processId = [int]$running.ProcessId
    Stop-Process -Id $processId -Force
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (-not (Get-DashboardServerProcessById $processId)) {
            break
        }
        Start-Sleep -Milliseconds 100
    }
    if (Get-DashboardServerProcessById $processId) {
        throw "Der Serverprozess (PID $processId) konnte nicht beendet werden."
    }
    if (Test-Path -LiteralPath $pidFile) {
        Remove-Item -LiteralPath $pidFile -Force
    }
    Write-Host "Server beendet." -ForegroundColor Green
}

function Show-DashboardStatus {
    $running = Get-TrackedDashboardServer
    if ($running) {
        Write-Host "Status: Server laeuft (PID $($running.ProcessId))." -ForegroundColor Green
    }
    else {
        Write-Host "Status: Server ist beendet." -ForegroundColor Yellow
    }
}

function Invoke-DashboardAction([string]$SelectedAction) {
    switch ($SelectedAction) {
        "start" { Start-DashboardServer }
        "stop" { Stop-DashboardServer }
        "restart" {
            Stop-DashboardServer
            Start-DashboardServer
        }
        "status" { Show-DashboardStatus }
    }
}

if ($Action) {
    Invoke-DashboardAction $Action
    exit 0
}

while ($true) {
    Clear-Host
    Write-Host "Scoover - Dashboard-Steuerung" -ForegroundColor Cyan
    try {
        Show-DashboardStatus
    }
    catch {
        Write-Host "Status nicht verfuegbar: $($_.Exception.Message)" -ForegroundColor Red
    }
    Write-Host ""
    Write-Host "1  Server starten"
    Write-Host "2  Server stoppen"
    Write-Host "3  Server neu starten"
    Write-Host "4  Beenden"
    $choice = Read-Host "Auswahl"

    if ($choice -eq "4") {
        break
    }

    try {
        switch ($choice) {
            "1" { Start-DashboardServer }
            "2" { Stop-DashboardServer }
            "3" {
                Stop-DashboardServer
                Start-DashboardServer
            }
            default { Write-Host "Bitte 1, 2, 3 oder 4 waehlen." -ForegroundColor Yellow }
        }
    }
    catch {
        Write-Host "Fehler: $($_.Exception.Message)" -ForegroundColor Red
    }
    [void](Read-Host "Weiter mit Enter")
}
