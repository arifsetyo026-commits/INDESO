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
  beserta akun pelakunya (menu Pengaturan). Buat akun terpisah untuk
  tiap petugas di Pengaturan → Pengguna.
- **Billing** — tagihan bulanan otomatis (dibuat di hari terakhir bulan), tandai lunas,
  pengingat WhatsApp, rekap belum bayar ke Telegram.
- **Isolir otomatis** — pelanggan yang lewat jatuh tempo dipindah ke profil PPP
  `ISOLIR`, dibuka lagi otomatis begitu tagihannya ditandai lunas.
- **Akun mitra** — akun berperan mitra yang hanya bisa membuka halaman mitra:
  daftar pelanggan dan status bayarnya, ubah nama/alamat/no. HP, dan rekap insentif
  (pelanggan dengan tagihan lunas bulan itu, ditambah pelanggan gratis yang dicentang
  "Gratis, tetap dihitung insentif mitra", × tarif per pelanggan, default Rp 10.000). Pelanggan yang tidak dicentang "Tampilkan ke mitra" (mis. akun server)
  tidak terlihat dan tidak dihitung. Semua API lain ditolak untuk akun mitra.
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

Buka `http://<ip-server>:8000`. Pada instalasi baru, halaman login meminta Anda membuat
**akun admin pertama**. Akun lain ditambahkan dari **Pengaturan → Pengguna**.

## Keamanan

- Semua halaman dan API wajib login. Sesi disimpan sebagai cookie `HttpOnly` +
  `SameSite=Strict`; berakhir setelah 12 jam tidak dipakai atau 7 hari sejak login.
- Password di-hash PBKDF2-SHA256 (200.000 iterasi); token sesi hanya disimpan hash-nya.
- Peran **admin** (akses penuh) dan **mitra** (hanya halaman mitra), dibatasi di server.
- 5 kali gagal login per IP (atau 10 per username) dalam 15 menit → dikunci 15 menit.
- Menonaktifkan akun, mengubah peran, atau reset password langsung mengeluarkan sesinya.
- Request pengubah data dari situs lain ditolak (cek Origin), ditambah header keamanan
  (CSP, X-Frame-Options, nosniff).
- Login, logout, login gagal, dan semua perubahan akun tercatat di **Log aktivitas**.

Saran operasional:
- `chmod 600 .env` — berkas ini berisi password MikroTik dan token Telegram.
- Koneksi masih HTTP. Untuk akses dari luar jaringan lokal, pasang HTTPS (mis. Cloudflare
  Tunnel atau reverse proxy); cookie otomatis bertanda `Secure` bila diakses lewat HTTPS.
  Jangan buka port 8000 langsung ke internet.
- Lupa password admin satu-satunya: hentikan server, lalu
  `sqlite3 billing.db "DELETE FROM akun; DELETE FROM sesi;"` dan buat admin baru dari
  halaman login (data pelanggan & tagihan tidak terhapus).

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

Tanggal jatuh tempo, mode isolir (aktif/simulasi), toleransi, jam otomasi, dan
nominal insentif mitra bisa diubah dari **Pengaturan → Sistem** dan langsung berlaku.
Pengaturan lain (ambang redaman, interval, dll.) ada di dict `CONFIG` di `main.py`.
