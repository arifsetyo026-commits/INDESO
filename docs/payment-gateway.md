# Persiapan verifikasi payment gateway (akun perorangan)

Payment gateway (mis. Tripay, iPaymu) memeriksa calon merchant sebelum akun bisa menerima uang. Syarat persisnya
berbeda tiap penyedia dan bisa berubah, jadi cek halaman pendaftaran resminya. Yang umumnya diminta untuk akun
**perorangan** (tanpa badan usaha):

## Dokumen pemilik
- KTP (foto jelas) dan swafoto memegang KTP.
- Rekening bank **atas nama yang sama dengan KTP** (untuk pencairan dana).
- NPWP pribadi: sebagian penyedia meminta, sebagian opsional untuk akun personal.
- Nomor HP dan email aktif.

## Website (sudah tersedia di INDESO)
Pemeriksa membuka website usaha. Alamat yang didaftarkan: **https://app.internetdesa.biz.id/**

| Syarat umum | Halaman INDESO |
|---|---|
| Website aktif & bisa dibuka publik (HTTPS) | `/` — beranda publik (pengunjung tanpa login; petugas yang login tetap melihat dashboard) |
| Deskripsi usaha & area layanan | Beranda → *Layanan kami* |
| Produk & harga jelas (Rupiah) | Beranda → *Paket & harga* (otomatis dari menu Paket) |
| Cara berlangganan & cara bayar | Beranda → *Cara berlangganan*, *Tagihan & cara pembayaran* |
| Kontak (WA, email, alamat, penanggung jawab) | Beranda → *Kontak* (dan di setiap halaman kebijakan) |
| Syarat & Ketentuan | `/syarat-ketentuan` |
| Kebijakan Privasi | `/kebijakan-privasi` |
| Kebijakan Pengembalian Dana | `/kebijakan-pengembalian-dana` |
| Halaman pelanggan membayar | `/portal` (tagihan, riwayat bayar, kwitansi) |

## Yang harus diisi owner sebelum mendaftar
Dashboard → **Pengaturan**, bagian *Halaman publik*:
- Deskripsi usaha, nama pemilik/penanggung jawab (sesuai KTP), alamat usaha, area layanan, nomor WhatsApp, email,
  jam layanan.
- *Cara bayar di pesan WA* juga tampil di beranda.
- Hapus paket yang tidak dijual lagi (menu Paket), karena semua paket tampil di beranda.

Baca ulang ketiga halaman kebijakan. Isinya draf umum (bukan nasihat hukum); sesuaikan bila ada aturan yang berbeda,
misalnya batas kompensasi gangguan (3 hari) atau batas pengajuan pengembalian dana (30 hari).

## Setelah akun disetujui
Kunci API **tidak dikirim ke chat**; dimasukkan langsung di VPS. Integrasi pembayaran QRIS di Portal Pelanggan
(lunas & buka isolir otomatis) dikerjakan terpisah.
