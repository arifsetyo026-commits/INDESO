"""Warehouse (gudang) & permintaan material dari admin wilayah.

Stok tidak disimpan sebagai angka yang bisa ditimpa: dihitung dari riwayat mutasi yang tidak bisa diubah (hanya dibatalkan
dengan alasan), jadi setiap perubahan stok jelas siapa, kapan, dan untuk apa. Admin wilayah meminta material (dikabarkan
ke grup admin Telegram); Pusat memprosesnya di Warehouse -> Permintaan (dikirim = stok bisa langsung dikurangi).

Dipindah dari main.py (Tahap 4, docs/rencana-perbaikan.md). Pembantu inti dipanggil lewat `inti.<nama>`;
jalur API didaftarkan main.py lewat `app.include_router(router)`.
"""
import sqlite3
from datetime import datetime, timedelta
from html import escape as _esc
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from indeso import inti
from indeso.format import _angka_stok, _rp

router = APIRouter()


@inti._skema_sekali
def _siapkan_gudang(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS gudang_barang (
        id INTEGER PRIMARY KEY AUTOINCREMENT, nama TEXT NOT NULL UNIQUE COLLATE NOCASE,
        satuan TEXT NOT NULL DEFAULT 'pcs', kategori TEXT, stok_min REAL NOT NULL DEFAULT 0,
        aktif INTEGER NOT NULL DEFAULT 1, catatan TEXT, dibuat_oleh TEXT, diubah_pada TEXT NOT NULL)""")
    # jenis: masuk (+), keluar (−, dipakai/rusak), koreksi (± hasil hitung fisik / opname)
    conn.execute("""CREATE TABLE IF NOT EXISTS gudang_mutasi (
        id INTEGER PRIMARY KEY AUTOINCREMENT, barang_id INTEGER NOT NULL REFERENCES gudang_barang(id),
        tanggal TEXT NOT NULL, jenis TEXT NOT NULL CHECK (jenis IN ('masuk','keluar','koreksi')),
        jumlah REAL NOT NULL, username TEXT, keterangan TEXT, oleh TEXT NOT NULL, dicatat_pada TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'aktif', batal_alasan TEXT, batal_oleh TEXT, batal_pada TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gudang_mutasi_barang ON gudang_mutasi(barang_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gudang_mutasi_user ON gudang_mutasi(username)")
    # Harga per satuan (diatur owner) & harga yang DIBEKUKAN di tiap pemakaian, supaya biaya pemasangan lama tidak ikut
    # berubah saat harga barang naik; pemasangan_id = pemakaian bagian dari satu catatan Pemasangan (indeso/pemasangan.py).
    if "harga" not in [r[1] for r in conn.execute("PRAGMA table_info(gudang_barang)")]:
        conn.execute("ALTER TABLE gudang_barang ADD COLUMN harga REAL NOT NULL DEFAULT 0")
    kolom = [r[1] for r in conn.execute("PRAGMA table_info(gudang_mutasi)")]
    if "harga_satuan" not in kolom:
        conn.execute("ALTER TABLE gudang_mutasi ADD COLUMN harga_satuan REAL")
    if "pemasangan_id" not in kolom:
        conn.execute("ALTER TABLE gudang_mutasi ADD COLUMN pemasangan_id INTEGER")
    # Wilayah tujuan material yang dikirim lewat permintaan material (#M): beban operasional wilayah itu (bagi hasil).
    if "lokasi_tujuan" not in kolom:
        conn.execute("ALTER TABLE gudang_mutasi ADD COLUMN lokasi_tujuan INTEGER")
        try:   # catatan lama: "Dikirim ke <wilayah> (#M<id>)"
            for mid, mm in conn.execute("SELECT m.id, mm.lokasi FROM gudang_mutasi m JOIN material_minta mm "
                                        "ON m.keterangan LIKE '%(#M' || mm.id || ')'").fetchall():
                conn.execute("UPDATE gudang_mutasi SET lokasi_tujuan = ? WHERE id = ?", (mm, mid))
        except sqlite3.OperationalError:
            pass       # tabel permintaan material belum ada: belum ada catatan lama


_SQL_STOK = ("COALESCE((SELECT SUM(CASE m.jenis WHEN 'keluar' THEN -m.jumlah ELSE m.jumlah END) FROM gudang_mutasi m "
             "WHERE m.barang_id = b.id AND m.status = 'aktif'), 0)")


def _stok_barang(conn, barang_id: int) -> float:
    r = conn.execute(f"SELECT {_SQL_STOK} FROM gudang_barang b WHERE b.id = ?", (barang_id,)).fetchone()
    return float(r[0]) if r else 0.0


def _cek_jumlah_gudang(x) -> float:
    try:
        v = round(float(x), 2)
    except (TypeError, ValueError):
        v = 0
    if not 0 < v <= 1_000_000:
        raise HTTPException(status_code=400, detail="Jumlah harus lebih dari 0.")
    return v


def _cek_tanggal_gudang(t: Optional[str]) -> str:
    hari = datetime.now().strftime("%Y-%m-%d")
    t = (t or "").strip() or hari
    try:
        datetime.strptime(t, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Tanggal tidak valid.")
    if t > hari:
        raise HTTPException(status_code=400, detail="Tanggal tidak boleh di masa depan.")
    return t


def _gudang_keluar(conn, item: list, username: Optional[str], oleh: str, tanggal: str, keterangan: str,
                   pemasangan_id: Optional[int] = None, lokasi_tujuan: Optional[int] = None) -> list[dict]:
    """Catat pemakaian beberapa barang sekaligus (dipanggil di dalam transaksi BEGIN IMMEDIATE, supaya dua orang
    yang mengurangi stok bersamaan tidak membuat stok minus). Semua atau tidak sama sekali."""
    jumlah: dict[int, float] = {}
    for it in item:
        try:
            bid = int(it["barang_id"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Data barang tidak valid.")
        jumlah[bid] = jumlah.get(bid, 0) + _cek_jumlah_gudang(it.get("jumlah"))
    hasil, sekarang = [], datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for bid, j in jumlah.items():
        b = conn.execute("SELECT * FROM gudang_barang WHERE id = ?", (bid,)).fetchone()
        if not b or not b["aktif"]:
            raise HTTPException(status_code=404, detail="Barang tidak ditemukan atau sudah tidak dipakai.")
        stok = _stok_barang(conn, bid)
        if j > stok + 1e-9:
            raise HTTPException(status_code=409, detail=f"Stok {b['nama']} tidak cukup: tersisa {_angka_stok(stok)} "
                                                        f"{b['satuan']}, diminta {_angka_stok(j)}.")
        hasil.append({"barang_id": bid, "nama": b["nama"], "satuan": b["satuan"], "jumlah": j,
                      "stok_sebelum": stok, "stok": round(stok - j, 2), "stok_min": b["stok_min"],
                      "harga_satuan": float(b["harga"] or 0)})
    for h in hasil:
        conn.execute("INSERT INTO gudang_mutasi (barang_id, tanggal, jenis, jumlah, username, keterangan, oleh, dicatat_pada, "
                     "harga_satuan, pemasangan_id, lokasi_tujuan) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (h["barang_id"], tanggal, "keluar", h["jumlah"], username, keterangan, oleh, sekarang,
                      h["harga_satuan"], pemasangan_id, lokasi_tujuan))
    return hasil


def _kabar_stok_menipis(hasil: list[dict]) -> None:
    """Kabari Telegram sekali saat stok melewati batas minimum (bukan setiap kali dipakai)."""
    turun = [h for h in hasil if h["stok_min"] and h["stok"] <= h["stok_min"] < h["stok_sebelum"]]
    if turun:
        inti.send_telegram_message("📦 <b>Stok warehouse menipis</b>\n\n" + "\n".join(
            f"• {_esc(h['nama'])}: sisa {_angka_stok(h['stok'])} {_esc(h['satuan'])} (minimum {_angka_stok(h['stok_min'])})"
            for h in turun) + "\n\nCatat barang masuk lewat dashboard → Warehouse.")


def _versi_gudang(conn) -> str:
    r = conn.execute("SELECT (SELECT COALESCE(MAX(id),0) FROM gudang_mutasi), "
                     "(SELECT COUNT(*) FROM gudang_mutasi WHERE status <> 'aktif'), "
                     "(SELECT COALESCE(MAX(diubah_pada),'') FROM gudang_barang), (SELECT COUNT(*) FROM gudang_barang)").fetchone()
    try:   # permintaan material dari wilayah ikut memperbarui layar Warehouse yang terbuka
        m = conn.execute("SELECT COALESCE(MAX(id),0), COALESCE(MAX(diproses_pada),'') FROM material_minta").fetchone()
    except sqlite3.OperationalError:
        m = (0, "")
    return ":".join(str(x) for x in (*r, *m))


@router.get("/api/gudang/versi")
def gudang_versi(request: Request):
    """Penanda perubahan untuk tampilan real time: layar Warehouse yang terbuka mengecek ini tiap beberapa detik."""
    inti._wajib_admin(request)
    with inti.db() as conn:
        _siapkan_gudang(conn)
        menipis = conn.execute(f"SELECT COUNT(*) FROM gudang_barang b WHERE b.aktif = 1 AND b.stok_min > 0 "
                               f"AND {_SQL_STOK} <= b.stok_min").fetchone()[0]
        _siapkan_material(conn)
        minta = conn.execute("SELECT COUNT(*) FROM material_minta WHERE status IN ('menunggu','diproses')").fetchone()[0]
        return {"versi": _versi_gudang(conn), "menipis": menipis, "permintaan": minta}


@router.get("/api/gudang")
def gudang_daftar(request: Request, riwayat: int = 60, barang_id: Optional[int] = None, username: Optional[str] = None):
    inti._wajib_admin(request)
    with inti.db() as conn:
        _siapkan_gudang(conn)
        barang = [dict(r) for r in conn.execute(
            f"SELECT b.*, {_SQL_STOK} AS stok, (SELECT MAX(m.dicatat_pada) FROM gudang_mutasi m WHERE m.barang_id = b.id) "
            "AS terakhir FROM gudang_barang b ORDER BY b.aktif DESC, b.kategori IS NULL, b.kategori COLLATE NOCASE, "
            "b.nama COLLATE NOCASE")]
        for b in barang:
            b["stok"] = round(b["stok"], 2)
            b["menipis"] = bool(b["aktif"] and b["stok_min"] and b["stok"] <= b["stok_min"])
        syarat, nilai = ["1=1"], []
        if barang_id:
            syarat.append("m.barang_id = ?"); nilai.append(barang_id)
        if username:
            syarat.append("m.username = ? COLLATE NOCASE"); nilai.append(username)
        mutasi = [dict(r) for r in conn.execute(
            "SELECT m.*, b.nama, b.satuan, p.nama AS nama_pelanggan FROM gudang_mutasi m "
            "JOIN gudang_barang b ON b.id = m.barang_id LEFT JOIN pelanggan p ON p.username = m.username "
            f"WHERE {' AND '.join(syarat)} ORDER BY m.id DESC LIMIT ?", (*nilai, max(1, min(riwayat, 500))))]
        return {"barang": barang, "mutasi": mutasi, "versi": _versi_gudang(conn), "saya": inti._oleh(request),
                "peran": request.state.peran}


class BarangIn(BaseModel):
    nama: str
    satuan: str = "pcs"
    kategori: Optional[str] = None
    stok_min: float = 0
    catatan: Optional[str] = None
    aktif: bool = True
    stok_awal: float = 0          # hanya saat menambah barang baru
    harga: Optional[float] = None  # harga per satuan (Rp); hanya owner yang boleh mengisi/mengubah


def _cek_barang(d: BarangIn) -> tuple:
    nama, satuan = d.nama.strip()[:80], (d.satuan or "").strip()[:20] or "pcs"
    if len(nama) < 2:
        raise HTTPException(status_code=400, detail="Nama barang minimal 2 huruf.")
    if not 0 <= d.stok_min <= 1_000_000:
        raise HTTPException(status_code=400, detail="Stok minimum tidak valid.")
    return nama, satuan, (d.kategori or "").strip()[:40] or None, round(d.stok_min, 2), (d.catatan or "").strip()[:300] or None


def _cek_harga(d: BarangIn, request: Request, lama: Optional[float] = None) -> Optional[float]:
    """Harga per satuan: None = tidak diubah. Hanya owner (harga dipakai menghitung biaya pemasangan)."""
    if d.harga is None or (lama is not None and round(d.harga, 2) == round(lama, 2)):
        return None
    if request.state.peran != "owner":
        raise HTTPException(status_code=403, detail="Harga barang hanya bisa diatur owner.")
    if not 0 <= d.harga <= 100_000_000:
        raise HTTPException(status_code=400, detail="Harga tidak valid.")
    return round(d.harga, 2)


@router.post("/api/gudang/barang")
def gudang_tambah_barang(d: BarangIn, request: Request):
    inti._wajib_admin(request)
    nama, satuan, kategori, stok_min, catatan = _cek_barang(d)
    harga = _cek_harga(d, request) or 0
    if not 0 <= d.stok_awal <= 1_000_000:
        raise HTTPException(status_code=400, detail="Stok awal tidak valid.")
    sekarang = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with inti.db() as conn:
        _siapkan_gudang(conn)
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM gudang_barang WHERE nama = ? COLLATE NOCASE", (nama,)).fetchone():
            raise HTTPException(status_code=409, detail=f"Barang {nama} sudah ada.")
        bid = conn.execute("INSERT INTO gudang_barang (nama, satuan, kategori, stok_min, catatan, dibuat_oleh, diubah_pada, harga) "
                           "VALUES (?,?,?,?,?,?,?,?)", (nama, satuan, kategori, stok_min, catatan, inti._oleh(request), sekarang,
                                                        harga)).lastrowid
        if d.stok_awal:
            conn.execute("INSERT INTO gudang_mutasi (barang_id, tanggal, jenis, jumlah, keterangan, oleh, dicatat_pada) "
                         "VALUES (?,?,?,?,?,?,?)", (bid, sekarang[:10], "masuk", round(d.stok_awal, 2), "Stok awal",
                                                    inti._oleh(request), sekarang))
        conn.commit()
    inti.catat_aktivitas(inti._oleh(request), "gudang_barang", f"Barang baru {nama} ({satuan})"
                    + (f", stok awal {_angka_stok(d.stok_awal)}" if d.stok_awal else "")
                    + (f", harga {_rp(harga)}/{satuan}" if harga else ""))
    return {"ok": True, "id": bid}


@router.put("/api/gudang/barang/{bid}")
def gudang_ubah_barang(bid: int, d: BarangIn, request: Request):
    inti._wajib_admin(request)
    nama, satuan, kategori, stok_min, catatan = _cek_barang(d)
    with inti.db() as conn:
        _siapkan_gudang(conn)
        lama = conn.execute("SELECT * FROM gudang_barang WHERE id = ?", (bid,)).fetchone()
        if not lama:
            raise HTTPException(status_code=404, detail="Barang tidak ditemukan.")
        if conn.execute("SELECT 1 FROM gudang_barang WHERE nama = ? COLLATE NOCASE AND id <> ?", (nama, bid)).fetchone():
            raise HTTPException(status_code=409, detail=f"Barang {nama} sudah ada.")
        harga = _cek_harga(d, request, float(lama["harga"] or 0))
        conn.execute("UPDATE gudang_barang SET nama=?, satuan=?, kategori=?, stok_min=?, catatan=?, aktif=?, diubah_pada=?, "
                     "harga=COALESCE(?, harga) WHERE id=?", (nama, satuan, kategori, stok_min, catatan, int(d.aktif),
                                                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), harga, bid))
        conn.commit()
    ubah = [f"{k} {a} → {b}" for k, a, b in (("nama", lama["nama"], nama), ("satuan", lama["satuan"], satuan),
                                             ("kategori", lama["kategori"], kategori), ("stok minimum", lama["stok_min"], stok_min),
                                             ("aktif", bool(lama["aktif"]), d.aktif)) if a != b]
    if harga is not None:
        ubah.append(f"harga {_rp(lama['harga'] or 0)} → {_rp(harga)}")
    inti.catat_aktivitas(inti._oleh(request), "gudang_barang", f"Ubah barang {lama['nama']}: " + (", ".join(ubah) or "catatan"))
    return {"ok": True}


class MutasiIn(BaseModel):
    barang_id: int
    jenis: str                       # masuk / keluar
    jumlah: float
    tanggal: Optional[str] = None
    username: Optional[str] = None   # pemakaian untuk pelanggan
    keterangan: Optional[str] = None


@router.post("/api/gudang/mutasi")
def gudang_mutasi(d: MutasiIn, request: Request):
    inti._wajib_admin(request)
    if d.jenis not in ("masuk", "keluar"):
        raise HTTPException(status_code=400, detail="Jenis harus masuk atau keluar.")
    jumlah, tanggal = _cek_jumlah_gudang(d.jumlah), _cek_tanggal_gudang(d.tanggal)
    ket, user = (d.keterangan or "").strip()[:200] or None, (d.username or "").strip() or None
    with inti.db() as conn:
        _siapkan_gudang(conn)
        if user and not conn.execute("SELECT 1 FROM pelanggan WHERE username = ? COLLATE NOCASE", (user,)).fetchone():
            raise HTTPException(status_code=404, detail=f"Pelanggan {user} tidak ditemukan.")
        conn.execute("BEGIN IMMEDIATE")
        if d.jenis == "keluar":
            hasil = _gudang_keluar(conn, [{"barang_id": d.barang_id, "jumlah": jumlah}], user, inti._oleh(request), tanggal, ket)[0]
        else:
            b = conn.execute("SELECT * FROM gudang_barang WHERE id = ?", (d.barang_id,)).fetchone()
            if not b:
                raise HTTPException(status_code=404, detail="Barang tidak ditemukan.")
            stok = _stok_barang(conn, d.barang_id)
            conn.execute("INSERT INTO gudang_mutasi (barang_id, tanggal, jenis, jumlah, username, keterangan, oleh, dicatat_pada) "
                         "VALUES (?,?,?,?,?,?,?,?)", (d.barang_id, tanggal, "masuk", jumlah, user, ket, inti._oleh(request),
                                                      datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            hasil = {"barang_id": d.barang_id, "nama": b["nama"], "satuan": b["satuan"], "jumlah": jumlah,
                     "stok_sebelum": stok, "stok": round(stok + jumlah, 2), "stok_min": b["stok_min"]}
        conn.commit()
    inti.catat_aktivitas(inti._oleh(request), "gudang_" + d.jenis,
                    f"{'Masuk' if d.jenis == 'masuk' else 'Pakai'} {hasil['nama']} {_angka_stok(jumlah)} {hasil['satuan']}"
                    + (f" untuk {user}" if user else "") + (f" · {ket}" if ket else "")
                    + f" (stok {_angka_stok(hasil['stok'])})")
    if d.jenis == "keluar":
        _kabar_stok_menipis([hasil])
    return {"ok": True, "stok": hasil["stok"]}


class PakaiItem(BaseModel):
    barang_id: int
    jumlah: float


class PakaiIn(BaseModel):
    username: Optional[str] = None
    item: list[PakaiItem]
    tanggal: Optional[str] = None
    keterangan: Optional[str] = None


@router.post("/api/gudang/pakai")
def gudang_pakai(d: PakaiIn, request: Request):
    """Pemakaian beberapa barang sekaligus (mis. untuk satu pelanggan): semua tercatat atau tidak sama sekali."""
    inti._wajib_admin(request)
    if not d.item or len(d.item) > 30:
        raise HTTPException(status_code=400, detail="Pilih 1–30 barang.")
    tanggal, ket, user = _cek_tanggal_gudang(d.tanggal), (d.keterangan or "").strip()[:200] or None, (d.username or "").strip() or None
    with inti.db() as conn:
        _siapkan_gudang(conn)
        if user and not conn.execute("SELECT 1 FROM pelanggan WHERE username = ? COLLATE NOCASE", (user,)).fetchone():
            raise HTTPException(status_code=404, detail=f"Pelanggan {user} tidak ditemukan.")
        conn.execute("BEGIN IMMEDIATE")
        hasil = _gudang_keluar(conn, [i.model_dump() for i in d.item], user, inti._oleh(request), tanggal, ket)
        conn.commit()
    inti.catat_aktivitas(inti._oleh(request), "gudang_keluar", "Pakai " + ", ".join(
        f"{h['nama']} {_angka_stok(h['jumlah'])} {h['satuan']}" for h in hasil) + (f" untuk {user}" if user else "")
        + (f" · {ket}" if ket else ""))
    _kabar_stok_menipis(hasil)
    return {"ok": True, "hasil": hasil}


class OpnameIn(BaseModel):
    barang_id: int
    stok_fisik: float
    keterangan: Optional[str] = None


@router.post("/api/gudang/opname")
def gudang_opname(d: OpnameIn, request: Request):
    """Hitung fisik: stok di sistem disamakan dengan hasil hitung, selisihnya dicatat sebagai koreksi."""
    inti._wajib_admin(request)
    if not 0 <= d.stok_fisik <= 1_000_000:
        raise HTTPException(status_code=400, detail="Stok fisik tidak valid.")
    with inti.db() as conn:
        _siapkan_gudang(conn)
        conn.execute("BEGIN IMMEDIATE")
        b = conn.execute("SELECT * FROM gudang_barang WHERE id = ?", (d.barang_id,)).fetchone()
        if not b:
            raise HTTPException(status_code=404, detail="Barang tidak ditemukan.")
        stok = _stok_barang(conn, d.barang_id)
        selisih = round(d.stok_fisik - stok, 2)
        if selisih:
            sekarang = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT INTO gudang_mutasi (barang_id, tanggal, jenis, jumlah, keterangan, oleh, dicatat_pada) "
                         "VALUES (?,?,?,?,?,?,?)", (d.barang_id, sekarang[:10], "koreksi", selisih,
                                                    ("Hitung fisik" + (f": {d.keterangan.strip()[:150]}" if d.keterangan and d.keterangan.strip() else "")),
                                                    inti._oleh(request), sekarang))
        conn.commit()
    if selisih:
        inti.catat_aktivitas(inti._oleh(request), "gudang_opname", f"Hitung fisik {b['nama']}: sistem {_angka_stok(stok)} → fisik "
                        f"{_angka_stok(d.stok_fisik)} {b['satuan']} (selisih {selisih:+g})")
    return {"ok": True, "selisih": selisih, "stok": round(d.stok_fisik, 2)}


class GudangBatalIn(BaseModel):
    alasan: str


@router.post("/api/gudang/mutasi/{mid}/batal")
def gudang_batal_mutasi(mid: int, d: GudangBatalIn, request: Request):
    """Batalkan catatan yang salah. Owner: kapan saja; admin: catatannya sendiri dalam 24 jam."""
    inti._wajib_admin(request)
    alasan = d.alasan.strip()
    if len(alasan) < 3:
        raise HTTPException(status_code=400, detail="Tuliskan alasan pembatalan.")
    with inti.db() as conn:
        _siapkan_gudang(conn)
        conn.execute("BEGIN IMMEDIATE")
        m = conn.execute("SELECT m.*, b.nama, b.satuan FROM gudang_mutasi m JOIN gudang_barang b ON b.id = m.barang_id "
                         "WHERE m.id = ?", (mid,)).fetchone()
        if not m or m["status"] != "aktif":
            raise HTTPException(status_code=404, detail="Catatan tidak ditemukan atau sudah dibatalkan.")
        if m["pemasangan_id"]:
            raise HTTPException(status_code=409, detail=f"Catatan ini bagian dari Pemasangan #{m['pemasangan_id']}. "
                                                        "Batalkan lewat menu Pemasangan supaya rinciannya tetap utuh.")
        if request.state.peran != "owner":
            umur = datetime.now() - datetime.strptime(m["dicatat_pada"], "%Y-%m-%d %H:%M:%S")
            if m["oleh"] != inti._oleh(request) or umur > timedelta(hours=24):
                raise HTTPException(status_code=403, detail="Admin hanya bisa membatalkan catatannya sendiri dalam 24 jam.")
        efek = -m["jumlah"] if m["jenis"] == "masuk" else (m["jumlah"] if m["jenis"] == "keluar" else -m["jumlah"])
        stok = _stok_barang(conn, m["barang_id"])
        if stok + efek < -1e-9:
            raise HTTPException(status_code=409, detail=f"Tidak bisa dibatalkan: stok {m['nama']} akan menjadi "
                                                        f"{_angka_stok(stok + efek)} (barangnya sudah terpakai).")
        conn.execute("UPDATE gudang_mutasi SET status='batal', batal_alasan=?, batal_oleh=?, batal_pada=? WHERE id=?",
                     (alasan[:200], inti._oleh(request), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), mid))
        conn.commit()
    inti.catat_aktivitas(inti._oleh(request), "gudang_batal", f"Batal catatan warehouse #{mid} ({m['jenis']} {m['nama']} "
                    f"{_angka_stok(abs(m['jumlah']))} {m['satuan']}): {alasan[:150]}")
    return {"ok": True}


_STATUS_MATERIAL = ("menunggu", "diproses", "dikirim", "ditolak", "batal")


@inti._skema_sekali
def _siapkan_material(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS material_minta (
        id INTEGER PRIMARY KEY AUTOINCREMENT, lokasi INTEGER NOT NULL, peminta TEXT NOT NULL, dibuat TEXT NOT NULL,
        catatan TEXT, status TEXT NOT NULL DEFAULT 'menunggu', diproses_oleh TEXT, diproses_pada TEXT, alasan TEXT,
        stok_dikurangi INTEGER NOT NULL DEFAULT 0)""")
    # barang_id: barang warehouse (NULL = barang yang belum ada di warehouse, diketik bebas)
    conn.execute("""CREATE TABLE IF NOT EXISTS material_minta_item (
        id INTEGER PRIMARY KEY AUTOINCREMENT, minta_id INTEGER NOT NULL REFERENCES material_minta(id),
        barang_id INTEGER, nama TEXT NOT NULL, jumlah REAL NOT NULL, satuan TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_material_lokasi ON material_minta(lokasi, id)")


def _daftar_material(conn, lokasi: Optional[int] = None, status: Optional[str] = None, limit: int = 50) -> list[dict]:
    syarat, nilai = ["1=1"], []
    if lokasi is not None:
        syarat.append("m.lokasi = ?"); nilai.append(lokasi)
    if status:
        syarat.append("m.status = ?"); nilai.append(status)
    rows = conn.execute(f"SELECT m.*, l.nama AS nama_lokasi FROM material_minta m LEFT JOIN lokasi l ON l.kode = m.lokasi "
                        f"WHERE {' AND '.join(syarat)} ORDER BY (m.status IN ('menunggu','diproses')) DESC, m.id DESC "
                        f"LIMIT ?", (*nilai, limit)).fetchall()
    hasil = []
    for r in rows:
        item = [dict(i) for i in conn.execute(
            f"SELECT i.barang_id, i.nama, i.jumlah, COALESCE(i.satuan, b.satuan) AS satuan, "
            f"CASE WHEN b.id IS NULL THEN NULL ELSE {_SQL_STOK} END AS stok FROM material_minta_item i "
            f"LEFT JOIN gudang_barang b ON b.id = i.barang_id WHERE i.minta_id = ? ORDER BY i.id", (r["id"],))]
        hasil.append(dict(r) | {"nomor": f"M{r['id']}", "item": item})
    return hasil


def _teks_item_material(item: list) -> str:
    return "\n".join(f"• {_esc(i['nama'])}: {_angka_stok(i['jumlah'])} {_esc(i.get('satuan') or '')}".rstrip() for i in item)


class MaterialItemIn(BaseModel):
    barang_id: Optional[int] = None
    nama: Optional[str] = None        # barang yang belum ada di warehouse
    jumlah: float
    satuan: Optional[str] = None


class MaterialIn(BaseModel):
    item: list[MaterialItemIn]
    catatan: Optional[str] = None


@router.get("/api/wilayah/material")
def wilayah_material(request: Request):
    """Daftar barang warehouse (tanpa angka stok Pusat) untuk dipilih, dan permintaan wilayah ini."""
    lok = inti._lokasi_wilayah(request)
    with inti.db() as conn:
        _siapkan_gudang(conn)
        _siapkan_material(conn)
        barang = [dict(r) for r in conn.execute("SELECT id, nama, satuan, kategori FROM gudang_barang WHERE aktif = 1 "
                                                "ORDER BY kategori IS NULL, kategori COLLATE NOCASE, nama COLLATE NOCASE")]
        minta = _daftar_material(conn, lok["kode"], limit=30)
    for m in minta:
        for i in m["item"]:
            i.pop("stok", None)
    return {"barang": barang, "permintaan": minta}


@router.post("/api/wilayah/material")
def wilayah_minta_material(d: MaterialIn, request: Request):
    lok = inti._lokasi_wilayah(request)
    if not d.item or len(d.item) > 30:
        raise HTTPException(status_code=400, detail="Isi 1–30 barang.")
    catatan = (d.catatan or "").strip()[:300] or None
    sekarang = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with inti.db() as conn:
        _siapkan_gudang(conn)
        _siapkan_material(conn)
        item = []
        for it in d.item:
            jumlah = _cek_jumlah_gudang(it.jumlah)
            if it.barang_id:
                b = conn.execute("SELECT id, nama, satuan FROM gudang_barang WHERE id = ? AND aktif = 1",
                                 (it.barang_id,)).fetchone()
                if not b:
                    raise HTTPException(status_code=404, detail="Barang tidak ditemukan di warehouse.")
                item.append({"barang_id": b["id"], "nama": b["nama"], "jumlah": jumlah, "satuan": b["satuan"]})
            else:
                nama = (it.nama or "").strip()[:80]
                if len(nama) < 2:
                    raise HTTPException(status_code=400, detail="Nama barang minimal 2 huruf.")
                item.append({"barang_id": None, "nama": nama, "jumlah": jumlah,
                             "satuan": (it.satuan or "").strip()[:20] or None})
        conn.execute("BEGIN IMMEDIATE")
        mid = conn.execute("INSERT INTO material_minta (lokasi, peminta, dibuat, catatan) VALUES (?,?,?,?)",
                           (lok["kode"], inti._oleh(request), sekarang, catatan)).lastrowid
        conn.executemany("INSERT INTO material_minta_item (minta_id, barang_id, nama, jumlah, satuan) VALUES (?,?,?,?,?)",
                         [(mid, i["barang_id"], i["nama"], i["jumlah"], i["satuan"]) for i in item])
        conn.commit()
    inti.catat_aktivitas(inti._oleh(request), "material_minta", f"#M{mid} {lok['nama']}: " + ", ".join(
        f"{i['nama']} {_angka_stok(i['jumlah'])} {i['satuan'] or ''}".rstrip() for i in item) + (f" · {catatan}" if catatan else ""))
    inti.send_telegram_message(f"📦 <b>Permintaan material #M{mid}</b>\nDari: {_esc(lok['nama'])} ({_esc(inti._oleh(request))})\n\n"
                          + _teks_item_material(item) + (f"\n\nCatatan: {_esc(catatan)}" if catatan else "")
                          + "\n\nProses di dashboard → <b>Warehouse → Permintaan</b>.")
    return {"ok": True, "id": mid, "nomor": f"M{mid}"}


@router.post("/api/wilayah/material/{mid}/batal")
def wilayah_batal_material(mid: int, request: Request):
    lok = inti._lokasi_wilayah(request)
    with inti.db() as conn:
        _siapkan_material(conn)
        n = conn.execute("UPDATE material_minta SET status = 'batal', diproses_oleh = ?, diproses_pada = ? "
                         "WHERE id = ? AND lokasi = ? AND status = 'menunggu'",
                         (inti._oleh(request), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), mid, lok["kode"])).rowcount
        conn.commit()
    if not n:
        raise HTTPException(status_code=409, detail="Permintaan tidak ditemukan atau sudah diproses Pusat.")
    inti.catat_aktivitas(inti._oleh(request), "material_batal", f"#M{mid} {lok['nama']} dibatalkan peminta")
    inti.send_telegram_message(f"📦 Permintaan material #M{mid} dari {_esc(lok['nama'])} <b>dibatalkan</b> peminta.")
    return {"ok": True}


@router.get("/api/gudang/permintaan")
def gudang_permintaan(request: Request, status: Optional[str] = None):
    inti._wajib_admin(request)
    if status and status not in _STATUS_MATERIAL:
        raise HTTPException(status_code=400, detail="Status tidak dikenal.")
    with inti.db() as conn:
        _siapkan_gudang(conn)
        _siapkan_material(conn)
        minta = _daftar_material(conn, status=status, limit=100)
        n = conn.execute("SELECT COUNT(*) FROM material_minta WHERE status IN ('menunggu','diproses')").fetchone()[0]
    for m in minta:
        for i in m["item"]:
            if i["stok"] is not None:
                i["stok"] = round(i["stok"], 2)
    return {"permintaan": minta, "terbuka": n}


class ProsesMaterialIn(BaseModel):
    status: str                      # diproses | dikirim | ditolak
    alasan: Optional[str] = None     # wajib bila ditolak
    kurangi_stok: bool = True        # saat dikirim: catat barang warehouse keluar


@router.post("/api/gudang/permintaan/{mid}")
def gudang_proses_permintaan(mid: int, d: ProsesMaterialIn, request: Request):
    inti._wajib_admin(request)
    if d.status not in ("diproses", "dikirim", "ditolak"):
        raise HTTPException(status_code=400, detail="Status harus diproses, dikirim, atau ditolak.")
    alasan = (d.alasan or "").strip()[:300] or None
    if d.status == "ditolak" and not alasan:
        raise HTTPException(status_code=400, detail="Tulis alasan penolakan (dibaca admin wilayah).")
    sekarang = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = inti._billing_conn()
    keluar = []
    try:
        _siapkan_gudang(conn)
        _siapkan_material(conn)
        conn.execute("BEGIN IMMEDIATE")
        m = conn.execute("SELECT m.*, l.nama AS nama_lokasi FROM material_minta m LEFT JOIN lokasi l ON l.kode = m.lokasi "
                         "WHERE m.id = ?", (mid,)).fetchone()
        if not m:
            raise HTTPException(status_code=404, detail="Permintaan tidak ditemukan.")
        if m["status"] not in ("menunggu", "diproses"):
            raise HTTPException(status_code=409, detail=f"Permintaan ini sudah {m['status']}.")
        item = [dict(i) for i in conn.execute("SELECT * FROM material_minta_item WHERE minta_id = ?", (mid,))]
        kurangi = d.status == "dikirim" and d.kurangi_stok
        if kurangi:
            dari_gudang = [{"barang_id": i["barang_id"], "jumlah": i["jumlah"]} for i in item if i["barang_id"]]
            if dari_gudang:
                keluar = _gudang_keluar(conn, dari_gudang, None, inti._oleh(request), sekarang[:10],
                                        f"Dikirim ke {m['nama_lokasi'] or 'lokasi ' + str(m['lokasi'])} (#M{mid})",
                                        lokasi_tujuan=m["lokasi"])
        conn.execute("UPDATE material_minta SET status = ?, diproses_oleh = ?, diproses_pada = ?, alasan = ?, "
                     "stok_dikurangi = ? WHERE id = ?", (d.status, inti._oleh(request), sekarang, alasan, int(bool(keluar)), mid))
        conn.commit()
    finally:
        conn.close()
    lok = m["nama_lokasi"] or f"lokasi {m['lokasi']}"
    inti.catat_aktivitas(inti._oleh(request), "material_" + d.status, f"#M{mid} {lok}: {d.status}"
                    + (f" · stok warehouse dikurangi ({len(keluar)} barang)" if keluar else "")
                    + (f" · {alasan}" if alasan else ""))
    judul = {"diproses": "sedang diproses", "dikirim": "DIKIRIM", "ditolak": "DITOLAK"}[d.status]
    inti.send_telegram_message(f"📦 Permintaan material #M{mid} ({_esc(lok)}) {judul} oleh {_esc(inti._oleh(request))}."
                          + (f"\nAlasan: {_esc(alasan)}" if alasan else ""))
    if keluar:
        _kabar_stok_menipis(keluar)
    return {"ok": True, "stok_dikurangi": bool(keluar)}
