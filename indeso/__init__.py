"""Modul INDESO yang dipisah dari main.py per topik (Tahap 4, docs/rencana-perbaikan.md).

    format.py    rupiah, tanggal/periode, durasi, angka, kecepatan & rate-limit MikroTik
    keamanan.py  hash password & token sesi
    onu.py       nomor seri (SN) GPON, merek, MAC, alamat PON, query GenieACS
    qris.py      membaca & menyusun kode QRIS (TLV, CRC16, nominal)

main.py mengimpor kembali semua nama di modul-modul ini, jadi kode lama tetap bisa memanggil main.<nama>.
"""
