#!/usr/bin/env bash
# Wajibkan ONU login ke ACS (cwmp.auth) memakai ACS username/password yang diisi teknisi.
# Tanpa ini, siapa pun di jaringan PPPoE bisa mendaftarkan "ONU palsu" ke 10.99.0.1:7547.
#
#   sudo bash wajibkan-login-acs.sh aktifkan    # aktifkan, uji 7 menit, batal otomatis bila ada ONU terputus
#   sudo bash wajibkan-login-acs.sh matikan     # kembalikan seperti semula (tanpa login)
#   sudo bash wajibkan-login-acs.sh status
#
# Username/password diambil dari /root/genieacs-cr-user.txt & /root/genieacs-cr-password.txt
# (sama dengan yang diisi di OLT/ONU, mis. INDESO / INDESO26). Bisa ditimpa: ACS_USER=.. ACS_PASS=..
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Jalankan dengan sudo."; exit 1; }
NBI="http://127.0.0.1:7557"
ACS_USER="${ACS_USER:-$(cat /root/genieacs-cr-user.txt 2>/dev/null || true)}"
ACS_PASS="${ACS_PASS:-$(cat /root/genieacs-cr-password.txt 2>/dev/null || true)}"
UJI_MENIT="${UJI_MENIT:-7}"

mongo() { mongosh --quiet genieacs --eval "$1"; }
lapor_baru() {   # daftar id ONU yang lapor setelah waktu $1 (epoch detik)
  curl -fsS "$NBI/devices/?projection=_id,_lastInform" | python3 -c '
import sys, json
from datetime import datetime
batas = float(sys.argv[1])
for d in json.load(sys.stdin):
    t = d.get("_lastInform")
    if t and datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() > batas:
        print(d["_id"])' "$1" | sort
}
matikan() { mongo 'db.config.deleteOne({_id: "cwmp.auth"})' >/dev/null; echo "Login ACS DIMATIKAN (ONU boleh lapor tanpa login)."; }

case "${1:-status}" in
  status)
    mongo 'const c = db.config.findOne({_id: "cwmp.auth"}); print(c ? "AKTIF: " + c.value.replace(/"[^"]*"\s*\)$/, "\"***\")") : "TIDAK AKTIF (ONU boleh lapor tanpa login)")'
    ;;
  matikan) matikan ;;
  aktifkan)
    [ -n "$ACS_USER" ] && [ -n "$ACS_PASS" ] || { echo "ACS username/password kosong."; exit 1; }
    case "$ACS_USER$ACS_PASS" in *\"*|*\\*) echo "Username/password tidak boleh berisi tanda kutip atau backslash."; exit 1;; esac
    SEBELUM=$(lapor_baru $(( $(date +%s) - 360 )))
    echo "ONU yang lapor 6 menit terakhir: $(echo "$SEBELUM" | grep -c . || true)"
    [ -n "$SEBELUM" ] || { echo "Belum ada ONU yang lapor 6 menit terakhir — tunggu ONU online dulu."; exit 1; }
    MULAI=$(date +%s)
    mongo "db.config.updateOne({_id: 'cwmp.auth'}, {\$set: {value: 'AUTH(\"${ACS_USER}\", \"${ACS_PASS}\")'}}, {upsert: true})" >/dev/null
    echo "Login ACS diaktifkan (${ACS_USER}). Menguji ${UJI_MENIT} menit — jangan tutup terminal…"
    sleep $(( UJI_MENIT * 60 ))
    SESUDAH=$(lapor_baru "$MULAI")
    HILANG=$(comm -23 <(echo "$SEBELUM") <(echo "$SESUDAH"))
    if [ -n "$HILANG" ]; then
      matikan
      echo
      echo "DIBATALKAN: ONU berikut tidak bisa lapor dengan login ${ACS_USER}:"
      echo "$HILANG" | sed 's/^/  - /'
      echo "Samakan ACS Username/Password ONU tersebut (di OLT untuk ZTE, di web ONU untuk Fiberhome),"
      echo "lalu jalankan skrip ini lagi."
      exit 2
    fi
    echo "BERHASIL: semua $(echo "$SESUDAH" | grep -c .) ONU tetap lapor. Login ACS sekarang wajib."
    echo "ONU baru WAJIB diisi ACS Username/Password ${ACS_USER} / (password yang sama)."
    ;;
  *) echo "Pakai: $0 aktifkan|matikan|status"; exit 1 ;;
esac
