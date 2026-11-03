# Hotspot voucher (dijual warung/reseller)

WiFi hotspot **terbuka (tanpa sandi)**, login dengan **voucher**. Voucher = user hotspot MikroTik dengan
`username = password = kode`, batas **total waktu pakai** (`limit-uptime`, boleh dicicil), dan profil kecepatan
`INDESO-HS-<id paket>` (`rate-limit`, `shared-users` = jumlah HP). MikroTik sendiri yang menghentikan voucher saat
waktunya habis, jadi tetap berlaku walau MikroTik tidak tersambung ke INDESO. Semua perintah berlaku untuk
**RouterOS v6 dan v7**.

## Sekali di MikroTik lokasi
1. Winbox → **IP → Hotspot → Hotspot Setup** pada interface WiFi hotspot (pool IP, DNS, dsb.).
2. Di INDESO: **Hotspot → Pengaturan**. Isi nama WiFi dan nama DNS login (mis. `hotspot.desa`), lalu
   **Tampilkan skrip pengaturan** dan tempel di Terminal MikroTik. Skrip ini mengatur `dns-name` dan
   `login-by=http-pap,http-chap,cookie` di server profile (ganti `hsprof1` dengan nama server profile Anda), serta
   profil kecepatan semua paket. `http-pap` diperlukan supaya QR login (`/login?username=..&password=..`) langsung
   masuk.
3. Nama WiFi (SSID) di pemancar dibuat sama persis dengan yang diisi di INDESO, tanpa sandi.

## Alur
1. **Owner** membuat paket di **Hotspot → Paket**: lama pakai, kecepatan, HP per voucher, harga eceran, dan
   **setoran reseller** per voucher (selisihnya keuntungan warung).
2. **Admin** membuat akun warung di **Pengaturan → Pengguna** (peran **Reseller**, pilih lokasinya).
3. **Titip voucher**: Hotspot → Reseller → **Titip voucher** (paket & jumlah).
   - Lokasi yang MikroTik-nya tersambung ke INDESO: user hotspot langsung dibuat (gagal di tengah = semua dibersihkan).
   - Lokasi **hanya penagihan**: INDESO menampilkan **skrip** untuk ditempel di Terminal MikroTik lokasi itu
     (aman ditempel ulang). Setelah ditempel, tekan **Sudah ditempel di MikroTik**; baru setelah itu voucher bisa dijual.
4. **Warung menjual** dari HP (login reseller): tekan **Jual** pada paketnya → layar menampilkan
   **QR 1 sambung WiFi** dan **QR 2 login** (unik per voucher) + kode cadangan. Voucher tercatat terjual saat QR tampil.
   Kartu kertas bisa dicetak dari **Cetakan voucher → Cetak kartu QR**.
5. **Setoran**: Hotspot → Reseller → **Terima setoran**: semua voucher terjual yang belum disetor × setoran reseller
   masuk kas sebagai *Pendapatan voucher hotspot* (bukti BKM). Kas ini tidak bisa diubah dari menu Keuangan; owner
   bisa membatalkan setoran dari menu Hotspot (voucher kembali "belum disetor").
6. **Tarik sisa** (mis. warung berhenti): voucher yang belum terjual dibatalkan; untuk lokasi hanya penagihan
   INDESO memberi skrip hapus untuk MikroTik.

Kode voucher tidak pernah ditulis di log aktivitas. Kode memakai huruf/angka yang tidak mudah tertukar (tanpa 0/o, 1/l/i).
