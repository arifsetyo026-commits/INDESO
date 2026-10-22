#!/usr/bin/env bash
# Akses MikroTik (Winbox/WebFig) dari HP owner lewat WireGuard, dari mana saja (data seluler pun bisa),
# tanpa membuka port MikroTik ke internet dan tetap jalan walaupun WAN kantor CGNAT (Starlink).
#
#   sudo bash tambah-hp.sh                # buat akses untuk "hp-owner" (atau tampilkan lagi kode QR-nya)
#   sudo bash tambah-hp.sh hp-teknisi     # akses untuk perangkat lain
#   sudo bash tambah-hp.sh hapus hp-lama  # cabut akses satu perangkat (mis. HP hilang)
#   sudo bash tambah-hp.sh berkas         # salin konfigurasi ke folder home untuk diimpor di HP
#                                         # (bila skrip dijalankan dari HP yang sama lewat SSH/Termux,
#                                         #  kode QR di layar tidak bisa dipindai HP itu sendiri)
#
# Cara kerja: HP <-> VPS lewat antarmuka WireGuard sendiri (wghp, 10.98.0.0/24); VPS meneruskan ke
# MikroTik lewat terowongan INDESO yang sudah ada (wgacs) ATAS NAMA VPS (10.99.0.1), alamat yang sudah
# diizinkan MikroTik sejak pemasangan ACS. Terowongan wgacs tidak diubah sama sekali.
set -euo pipefail
umask 077            # kunci WireGuard tidak boleh terbaca pengguna lain
[ "$(id -u)" -eq 0 ] || { echo "Jalankan dengan sudo."; exit 1; }

WG_ACS="${WG_ACS:-wgacs}"
WG_HP="${WG_HP:-wghp}"
NET="10.98.0"
DIR=/etc/wireguard/hp
CONF="/etc/wireguard/${WG_HP}.conf"

ip link show "$WG_ACS" >/dev/null 2>&1 || { echo "Terowongan ${WG_ACS} ke MikroTik belum ada/aktif. Cek: sudo wg show"; exit 1; }

if [ "${1:-}" = "hapus" ]; then
  NAMA="${2:?Sebutkan nama perangkat, mis. hapus hp-lama}"
  [ -f "$DIR/$NAMA.pub" ] || { echo "Perangkat '$NAMA' tidak ditemukan. Yang ada: $(ls "$DIR" 2>/dev/null | sed -n 's/\.pub$//p' | xargs)"; exit 1; }
  wg set "$WG_HP" peer "$(cat "$DIR/$NAMA.pub")" remove || true
  python3 - "$CONF" "$NAMA" <<'PY'
import sys
path, nama = sys.argv[1], sys.argv[2]
bagian = open(path).read().split("\n[Peer]")
sisa = [b for b in bagian if not b.startswith(f"\n# {nama}\n")]
open(path, "w").write("\n[Peer]".join(sisa))
PY
  rm -f "$DIR/$NAMA".*
  echo "Akses '$NAMA' dicabut."
  exit 0
fi

BERKAS=0
if [ "${1:-}" = "berkas" ]; then BERKAS=1; shift; fi
NAMA="${1:-hp-owner}"
[[ "$NAMA" =~ ^[A-Za-z0-9_-]{1,30}$ ]] || { echo "Nama hanya huruf, angka, - atau _ (maks. 30)."; exit 1; }
command -v qrencode >/dev/null || apt-get install -y qrencode >/dev/null
mkdir -p "$DIR"; chmod 700 "$DIR"

if [ ! -f "$CONF" ]; then
  echo "== Menyiapkan antarmuka ${WG_HP} (sekali saja)"
  PORT=""
  for p in $(seq 51850 51870); do
    { ss -lunH | awk '{print $4}'; wg show all listen-port 2>/dev/null | awk '{print ":"$2}'; } | grep -q ":$p\$" \
      || { PORT=$p; break; }
  done
  [ -n "$PORT" ] || { echo "Tidak ada port UDP kosong di 51850-51870."; exit 1; }
  wg genkey > "$DIR/server.key"; chmod 600 "$DIR/server.key"
  cat > "$CONF" <<WG
# Akses HP owner ke MikroTik (dibuat tambah-hp.sh)
[Interface]
Address = ${NET}.1/24
ListenPort = ${PORT}
PrivateKey = $(cat "$DIR/server.key")
PostUp = iptables -I FORWARD 1 -i %i -o ${WG_ACS} -j ACCEPT; iptables -I FORWARD 1 -i ${WG_ACS} -o %i -m state --state RELATED,ESTABLISHED -j ACCEPT; iptables -t nat -A POSTROUTING -s ${NET}.0/24 -o ${WG_ACS} -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -o ${WG_ACS} -j ACCEPT; iptables -D FORWARD -i ${WG_ACS} -o %i -m state --state RELATED,ESTABLISHED -j ACCEPT; iptables -t nat -D POSTROUTING -s ${NET}.0/24 -o ${WG_ACS} -j MASQUERADE
WG
  chmod 600 "$CONF"
  echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/90-indeso-hp.conf
  sysctl -q -p /etc/sysctl.d/90-indeso-hp.conf
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow "${PORT}/udp" >/dev/null; fi
  systemctl enable --now "wg-quick@${WG_HP}" >/dev/null
fi

PORT=$(awk -F' *= *' '/^ListenPort/ {print $2}' "$CONF")
SERVER_PUB=$(wg pubkey < "$DIR/server.key")

if [ ! -f "$DIR/$NAMA.conf" ]; then
  # Alamat berikutnya yang belum dipakai: 10.98.0.2, .3, ...
  N=2
  while grep -q "AllowedIPs = ${NET}.${N}/32" "$CONF"; do N=$((N + 1)); done
  [ "$N" -lt 255 ] || { echo "Alamat 10.98.0.x habis."; exit 1; }
  PRIV=$(wg genkey); PUB=$(echo "$PRIV" | wg pubkey)
  printf '\n[Peer]\n# %s\nPublicKey = %s\nAllowedIPs = %s.%s/32\n' "$NAMA" "$PUB" "$NET" "$N" >> "$CONF"
  wg set "$WG_HP" peer "$PUB" allowed-ips "${NET}.${N}/32"
  echo "$PUB" > "$DIR/$NAMA.pub"

  # Yang dilewatkan ke VPS: MikroTik (10.99.0.2) + jaringan yang sudah dirutekan terowongan INDESO
  # (LAN kantor, pool PPPoE pelanggan). Internet HP lainnya tetap lewat jalur HP sendiri.
  TUJUAN="10.99.0.2/32"
  for net in $(wg show "$WG_ACS" allowed-ips | cut -f2- | tr ' \t' '\n\n' | grep '/' | sort -u); do
    case "$net" in 10.99.0.*) ;; *) TUJUAN="$TUJUAN, $net";; esac
  done
  ENDPOINT="${ENDPOINT:-$(curl -4 -fsS https://api.ipify.org || true)}"
  [ -n "$ENDPOINT" ] || { echo "IP publik VPS tidak terbaca; jalankan lagi dengan ENDPOINT=<ip-vps>"; exit 1; }
  cat > "$DIR/$NAMA.conf" <<CL
[Interface]
PrivateKey = ${PRIV}
Address = ${NET}.${N}/32

[Peer]
PublicKey = ${SERVER_PUB}
Endpoint = ${ENDPOINT}:${PORT}
AllowedIPs = ${TUJUAN}
PersistentKeepalive = 25
CL
  chmod 600 "$DIR/$NAMA.conf"
fi

if [ "$BERKAS" = 1 ]; then
  PEMILIK="${SUDO_USER:-root}"
  RUMAH=$(getent passwd "$PEMILIK" | cut -d: -f6)
  install -m 600 -o "$PEMILIK" "$DIR/$NAMA.conf" "$RUMAH/$NAMA.conf"
  echo
  echo "Berkas disalin ke: $RUMAH/$NAMA.conf"
  echo "Di Termux HP (sesi baru, BUKAN di dalam SSH), ambil berkasnya:"
  echo "     termux-setup-storage"
  echo "     scp ${PEMILIK}@$(curl -4 -fsS https://api.ipify.org 2>/dev/null || echo IP_VPS):${NAMA}.conf ~/storage/downloads/"
  echo "Lalu di aplikasi WireGuard: + -> Impor dari berkas -> Download -> ${NAMA}.conf"
  echo "Setelah berhasil diimpor, HAPUS salinannya (berisi kunci akses):"
  echo "     di VPS : rm ~/${NAMA}.conf"
  echo "     di HP  : rm ~/storage/downloads/${NAMA}.conf"
  exit 0
fi

echo
echo "================ Kode QR untuk '${NAMA}' ================"
# Kode QR di terminal HP (Termux) sering pecah karena jarak antarbaris; pindai dari layar laptop/PC,
# atau pakai: sudo bash tambah-hp.sh berkas
qrencode -t ansiutf8 -m 2 < "$DIR/$NAMA.conf"
echo "(Kode QR pecah/tidak terbaca, atau dijalankan dari HP yang sama? Pakai: sudo bash $0 berkas ${NAMA})"
echo
echo "1. Pasang aplikasi WireGuard di HP (Play Store / App Store)."
echo "2. Ketuk tombol + -> Pindai dari kode QR -> arahkan kamera ke kode di atas -> beri nama 'MikroTik'."
echo "3. Nyalakan tombolnya, lalu buka:"
echo "     Winbox : 10.99.0.2   (port 8291)"
echo "     WebFig : http://10.99.0.2/"
echo
echo "Jalur yang lewat terowongan: $(awk -F' *= *' '/^AllowedIPs/ {print $2}' "$DIR/$NAMA.conf")"
echo "Port VPS: ${PORT}/udp. Bila HP tidak bisa tersambung, izinkan port ini di firewall penyedia VPS (panel cloud)."
echo "Bila Winbox/WebFig ditolak, di MikroTik izinkan 10.99.0.0/24 pada layanan winbox & www:"
echo "     /ip service print where name~\"www|winbox\""
echo "Kode QR ini adalah kunci akses: jangan dibagikan. Cabut bila HP hilang: sudo bash $0 hapus ${NAMA}"
