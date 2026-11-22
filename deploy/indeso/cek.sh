#!/usr/bin/env bash
# Periksa kesehatan INDESO sekali jalan (layanan, database, backup, MikroTik, OLT, ACS, Telegram, VPN, galat):
#
#   sudo bash /opt/indeso/deploy/indeso/cek.sh
#
# Hanya membaca. Password/token tidak ditampilkan, jadi hasilnya aman dikirim ke pengembang.
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Jalankan dengan sudo: sudo bash $0"; exit 1; }
APP=/opt/indeso
PY="$APP/.venv/bin/python"
[ -x "$PY" ] || PY=python3
INDESO_APP="$APP" INDESO_DATA_DIR="${INDESO_DATA_DIR:-/var/lib/indeso}" INDESO_ENV="${INDESO_ENV:-/etc/indeso/indeso.env}" \
  exec "$PY" "$APP/deploy/indeso/periksa.py"
