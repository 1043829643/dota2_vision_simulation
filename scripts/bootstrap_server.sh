#!/usr/bin/env bash
# One-shot bootstrap for Ubuntu server (run as root via cloud Workbench).
set -euo pipefail

PUBKEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICwfepSJvOKnEqaGv2EyRTTu+x82SoG9avcOExToM/wb dota2-cursor-deploy'
REPO='https://github.com/1043829643/dota2_vision_simulation.git'
APP_DIR='/opt/dota2_vision_simulation'
# Note: many CN cloud VMs cannot reach github.com. Prefer scripts/deploy_server.ps1
# from your dev machine (pack + scp) when git clone fails.

echo "==> SSH key for root"
mkdir -p /root/.ssh
chmod 700 /root/.ssh
if ! grep -Fq "$PUBKEY" /root/.ssh/authorized_keys 2>/dev/null; then
  echo "$PUBKEY" >> /root/.ssh/authorized_keys
fi
chmod 600 /root/.ssh/authorized_keys

echo "==> Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git python3 python3-venv python3-pip nginx

echo "==> Application"
if [ -d "$APP_DIR/.git" ]; then
  cd "$APP_DIR"
  git fetch origin
  git checkout main
  git pull --ff-only
else
  git clone "$REPO" "$APP_DIR"
  cd "$APP_DIR"
fi

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

echo "==> systemd"
cat > /etc/systemd/system/dota2-vision.service <<'EOF'
[Unit]
Description=Dota 2 Vision Simulation Web API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/dota2_vision_simulation
Environment=DOTA_CACHE_ROOT=/tmp/dota_vision_web_cache
Environment=DOTA_JOB_WORKERS=2
ExecStart=/opt/dota2_vision_simulation/.venv/bin/python -m uvicorn web.backend.app:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "==> nginx"
cat > /etc/nginx/sites-available/dota2-vision <<'EOF'
server {
    listen 80;
    server_name _;
    client_max_body_size 20m;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
    }
}
EOF

ln -sf /etc/nginx/sites-available/dota2-vision /etc/nginx/sites-enabled/dota2-vision
rm -f /etc/nginx/sites-enabled/default
nginx -t

systemctl daemon-reload
systemctl enable dota2-vision nginx
systemctl restart dota2-vision nginx

echo "==> Health"
sleep 2
curl -sS http://127.0.0.1/api/health
echo
git -C "$APP_DIR" rev-parse --short HEAD
systemctl is-active dota2-vision nginx
