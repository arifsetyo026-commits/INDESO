"""Format & baca nilai umum: rupiah, tanggal/periode, durasi, angka, kecepatan & rate-limit MikroTik.

Dipindah apa adanya dari main.py (Tahap 4, docs/rencana-perbaikan.md); main.py mengimpor kembali
semua nama di sini.
"""
import re
from datetime import datetime, timedelta
from typing import Optional


_UPTIME_RE = re.compile(r"(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


_RATE_TOKEN_RE = re.compile(r"([\d.]+)([kKmMgG]?)")


def parse_uptime_to_seconds(uptime: str) -> Optional[int]:
    """'3d8h27m32s' -> total detik. Format MikroTik: w(minggu) d h m s, semua opsional."""
    if not uptime or uptime == "-":
        return None
    m = _UPTIME_RE.fullmatch(uptime.strip())
    if not m:
        return None
    w, d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return ((w * 7 + d) * 24 + h) * 3600 + mi * 60 + s


def _parse_rate_token_to_mbps(token: str) -> Optional[float]:
    """'5M' -> 5.0, '512k' -> 0.512, '1G' -> 1000.0, '2000000' -> 2.0 (tanpa satuan = bit/s).
    Satuan MikroTik: k/M/G (desimal, bukan biner)."""
    m = _RATE_TOKEN_RE.match(token.strip())
    if not m:
        return None
    value, unit = m.groups()
    try:
        value = float(value)
    except ValueError:
        return None
    multiplier = {"": 0.000001, "k": 0.001, "K": 0.001, "m": 1.0, "M": 1.0, "g": 1000.0, "G": 1000.0}.get(unit, 1.0)
    return value * multiplier


def _parse_rate_limit(raw: Optional[str]) -> tuple[Optional[str], Optional[float]]:
    """'5M/10M' -> ("5M/10M", 10.0) — ambil angka TERBESAR dari dua sisi sebagai
    acuan kapasitas (biasanya sisi download yang jadi ukuran pemakaian utama)."""
    if not raw or raw in ("-", ""):
        return None, None
    parts = raw.split("/")
    values = [v for v in (_parse_rate_token_to_mbps(p) for p in parts) if v is not None]
    if not values:
        return raw, None
    return raw, max(values)


def _ringkas_limit(raw: str) -> str:
    """API mengembalikan max-limit dalam bit ("10000000/10000000") — jadikan
    "10M/10M" biar sama dengan tampilan Winbox."""
    def satu(v: str) -> str:
        v = v.strip()
        if not v.isdigit():
            return v
        n = int(v)
        if n >= 1_000_000_000 and n % 1_000_000_000 == 0:
            return f"{n // 1_000_000_000}G"
        if n >= 1_000_000:
            return f"{n / 1_000_000:g}M"
        return f"{n / 1_000:g}k" if n >= 1_000 else v
    return "/".join(satu(p) for p in raw.split("/"))


def _angka_stok(x: float) -> str:
    return f"{x:g}"


def _periode_valid(periode: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", periode))


def _tgl_indo(tgl: Optional[str]) -> str:
    if not tgl:
        return "-"
    y, m, d = tgl[:10].split("-")
    return f"{int(d)} {_BLN_PANJANG[int(m) - 1]} {y}"


def _tanggal_valid(t: str) -> bool:
    try:
        datetime.strptime(t, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def _bulan_ke(tanggal: str) -> int:
    return int(tanggal[:4]) * 12 + int(tanggal[5:7]) - 1


def _geser_periode(periode: str, n: int) -> str:
    th, bl = map(int, periode.split("-"))
    total = th * 12 + (bl - 1) + n
    return f"{total // 12}-{total % 12 + 1:02d}"


def _tambah_bulan(tanggal: str, n: int) -> str:
    """Tanggal yang sama n bulan kemudian; tanggal 29-31 menyesuaikan akhir bulan."""
    d = datetime.strptime(tanggal, "%Y-%m-%d")
    total = d.year * 12 + (d.month - 1) + n
    th, bl = total // 12, total % 12 + 1
    akhir = ((datetime(th + (bl == 12), bl % 12 + 1, 1)) - timedelta(days=1)).day
    return f"{th}-{bl:02d}-{min(d.day, akhir):02d}"


def _angka_qty(qty):
    """SQLite REAL mengembalikan 3.0 — tampilkan 3 bila bulat."""
    if qty is None:
        return None
    return int(qty) if float(qty).is_integer() else qty


def _teks_qty(qty, satuan) -> str:
    if not qty:
        return ""
    return f"{int(qty) if float(qty).is_integer() else qty:g} {satuan or 'pcs'}"


def _persen_ubah(ini: int, lalu: int) -> str:
    if not lalu:
        return ""
    p = round((ini - lalu) * 100 / abs(lalu))
    return " (sama)" if p == 0 else f" ({'▲' if p > 0 else '▼'}{abs(p)}%)"


def _sekarang_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _durasi_teks(mulai: str, sampai: Optional[str] = None) -> str:
    try:
        a = datetime.strptime(mulai, "%Y-%m-%d %H:%M:%S")
        b = datetime.strptime(sampai, "%Y-%m-%d %H:%M:%S") if sampai else datetime.now()
    except (TypeError, ValueError):
        return "-"
    return _teks_menit(int((b - a).total_seconds() // 60))


def _teks_menit(menit: int) -> str:
    menit = max(0, menit)
    if menit < 1:
        return "kurang dari 1 menit"
    if menit < 60:
        return f"{menit} menit"
    jam, menit = divmod(menit, 60)
    if jam < 24:
        return f"{jam} jam {menit} menit" if menit else f"{jam} jam"
    hari, jam = divmod(jam, 24)
    return f"{hari} hari {jam} jam" if jam else f"{hari} hari"


def _teks_durasi(menit: int) -> str:
    hari, sisa = divmod(int(menit), 1440)
    jam, mnt = divmod(sisa, 60)
    return " ".join(b for b in (f"{hari} hari" if hari else "", f"{jam} jam" if jam else "",
                                f"{mnt} menit" if mnt else "") if b) or "0 menit"


def _hp_baku(hp: Optional[str]) -> str:
    n = re.sub(r"\D", "", hp or "")
    return "0" + n[2:] if n.startswith("62") else ("0" + n if n.startswith("8") else n)


def _rp_publik(n) -> str:
    return "Rp" + f"{int(n or 0):,}".replace(",", ".")


_BLN_PANJANG = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
                "Agustus", "September", "Oktober", "November", "Desember"]


def _rp(n) -> str:
    n = int(n)
    return ("-" if n < 0 else "") + "Rp " + f"{abs(n):,}".replace(",", ".")


def _nama_periode(p: str) -> str:
    tahun, bulan = p.split("-")
    return f"{_BLN_PANJANG[int(bulan) - 1]} {tahun}"


def _waktu_iso(teks) -> Optional[datetime]:
    t = str(teks or "").strip()
    if not t or t.startswith("0001"):
        return None
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:?\d\d)?$", t)
    if not m:
        return None
    pecahan = (m.group(2) or ".0")[1:7].ljust(6, "0")
    zona = m.group(3) or "Z"
    zona = "+00:00" if zona == "Z" else (zona if ":" in zona else zona[:3] + ":" + zona[3:])
    try:
        return datetime.fromisoformat(f"{m.group(1)}.{pecahan}{zona}")
    except ValueError:
        return None


def _ms(t) -> Optional[float]:
    """'1ms234us' / '12ms' / '850us' → milidetik."""
    if not t:
        return None
    m = re.fullmatch(r"(?:(\d+)s)?(?:(\d+)ms)?(?:(\d+)us)?", str(t).replace(" ", ""))
    if not m or not any(m.groups()):
        return None
    return round(int(m.group(1) or 0) * 1000 + int(m.group(2) or 0) + int(m.group(3) or 0) / 1000, 1)


# --- Cek DNS & MTU di MikroTik (hanya membaca) ---
def _ya(v) -> Optional[bool]:
    v = str(v or "").strip().lower()
    return True if v in ("yes", "true") else (False if v in ("no", "false") else None)
