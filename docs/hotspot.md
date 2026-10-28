# Paket hotspot (voucher)

Paket baru selain langganan rumah: siapa pun di desa bisa membeli voucher dan memakai WiFi hotspot. Hotspot
dipancarkan oleh ONU pelanggan yang sudah tersebar, jadi jangkauannya ikut merata.

Keputusan owner (28 Okt 2026):

| Hal | Keputusan |
|---|---|
| Login | kode voucher |
| Masa aktif | waktu kalender sejak pertama login (mis. 1 hari = 24 jam sejak login, dipakai atau tidak) |
| Penjual | admin/owner saja |
| ONU yang memancarkan | semua ONU pelanggan |

## Cara kerja

```
HP pembeli ──WiFi "INTERNET DESA"──► ONU pelanggan ──VLAN hotspot──► OLT ──► MikroTik (Hotspot: halaman login)
HP pelanggan ──WiFi rumah──────────► ONU pelanggan ──PPPoE─────────► OLT ──► MikroTik (langganan biasa)
```

- WiFi hotspot adalah WiFi kedua di ONU, terpisah dari WiFi rumah. Jalurnya VLAN tersendiri, bukan PPPoE pelanggan:
  paket pelanggan rumah tidak terpakai, dan pemakai hotspot tidak bisa melihat perangkat di rumah itu.
- Sinyal WiFi satu ONU tetap dipakai bersama. Bila banyak pemakai hotspot di dekat satu rumah, WiFi rumah itu ikut
  lebih ramai; batas kecepatan per voucher mengurangi dampaknya.

## Yang sudah jadi di INDESO (menu **Hotspot**)

- **Paket** (diatur owner): nama, masa aktif (jam/hari), harga, kecepatan per pemakai, jumlah perangkat per voucher.
  INDESO membuat profil user hotspot `INDESO-HS-<id>` di MikroTik (`rate-limit`, `shared-users`).
- **Jual voucher** (admin/owner): pilih paket & jumlah (1–100), cara bayar. Urutannya:
  1. user hotspot dibuat di MikroTik (`/ip/hotspot/user`, nama = sandi = kode, komentar `INDESO voucher …`). Bila
     MikroTik menolak di tengah jalan, user yang sudah dibuat dihapus lagi dan tidak ada yang tercatat;
  2. voucher dan **satu bukti kas masuk** (kategori *Pendapatan voucher hotspot*, nomor BKM) dicatat.
  Kode 8 huruf/angka tanpa huruf yang mudah tertukar (0/o, 1/l/i). Kode tidak ditulis di log aktivitas.
- **Cetak** kartu voucher (3 kolom, A4) atau **kirim lewat WA** per voucher.
- **Kedaluwarsa otomatis** (cek tiap 60 detik, hanya bila ada voucher yang menunggu):
  - login pertama dibaca dari sesi hotspot di MikroTik (atau total waktu pakai bila pembeli sudah keluar), lalu
    *berlaku sampai* = login pertama + masa aktif. Taksiran ini tidak pernah lebih awal dari login sebenarnya;
  - begitu habis: sesi diputus, user hotspot dihapus, voucher berstatus *habis*.
  User hotspot tanpa komentar `INDESO` (buatan teknisi) tidak pernah disentuh.
- **Daftar voucher** (belum dipakai / aktif / habis / batal), **Sedang online**, ringkasan penjualan hari ini & bulan ini.
- **Batalkan penjualan** (uang dikembalikan) hanya bila semua vouchernya belum dipakai; bukti kasnya ikut batal. Bukti
  kas penjualan voucher tidak bisa diubah/dibatalkan dari menu Keuangan, supaya kas dan voucher selalu cocok.
- Nama WiFi di kartu voucher: **Pengaturan → Nama WiFi hotspot** (bawaan `INTERNET DESA`).

Selama hotspot belum disiapkan di MikroTik, menu Hotspot menampilkan peringatan; voucher bisa dibuat untuk uji tetapi
belum bisa dipakai pembeli.

## Belum dikerjakan: jaringan (menunggu data & persetujuan owner)

1. **Data (hanya membaca)**:
   - OLT, di `interface gpon 0/0`, ketik lalu tekan `?` tanpa Enter:
     `ont ipconfig 2 6 ip-index 1 connection-type ?`, `ont ipconfig 2 6 ip-index 1 dhcp ?`,
     `ont ipconfig 2 6 ip-index 1 binding ?`, `ont wifi 2 6 2.4g ?`.
     Tujuannya: apakah OLT bisa membuat jalur kedua (ip-index 1) mode bridge ber-VLAN yang diikat ke SSID 2.
   - MikroTik: `/interface print where type!="pppoe-in"`, `/interface pppoe-server server print`,
     `/interface vlan print`, `/ip hotspot print`.
2. **Uji satu ONU** (mengubah MikroTik & OLT, dengan persetujuan): VLAN hotspot + DHCP + Hotspot di MikroTik
   (batas total kecepatan hotspot supaya pelanggan rumah aman, pemakai hotspot tidak bisa ke jaringan internal),
   jalur VLAN di OLT, WiFi kedua di satu ONU uji, halaman login satu kolom (kode voucher), uji beli & login dari HP.
3. **Peluncuran ke semua ONU** lewat INDESO, bertahap per PON. Untuk ONU Huawei, WiFi hotspot diatur lewat ACS.
