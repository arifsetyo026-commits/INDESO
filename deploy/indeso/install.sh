#!/usr/bin/env bash
# Pasang INDESO di VPS Ubuntu 22.04/24.04 (berdampingan dengan GenieACS dari deploy/genieacs).
#
#   sudo bash install.sh                          # alamat otomatis indeso.<ip>.sslip.io
#   sudo DOMAIN=indeso.contoh.my.id EMAIL=anda@mail.com bash install.sh
#
# - INDESO jalan sebagai layanan systemd "indeso" di 127.0.0.1:8000, dibuka lewat nginx + HTTPS
#   (Let's Encrypt). Data di /var/lib/indeso, konfigurasi di /etc/indeso/indeso.env.
# - MikroTik & OLT dijangkau lewat terowongan WireGuard "wgacs"; subnet OLT ditambahkan ke terowongan.
# - Layanan BELUM dinyalakan sampai data dari HP dipindahkan (impor-data.sh), supaya tidak
#   ada dua INDESO yang jalan bersamaan.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Jalankan dengan sudo."; exit 1; }
OLT_SUBNET="${OLT_SUBNET:-192.168.2.0/24}"
WG_IF="${WG_IF:-wgacs}"
APP=/opt/indeso
DATA=/var/lib/indeso
ETC=/etc/indeso
REPO_URL="${REPO_URL:-https://github.com/arifsetyo026-commits/INDESO.git}"

IP_PUBLIK=$(curl -fsS -4 https://api.ipify.org)
DOMAIN="${DOMAIN:-indeso.$(echo "$IP_PUBLIK" | tr . -).sslip.io}"
echo "Alamat dashboard: https://${DOMAIN}"

echo "== Cek port 80/443"
if ss -ltnpH '( sport = :80 or sport = :443 )' | grep -v nginx | grep -q .; then
  echo "Port 80/443 sudah dipakai program lain (bukan nginx):"; ss -ltnp '( sport = :80 or sport = :443 )'
  echo "Hentikan program itu dulu atau hubungi pengembang."; exit 1
fi

echo "== Paket"
apt-get update -y
apt-get install -y git python3-venv python3-pip snmp nginx certbot python3-certbot-nginx

echo "== Kode aplikasi di ${APP}"
id indeso >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin --user-group indeso
if [ -d "$APP/.git" ]; then git -C "$APP" pull --ff-only; else git clone "$REPO_URL" "$APP"; fi
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --quiet --upgrade pip
"$APP/.venv/bin/pip" install --quiet -r "$APP/requirements.txt"
mkdir -p "$DATA" "$ETC"
chown -R indeso:indeso "$DATA"
if [ ! -f "$ETC/indeso.env" ]; then
  cp "$APP/.env.example" "$ETC/indeso.env"
fi
chown root:indeso "$ETC/indeso.env"; chmod 640 "$ETC/indeso.env"

cat > /etc/systemd/system/indeso.service <<UNIT
[Unit]
Description=INDESO - monitoring & manajemen pelanggan
After=network-online.target wg-quick@${WG_IF}.service genieacs-nbi.service
Wants=network-online.target

[Service]
User=indeso
WorkingDirectory=${DATA}
EnvironmentFile=${ETC}/indeso.env
Environment=INDESO_DATA_DIR=${DATA}
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Jakarta
# --proxy-headers: IP asli pengunjung dari nginx (pembatas login per IP tetap bekerja).
ExecStart=${APP}/.venv/bin/uvicorn main:app --app-dir ${APP} --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload

echo "== Terowongan: tambahkan subnet OLT (${OLT_SUBNET})"
CONF=/etc/wireguard/${WG_IF}.conf
if [ -f "$CONF" ] && ! grep -q "$OLT_SUBNET" "$CONF"; then
  sed -i "/^AllowedIPs/ s|\$|, ${OLT_SUBNET}|" "$CONF"
  systemctl restart "wg-quick@${WG_IF}"
fi

echo "== nginx + HTTPS"
cat > /etc/nginx/sites-available/indeso <<NGX
server {
    listen 80;
    server_name ${DOMAIN};
    client_max_body_size 25m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
NGX
ln -sf /etc/nginx/sites-available/indeso /etc/nginx/sites-enabled/indeso
nginx -t && systemctl reload-or-restart nginx
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 80/tcp; ufw allow 443/tcp; fi
if [ -n "${EMAIL:-}" ]; then AKUN=(-m "$EMAIL"); else AKUN=(--register-unsafely-without-email); fi
if ! certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos "${AKUN[@]}" --redirect; then
  echo "Sertifikat HTTPS gagal dibuat. Pastikan firewall penyedia VPS membuka TCP 80 & 443,"
  echo "lalu jalankan ulang skrip ini. Bila tetap gagal dengan sslip.io, pakai domain sendiri: DOMAIN=..."
  exit 1
fi
# HSTS: browser selalu memakai HTTPS untuk alamat ini.
grep -q Strict-Transport-Security /etc/nginx/sites-available/indeso || \
  sed -i "0,/listen 443 ssl/ s|listen 443 ssl;|listen 443 ssl;\n    add_header Strict-Transport-Security \"max-age=31536000\" always;|" /etc/nginx/sites-available/indeso
nginx -t && systemctl reload nginx

cat <<SELESAI

==================================================================
 INDESO terpasang (belum dinyalakan).
 Dashboard nanti: https://${DOMAIN}

 Langkah berikutnya (lihat docs/vps-indeso.md):
 1. Tambahkan aturan MikroTik:
/ip firewall filter add chain=input in-interface=wg-acs src-address=10.99.0.1 protocol=tcp dst-port=8728 action=accept comment="INDESO VPS -> API MikroTik" place-before=0
/ip firewall nat add chain=srcnat src-address=10.99.0.1 dst-address=${OLT_SUBNET} action=masquerade comment="INDESO VPS -> OLT"
 2. Pindahkan data dari HP, lalu:
    sudo bash ${APP}/deploy/indeso/impor-data.sh ~/indeso-data.tgz
==================================================================
SELESAI
