#!/usr/bin/env bash
# One-time setup of a fresh Debian/Ubuntu VM (e.g. Google Compute Engine).
# Run from the project folder:  bash deploy/setup-vm.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
fi

# small VMs can run out of memory while building images
if [ ! -f /swapfile ]; then
  sudo fallocate -l 1G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s/^SECRET_KEY=$/SECRET_KEY=$(openssl rand -hex 32)/" .env
  echo "Created .env with a random SECRET_KEY."
fi

echo
echo "Next:"
echo "  1) nano .env            # set DOMAIN, ADMIN_PASSWORD, SUPPORT_WHATSAPP"
echo "  2) sudo docker compose up -d --build"
echo "  3) open https://<your domain>"
