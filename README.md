# INDESO — Monitoring & Manajemen Pelanggan Internet Desa

Sistem manajemen pelanggan untuk ISP kecil (tier 3) berbasis FastAPI, dirancang
berjalan 24/7 di Termux (Android) atau server Linux kecil.

## Fitur

- **Monitoring** — pelanggan PPPoE aktif dan bandwidth dari MikroTik (RouterOS API),
  redaman/kesehatan ONU dari OLT C-Data (SNMP). ONU dicocokkan ke pelanggan lewat MAC.
- **Alert Telegram** — redaman kritis, pelanggan putus lebih dari 10 menit, gangguan
  massal, laporan harian. Perintah bot: `/cek nama`, `/masalah`, `/boncos`, `/putus`.
- **Data pelanggan & paket** — kelola paket, impor pelanggan dari PPP secret MikroTik,
  nama/alamat/no. HP, pelanggan gratis, status Aktif/Berhenti (yang berhenti tidak
  ditagih, tidak diisolir otomatis, tidak dipantau putusnya).
- **Log aktivitas** — setiap pelunasan, isolir, perubahan pelanggan/paket tercatat
  beserta akun pelakunya (menu Pengaturan). Gunakan `DASHBOARD_USERS` agar tiap
  petugas punya akun sendiri.
- **Billing** — tagihan bulanan otomatis (dibuat di hari terakhir bulan), tandai lunas,
  pengingat WhatsApp, rekap belum bayar ke Telegram.
- **Isolir otomatis** — pelanggan yang lewat jatuh tempo dipindah ke profil PPP
  `ISOLIR`, dibuka lagi otomatis begitu tagihannya ditandai lunas.
- **Kesehatan jaringan** — skor per pelanggan (redaman, kestabilan sinyal, reconnect,
  ketersediaan) beserta saran untuk teknisi.
- **Backup harian** `billing.db` ke folder `backup/`, dikirim mingguan ke Telegram.

## Instalasi

```bash
# Termux: pkg install python net-snmp
# Debian/Ubuntu: apt install python3-pip snmp
pip install -r requirements.txt
cp .env.example .env      # lalu isi password MikroTik, token Telegram, password dashboard
uvicorn main:app --host 0.0.0.0 --port 8000
```

Buka `http://<ip-server>:8000`. Bila `DASHBOARD_PASSWORD` atau `DASHBOARD_USERS` diisi,
browser akan meminta login.

## Persiapan data awal

1. Menu **Pelanggan → Kelola paket**: tambahkan paket (nama, kecepatan, harga).
2. Menu **Pelanggan → Impor dari MikroTik**: pelanggan diambil dari PPP secret. Paket
   dicocokkan dari nama profil PPP (mis. profil `paket-10mbps` ↔ paket 10 Mbps).
   Pelanggan yang secret-nya sudah tidak ada di MikroTik ikut dilaporkan supaya bisa
   ditandai Berhenti.
3. Lengkapi alamat, no. HP, dan paket tiap pelanggan (filter **Belum lengkap**).

## Isolir

Buat profil PPP bernama `ISOLIR` di MikroTik (cek dengan `GET /api/isolir-cek`).
Untuk uji coba tanpa benar-benar memutus pelanggan, ubah `ISOLIR_MODE` di `main.py`
menjadi `"simulasi"`. Isolir otomatis ditahan bila jumlah pelanggan menunggak melebihi
`ISOLIR_MAKS_SEKALI`.

## Konfigurasi lain

Jadwal, ambang redaman, tanggal jatuh tempo, dan pengaturan lain ada di dict `CONFIG`
di bagian atas `main.py`.
