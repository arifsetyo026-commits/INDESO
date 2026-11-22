# Peta database INDESO

INDESO menyimpan data di dua berkas SQLite di `/var/lib/indeso`. Semua tabel disiapkan otomatis oleh
`siapkan_database()` saat INDESO menyala (tabel/kolom baru ditambahkan, data lama tidak diubah).

| Berkas | Isi | Backup |
|---|---|---|
| `billing.db` | Semua data usaha: pelanggan, tagihan, keuangan, akun, gudang, tiket, hotspot | Harian, 14 hari, dikirim ke Telegram owner |
| `isp_history.db` | Riwayat teknis: redaman & trafik 30 hari, event, catatan ONU | Harian, 3 hari, **hanya di VPS** (terlalu besar untuk Telegram) |

Backup ada di `/var/lib/indeso/backup` (`billing-TANGGAL.db`, `riwayat-TANGGAL.db`). Periksa umurnya dengan
`sudo bash /opt/indeso/deploy/indeso/cek.sh`.

## billing.db

| Kelompok | Tabel | Isi |
|---|---|---|
| Pelanggan | `pelanggan` | Data pelanggan: username PPPoE, nama, paket, lokasi, status aktif/berhenti |
| | `paket` | Paket internet & harga |
| | `lokasi` | Wilayah (Pusat, Dempel, …) beserta MikroTik-nya dan pemegang kas |
| Penagihan | `tagihan` | Tagihan per pelanggan per bulan: belum / lunas, cara bayar, siapa yang mencatat |
| | `pengingat` | Riwayat pengingat WA yang dikirim |
| | `isolir` | Pelanggan yang sedang diisolir dan profil asalnya |
| | `konfirmasi_bayar` | Bukti bayar QRIS dari portal yang menunggu diperiksa |
| | `qris_bayar` | QRIS toko yang dipakai di portal |
| Keuangan | `transaksi` | Uang masuk & keluar (kas Pusat atau kas wilayah) |
| | `kategori_kas` | Kategori transaksi |
| | `utang`, `utang_cicilan` | Utang ke pemasok dan jadwal cicilannya |
| | `tutup_buku` | Bulan yang sudah ditutup (tidak bisa diubah lagi) |
| | `kas_setoran` | Setoran kas wilayah ke Pusat + bukti transfer |
| | `komisi_kunci` | Komisi mitra yang sudah dikunci per bulan per lokasi |
| | `nota` | Foto nota/bukti yang diunggah (berkasnya di folder `nota/`) |
| Akun & keamanan | `akun`, `sesi` | Akun staf (owner/admin/wilayah/mitra/reseller) dan sesi login |
| | `portal_akun`, `portal_sesi` | Akun & sesi Portal Pelanggan |
| | `aktivitas` | Catatan siapa melakukan apa dan kapan (audit) |
| Operasional | `tiket`, `tiket_log` | Tiket gangguan dan riwayat penanganannya |
| | `gudang_barang`, `gudang_mutasi` | Barang gudang; stok dihitung dari riwayat keluar-masuk |
| | `material_minta`, `material_minta_item` | Permintaan material dari admin wilayah |
| Hotspot | `hs_paket`, `hs_batch`, `hs_voucher`, `hs_setoran` | Paket voucher, cetakan, voucher, setoran reseller |
| Sistem | `pengaturan` | Pengaturan yang diubah dari dashboard |
| | `status_sistem`, `otomasi_log` | Penanda pekerjaan otomatis (mis. backup/tagihan hari ini sudah jalan) |

## isp_history.db

| Tabel | Isi |
|---|---|
| `onu_history` | Redaman & status setiap ONU (dibuang otomatis setelah 30 hari) |
| `usage_history` | Kecepatan pemakaian per pelanggan (30 hari) |
| `upstream_history`, `upstream_trafik` | Ping & trafik ke ISP (30 hari) |
| `event` | Riwayat Alert & Event (90 hari) |
| `onu_baru` | Catatan setiap ONU yang pernah terlihat: pemakai terakhir, bekas siapa, status aktivasi |
| `acs_tugas`, `ping_onu`, `ping_cara2`, `speedtest_onu` | Riwayat perintah ke ONU lewat ACS, ping, dan speedtest |

## Bila bermasalah

- **Periksa keutuhan:** `cek.sh` menjalankan `PRAGMA quick_check` pada kedua berkas (hanya membaca).
- **Database rusak:** hentikan INDESO (`sudo systemctl stop indeso`), salin dulu berkas yang rusak ke tempat lain, lalu
  pulihkan dari backup terakhir dan nyalakan lagi. Mintalah bantuan pengembang bila ragu.
- **Melihat isi tanpa mengubah:** `sudo sqlite3 -readonly /var/lib/indeso/billing.db "SELECT COUNT(*) FROM pelanggan"`.
