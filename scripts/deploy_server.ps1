# Deploy / update Dota 2 Vision Simulation on a remote Ubuntu server.
# Usage (from repo root or scripts/):
#   .\scripts\deploy_server.ps1
#   .\scripts\deploy_server.ps1 -Server 43.139.240.241 -RestartOnly

param(
    [string]$Server = "43.139.240.241",
    [string]$SshKey = "$env:USERPROFILE\.ssh\dota2_aliyun_cursor",
    [string]$RemoteUser = "root",
    [string]$AppDir = "/opt/dota2_vision_simulation",
    [switch]$RestartOnly
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Remote = "${RemoteUser}@${Server}"
$SshArgs = @("-i", $SshKey, "-o", "BatchMode=yes")
$ScpArgs = @("-i", $SshKey, "-o", "BatchMode=yes")

function Invoke-Remote([string]$Command) {
    # Strip CR so the remote POSIX/bash shell doesn't choke on Windows CRLF.
    $Command = $Command -replace "`r", ""
    & ssh @SshArgs $Remote $Command
    if ($LASTEXITCODE -ne 0) { throw "Remote command failed ($LASTEXITCODE): $Command" }
}

if (-not (Test-Path $SshKey)) {
    throw "SSH key not found: $SshKey"
}

if ($RestartOnly) {
    Write-Host "==> Restart services on $Server"
    Invoke-Remote "systemctl restart dota2-vision nginx && sleep 2 && curl -sS http://127.0.0.1/api/health"
    exit 0
}

$Archive = Join-Path $env:TEMP "dota2_deploy_$(Get-Date -Format 'yyyyMMdd_HHmmss').tar.gz"
Write-Host "==> Pack release (include tree CSV, exclude .dem)"
Push-Location $Root
try {
    & tar -czf $Archive `
        --exclude=.git `
        --exclude=.venv `
        --exclude=venv `
        --exclude=__pycache__ `
        --exclude=outputs `
        --exclude=package `
        --exclude=demo `
        --exclude=node_modules `
        --exclude="resources/source/*.dem" `
        web tools resources map-data-741 requirements.txt db_settings.json start.sh scripts .coze
    if ($LASTEXITCODE -ne 0) { throw "tar failed" }
}
finally {
    Pop-Location
}

$SizeMb = [math]::Round((Get-Item $Archive).Length / 1MB, 2)
Write-Host "    Archive: $Archive ($SizeMb MB)"

$TreeCsv = Join-Path $Root "resources\source\dota-map-trees.csv"
if (-not (Test-Path $TreeCsv)) {
    throw "Missing required file: resources/source/dota-map-trees.csv"
}

Write-Host "==> Upload to $Server"
& scp @ScpArgs $Archive "${Remote}:/tmp/dota2_deploy.tar.gz"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }

Write-Host "==> Install on server"
$RemoteScript = @'
set -eu
APP_DIR='__APP_DIR__'
ARCHIVE=/tmp/dota2_deploy.tar.gz
mkdir -p "$APP_DIR"
tar -xzf "$ARCHIVE" -C "$APP_DIR"
cd "$APP_DIR"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

# Persist cache dir: migrate from volatile /tmp to APP_DIR/var/web_cache.
PERSIST_CACHE="$APP_DIR/var/web_cache"
OLD_CACHE=/tmp/dota_vision_web_cache
mkdir -p "$PERSIST_CACHE"
if [ -d "$OLD_CACHE" ] && [ "$OLD_CACHE" != "$PERSIST_CACHE" ]; then
  echo "==> Migrate cache from $OLD_CACHE to $PERSIST_CACHE"
  cp -rn "$OLD_CACHE/." "$PERSIST_CACHE/" 2>/dev/null || true
fi
UNIT=/etc/systemd/system/dota2-vision.service
if [ -f "$UNIT" ] && grep -q "DOTA_CACHE_ROOT=$OLD_CACHE" "$UNIT"; then
  echo "==> Rewrite systemd DOTA_CACHE_ROOT -> $PERSIST_CACHE"
  sed -i "s#DOTA_CACHE_ROOT=$OLD_CACHE#DOTA_CACHE_ROOT=$PERSIST_CACHE#" "$UNIT"
  systemctl daemon-reload
fi

systemctl restart dota2-vision nginx
sleep 2
echo "==> Health"
curl -sS http://127.0.0.1/api/health
echo
test -f resources/source/dota-map-trees.csv
echo "tree_csv: ok"
'@ -replace '__APP_DIR__', $AppDir

Invoke-Remote $RemoteScript
Write-Host "==> Done. Open http://${Server}/"
