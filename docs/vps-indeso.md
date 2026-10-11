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
| Update INDESO (setelah PR di-merge) | `sudo git -C /opt/indeso pull && sudo systemctl restart indeso` |
| Ubah pengaturan `.env` | `sudo nano /etc/indeso/indeso.env` lalu restart |
| Lokasi data | `/var/lib/indeso` (billing.db, nota/, backup/) |

Sertifikat HTTPS diperbarui otomatis oleh certbot. Backup harian dan mingguan ke Telegram tetap berjalan seperti biasa.

**Kembali ke HP (darurat):** jalankan `sudo systemctl stop indeso && sudo systemctl disable indeso` di VPS.
Setelah itu, salin balik `/var/lib/indeso/billing.db` ke HP sebelum INDESO dinyalakan di sana. Kalau tidak disalin, HP memakai data versi lama.
