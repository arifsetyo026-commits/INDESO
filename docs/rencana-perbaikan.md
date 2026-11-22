# Audit & rencana perbaikan INDESO

Diaudit 22 Nov 2026 (versi `b08b63c`). Tujuannya: INDESO lebih efisien, kodenya lebih tertata, mudah dipahami orang
awam, dan mudah dilacak saat ada masalah. Perbaikan dikerjakan **bertahap**: satu tahap = satu PR kecil yang lolos
semua tes, supaya kalau ada yang salah mudah dikembalikan.

## Kondisi sekarang (data)

| Bagian | Angka |
|---|---|
| `main.py` | 15.835 baris, 635 fungsi, 73 kelas data, **217 jalur API** dalam **satu berkas** |
| Fungsi > 100 baris | 11 (terpanjang `proses_isolir` 137 baris) · > 60 baris: 32 |
| Buka/tutup database manual | ±250 kali (`conn = …` / `finally: conn.close()`) |
| Pembuatan/ubah tabel | 53 `CREATE TABLE`, 37 `ALTER TABLE`, tersebar di **14 fungsi** |
| Pembuatan tabel di tengah permintaan | 8 fungsi `_siapkan_*` dipanggil **55 kali** di jalur API (dijalankan ulang setiap klik) |
| Penanganan galat | 422 pesan galat untuk pengguna (bahasa Indonesia, bagus) · 98 `except Exception` |
| `index.html` | **634 KB** dan diunduh ulang setiap halaman dibuka (219 KB di antaranya foto banner) |
| Kompresi HTTP | tidak ada (halaman & data API dikirim mentah) |
| Tes | 126 tes otomatis, ±2,5 menit, semua lulus |

Jalur API terbanyak: keuangan 33, portal 14, hotspot 11, gudang 10, ACS 10, ONU baru 10, tagihan 9, mitra 9.

## Yang sudah baik (dipertahankan)

- Semua pesan galat & catatan aktivitas berbahasa Indonesia dan menjelaskan langkah perbaikan.
- Rahasia disamarkan di log INDESO; password WiFi/PPPoE tidak pernah dikirim ke browser.
- Update aman (`update.sh` kembali otomatis ke versi lama bila gagal), backup harian diperiksa keutuhannya.
- Pemantauan latar belakang berjalan di thread terpisah, dan tampilan mengambil data dengan jeda yang menyesuaikan.
- Tes otomatis mencakup hampir semua fitur penting.

## Masalah yang ditemukan

1. **Sulit dilacak saat bermasalah.** Tidak ada satu perintah yang memeriksa semuanya (layanan, database, backup,
   MikroTik, OLT, ACS, Telegram, terowongan, ruang disk, jam). Saat ini harus menebak dan mengetik banyak perintah.
2. **Boros bandwidth.** Halaman dashboard 634 KB tanpa kompresi diunduh setiap kali dibuka, termasuk foto banner yang
   sama berulang (juga di halaman admin wilayah). Di sinyal desa ini terasa lambat.
3. **Struktur database tersebar.** Tabel dibuat/diubah di 14 tempat; sebagian dijalankan ulang di setiap permintaan.
   Sulit menjawab "tabel apa saja yang ada dan kapan kolomnya ditambah".
4. **Satu berkas raksasa.** 15.835 baris dalam `main.py` membuat perubahan kecil berisiko menyentuh fitur lain dan
   menyulitkan orang baru mencari kode.
5. **Pola berulang.** Buka/tutup database ditulis manual ±250 kali.
6. **Hal kecil:** keterangan di awal `main.py` masih menyebut Termux (sekarang VPS); penyamaran rahasia belum berlaku
   untuk log uvicorn; backup harian hanya `billing.db` (catatan ONU/riwayat di `isp_history.db` tidak ikut).

## Rencana bertahap

| Tahap | Isi | Risiko | Manfaat |
|---|---|---|---|
| **1** | Perintah **`cek.sh`** (periksa INDESO sekali jalan, hasil ✅/⚠️/❌ + saran) · kompresi gzip · banner jadi berkas statis yang di-cache · rapikan keterangan & penyamaran log | Rendah | Troubleshooting jauh lebih mudah; halaman ±80% lebih ringan |
| **2** | Satu tempat untuk struktur database (daftar migrasi bernomor, dijalankan sekali saat INDESO menyala); `_siapkan_*` tidak lagi dipanggil di tiap permintaan; backup ikut `isp_history.db` | Sedang | Lebih cepat; struktur database jelas |
| **3** | Pembantu `with db() as conn:` menggantikan pola buka/tutup manual, dikerjakan per fitur | Rendah–sedang | Kode lebih pendek, tidak ada koneksi yang lupa ditutup |
| **4** | Pecah `main.py` menjadi modul per fitur (`indeso/pelanggan.py`, `keuangan.py`, `olt.py`, `acs.py`, …), satu modul per PR, `main:app` tetap sama | Sedang | Mudah dicari, perubahan tidak merembet |
| **5** | Pecah fungsi > 100 baris (isolir, tagihan harian, laporan bulanan) menjadi langkah-langkah kecil bernama jelas | Sedang | Mudah dibaca & diuji |

Setiap tahap: tes otomatis lulus, diff dibaca ulang, PR kecil, dan cara kembali ke versi lama tercantum.
