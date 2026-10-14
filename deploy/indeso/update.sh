#!/usr/bin/env bash
# Update INDESO dengan aman setelah PR di-merge:
#
#   sudo bash /opt/indeso/deploy/indeso/update.sh
#
# 1. catat versi yang sedang jalan, 2. git pull, 3. pasang library bila requirements.txt berubah,
# 4. cek kode bisa dibaca Python, 5. restart, 6. tunggu /healthz sehat.
# Bila langkah 4–6 gagal, otomatis KEMBALI ke versi sebelumnya dan restart lagi.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Jalankan dengan sudo."; exit 1; }
APP=/opt/indeso
PY="$APP/.venv/bin/python"
PIP="$APP/.venv/bin/pip"

# Zona waktu dikunci ke WIB untuk layanan INDESO (tagihan, isolir, tanggal transaksi),
# tidak bergantung pada pengaturan zona waktu VPS.
DROPIN=/etc/systemd/system/indeso.service.d/zona-waktu.conf
if [ ! -f "$DROPIN" ]; then
  mkdir -p "$(dirname "$DROPIN")"
  printf '[Service]\nEnvironment=TZ=Asia/Jakarta\n' > "$DROPIN"
  systemctl daemon-reload
fi

LAMA=$(git -C "$APP" rev-parse HEAD)
git -C "$APP" pull --ff-only
BARU=$(git -C "$APP" rev-parse HEAD)
if [ "$LAMA" = "$BARU" ]; then
  echo "Sudah versi terbaru ($(git -C "$APP" log -1 --format='%h %s'))."
  exit 0
fi
echo "Update ${LAMA:0:7} -> ${BARU:0:7}:"
git -C "$APP" log --format='  - %s' "$LAMA..$BARU" | head -20

pasang_library() {
  if ! git -C "$APP" diff --quiet "$1" "$2" -- requirements.txt; then
    echo "requirements.txt berubah, memasang library…"
    "$PIP" install --quiet -r "$APP/requirements.txt"
  fi
}

sehat() {
  for _ in $(seq 30); do
    sleep 2
    curl -fsS -o /dev/null http://127.0.0.1:8000/healthz && return 0
  done
  return 1
}

kembalikan() {
  echo
  echo "GAGAL: $1. Kembali ke versi ${LAMA:0:7}…"
  git -C "$APP" reset --hard --quiet "$LAMA"
  pasang_library "$BARU" "$LAMA" || true
  systemctl restart indeso
  if sehat; then echo "INDESO kembali jalan dengan versi lama. Kirim log di bawah ke pengembang:"
  else echo "PERHATIAN: versi lama juga tidak sehat. Cek: sudo journalctl -u indeso -n 80"; fi
  journalctl -u indeso -n 30 --no-pager || true
  exit 1
}

pasang_library "$LAMA" "$BARU" || kembalikan "pemasangan library gagal"
"$PY" -m py_compile "$APP/main.py" || kembalikan "kode baru tidak bisa dibaca Python"
systemctl restart indeso
sehat || kembalikan "INDESO tidak sehat setelah restart"
echo "BERHASIL: INDESO jalan dengan versi ${BARU:0:7}."
echo "Bila ada masalah, kembali ke versi sebelumnya:"
echo "  sudo git -C $APP reset --hard ${LAMA:0:7} && sudo systemctl restart indeso"
