# Menjalankan INDESO di VPS

Dengan INDESO di VPS:
- dashboard bisa dibuka **dari mana saja** lewat HTTPS, termasuk oleh owner, admin, dan mitra;
- INDESO menyala 24 jam;
- INDESO tetap mengirim kabar ke Telegram ketika listrik atau internet kantor mati.

MikroTik dan OLT dijangkau lewat terowongan WireGuard yang sama dengan GenieACS (`docs/genieacs.md`).

> **Penting:** INDESO hanya boleh jalan di **satu tempat**. Setelah pindah ke VPS, INDESO di HP
> harus dimatikan. Kalau keduanya menyala, tagihan, isolir, dan pesan Telegram jadi dobel, dan
> data terpecah di dua tempat.

## 0. Prasyarat
- GenieACS + terowongan WireGuard sudah terpasang dan tersambung (`docs/genieacs.md`).
- Di panel penyedia VPS, firewall mengizinkan **TCP 80 dan 443** masuk. Keduanya dibutuhkan untuk sertifikat HTTPS.
- Tanpa domain, alamat dashboard dibuat otomatis: `https://indeso.<ip-vps-pakai-strip>.sslip.io`, misalnya `https://indeso.139-190-100-136.sslip.io`.
  - Kalau nanti punya domain, arahkan domain itu ke IP VPS, lalu jalankan ulang skrip dengan `DOMAIN=indeso.domainanda.my.id`.

## 1. Pasang INDESO di VPS
```bash
cd ~/INDESO && git pull
sudo bash deploy/indeso/install.sh
```
Skrip ini melakukan:
- memasang Python, `snmpwalk`, nginx, dan certbot;
- menyalin aplikasi ke `/opt/indeso`;
- membuat layanan `indeso` (**belum dinyalakan**);
- menambahkan subnet OLT ke terowongan;
- membuat sertifikat HTTPS.

Di akhir, skrip mencetak **2 perintah MikroTik**.

## 2. Izinkan VPS mengakses MikroTik & OLT
Tempel kedua perintah dari langkah 1 ke Terminal MikroTik:
- perintah pertama mengizinkan API MikroTik (port 8728) dari VPS;
- perintah kedua membuat akses SNMP ke OLT memakai alamat MikroTik. Dengan begitu OLT tidak perlu tahu rute ke VPS, dan ACL SNMP di OLT tetap terpenuhi.

Lalu periksa dua batasan yang mungkin sudah Anda pasang:
```
/ip service print where name=api
/user print where name=INDESO
```
Kalau kolom **address** berisi daftar IP, misalnya `192.168.50.0/24`, tambahkan `10.99.0.0/24`:
```
/ip service set api address=192.168.50.0/24,10.99.0.0/24
/user set INDESO address=192.168.50.0/24,10.99.0.0/24
```

Uji dari VPS:
```bash
nc -zv 10.99.0.2 8728                                     # harus "succeeded"
snmpwalk -v2c -c public -t 3 192.168.2.100 1.3.6.1.2.1.1.1 # ganti "public" dengan community OLT Anda
```

## 3. Bungkus data di HP (Termux)
```bash
cd ~/storage/shared/INDESO26        # folder INDESO di HP
pkill -f uvicorn                    # hentikan INDESO di HP dulu supaya database tidak sedang ditulis
tar czf ~/indeso-data.tgz $(ls -d billing.db isp_history.db nota backup .env 2>/dev/null)
pkg install -y openssh              # sekali saja, untuk perintah scp
scp ~/indeso-data.tgz UNICORN@139.190.100.136:~/
```
`scp` akan meminta password SSH VPS, sama dengan saat Anda login ke VPS.

## 4. Impor data & nyalakan di VPS
```bash
sudo bash /opt/indeso/deploy/indeso/impor-data.sh ~/indeso-data.tgz
```
Skrip ini melakukan:
- memeriksa keutuhan `billing.db`;
- menyalin data ke `/var/lib/indeso`;
- menyalin `.env` ke `/etc/indeso/indeso.env`, dengan `MIKROTIK_HOST=10.99.0.2` dan GenieACS langsung lewat localhost;
- menyalakan layanan.

Buka `https://indeso.139-190-100-136.sslip.io` dan login dengan akun yang sama seperti di HP.
Mulai sekarang, **jangan nyalakan lagi INDESO di HP.**

## Perawatan
| Keperluan | Perintah di VPS |
|---|---|
| Lihat log langsung | `sudo journalctl -u indeso -f` |
| Restart | `sudo systemctl restart indeso` |
| Update INDESO (setelah PR di-merge) | `sudo bash /opt/indeso/deploy/indeso/update.sh` (otomatis kembali ke versi lama bila gagal) |
| Cek kesehatan | `curl -s http://127.0.0.1:8000/healthz` |
| Ubah pengaturan `.env` | `sudo nano /etc/indeso/indeso.env` lalu restart |
| Lokasi data | `/var/lib/indeso` (billing.db, nota/, backup/) |
| Lupa password dashboard | `sudo -u indeso python3 /opt/indeso/deploy/indeso/reset-password.py` lalu `sudo systemctl restart indeso` |

Sertifikat HTTPS diperbarui otomatis oleh certbot.

**Backup.** Setiap hari setelah jam 02.00, `billing.db` dibackup ke `/var/lib/indeso/backup` (14 hari terakhir) dan dikirim ke Telegram owner tanpa bunyi notifikasi. Hari Minggu, kirimannya berbunyi dan ikut mengirim ZIP nota. Untuk kembali ke pengiriman mingguan saja, isi `BACKUP_KIRIM_HARIAN=0` di `.env`.

**Pemantau dari luar.** Daftarkan `https://<alamat-dashboard>/healthz` di layanan pemantau gratis (mis. UptimeRobot, cek tiap 5 menit, kirim notifikasi ke email/Telegram). Alamat ini membalas 503 bila loop pemantauan INDESO macet, dan tidak bisa dihubungi sama sekali bila VPS/INDESO mati. Server GenieACS yang tidak merespons ±15 menit dilaporkan otomatis ke Telegram.

### Bila VPS hilang / rusak total
1. Siapkan VPS Ubuntu baru, pasang ulang GenieACS (`deploy/genieacs/install.sh`) dan INDESO (`deploy/indeso/install.sh`).
2. Unduh backup `billing-YYYY-MM-DD.db` terbaru dari chat Telegram owner, dan ZIP nota terakhir bila ada.
3. Salin ke VPS (layanan dalam keadaan mati): `sudo systemctl stop indeso; sudo rm -f /var/lib/indeso/billing.db-wal /var/lib/indeso/billing.db-shm; sudo cp billing-*.db /var/lib/indeso/billing.db && sudo chown indeso:indeso /var/lib/indeso/billing.db`. Ekstrak ZIP nota ke `/var/lib/indeso/nota/`.
4. Isi `/etc/indeso/indeso.env` (simpan salinan isinya di tempat aman, mis. pengelola sandi, karena berkas ini tidak ikut backup).
5. `sudo systemctl enable --now indeso`, lalu ganti alamat ACS di OLT/ONU bila IP VPS berubah. ONU akan lapor ulang ke GenieACS dengan sendirinya.

**Kembali ke HP (darurat):** jalankan `sudo systemctl stop indeso && sudo systemctl disable indeso` di VPS.
Setelah itu, salin balik `/var/lib/indeso/billing.db` ke HP sebelum INDESO dinyalakan di sana. Kalau tidak disalin, HP memakai data versi lama.

## Keamanan (ringkasan)
- Dashboard hanya lewat HTTPS (HSTS). Cookie sesi `HttpOnly`, `Secure`, dan `SameSite=Strict`, ditambah pemeriksaan Origin untuk setiap perubahan data.
- Login dibatasi 5 percobaan gagal per IP dan 10 percobaan per username dalam 15 menit. IP asli pengunjung diteruskan oleh nginx (`--proxy-headers`). Password disimpan dengan PBKDF2-SHA256 600.000 iterasi.
- `/docs` dan `/openapi.json` dimatikan.
- INDESO hanya mendengarkan di `127.0.0.1:8000`. GenieACS (API/UI), MongoDB, dan PostgreSQL hanya dapat diakses dari localhost atau terowongan.
- Web admin GenieACS (`10.99.0.1:3000`) hanya untuk jaringan kantor (`LAN_SUBNET`) dan MikroTik. Pelanggan PPPoE hanya bisa menjangkau port lapor ONU (7547).
- SSH hanya menerima kunci, tanpa password.
