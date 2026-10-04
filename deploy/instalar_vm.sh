#!/usr/bin/env bash
# Preparacion unica de la VM (Debian 12 en Compute Engine). Uso, dentro de la VM:
#
#   sudo bash instalar_vm.sh us-east1-docker.pkg.dev/<proyecto>/espartaco/bot:v2
#
# Deja todo listo pero NO arranca el bot ni lo habilita al inicio: si el bot
# del PC sigue vivo, los dos se pelean el polling de Telegram (Conflict) y se
# reinician uno al otro. El arranque es un paso aparte, a proposito:
#
#   sudo systemctl enable --now espartaco
set -euo pipefail

IMAGEN="${1:?Uso: sudo bash instalar_vm.sh <imagen>}"
AQUI="$(cd "$(dirname "$0")" && pwd)"
BASE=/opt/espartaco

command -v gcloud >/dev/null || { echo "Falta gcloud (las imagenes Debian de Compute Engine lo traen)." >&2; exit 1; }

if ! command -v docker >/dev/null; then
    apt-get update
    apt-get install -y docker.io
fi
systemctl enable --now docker

# e2-micro: 1 GB de RAM. El swap evita que el kernel mate al bot en un pico
# (por ejemplo un docker pull con el bot corriendo).
if ! swapon --show | grep -q /swapfile; then
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

install -d -m 755 "$BASE"
install -d -m 700 -o 10001 "$BASE/data"
install -m 755 "$AQUI/arrancar.sh" "$BASE/arrancar.sh"
echo "$IMAGEN" > "$BASE/imagen"
install -m 644 "$AQUI/espartaco.service" /etc/systemd/system/espartaco.service
systemctl daemon-reload

echo
echo "VM lista. Faltan:"
echo "  1. Copiar token.json y contexto_familia.md a $BASE/data/"
echo "  2. Copiar config.env a $BASE/ (chmod 600)"
echo "  3. Con el bot del PC APAGADO: sudo systemctl enable --now espartaco"
