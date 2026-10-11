#!/usr/bin/env bash
# Pindahkan data INDESO dari HP (Termux) ke VPS lalu nyalakan layanan.
#
#   sudo bash impor-data.sh ~/indeso-data.tgz
#
# Arsip dibuat di Termux (lihat docs/vps-indeso.md) dan berisi billing.db, isp_history.db,
# nota/, backup/ (opsional), dan .env. Data lama di VPS (bila ada) disimpan ke /var/lib/indeso.lama-*.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Jalankan dengan sudo."; exit 1; }
ARSIP="${1:?Sebutkan berkas arsip, mis. ~/indeso-data.tgz}"
DATA=/var/lib/indeso
ENVF=/etc/indeso/indeso.env
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

tar -xzf "$ARSIP" -C "$TMP"
[ -f "$TMP/billing.db" ] || { echo "billing.db tidak ada di arsip."; exit 1; }
python3 - "$TMP/billing.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
r = c.execute("PRAGMA integrity_check").fetchone()[0]
n = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("pelanggan", "transaksi", "akun")
     if c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone()}
print("Cek billing.db:", r, n)
sys.exit(0 if r == "ok" else 1)
PY

systemctl stop indeso 2>/dev/null || true
if [ -n "$(ls -A "$DATA" 2>/dev/null)" ]; then mv "$DATA" "${DATA}.lama-$(date +%Y%m%d%H%M%S)"; mkdir -p "$DATA"; fi
for x in billing.db isp_history.db nota backup; do [ -e "$TMP/$x" ] && cp -a "$TMP/$x" "$DATA/"; done
chown -R indeso:indeso "$DATA"

if [ -f "$TMP/.env" ]; then
  cp "$ENVF" "${ENVF}.sebelum-impor" 2>/dev/null || true
  # Dari VPS: MikroTik lewat alamat terowongan, GenieACS langsung di localhost (tanpa nginx).
  grep -v -E '^(MIKROTIK_HOST|GENIEACS_URL|GENIEACS_USER|GENIEACS_PASS|INDESO_DATA_DIR)=' "$TMP/.env" > "$ENVF"
  { echo "MIKROTIK_HOST=10.99.0.2"; echo "GENIEACS_URL=http://127.0.0.1:7557"; echo "GENIEACS_USER="; echo "GENIEACS_PASS="; } >> "$ENVF"
  chown root:indeso "$ENVF"; chmod 640 "$ENVF"
fi

systemctl enable indeso
systemctl restart indeso
sleep 5
systemctl --no-pager --lines=15 status indeso || true
echo
echo "Selesai. Matikan INDESO di HP (pkill -f uvicorn) dan JANGAN nyalakan lagi di sana."
echo "Log langsung: sudo journalctl -u indeso -f"
