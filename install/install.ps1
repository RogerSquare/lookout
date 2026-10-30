# Lookout Hub installer for Windows with Docker Desktop.
#   irm https://github.com/RogerSquare/lookout/releases/latest/download/install.ps1 | iex
# Installs into %USERPROFILE%\lookout-hub (or $env:LOOKOUT_DIR), starts the hub, and prints the
# setup link. Keep this file ASCII-only: Windows PowerShell 5.1 misreads anything else.
$ErrorActionPreference = 'Stop'

$dir = if ($env:LOOKOUT_DIR) { $env:LOOKOUT_DIR } else { Join-Path $env:USERPROFILE 'lookout-hub' }
$base = 'https://github.com/RogerSquare/lookout/releases/latest/download'

function Fail($msg) { Write-Host "Lookout: $msg" -ForegroundColor Red; exit 1 }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker Desktop isn't installed. Get it from https://www.docker.com/products/docker-desktop/ and run this again."
}
docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker Desktop isn't running. Start it, wait for it to say it's running, and run this again." }

# This PC's address on the home network: the interface with the default route
$route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric | Select-Object -First 1
$ip = (Get-NetIPAddress -InterfaceIndex $route.ifIndex -AddressFamily IPv4 | Select-Object -First 1).IPAddress
if (-not $ip) { Fail "Couldn't find this PC's network address. Connect to your home network and try again." }

New-Item -ItemType Directory -Force (Join-Path $dir 'data') | Out-Null
Set-Location $dir
Write-Host "Downloading Lookout Hub into $dir ..."
Invoke-WebRequest -UseBasicParsing -Uri "$base/compose.desktop.yml" -OutFile 'compose.desktop.yml'
Set-Content -Path '.env' -Value "LOOKOUT_PUBLIC_HOST=$ip" -Encoding ascii

docker compose -f compose.desktop.yml pull
if ($LASTEXITCODE -ne 0) { Fail "Couldn't download the hub image." }
docker compose -f compose.desktop.yml up -d
if ($LASTEXITCODE -ne 0) { Fail "Couldn't start the hub." }

# Windows Firewall: let phones and watches on the home network reach the hub
try {
    if (-not (Get-NetFirewallRule -DisplayName 'Lookout Hub' -ErrorAction SilentlyContinue)) {
        Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile', '-Command',
            "New-NetFirewallRule -DisplayName 'Lookout Hub' -Direction Inbound -Protocol TCP -LocalPort 8765,8766,8555 -Profile Private -Action Allow | Out-Null"
    }
} catch {
    Write-Host "Couldn't add the firewall rule. If the apps can't reach the hub, allow TCP ports 8765, 8766 and 8555 in Windows Firewall." -ForegroundColor Yellow
}

Write-Host "Starting (the first start tests this PC's speed, about a minute) ..."
for ($i = 0; $i -lt 90; $i++) {
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:8765/healthz' | Out-Null; break } catch { Start-Sleep 2 }
}

$code = docker exec lookout-hub cat /data/setup-code 2>$null
Write-Host ''
if ($code) {
    $url = "http://${ip}:8765/?setup=$($code.Trim())"
    Write-Host "Lookout Hub is running. Opening the setup page: $url" -ForegroundColor Green
    Start-Process $url
} else {
    Write-Host "Lookout Hub is running. Open http://${ip}:8765 to finish setting it up." -ForegroundColor Green
}
Write-Host "To update later: cd $dir; docker compose -f compose.desktop.yml pull; docker compose -f compose.desktop.yml up -d"
