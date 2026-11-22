<#
AI Trading Desk launcher (2026-11-21, the user: "start the app without VS Code - when I open it, it runs, or a
start / stop button").

Opens a small window that starts what is not running yet - MetaTrader 5, the backend (uvicorn on 8001, with
--reload as in development) and the desk's screen server (vite on 5173) - then opens the desk in its app window
(the web app Chrome installed for it, see Open-Desk). The
window shows what runs, the LIVE state and the open trades, with Open desk / Start / Stop.

Closing the window does NOT stop the app: the backend is what manages the trades (strategy, close rules, follow
the target, daily loss pause), so turning it off is the explicit Stop. MetaTrader 5 is never closed.

Runs from "AI Trading Desk" on the desktop (launcher\TradingDesk.vbs: no console flash). Also:
  TradingDesk.ps1 -Status             what runs now (and what Stop would stop)
  TradingDesk.ps1 -Stop               stop the app from a console (asks first)
  TradingDesk.ps1 -Snapshot x.png     draw the window to a PNG and exit (layout check; starts nothing)
  TradingDesk.ps1 -TestFrontendPort N start the screen server on port N, check it, stop it (launch check)
Logs: logs\backend.log, logs\frontend.log at the project root.
ASCII only: Windows PowerShell 5.1 reads a BOM-less script as ANSI, so special characters are built with [char].
#>
param(
  [switch]$Stop,
  [switch]$Status,
  [string]$Snapshot = "",
  [int]$TestFrontendPort = 0
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $Root "backend"
$FrontendDir = Join-Path $Root "frontend"
# Logs outside OneDrive (2026-11-22): OneDrive re-uploaded the growing log - and the database - all the time.
$LogDir = Join-Path $env:LOCALAPPDATA "AITradingDesk\logs"
$Py = Join-Path $BackendDir ".venv\Scripts\python.exe"
$Runner = Join-Path $PSScriptRoot "run_backend.py"
$Vite = Join-Path $FrontendDir "node_modules\vite\bin\vite.js"
$Node = $null
$nodeCmd = Get-Command node.exe -ErrorAction SilentlyContinue
if ($nodeCmd) { $Node = $nodeCmd.Source } elseif (Test-Path "C:\Program Files\nodejs\node.exe") { $Node = "C:\Program Files\nodejs\node.exe" }
$Mt5 = Get-ChildItem "C:\Program Files\MetaTrader 5*\terminal64.exe" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
$Chrome = @("C:\Program Files\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
            "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe") | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
$BackendPort = 8001
$FrontPort = 5173
$Api = "http://127.0.0.1:$BackendPort"
$DeskUrl = "http://localhost:$FrontPort/"
$Dot = [string][char]0x25CF
$Mid = [string][char]0x00B7
$Ell = [string][char]0x2026

# --------------------------------------------------------------------------------------------------------------
#  What runs
# --------------------------------------------------------------------------------------------------------------

function Test-Listening([int]$Port) {
  return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Get-DeskState {
  $s = @{ Mt5 = $false; BackendPort = $false; BackendOk = $false; Reconfirm = $false; BrokerEnv = $null; Kill = $false
          FrontPort = $false; Trades = $null; BackendProc = $false; FrontProc = $false }
  $s.Mt5 = [bool](Get-Process terminal64 -ErrorAction SilentlyContinue)
  $s.BackendPort = Test-Listening $BackendPort
  $s.FrontPort = Test-Listening $FrontPort
  # The processes too: a starting backend has no listening port for its first ~15-20 s (uvicorn's reloader binds
  # the port, its worker only listens once the app has started) - judged by the port alone, a second Start then
  # started a second backend (2026-11-21 10:14; the scheduler lock put it in standby).
  # Process lists (WMI) only while a port is not up yet: asked every 3 s they cost the window ~13% of a core.
  $s.BackendProc = if ($s.BackendPort) { $true } else { (Get-BackendRoots).Count -gt 0 }
  $s.FrontProc = if ($s.FrontPort) { $true } else { (Get-FrontendRoots).Count -gt 0 }
  if ($s.BackendPort) {
    try {
      $set = Invoke-RestMethod "$Api/api/settings" -TimeoutSec 3
      $s.BackendOk = $true
      $s.Reconfirm = [bool]$set.live_re_confirm_required
      $s.BrokerEnv = $set.app.broker_env
      $s.Kill = [bool]$set.app.kill_switch_engaged
    } catch {}
    if ($s.BackendOk) {
      try { $s.Trades = @(Invoke-RestMethod "$Api/api/broker/positions" -TimeoutSec 4).Count } catch {}
    }
  }
  return $s
}

# The roots of the backend / screen-server process trees: started by this launcher OR from a VS Code terminal.
function Get-Roots($procs) {
  $ids = @($procs | ForEach-Object { $_.ProcessId })
  return @($procs | Where-Object { $ids -notcontains $_.ParentProcessId })
}

function Get-BackendRoots {
  $p = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" |
         Where-Object { $_.CommandLine -like "*app.main:app*" -and $_.CommandLine -like "*--port $BackendPort*" })
  return Get-Roots $p
}

function Get-FrontendRoots([int]$Port = 0) {
  $p = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
         Where-Object { $_.CommandLine -like "*$FrontendDir*vite*" -and ($Port -eq 0 -or $_.CommandLine -like "*--port $Port*") })
  return Get-Roots $p
}

# --------------------------------------------------------------------------------------------------------------
#  Start / stop
# --------------------------------------------------------------------------------------------------------------

function Step-Log([string]$Path) {
  # keep one previous file once a log passes 20 MB
  try { if ((Test-Path $Path) -and (Get-Item $Path).Length -gt 20MB) { Move-Item -Force $Path "$Path.1" } } catch {}
}

function Start-Mt5 {
  if (Get-Process terminal64 -ErrorAction SilentlyContinue) { return $null }
  if (-not $Mt5) { return "MetaTrader 5 was not found under C:\Program Files - start it yourself." }
  Start-Process -FilePath $Mt5
  return $null
}

function Start-Backend {
  # Not when one runs or is starting (its port only listens once the app is up - see Get-DeskState).
  if ((Test-Listening $BackendPort) -or (Get-BackendRoots).Count -gt 0) { return $null }
  if (-not (Test-Path $Py)) { return "The backend's Python was not found: $Py" }
  New-Item -ItemType Directory -Force $LogDir | Out-Null
  $log = Join-Path $LogDir "backend.log"
  Step-Log $log
  Add-Content -Path $log -Value ("`r`n===== started by the launcher " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " =====")
  $env:PYTHONUNBUFFERED = "1"
  $env:TRADING_DESK_LOG = $log
  # Same command as in development (uvicorn --reload --reload-dir app on 8001), through run_backend.py so it can
  # run hidden with its log in a file and still reload. No access log: one line per desk poll filled the file.
  $argv = @("`"$Runner`"", "app.main:app", "--host", "127.0.0.1", "--port", "$BackendPort", "--reload",
            "--reload-dir", "app", "--no-access-log")
  Start-Process -FilePath $Py -ArgumentList $argv -WorkingDirectory $BackendDir -WindowStyle Hidden
  return $null
}

function Start-Frontend([int]$Port = $FrontPort, [string]$LogName = "frontend") {
  $mine = if ($Port -eq $FrontPort) { Get-FrontendRoots } else { Get-FrontendRoots -Port $Port }
  if ((Test-Listening $Port) -or $mine.Count -gt 0) { return $null }
  if (-not $Node) { return "Node.js was not found." }
  New-Item -ItemType Directory -Force $LogDir | Out-Null
  $argv = @("`"$Vite`"", "--port", "$Port", "--strictPort")
  $p = Start-Process -FilePath $Node -ArgumentList $argv -WorkingDirectory $FrontendDir -WindowStyle Hidden -PassThru `
         -RedirectStandardOutput (Join-Path $LogDir "$LogName.log") -RedirectStandardError (Join-Path $LogDir "$LogName.err.log")
  return $p
}

# The desk in its own app window (2026-11-22, the user: "don't open it in the browser for me to click Open in app -
# open the app straight away"): the web app Chrome installed for it ("Open in app"), found by its Start menu
# shortcut. Without one, Chrome's app mode on the desk's address; without Chrome, the default browser.
function Find-DeskApp {
  $dir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Chrome Apps"
  return Get-ChildItem $dir -Filter "*Trading Desk*.lnk" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
}

function Open-Desk {
  $app = Find-DeskApp
  if ($app) { Start-Process -FilePath $app }
  elseif ($Chrome) { Start-Process -FilePath $Chrome -ArgumentList "--app=$DeskUrl" }
  else { Start-Process $DeskUrl }
}

function Stop-Desk([scriptblock]$Pump) {
  # 1. Ask the backend to stop ITSELF: it stops starting new work and lets an order being sent finish and be
  #    recorded first (killing it outright could land mid order_send - same as the old Stop .bat).
  if (Test-Listening $BackendPort) {
    try { Invoke-RestMethod -Method Post "$Api/api/app/shutdown" -Headers @{ "X-Desk" = "1" } -TimeoutSec 10 | Out-Null } catch {}
    $t0 = Get-Date
    while (((Get-Date) - $t0).TotalSeconds -lt 30) {
      try { Invoke-RestMethod "$Api/health" -TimeoutSec 2 | Out-Null } catch { break }
      Start-Sleep -Milliseconds 500
      if ($Pump) { & $Pump }
    }
  }
  # 2. Then the process trees (with --reload the reloader keeps the port after its worker has stopped).
  foreach ($p in (Get-BackendRoots)) { & taskkill.exe /PID $p.ProcessId /T /F 2>$null | Out-Null }
  foreach ($p in (Get-FrontendRoots)) { & taskkill.exe /PID $p.ProcessId /T /F 2>$null | Out-Null }
  $t0 = Get-Date
  while (((Get-Date) - $t0).TotalSeconds -lt 10 -and ((Test-Listening $BackendPort) -or (Test-Listening $FrontPort))) {
    Start-Sleep -Milliseconds 500
    if ($Pump) { & $Pump }
  }
  return @{ Backend = -not (Test-Listening $BackendPort); Frontend = -not (Test-Listening $FrontPort) }
}

function Get-StopWarning($s) {
  $msg = "Stop the AI Trading Desk?`r`n`r`n"
  if ($s.Trades -gt 0) {
    $msg += "$($s.Trades) trade(s) are open. Their stop-loss and take-profit stay at the broker, but the app stops " +
            "managing them (strategy close rules, follow the target, daily loss pause) until you start it again.`r`n`r`n"
  }
  return $msg + "MetaTrader 5 stays open. After Start, LIVE has to be typed again in the desk."
}

# --------------------------------------------------------------------------------------------------------------
#  Console modes
# --------------------------------------------------------------------------------------------------------------

if ($Status) {
  $s = Get-DeskState
  "MetaTrader 5   : " + $(if ($s.Mt5) { "running" } else { "not running" }) + "   ($Mt5)"
  "Backend :$BackendPort  : " + $(if ($s.BackendOk) { "answering, broker $($s.BrokerEnv), LIVE re-type needed: $($s.Reconfirm), open trades: $($s.Trades)" } elseif ($s.BackendPort) { "port busy, not answering" } else { "stopped" })
  "Desk    :$FrontPort  : " + $(if ($s.FrontPort) { "running" } else { "stopped" })
  "Stop would end : backend " + ((Get-BackendRoots | ForEach-Object { "$($_.ProcessId)" }) -join ",") +
    " | screen server " + ((Get-FrontendRoots | ForEach-Object { "$($_.ProcessId)" }) -join ",")
  "Python $Py | Node $Node | Chrome $Chrome"
  "Desk app (opened by Open desk): " + $(if (Find-DeskApp) { Find-DeskApp } else { "none installed - Chrome app mode on $DeskUrl" })
  exit 0
}

if ($TestFrontendPort -gt 0) {
  if (Test-Listening $TestFrontendPort) { "port $TestFrontendPort is busy - pick another"; exit 2 }
  $p = Start-Frontend -Port $TestFrontendPort -LogName "frontend-test"
  $ok = $false
  foreach ($i in 1..80) {
    Start-Sleep -Milliseconds 500
    try {
      $r = Invoke-WebRequest -UseBasicParsing "http://localhost:$TestFrontendPort/" -TimeoutSec 3
      if ($r.StatusCode -eq 200 -and $r.Content -like '*id="root"*') { $ok = $true; break }
    } catch {}
  }
  "screen server on $TestFrontendPort answered: $ok (after about $([int]($i / 2)) s)"
  $roots = Get-FrontendRoots -Port $TestFrontendPort
  "test roots found by the stop logic: " + (($roots | ForEach-Object { "$($_.ProcessId)" }) -join ",") + " (started: $($p.Id))"
  foreach ($r in $roots) { & taskkill.exe /PID $r.ProcessId /T /F 2>$null | Out-Null }
  Start-Sleep 1
  "port $TestFrontendPort free after stop: " + (-not (Test-Listening $TestFrontendPort))
  Remove-Item (Join-Path $LogDir "frontend-test*.log") -ErrorAction SilentlyContinue
  exit $(if ($ok) { 0 } else { 1 })
}

if ($Stop) {
  $s = Get-DeskState
  if (-not ($s.BackendPort -or $s.FrontPort)) { "AI Trading Desk is not running."; exit 0 }
  (Get-StopWarning $s) -replace "`r`n", "`n"
  $a = Read-Host "Stop it? (y/n)"
  if ($a -notmatch '^(y|yes)$') { "Nothing stopped."; exit 0 }
  "Asking the app to stop - an order being sent finishes first ..."
  $r = Stop-Desk
  if ($r.Backend -and $r.Frontend) { "Stopped. Open trades are no longer managed by the app (broker stop-loss / take-profit stay)." }
  else { "COULD NOT STOP everything (backend stopped: $($r.Backend), screen server stopped: $($r.Frontend)). Try again as administrator." }
  exit 0
}

# --------------------------------------------------------------------------------------------------------------
#  The window
# --------------------------------------------------------------------------------------------------------------

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$mutex = $null
if (-not $Snapshot) {
  $created = $false
  $mutex = New-Object System.Threading.Mutex($true, "Local\AITradingDeskLauncher", [ref]$created)
  if (-not $created) {
    [System.Windows.Forms.MessageBox]::Show("The AI Trading Desk window is already open - look for it in the taskbar.",
                                            "AI Trading Desk") | Out-Null
    exit 0
  }
}

function Color([int]$r, [int]$g, [int]$b) { return [System.Drawing.Color]::FromArgb($r, $g, $b) }
$cBg = Color 14 17 22; $cFg = Color 229 231 235; $cMuted = Color 140 148 160; $cGrey = Color 90 96 108
$cGreen = Color 34 197 94; $cRed = Color 239 68 68; $cAmber = Color 245 158 11; $cBrand = Color 79 70 229
$cBtn = Color 38 43 52

$form = New-Object System.Windows.Forms.Form
$form.Text = "AI Trading Desk"
$form.ClientSize = New-Object System.Drawing.Size(480, 300)
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox = $false
$form.StartPosition = "CenterScreen"
$form.BackColor = $cBg
$form.ForeColor = $cFg
$form.Font = New-Object System.Drawing.Font("Segoe UI", 10)
foreach ($ico in @((Join-Path $PSScriptRoot "desk.ico"), (Join-Path $FrontendDir "public\favicon.ico"))) {
  if (Test-Path $ico) { try { $form.Icon = New-Object System.Drawing.Icon($ico); break } catch {} }
}

function New-Label([string]$text, [int]$x, [int]$y, [int]$w = 0, [object]$color = $null, [float]$size = 10, [string]$face = "Segoe UI") {
  $l = New-Object System.Windows.Forms.Label
  $l.Text = $text
  $l.Location = New-Object System.Drawing.Point($x, $y)
  if ($w -gt 0) { $l.Size = New-Object System.Drawing.Size($w, [int]($size * 2.4)) } else { $l.AutoSize = $true }
  $l.Font = New-Object System.Drawing.Font($face, $size)
  if ($color) { $l.ForeColor = $color }
  $l.BackColor = [System.Drawing.Color]::Transparent
  $form.Controls.Add($l)
  return $l
}

$null = New-Label "AI Trading Desk" 18 14 0 $cFg 15 "Segoe UI Semibold"
$lblSub = New-Label "" 20 46 440 $cMuted 9

function New-Row([int]$y, [string]$name) {
  $d = New-Label $Dot 18 ($y - 5) 0 $cGrey 14
  $null = New-Label $name 44 $y 0 $cFg 10 "Segoe UI Semibold"
  $t = New-Label "" 168 $y 300 $cMuted 10
  return @{ Dot = $d; Text = $t }
}
$rowMt5 = New-Row 80 "MetaTrader 5"
$rowBe = New-Row 112 "Backend"
$rowFe = New-Row 144 "Desk screen"
$lblTrades = New-Label "" 44 176 420 $cFg 10

function New-Button([string]$text, [int]$x, [int]$w, [object]$back) {
  $b = New-Object System.Windows.Forms.Button
  $b.Text = $text
  $b.Location = New-Object System.Drawing.Point($x, 212)
  $b.Size = New-Object System.Drawing.Size($w, 36)
  $b.FlatStyle = "Flat"
  $b.FlatAppearance.BorderSize = 0
  $b.BackColor = $back
  $b.ForeColor = [System.Drawing.Color]::White
  $b.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 10)
  $b.Cursor = [System.Windows.Forms.Cursors]::Hand
  $form.Controls.Add($b)
  return $b
}
$btnOpen = New-Button "Open desk" 18 150 $cBrand
$btnStart = New-Button "Start" 180 120 $cBtn
$btnStop = New-Button "Stop" 312 150 (Color 127 29 29)

$null = New-Label "Closing this window keeps the app running - it manages your trades. Stop turns it off." 18 260 446 $cMuted 8.5
$lnkLogs = New-Object System.Windows.Forms.LinkLabel
$lnkLogs.Text = "logs"
$lnkLogs.Location = New-Object System.Drawing.Point(18, 278)
$lnkLogs.AutoSize = $true
$lnkLogs.Font = New-Object System.Drawing.Font("Segoe UI", 8.5)
$lnkLogs.LinkColor = $cMuted
$lnkLogs.ActiveLinkColor = $cFg
$lnkLogs.BackColor = [System.Drawing.Color]::Transparent
$form.Controls.Add($lnkLogs)

$script:busy = $false
$script:starting = $false
$script:startedAt = $null
$script:openWhenUp = $false
$script:last = $null

function Set-Row($row, $color, [string]$text) { $row.Dot.ForeColor = $color; $row.Text.Text = $text }

function Update-View {
  $s = Get-DeskState
  $script:last = $s
  $starting = $script:starting -and $script:startedAt -and (((Get-Date) - $script:startedAt).TotalSeconds -lt 150)

  if ($s.Mt5) { Set-Row $rowMt5 $cGreen "running" }
  elseif ($starting) { Set-Row $rowMt5 $cAmber ("starting" + $Ell) }
  else { Set-Row $rowMt5 $cRed "not running" }

  if ($s.BackendOk) {
    if ($s.Kill) { Set-Row $rowBe $cRed "running $Mid kill-switch ON" }
    elseif ($s.BrokerEnv -eq "live" -and $s.Reconfirm) { Set-Row $rowBe $cAmber "running $Mid press Confirm LIVE in the desk" }
    elseif ($s.BrokerEnv -eq "live") { Set-Row $rowBe $cGreen "running $Mid LIVE on" }
    else { Set-Row $rowBe $cGreen "running $Mid $($s.BrokerEnv)" }
  }
  elseif ($starting -or ($s.BackendProc -and -not $s.BackendPort)) { Set-Row $rowBe $cAmber ("starting" + $Ell + " (about 20 s)") }
  elseif ($s.BackendPort) { Set-Row $rowBe $cAmber "not answering - Stop, then Start" }
  else { Set-Row $rowBe $cRed "stopped" }

  if ($s.FrontPort) { Set-Row $rowFe $cGreen "running $Mid localhost:$FrontPort" }
  elseif ($starting -or $s.FrontProc) { Set-Row $rowFe $cAmber ("starting" + $Ell) }
  else { Set-Row $rowFe $cRed "stopped" }

  if ($s.BackendOk -and $null -ne $s.Trades) { $lblTrades.Text = "Open trades: $($s.Trades)" } else { $lblTrades.Text = "" }

  $allUp = $s.Mt5 -and $s.BackendOk -and $s.FrontPort
  if ($allUp) { $lblSub.Text = "Everything is running." }
  elseif ($starting) { $lblSub.Text = "Starting - the desk opens in its app window when it is ready." }
  elseif (-not ($s.BackendPort -or $s.FrontPort)) { $lblSub.Text = "The app is off. Start turns it on." }
  else { $lblSub.Text = "Part of the app is not running - Start starts the rest." }

  # Start stays off while a start is under way (the second backend of 2026-11-21 came from a Start during one).
  $startingNow = $starting -or ($s.BackendProc -and -not $s.BackendOk -and -not $s.BackendPort)
  $btnStart.Enabled = (-not $allUp) -and (-not $script:busy) -and (-not $startingNow)
  $btnStop.Enabled = ($s.BackendPort -or $s.FrontPort -or $s.BackendProc -or $s.FrontProc) -and (-not $script:busy)
  $btnOpen.Enabled = $s.FrontPort -and (-not $script:busy)
  foreach ($b in @($btnOpen, $btnStart, $btnStop)) { if ($b.Enabled) { $b.ForeColor = [System.Drawing.Color]::White } else { $b.ForeColor = $cGrey } }

  if ($script:openWhenUp -and $s.BackendOk -and $s.FrontPort) {
    $script:openWhenUp = $false
    $script:starting = $false
    Open-Desk
  }
  if ($script:starting -and -not $starting) { $script:starting = $false; $script:openWhenUp = $false }
}

function Start-All {
  $script:busy = $true
  $script:starting = $true
  $script:startedAt = Get-Date
  $problems = @()
  foreach ($r in @((Start-Mt5), (Start-Backend))) { if ($r -is [string]) { $problems += $r } }
  $fr = Start-Frontend
  if ($fr -is [string]) { $problems += $fr }
  $script:openWhenUp = $true
  $script:busy = $false
  if ($problems.Count) { [System.Windows.Forms.MessageBox]::Show($form, ($problems -join "`r`n"), "AI Trading Desk") | Out-Null }
  Update-View
}

$btnOpen.Add_Click({ Open-Desk })
$btnStart.Add_Click({ Start-All })
$btnStop.Add_Click({
  $s = Get-DeskState
  $r = [System.Windows.Forms.MessageBox]::Show($form, (Get-StopWarning $s), "Stop AI Trading Desk", "YesNo", "Warning")
  if ($r -ne [System.Windows.Forms.DialogResult]::Yes) { return }
  $script:busy = $true
  $script:openWhenUp = $false
  $script:starting = $false
  $lblSub.Text = "Stopping - an order being sent finishes first" + $Ell
  Set-Row $rowBe $cAmber ("stopping" + $Ell)
  Set-Row $rowFe $cAmber ("stopping" + $Ell)
  $btnStart.Enabled = $false; $btnStop.Enabled = $false; $btnOpen.Enabled = $false
  $res = Stop-Desk -Pump { [System.Windows.Forms.Application]::DoEvents() }
  $script:busy = $false
  Update-View
  if (-not ($res.Backend -and $res.Frontend)) {
    [System.Windows.Forms.MessageBox]::Show($form, "Could not stop everything. Close this window, right-click `"Stop AI Trading Desk.bat`" and choose Run as administrator.",
                                            "AI Trading Desk") | Out-Null
  }
})
$lnkLogs.Add_LinkClicked({ New-Item -ItemType Directory -Force $LogDir | Out-Null; Start-Process explorer.exe $LogDir })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 3000
$timer.Add_Tick({ if (-not $script:busy) { Update-View } })

if ($Snapshot) {
  $form.StartPosition = "Manual"
  $form.Location = New-Object System.Drawing.Point(-3000, -3000)
  $form.ShowInTaskbar = $false
  $form.Add_Shown({
    Update-View
    [System.Windows.Forms.Application]::DoEvents()
    $bmp = New-Object System.Drawing.Bitmap($form.Width, $form.Height)
    $form.DrawToBitmap($bmp, (New-Object System.Drawing.Rectangle(0, 0, $form.Width, $form.Height)))
    $bmp.Save($Snapshot, [System.Drawing.Imaging.ImageFormat]::Png)
    $form.Close()
  })
  [void]$form.ShowDialog()
  exit 0
}

$form.Add_Shown({
  Update-View
  $s = $script:last
  if (-not ($s.Mt5 -and $s.BackendPort -and $s.FrontPort)) { Start-All }
  $timer.Start()
})
[void]$form.ShowDialog()
$timer.Stop()
if ($mutex) { $mutex.ReleaseMutex() }
