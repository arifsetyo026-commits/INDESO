"""Pemasangan (provisioning): catatan lengkap setiap pemasangan pelanggan, supaya pemakaian material bisa
dipertanggungjawabkan ke pemegang saham.

Satu catatan = satu kunjungan pasang: pelanggan, tanggal, teknisi, ODP, ONU, biaya pasang yang dibayar pelanggan, dan
daftar material. Material langsung dikurangi dari stok warehouse (gudang_mutasi, ditandai pemasangan_id) dengan harga
per satuan yang DIBEKUKAN saat dicatat. Tidak ada ubah diam-diam: catatan yang salah hanya bisa dibatalkan owner dengan
alasan (stok kembali), dan setiap pemasangan/pembatalan dikabarkan ke grup Telegram beserta rinciannya.
"""
from datetime import datetime
from html import escape as _esc
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from indeso import inti
from indeso.format import _angka_stok, _rp
from indeso.gudang import _SQL_STOK, _cek_tanggal_gudang, _gudang_keluar, _kabar_stok_menipis, _siapkan_gudang

router = APIRouter()


@inti._skema_sekali
def _skema_pemasangan(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS pemasangan (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, tanggal TEXT NOT NULL, teknisi TEXT NOT NULL,
        odp TEXT, onu TEXT, biaya_pasang INTEGER NOT NULL DEFAULT 0, catatan TEXT, oleh TEXT NOT NULL,
        dicatat_pada TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'aktif', batal_alasan TEXT, batal_oleh TEXT,
        batal_pada TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pemasangan_tanggal ON pemasangan(tanggal)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pemasangan_user ON pemasangan(username)")


def _siapkan_pemasangan(conn) -> None:
    _siapkan_gudang(conn)
    _skema_pemasangan(conn)


def _material(conn, ids: list[int]) -> dict[int, list[dict]]:
    """Material per pemasangan (termasuk yang ikut dibatalkan), dengan harga yang dibekukan saat dicatat."""
    if not ids:
        return {}
    tanda = ",".join("?" * len(ids))
    hasil: dict[int, list[dict]] = {i: [] for i in ids}
    for r in conn.execute(
            "SELECT m.pemasangan_id, m.barang_id, b.nama, b.satuan, m.jumlah, COALESCE(m.harga_satuan, 0) AS harga_satuan "
            f"FROM gudang_mutasi m JOIN gudang_barang b ON b.id = m.barang_id WHERE m.pemasangan_id IN ({tanda}) "
            "ORDER BY m.id", ids):
        hasil[r["pemasangan_id"]].append({"barang_id": r["barang_id"], "nama": r["nama"], "satuan": r["satuan"],
                                          "jumlah": r["jumlah"], "harga_satuan": r["harga_satuan"],
                                          "subtotal": round(r["jumlah"] * r["harga_satuan"])})
    return hasil


def _baris(r, material: list[dict]) -> dict:
    d = dict(r)
    d["material"] = material
    d["biaya_material"] = sum(m["subtotal"] for m in material)
    d["tanpa_harga"] = sum(1 for m in material if not m["harga_satuan"])
    return d


@router.get("/api/pemasangan")
def pemasangan_daftar(request: Request, periode: Optional[str] = None):
    """Pemasangan satu bulan + ringkasannya, daftar barang (stok & harga) dan pelanggan untuk formulir."""
    inti._wajib_admin(request)
    periode = periode or datetime.now().strftime("%Y-%m")
    if not inti._periode_valid(periode):
        raise HTTPException(status_code=400, detail="Format periode harus YYYY-MM.")
    with inti.db() as conn:
        _siapkan_pemasangan(conn)
        rows = conn.execute("SELECT s.*, p.nama AS nama_pelanggan, p.alamat FROM pemasangan s "
                            "LEFT JOIN pelanggan p ON p.username = s.username WHERE substr(s.tanggal, 1, 7) = ? "
                            "ORDER BY s.tanggal DESC, s.id DESC", (periode,)).fetchall()
        mat = _material(conn, [r["id"] for r in rows])
        daftar = [_baris(r, mat[r["id"]]) for r in rows]
        barang = [dict(r) for r in conn.execute(
            f"SELECT b.id, b.nama, b.satuan, b.kategori, b.harga, {_SQL_STOK} AS stok FROM gudang_barang b "
            "WHERE b.aktif = 1 ORDER BY b.kategori IS NULL, b.kategori COLLATE NOCASE, b.nama COLLATE NOCASE")]
        sudah = {r[0] for r in conn.execute("SELECT DISTINCT username FROM pemasangan WHERE status = 'aktif'")}
        pelanggan = [{"username": r["username"], "nama": r["nama"], "tgl_pasang": r["tgl_pasang"],
                      "sudah_dicatat": r["username"] in sudah} for r in conn.execute(
            "SELECT username, nama, tgl_pasang FROM pelanggan WHERE status <> 'berhenti' "
            "ORDER BY tgl_pasang DESC, nama COLLATE NOCASE")]
    aktif = [d for d in daftar if d["status"] == "aktif"]
    per_barang: dict[int, dict] = {}
    for d in aktif:
        for m in d["material"]:
            x = per_barang.setdefault(m["barang_id"], {"nama": m["nama"], "satuan": m["satuan"], "jumlah": 0, "biaya": 0})
            x["jumlah"] = round(x["jumlah"] + m["jumlah"], 2)
            x["biaya"] += m["subtotal"]
    total = sum(d["biaya_material"] for d in aktif)
    return {"periode": periode, "pemasangan": daftar, "barang": barang, "pelanggan": pelanggan,
            "peran": request.state.peran,
            "ringkasan": {"jumlah": len(aktif), "biaya_material": total,
                          "rata_rata": round(total / len(aktif)) if aktif else 0,
                          "biaya_pasang": sum(d["biaya_pasang"] for d in aktif),
                          "dibatalkan": len(daftar) - len(aktif),
                          "per_barang": sorted(per_barang.values(), key=lambda x: -x["biaya"])}}


class PemasanganItem(BaseModel):
    barang_id: int
    jumlah: float


class PemasanganIn(BaseModel):
    username: str
    tanggal: Optional[str] = None
    teknisi: str
    odp: Optional[str] = None
    onu: Optional[str] = None
    biaya_pasang: int = 0
    catatan: Optional[str] = None
    item: list[PemasanganItem]


def _teks_material(material: list[dict]) -> str:
    return "\n".join(f"• {_esc(m['nama'])}: {_angka_stok(m['jumlah'])} {_esc(m['satuan'])}"
                     + (f" × {_rp(m['harga_satuan'])} = {_rp(round(m['jumlah'] * m['harga_satuan']))}"
                        if m["harga_satuan"] else " (harga belum diisi)") for m in material)


@router.post("/api/pemasangan")
def pemasangan_catat(d: PemasanganIn, request: Request):
    """Catat satu pemasangan: semua material dikurangi dari stok sekaligus (atau tidak sama sekali bila ada yang kurang)."""
    inti._wajib_admin(request)
    user, teknisi = d.username.strip(), " ".join(d.teknisi.split())[:80]
    if len(teknisi) < 2:
        raise HTTPException(status_code=400, detail="Isi nama teknisi yang memasang.")
    if not d.item or len(d.item) > 30:
        raise HTTPException(status_code=400, detail="Isi material yang dipakai (1–30 barang).")
    if not 0 <= d.biaya_pasang <= 100_000_000:
        raise HTTPException(status_code=400, detail="Biaya pasang tidak valid.")
    tanggal, oleh = _cek_tanggal_gudang(d.tanggal), inti._oleh(request)
    odp, onu = (d.odp or "").strip()[:60] or None, (d.onu or "").strip()[:60] or None
    catatan = (d.catatan or "").strip()[:300] or None
    sekarang = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with inti.db() as conn:
        _siapkan_pemasangan(conn)
        p = conn.execute("SELECT username, nama FROM pelanggan WHERE username = ? COLLATE NOCASE", (user,)).fetchone()
        if not p:
            raise HTTPException(status_code=404, detail=f"Pelanggan {user} tidak ditemukan.")
        user = p["username"]
        conn.execute("BEGIN IMMEDIATE")
        pid = conn.execute("INSERT INTO pemasangan (username, tanggal, teknisi, odp, onu, biaya_pasang, catatan, oleh, "
                           "dicatat_pada) VALUES (?,?,?,?,?,?,?,?,?)",
                           (user, tanggal, teknisi, odp, onu, int(d.biaya_pasang), catatan, oleh, sekarang)).lastrowid
        hasil = _gudang_keluar(conn, [i.model_dump() for i in d.item], user, oleh, tanggal, f"Pemasangan #{pid}",
                               pemasangan_id=pid)
        conn.commit()
    biaya = sum(round(h["jumlah"] * h["harga_satuan"]) for h in hasil)
    inti.catat_aktivitas(oleh, "pemasangan", f"#{pid} {user}: teknisi {teknisi}, {len(hasil)} jenis material {_rp(biaya)}"
                         + (f", biaya pasang {_rp(d.biaya_pasang)}" if d.biaya_pasang else ""))
    inti.send_telegram_message(
        f"🔧 <b>Pemasangan #{pid}</b> · {_esc(p['nama'] or user)} ({_esc(user)})\n"
        f"Tanggal {_esc(tanggal)} · teknisi {_esc(teknisi)}"
        + (f"\nODP {_esc(odp)}" if odp else "") + (f" · ONU {_esc(onu)}" if onu else "")
        + "\n\n<b>Material:</b>\n" + _teks_material(hasil)
        + f"\n\nTotal material <b>{_rp(biaya)}</b>"
        + (f" · biaya pasang dibayar pelanggan <b>{_rp(d.biaya_pasang)}</b>" if d.biaya_pasang else "")
        + f"\nDicatat oleh {_esc(oleh)}")
    _kabar_stok_menipis(hasil)
    return {"ok": True, "id": pid, "biaya_material": biaya, "hasil": hasil}


class PemasanganBatalIn(BaseModel):
    alasan: str


@router.post("/api/pemasangan/{pid}/batal")
def pemasangan_batal(pid: int, d: PemasanganBatalIn, request: Request):
    """Hanya owner: catatan salah dibatalkan beserta seluruh materialnya (stok kembali). Catatan tetap terlihat."""
    inti._wajib_owner(request, "Pemasangan hanya bisa dibatalkan owner.")
    alasan = d.alasan.strip()
    if len(alasan) < 3:
        raise HTTPException(status_code=400, detail="Tuliskan alasan pembatalan.")
    sekarang, oleh = datetime.now().strftime("%Y-%m-%d %H:%M:%S"), inti._oleh(request)
    with inti.db() as conn:
        _siapkan_pemasangan(conn)
        conn.execute("BEGIN IMMEDIATE")
        s = conn.execute("SELECT * FROM pemasangan WHERE id = ?", (pid,)).fetchone()
        if not s or s["status"] != "aktif":
            raise HTTPException(status_code=404, detail="Pemasangan tidak ditemukan atau sudah dibatalkan.")
        conn.execute("UPDATE pemasangan SET status='batal', batal_alasan=?, batal_oleh=?, batal_pada=? WHERE id=?",
                     (alasan[:200], oleh, sekarang, pid))
        conn.execute("UPDATE gudang_mutasi SET status='batal', batal_alasan=?, batal_oleh=?, batal_pada=? "
                     "WHERE pemasangan_id=? AND status='aktif'", (f"Pemasangan #{pid} dibatalkan: {alasan[:150]}", oleh,
                                                                    sekarang, pid))
        material = _material(conn, [pid])[pid]
        conn.commit()
    biaya = sum(m["subtotal"] for m in material)
    inti.catat_aktivitas(oleh, "pemasangan_batal", f"#{pid} {s['username']} dibatalkan ({_rp(biaya)} material kembali ke "
                                                   f"stok): {alasan[:150]}")
    inti.send_telegram_message(f"↩️ <b>Pemasangan #{pid} dibatalkan</b> · {_esc(s['username'])}\nAlasan: {_esc(alasan[:200])}\n"
                               f"Material {_rp(biaya)} dikembalikan ke stok warehouse.\nOleh {_esc(oleh)}")
    return {"ok": True}
