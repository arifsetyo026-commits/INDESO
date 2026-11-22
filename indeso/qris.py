"""Membaca & menyusun kode QRIS (EMVCo): TLV, kode cek CRC16, dan menyisipkan nominal ke QRIS statis.

Dipindah apa adanya dari main.py (Tahap 4, docs/rencana-perbaikan.md); main.py mengimpor kembali
semua nama di sini.
"""
import re


def _crc16_qris(data: str) -> str:
    crc = 0xFFFF
    for b in data.encode("utf-8"):
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"


def _tlv(s: str) -> list:
    hasil, i = [], 0
    while i < len(s):
        if i + 4 > len(s):
            raise ValueError("struktur QRIS terpotong")
        tag, n = s[i:i + 2], s[i + 2:i + 4]
        if not (tag.isdigit() and n.isdigit()):
            raise ValueError("struktur QRIS tidak valid")
        n = int(n)
        val = s[i + 4:i + 4 + n]
        if len(val) != n:
            raise ValueError("struktur QRIS terpotong")
        hasil.append((tag, val))
        i += 4 + n
    return hasil


def _baca_qris(payload: str) -> dict:
    """Periksa kode QRIS (format EMV + CRC) dan ambil nama merchant, kota, NMID. ValueError bila tidak sah."""
    p = (payload or "").strip()
    if not p.startswith("000201") or len(p) > 512:
        raise ValueError("bukan kode QRIS (harus diawali 000201)")
    if not re.fullmatch(r"6304[0-9A-Fa-f]{4}", p[-8:]):
        raise ValueError("kode cek (CRC) QRIS tidak ditemukan")
    if _crc16_qris(p[:-4]) != p[-4:].upper():
        raise ValueError("kode cek (CRC) QRIS tidak cocok: kode rusak atau sudah diubah")
    data = dict(_tlv(p))
    if data.get("53") != "360":
        raise ValueError("QRIS bukan mata uang Rupiah")
    nmid = None
    for tag in [f"{n:02d}" for n in range(26, 52)]:
        if tag in data:
            try:
                for st, v in _tlv(data[tag]):
                    if st == "02" and v.startswith("ID"):
                        nmid = nmid or v
            except ValueError:
                pass
    merchant = (data.get("59") or "").strip()
    if not merchant:
        raise ValueError("nama merchant tidak ada di QRIS")
    return {"merchant": merchant, "kota": (data.get("60") or "").strip(), "nmid": nmid,
            "nominal_tetap": data.get("54"), "statis": data.get("01") == "11"}


def _qris_bernominal(payload: str, jumlah: int) -> str:
    """QRIS statis -> QRIS dengan nominal tagihan (tag 54). Tujuan pembayaran (merchant) tidak berubah."""
    bagian = [(t, v) for t, v in _tlv(payload.strip()[:-8]) if t not in ("54", "63")]
    bagian = [(t, "12" if t == "01" else v) for t, v in bagian]
    nominal = str(int(jumlah))
    idx = next((i for i, (t, _v) in enumerate(bagian) if t > "54"), len(bagian))
    bagian.insert(idx, ("54", nominal))
    isi = "".join(f"{t}{len(v):02d}{v}" for t, v in bagian) + "6304"
    return isi + _crc16_qris(isi)
