"""Modul INDESO yang dipisah dari main.py per topik (Tahap 4, docs/rencana-perbaikan.md).

    format.py    rupiah, tanggal/periode, durasi, angka, kecepatan & rate-limit MikroTik
    keamanan.py  hash password & token sesi
    onu.py       nomor seri (SN) GPON, merek, MAC, alamat PON, query GenieACS
    qris.py      membaca & menyusun kode QRIS (TLV, CRC16, nominal)

Fitur lengkap (punya jalur API sendiri, didaftarkan main.py lewat app.include_router):
    qris_bayar.py  QRIS toko di portal + konfirmasi bukti bayar (/api/qris, /api/portal/bayar|konfirmasi,
                   /api/konfirmasi-bayar)
    gudang.py      Warehouse: barang, stok dari riwayat mutasi, pakai/opname/batal + permintaan material dari admin
                   wilayah (/api/gudang/*, /api/wilayah/material*)

    inti.py      pintu ke pembantu yang masih di main.py (db, CONFIG, cek login, aktivitas, Telegram), dibaca saat dipakai

main.py mengimpor kembali semua nama di modul-modul ini, jadi kode lama tetap bisa memanggil main.<nama>.
"""
