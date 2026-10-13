#!/usr/bin/env bash
# Pasang GenieACS (server TR-069) + WireGuard di VPS Ubuntu 22.04/24.04 untuk INDESO.
#
#   sudo PPPOE_SUBNET=172.20.52.0/24 LAN_SUBNET=192.168.50.0/24 bash install.sh
#
# PPPOE_SUBNET : pool IP PPPoE pelanggan di MikroTik (ONU memakai IP ini); boleh beberapa, pisahkan koma
# LAN_SUBNET   : jaringan kantor tempat INDESO (HP Termux) berjalan
# Semua layanan ACS hanya mendengarkan di alamat terowongan WireGuard (10.99.0.1) atau
# localhost, jadi tidak terbuka ke internet tanpa perlu mengubah firewall VPS.
# Aman dipasang di VPS yang sudah menjalankan layanan lain: memakai antarmuka WireGuard
# sendiri (wgacs) dan port UDP yang belum terpakai; firewall (ufw) tidak dinyalakan paksa.
# Lihat docs/genieacs.md untuk langkah lengkap.
set -euo pipefail

PPPOE_SUBNET="${PPPOE_SUBNET:?Isi PPPOE_SUBNET, mis. 10.10.0.0/16}"
LAN_SUBNET="${LAN_SUBNET:?Isi LAN_SUBNET, mis. 192.168.50.0/24}"
WG_IF="${WG_IF:-wgacs}"
WG_PORT="${WG_PORT:-}"
MT_WG_PORT="${MT_WG_PORT:-13299}"   # listen-port antarmuka baru di MikroTik (beda dengan terowongan yang sudah ada)
WG_VPS_IP="10.99.0.1"
WG_MT_IP="10.99.0.2"
GENIEACS_VERSI="${GENIEACS_VERSI:-1.2.13}"
NBI_USER="${NBI_USER:-indeso}"
WITH_UI="${WITH_UI:-1}"     # web admin GenieACS (opsional; INDESO tidak membutuhkannya)
WITH_FS="${WITH_FS:-0}"     # server file untuk update firmware massal (opsional)
RAM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)

[ "$(id -u)" -eq 0 ] || { echo "Jalankan dengan sudo."; exit 1; }
. /etc/os-release
case "$VERSION_CODENAME" in jammy|noble) ;; *) echo "Didukung: Ubuntu 22.04 (jammy) / 24.04 (noble)."; exit 1;; esac
grep -qw avx /proc/cpuinfo || echo "PERINGATAN: CPU VPS tidak punya AVX — MongoDB 5+ tidak akan jalan. Pilih VPS lain."

echo "== Paket dasar"
NGINX_SUDAH_ADA=$(command -v nginx || true)
apt-get update -y
apt-get install -y curl gnupg ca-certificates nginx apache2-utils wireguard openssl
# Pilih port UDP WireGuard yang belum dipakai (VPS mungkin sudah punya terowongan lain).
if [ -z "$WG_PORT" ]; then
  for p in $(seq 51820 51840); do
    # Socket WireGuard milik kernel tidak tampil di ss, jadi cek juga "wg show".
    { ss -lunH | awk '{print $4}'; wg show all listen-port 2>/dev/null | awk '{print ":"$2}'; } | grep -q ":$p\$" \
      || { WG_PORT=$p; break; }
  done
fi
echo "Port WireGuard ACS: ${WG_PORT}/udp, antarmuka ${WG_IF}"
# Izinkan layanan mengikat alamat 10.99.0.1 walau terowongan belum naik (urutan boot bebas).
sysctl -w net.ipv4.ip_nonlocal_bind=1 >/dev/null
echo 'net.ipv4.ip_nonlocal_bind=1' > /etc/sysctl.d/99-genieacs-bind.conf

# VPS RAM kecil (mis. 1 GB): tambah swap supaya MongoDB/Node tidak dimatikan kernel saat memori penuh.
if [ "$RAM_MB" -lt 2000 ] && ! swapon --show | grep -q .; then
  echo "== RAM ${RAM_MB} MB: membuat swap 2 GB"
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -w vm.swappiness=10 && echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf
fi

echo "== MongoDB 8.0"
curl -fsSL https://www.mongodb.org/static/pgp/server-8.0.asc | gpg --dearmor -o /usr/share/keyrings/mongodb-server-8.0.gpg --yes
echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/ubuntu ${VERSION_CODENAME}/mongodb-org/8.0 multiverse" \
  > /etc/apt/sources.list.d/mongodb-org-8.0.list
apt-get update -y
apt-get install -y mongodb-org
# Batasi cache MongoDB: bawaannya ±50% RAM, terlalu boros untuk VPS kecil. Hanya dengar localhost.
CACHE_GB=$([ "$RAM_MB" -lt 2000 ] && echo 0.25 || echo 0.5)
cat > /etc/mongod.conf <<MONGO
storage:
  dbPath: /var/lib/mongodb
  wiredTiger:
    engineConfig:
      cacheSizeGB: ${CACHE_GB}
systemLog:
  destination: file
  logAppend: true
  path: /var/log/mongodb/mongod.log
net:
  port: 27017
  bindIp: 127.0.0.1
processManagement:
  timeZoneInfo: /usr/share/zoneinfo
MONGO
systemctl enable mongod && systemctl restart mongod

echo "== Node.js 20 + GenieACS ${GENIEACS_VERSI}"
if ! command -v node >/dev/null || ! node -v | grep -q '^v20'; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
fi
npm install -g --unsafe-perm "genieacs@${GENIEACS_VERSI}"

id genieacs >/dev/null 2>&1 || useradd --system --no-create-home --user-group genieacs
mkdir -p /opt/genieacs/ext /var/log/genieacs
chown -R genieacs:genieacs /opt/genieacs /var/log/genieacs

# NBI & UI hanya di localhost (nginx yang membukanya ke terowongan, NBI dengan password).
# CWMP (ONU lapor) & FS (firmware) di semua interface, tapi firewall hanya mengizinkan dari terowongan.
if [ ! -f /opt/genieacs/genieacs.env ]; then
  cat > /opt/genieacs/genieacs.env <<ENV
GENIEACS_CWMP_ACCESS_LOG_FILE=/var/log/genieacs/genieacs-cwmp-access.log
GENIEACS_NBI_ACCESS_LOG_FILE=/var/log/genieacs/genieacs-nbi-access.log
GENIEACS_FS_ACCESS_LOG_FILE=/var/log/genieacs/genieacs-fs-access.log
GENIEACS_UI_ACCESS_LOG_FILE=/var/log/genieacs/genieacs-ui-access.log
GENIEACS_DEBUG_FILE=/var/log/genieacs/genieacs-debug.yaml
GENIEACS_EXT_DIR=/opt/genieacs/ext
GENIEACS_NBI_INTERFACE=127.0.0.1
GENIEACS_UI_INTERFACE=127.0.0.1
GENIEACS_CWMP_INTERFACE=${WG_VPS_IP}
GENIEACS_FS_INTERFACE=${WG_VPS_IP}
GENIEACS_UI_JWT_SECRET=$(openssl rand -hex 32)
ENV
  chmod 600 /opt/genieacs/genieacs.env
  chown genieacs:genieacs /opt/genieacs/genieacs.env
fi

LAYANAN="cwmp nbi"
[ "$WITH_FS" = 1 ] && LAYANAN="$LAYANAN fs"
[ "$WITH_UI" = 1 ] && LAYANAN="$LAYANAN ui"
for svc in cwmp nbi fs ui; do
  # Batas heap Node per layanan supaya muat di VPS 1 GB (cwmp paling sibuk).
  HEAP=$([ "$svc" = cwmp ] && echo 256 || echo 160)
  cat > /etc/systemd/system/genieacs-${svc}.service <<UNIT
[Unit]
Description=GenieACS ${svc}
After=network.target mongod.service

[Service]
User=genieacs
EnvironmentFile=/opt/genieacs/genieacs.env
Environment=NODE_OPTIONS=--max-old-space-size=${HEAP}
ExecStart=$(command -v genieacs-${svc})
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
done
systemctl daemon-reload

echo "== WireGuard (VPS ${WG_VPS_IP} <-> MikroTik ${WG_MT_IP})"
umask 077
[ -f /etc/wireguard/vps.key ] || wg genkey > /etc/wireguard/vps.key
[ -f /etc/wireguard/mikrotik.key ] || wg genkey > /etc/wireguard/mikrotik.key
VPS_PRIV=$(cat /etc/wireguard/vps.key); VPS_PUB=$(wg pubkey < /etc/wireguard/vps.key)
MT_PRIV=$(cat /etc/wireguard/mikrotik.key); MT_PUB=$(wg pubkey < /etc/wireguard/mikrotik.key)
if [ -f "/etc/wireguard/${WG_IF}.conf" ] && ! grep -q "MikroTik kantor (INDESO ACS)" "/etc/wireguard/${WG_IF}.conf"; then
  echo "/etc/wireguard/${WG_IF}.conf sudah ada dan bukan buatan skrip ini. Pakai WG_IF=nama-lain."; exit 1
fi
cat > /etc/wireguard/${WG_IF}.conf <<WG
[Interface]
Address = ${WG_VPS_IP}/24
ListenPort = ${WG_PORT}
PrivateKey = ${VPS_PRIV}

[Peer]
# MikroTik kantor (INDESO ACS). ONU (pool PPPoE) dan LAN kantor (INDESO) diakses lewat peer ini.
PublicKey = ${MT_PUB}
AllowedIPs = ${WG_MT_IP}/32, ${PPPOE_SUBNET}, ${LAN_SUBNET}
PersistentKeepalive = 25
WG
systemctl enable "wg-quick@${WG_IF}"
systemctl restart "wg-quick@${WG_IF}"

for svc in $LAYANAN; do systemctl enable "genieacs-${svc}"; systemctl restart "genieacs-${svc}"; done

echo "== nginx: NBI (berpassword) & UI hanya di alamat terowongan"
if [ ! -f /etc/nginx/genieacs-nbi.htpasswd ]; then
  NBI_PASS=$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)
  htpasswd -bc /etc/nginx/genieacs-nbi.htpasswd "$NBI_USER" "$NBI_PASS"
  echo "$NBI_PASS" > /root/genieacs-nbi-password.txt; chmod 600 /root/genieacs-nbi-password.txt
fi
# Pekerja nginx (www-data) harus bisa membaca file password; umask 077 di atas membuatnya root saja (→ HTTP 500).
chown root:www-data /etc/nginx/genieacs-nbi.htpasswd
chmod 640 /etc/nginx/genieacs-nbi.htpasswd
IZIN_KANTOR=$(for net in $(echo "$LAN_SUBNET" | tr ',' ' ') "${WG_MT_IP}"; do printf '    allow %s;\n' "$net"; done; printf '    deny all;')
cat > /etc/nginx/sites-available/genieacs <<NGX
server {
    listen ${WG_VPS_IP}:7557;
    # Hanya kantor (LAN) & MikroTik. Pelanggan PPPoE juga bisa menjangkau 10.99.0.1
    # (perlu untuk lapor ONU ke 7547), jadi API & web admin wajib dibatasi.
${IZIN_KANTOR}
    auth_basic "GenieACS NBI";
    auth_basic_user_file /etc/nginx/genieacs-nbi.htpasswd;
    client_max_body_size 50m;
    location / { proxy_pass http://127.0.0.1:7557; proxy_read_timeout 90s; }
}
server {
    listen ${WG_VPS_IP}:3000;
${IZIN_KANTOR}
    location / { proxy_pass http://127.0.0.1:3000; proxy_set_header Host \$host; }
}
NGX
ln -sf /etc/nginx/sites-available/genieacs /etc/nginx/sites-enabled/genieacs
# nginx baru dipasang skrip ini: matikan situs bawaan (port 80) supaya tidak bentrok dengan layanan lain.
[ -z "$NGINX_SUDAH_ADA" ] && rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload-or-restart nginx

echo "== Firewall"
# Layanan ACS hanya mendengarkan di 10.99.0.1/localhost, jadi firewall tidak wajib.
# Bila ufw sudah aktif di VPS ini, buka port WireGuard dan port ACS di antarmuka terowongan saja.
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "${WG_PORT}/udp"
  for port in 7547 7557 7567 3000; do ufw allow in on "${WG_IF}" to any port "$port" proto tcp; done
else
  echo "ufw tidak aktif — dilewati. Pastikan firewall penyedia VPS mengizinkan ${WG_PORT}/udp."
fi

echo "== Provision bawaan INDESO"
NBI="http://127.0.0.1:7557"
# Di VPS kecil MongoDB + Node butuh waktu untuk menyala: tunggu NBI siap (maks. 2 menit).
echo -n "Menunggu GenieACS NBI siap"
for i in $(seq 1 60); do
  curl -fsS -o /dev/null "$NBI/devices/?projection=_id" 2>/dev/null && { echo " — siap."; break; }
  echo -n "."; sleep 2
  if [ "$i" -eq 60 ]; then
    echo; echo "GenieACS NBI tidak menyala. Status layanan:"
    systemctl --no-pager --lines=0 status mongod genieacs-nbi || true
    echo "--- log genieacs-nbi ---"; journalctl -u genieacs-nbi -n 25 --no-pager || true
    echo "--- log mongod ---"; tail -n 15 /var/log/mongodb/mongod.log 2>/dev/null || journalctl -u mongod -n 15 --no-pager || true
    exit 1
  fi
done
DIR="$(cd "$(dirname "$0")" && pwd)"
curl -fsS -X PUT --data-binary @"$DIR/provision-indeso.js" "$NBI/provisions/indeso-default"
curl -fsS -X PUT --data-binary @"$DIR/vp-pppoeUsername.js" "$NBI/virtual_parameters/pppoeUsername"
curl -fsS -X PUT --data-binary @"$DIR/provision-diagnostik.js" "$NBI/provisions/indeso-diagnostik"
curl -fsS -X PUT -H 'Content-Type: application/json' \
  --data '{"weight":0,"channel":"","events":{"8 DIAGNOSTICS COMPLETE":true},"precondition":"","configurations":[{"type":"provision","name":"indeso-diagnostik","args":null}]}' \
  "$NBI/presets/indeso-diagnostik"
# Kredensial connection request bersama untuk semua ONU (disimpan hanya di VPS & di preset).
# Sebaiknya sama dengan ACS username/password yang diisi teknisi di OLT/ONU: sebagian ONU
# (mis. ZTE yang diatur lewat OLT) memakai pasangan itu juga untuk connection request.
# Ubah dengan: CR_USER=INDESO CR_PASS=INDESO26 sudo -E bash install.sh, atau edit kedua file.
[ -n "${CR_USER:-}" ] && { echo "$CR_USER" > /root/genieacs-cr-user.txt; chmod 600 /root/genieacs-cr-user.txt; }
[ -n "${CR_PASS:-}" ] && { echo "$CR_PASS" > /root/genieacs-cr-password.txt; chmod 600 /root/genieacs-cr-password.txt; }
[ -f /root/genieacs-cr-user.txt ] || { echo indeso > /root/genieacs-cr-user.txt; chmod 600 /root/genieacs-cr-user.txt; }
[ -f /root/genieacs-cr-password.txt ] || { openssl rand -hex 12 > /root/genieacs-cr-password.txt; chmod 600 /root/genieacs-cr-password.txt; }
CR_USER=$(cat /root/genieacs-cr-user.txt)
CR_PASS=$(cat /root/genieacs-cr-password.txt)
curl -fsS -X PUT -H 'Content-Type: application/json' \
  --data "{\"weight\":0,\"channel\":\"\",\"events\":{},\"precondition\":\"\",\"configurations\":[{\"type\":\"provision\",\"name\":\"indeso-default\",\"args\":[\"${CR_USER}\",\"${CR_PASS}\"]}]}" \
  "$NBI/presets/indeso-default"

VPS_IP=$(curl -fsS https://api.ipify.org || echo "IP_PUBLIK_VPS")
cat <<SELESAI

==================================================================
 SELESAI. Simpan informasi ini.

 Password NBI (untuk .env INDESO): $(cat /root/genieacs-nbi-password.txt)
   GENIEACS_URL=http://${WG_VPS_IP}:7557
   GENIEACS_USER=${NBI_USER}
   GENIEACS_PASS=(password di atas)

 Tempel perintah ini di Terminal MikroTik (RouterOS v7):

/interface wireguard add name=wg-acs listen-port=${MT_WG_PORT} private-key="${MT_PRIV}"
/interface wireguard peers add interface=wg-acs public-key="${VPS_PUB}" endpoint-address=${VPS_IP} endpoint-port=${WG_PORT} allowed-address=${WG_VPS_IP}/32 persistent-keepalive=25s
/ip address add address=${WG_MT_IP}/24 interface=wg-acs
/ip firewall filter add chain=forward in-interface=wg-acs src-address=${WG_VPS_IP} action=accept comment="ACS -> ONU (connection request)" place-before=0
/ip firewall filter add chain=forward out-interface=wg-acs action=accept comment="ONU & INDESO -> ACS" place-before=0

 URL ACS untuk diisi di menu TR-069 setiap ONU:  http://${WG_VPS_IP}:7547/
 Web GenieACS (lewat LAN kantor):                http://${WG_VPS_IP}:3000/
==================================================================
SELESAI
