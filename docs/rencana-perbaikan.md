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
| **1** ✅ | Perintah **`cek.sh`** (periksa INDESO sekali jalan, hasil ✅/⚠️/❌ + saran) · kompresi gzip · banner jadi berkas statis yang di-cache · rapikan keterangan & penyamaran log | Rendah | Troubleshooting jauh lebih mudah; halaman ±80% lebih ringan |
| **2** ✅ | Satu tempat untuk struktur database (daftar migrasi bernomor, dijalankan sekali saat INDESO menyala); `_siapkan_*` tidak lagi dipanggil di tiap permintaan; backup ikut `isp_history.db` | Sedang | Lebih cepat; struktur database jelas |
| **3** ✅ | Pembantu `with db() as conn:` menggantikan pola buka/tutup manual, dikerjakan per fitur | Rendah–sedang | Kode lebih pendek, tidak ada koneksi yang lupa ditutup |
| **4** | Pecah `main.py` menjadi modul per fitur (`indeso/pelanggan.py`, `keuangan.py`, `olt.py`, `acs.py`, …), satu modul per PR, `main:app` tetap sama | Sedang | Mudah dicari, perubahan tidak merembet |
| **5** | Pecah fungsi > 100 baris (isolir, tagihan harian, laporan bulanan) menjadi langkah-langkah kecil bernama jelas | Sedang | Mudah dibaca & diuji |

## Catatan pengerjaan

- **Tahap 1 (PR #112, #113):** `cek.sh`; dashboard 634 KB → 110 KB per buka (gzip + banner di-cache).
- **Tahap 2:** `siapkan_database()` menyiapkan semua tabel saat menyala; fungsi `_siapkan_*` ditandai
  `@_skema_sekali` sehingga tidak lagi membuat tabel di setiap permintaan (2,4 ms → 0,04 ms per panggilan, belum
  termasuk tulis ke disk); backup harian ikut menyalin `isp_history.db` (3 hari, hanya di VPS);
  peta tabel di `docs/database.md`.
- **Tahap 3:** pembantu `db()` (`with db() as conn:` / `db("riwayat")` / `db(polos=True)`) menggantikan 241 pola
  buka–`try`–`finally: close()`; `main.py` ±700 baris lebih pendek. Diubah mekanis dan **dibuktikan setara** dengan
  membandingkan pohon kode (AST) lama yang dinormalkan dengan yang baru. 21 sisanya sengaja dibiarkan (blok dengan
  `except`, backup yang membuka dua database, dan definisi pembantunya sendiri).
- **Tahap 4 (bagian 1):** folder `indeso/` dengan modul fungsi murni: `format`, `keamanan`, `onu`, `qris` (44 fungsi +
  7 konstanta). Dipindah apa adanya (dibuktikan identik per fungsi; sisa `main.py` tidak berubah) dan diimpor kembali
  oleh `main.py`. `update.sh` ikut memeriksa folder `indeso/`. Berikutnya: fitur yang punya jalur API sendiri
  (QRIS & konfirmasi bayar, gudang & material, hotspot, wilayah) dipindah satu per satu memakai `APIRouter`.
- **Tahap 4 (bagian 2, QRIS):** `indeso/qris_bayar.py` (10 jalur API, 16 fungsi/kelas) memakai `APIRouter`; pembantu
  inti lewat `indeso/inti.py` (dibaca saat dipakai, jadi tidak ada impor melingkar dan tiruan di tes tetap berlaku).
  Dibuktikan: kode fitur identik dengan aslinya setelah aturan `nama → inti.nama` & `@app → @router`; sisa `main.py`
  tidak berubah; tidak ada nama yang tak terdefinisi.
- **Tahap 4 (bagian 3, gudang & material):** `indeso/gudang.py` (13 jalur API, 35 fungsi/kelas/konstanta), cara &
  bukti yang sama; `main.py` 14.680 → 14.154 baris.
- **Tahap 4 (bagian 4, hotspot):** `indeso/hotspot.py` (15 jalur API, 36 fungsi/kelas/konstanta); `main.py` → 13.634
  baris. Komentar judul bagian yang tertinggal di `main.py` (QRIS, gudang, hotspot) diganti penunjuk ke modulnya.
- **Tahap 4 (bagian 5, admin wilayah):** `indeso/wilayah.py` (4 jalur API); `_latest_data` dibaca lewat `inti` supaya
  selalu nilai terbaru (bukan nilai saat modul dimuat). `main.py` → 13.495 baris. Daftar fitur awal Tahap 4 selesai;
  bagian besar yang masih di `main.py` (keuangan, tagihan & isolir, ONU/ACS, OLT, portal, pemantauan) bisa dipindah
  dengan cara yang sama bila diperlukan.
- **Tahap 5 (bagian 1, isolir):** `proses_isolir` (133 baris) dipecah menjadi langkah bernama: `_isolir_layak_dibuka`,
  `_isolir_buka_yang_lunas` (A), `_isolir_harian`, `_isolir_kandidat`, `_kabar_isolir_manual`, `_teks_daftar_isolir`,
  `_jalankan_isolir` (B). Isi & urutan perintah sama. Dibuktikan dengan **uji banding**: versi lama dan baru dijalankan
  pada 500 skenario acak dari keadaan database yang sama (semua jalur: buka, isolir, gagal, simulasi, ditahan, isolir
  manual); pesan Telegram, log, aktivitas, panggilan MikroTik, dan isi database setelahnya identik.
- **Tahap 5 (bagian 2, tagihan harian):** `proses_tagihan_harian` dipecah: `_tagihan_otomatis_buat`
  (`_periode_tagihan_otomatis`, `_kabar_tagihan_dibuat`), `_tagihan_otomatis_ingatkan` (`_judul_jatuh_tempo`), dan
  penanda `_otomasi_sudah` / `_otomasi_tandai`. Uji banding 1.500 skenario (buat_tagihan asli ikut jalan; semua
  variasi: dibuat, terlambat, diperbarui, sudah siap, N hari lagi, hari ini, lewat, semua lunas): identik.

Setiap tahap: tes otomatis lulus, diff dibaca ulang, PR kecil, dan cara kembali ke versi lama tercantum.
