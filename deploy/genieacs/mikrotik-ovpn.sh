#!/usr/bin/env bash
# Terowongan OpenVPN untuk MikroTik RouterOS v6 (yang belum punya WireGuard), mis. MikroTik wilayah 2.
# MikroTik memanggil VPS (TCP), jadi tetap jalan di belakang modem/NAT/CGNAT dan walau sumber bandwidth
# diganti-ganti. Setelah tersambung:
#   - HP owner (WireGuard "wghp", dari tambah-hp.sh) bisa membuka Winbox MikroTik ini dari mana saja;
#   - INDESO di VPS bisa menjangkau MikroTik ini di alamat 10.97.0.x.
#
#   sudo bash mikrotik-ovpn.sh w2          # siapkan akses untuk MikroTik "w2" (jalankan lagi = tampilkan lagi)
#   sudo bash mikrotik-ovpn.sh hapus w2    # cabut akses
#   sudo bash mikrotik-ovpn.sh status      # siapa yang sedang tersambung
#
# Terowongan WireGuard yang sudah ada (wgacs untuk MikroTik kantor, wghp untuk HP) tidak diubah.
# Setelah MikroTik diganti ke RouterOS v7, pakai WireGuard dan cabut akses OpenVPN-nya.
set -euo pipefail
umask 077
[ "$(id -u)" -eq 0 ] || { echo "Jalankan dengan sudo."; exit 1; }

NAMA_SRV=indeso-mt
DIR=/etc/openvpn/server
CONF="$DIR/${NAMA_SRV}.conf"
DATA="$DIR/${NAMA_SRV}"
DEV=ovpnmt
NET=10.97.0
WG_HP="${WG_HP:-wghp}"
HP_NET="10.98.0.0/24"

if [ "${1:-}" = "status" ]; then
  [ -f /run/openvpn-server/${NAMA_SRV}.status ] && sed -n '/^CLIENT_LIST/p' /run/openvpn-server/${NAMA_SRV}.status \
    | awk -F, '{print "  " $2 "  dari " $3 "  alamat " $4 "  sejak " $8}' || echo "Belum ada yang tersambung."
  exit 0
fi

if [ "${1:-}" = "hapus" ]; then
  NAMA="${2:?Sebutkan nama, mis. hapus w2}"
  [ -f "$DATA/ccd/$NAMA" ] || { echo "Akses '$NAMA' tidak ada."; exit 1; }
  rm -f "$DATA/ccd/$NAMA"
  python3 - "$DATA/pengguna" "$NAMA" <<'PY'
import sys
p, n = sys.argv[1], sys.argv[2]
baris = [b for b in open(p).read().splitlines() if b.split(":", 1)[0] != n]
open(p, "w").write("".join(b + "\n" for b in baris))
PY
  systemctl restart "openvpn-server@${NAMA_SRV}"
  echo "Akses '$NAMA' dicabut."
  exit 0
fi

NAMA="${1:-w2}"
[[ "$NAMA" =~ ^[a-z0-9-]{1,20}$ ]] || { echo "Nama hanya huruf kecil, angka, atau - (maks. 20), mis. w2"; exit 1; }

if [ ! -f "$CONF" ]; then
  echo "== Menyiapkan server OpenVPN untuk MikroTik v6 (sekali saja)"
  command -v openvpn >/dev/null || { apt-get update -q >/dev/null; apt-get install -y -q openvpn >/dev/null; }
  PORT=""
  for p in 1194 1195 1196 1197 1198 1199; do
    { ss -ltnH 2>/dev/null || true; } | awk '{print $4}' | grep -q ":$p\$" || { PORT=$p; break; }
  done
  [ -n "$PORT" ] || { echo "Tidak ada port TCP kosong di 1194-1199."; exit 1; }
  mkdir -p "$DATA/ccd"; chmod 700 "$DATA"
  echo "   membuat sertifikat (bisa 1-2 menit)…"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=INDESO MikroTik CA" \
    -keyout "$DATA/ca.key" -out "$DATA/ca.crt" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=indeso-vps" -keyout "$DATA/server.key" -out "$DATA/server.csr" 2>/dev/null
  printf 'basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' > "$DATA/ext"
  openssl x509 -req -in "$DATA/server.csr" -CA "$DATA/ca.crt" -CAkey "$DATA/ca.key" -CAcreateserial -days 3650 \
    -extfile "$DATA/ext" -out "$DATA/server.crt" 2>/dev/null
  rm -f "$DATA/server.csr" "$DATA/ext"
  openssl dhparam -out "$DATA/dh.pem" 2048 2>/dev/null
  : > "$DATA/pengguna"

  # Cek nama & sandi dari MikroTik. Berkas berisi "nama:sandi" (hanya terbaca root).
  cat > "$DATA/cek.sh" <<'SH'
#!/bin/sh
f="$1"; u=$(sed -n 1p "$f"); p=$(sed -n 2p "$f")
[ -n "$u" ] && [ -n "$p" ] || exit 1
grep -qxF -- "$u:$p" /etc/openvpn/server/indeso-mt/pengguna
SH
  # HP owner (wghp) boleh lewat ke MikroTik di terowongan ini, atas nama VPS (10.97.0.1).
  cat > "$DATA/naik.sh" <<SH
#!/bin/sh
iptables -I FORWARD 1 -i ${WG_HP} -o ${DEV} -j ACCEPT
iptables -I FORWARD 1 -i ${DEV} -o ${WG_HP} -m state --state RELATED,ESTABLISHED -j ACCEPT
iptables -t nat -A POSTROUTING -s ${HP_NET} -o ${DEV} -j MASQUERADE
exit 0
SH
  cat > "$DATA/turun.sh" <<SH
#!/bin/sh
iptables -D FORWARD -i ${WG_HP} -o ${DEV} -j ACCEPT 2>/dev/null
iptables -D FORWARD -i ${DEV} -o ${WG_HP} -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null
iptables -t nat -D POSTROUTING -s ${HP_NET} -o ${DEV} -j MASQUERADE 2>/dev/null
exit 0
SH
  chmod 700 "$DATA/cek.sh" "$DATA/naik.sh" "$DATA/turun.sh"

  # RouterOS v6: OpenVPN hanya TCP, tanpa kompresi & tls-auth, cipher AES-CBC + SHA1, login nama/sandi.
  cat > "$CONF" <<OV
# OpenVPN untuk MikroTik RouterOS v6 (dibuat mikrotik-ovpn.sh)
port ${PORT}
proto tcp-server
dev ${DEV}
dev-type tun
topology subnet
server ${NET}.0 255.255.255.0 nopool
client-config-dir ${DATA}/ccd
ccd-exclusive
ca ${DATA}/ca.crt
cert ${DATA}/server.crt
key ${DATA}/server.key
dh ${DATA}/dh.pem
data-ciphers AES-256-GCM:AES-128-GCM:AES-256-CBC:AES-128-CBC
data-ciphers-fallback AES-256-CBC
auth SHA1
verify-client-cert none
username-as-common-name
script-security 2
auth-user-pass-verify ${DATA}/cek.sh via-file
up ${DATA}/naik.sh
down ${DATA}/turun.sh
keepalive 10 60
persist-key
persist-tun
status /run/openvpn-server/${NAMA_SRV}.status 30
verb 3
OV
  echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/90-indeso-mt.conf
  sysctl -q -p /etc/sysctl.d/90-indeso-mt.conf
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow "${PORT}/tcp" >/dev/null; fi
  systemctl enable --now "openvpn-server@${NAMA_SRV}" >/dev/null
fi

PORT=$(awk '$1=="port" {print $2}' "$CONF")
if [ ! -f "$DATA/ccd/$NAMA" ]; then
  N=2
  while grep -qs "ifconfig-push ${NET}.${N} " "$DATA"/ccd/*; do N=$((N + 1)); done
  [ "$N" -lt 255 ] || { echo "Alamat ${NET}.x habis."; exit 1; }
  SANDI=$(openssl rand -hex 12)
  echo "$NAMA:$SANDI" >> "$DATA/pengguna"
  echo "ifconfig-push ${NET}.${N} 255.255.255.0" > "$DATA/ccd/$NAMA"
fi
ALAMAT=$(awk '{print $2}' "$DATA/ccd/$NAMA")
SANDI=$(awk -F: -v n="$NAMA" '$1==n {print substr($0, length(n) + 2)}' "$DATA/pengguna")
VPS_IP="${ENDPOINT:-$(curl -4 -fsS https://api.ipify.org || true)}"
[ -n "$VPS_IP" ] || { echo "IP publik VPS tidak terbaca; jalankan lagi dengan ENDPOINT=<ip-vps>"; exit 1; }

echo
echo "================ Perintah untuk MikroTik '${NAMA}' (Terminal Winbox) ================"
echo "/interface ovpn-client add name=ovpn-vps connect-to=${VPS_IP} port=${PORT} mode=ip user=${NAMA} password=${SANDI} cipher=aes256 auth=sha1 add-default-route=no comment=\"Terowongan ke VPS INDESO\""
echo "/interface list member add list=LAN interface=ovpn-vps"
echo "======================================================================================"
echo
echo "Cek di MikroTik  : /interface ovpn-client monitor ovpn-vps once   (status: connected)"
echo "                   /ping 10.97.0.1 count=4"
echo "Cek di VPS       : sudo bash $0 status"
echo "Alamat MikroTik  : ${ALAMAT}  (Winbox/aplikasi MikroTik dari HP yang tersambung WireGuard)"
echo
echo "HP owner: supaya aplikasi WireGuard ikut melewatkan ${NET}.0/24, jalankan:"
echo "     sudo bash $(dirname "$0")/tambah-hp.sh perbarui hp-owner"
echo "lalu impor ulang kode QR-nya, ATAU di aplikasi WireGuard: edit terowongan -> Peer -> Allowed IPs,"
echo "tambahkan ', ${NET}.0/24' di akhir, simpan."
echo
echo "Port VPS: ${PORT}/tcp. Bila MikroTik tidak bisa tersambung, izinkan port ini di firewall penyedia VPS."
echo "Baris password di atas adalah kunci akses MikroTik ini: jangan dibagikan atau dikirim ke chat."
