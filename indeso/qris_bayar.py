"""QRIS statis toko + konfirmasi bukti bayar dari Portal Pelanggan (sebelum ada payment gateway).

Owner menyimpan ISI kode QRIS (bukan gambar); portal menggambar QR-nya sendiri, bernominal sesuai tagihan, dan
menampilkan nama merchant + NMID yang harus dicek pelanggan sebelum membayar. Pelanggan mengunggah bukti; admin
menerima (tagihan lunas, kas Pusat, isolir dibuka otomatis di putaran berikutnya) atau menolak dengan alasan.

Dipindah dari main.py (Tahap 4, docs/rencana-perbaikan.md). Pembantu inti dipanggil lewat `inti.<nama>`;
jalur API didaftarkan main.py lewat `app.include_router(router)`.
"""
import asyncio
import json
import os
import secrets
from datetime import datetime
from html import escape as _esc
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from indeso import inti
from indeso.format import _nama_periode, _periode_valid, _rp
from indeso.qris import _baca_qris, _qris_bernominal

router = APIRouter()


@inti._skema_sekali
def _siapkan_qris(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS qris_bayar (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL,
        merchant TEXT NOT NULL, nmid TEXT, kota TEXT, aktif INTEGER NOT NULL DEFAULT 1, oleh TEXT, pada TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS konfirmasi_bayar (id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL, tagihan_ids TEXT NOT NULL, periode TEXT NOT NULL, jumlah INTEGER NOT NULL,
        catatan TEXT, berkas TEXT NOT NULL, mime TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'menunggu',
        alasan TEXT, dibuat_pada TEXT NOT NULL, diperiksa_oleh TEXT, diperiksa_pada TEXT, metode TEXT, tgl_bayar TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_konfirmasi_status ON konfirmasi_bayar(status, username)")


def _qris_aktif(conn):
    _siapkan_qris(conn)
    return conn.execute("SELECT * FROM qris_bayar WHERE aktif = 1 ORDER BY id DESC LIMIT 1").fetchone()


@router.get("/api/qris")
def qris_lihat(request: Request):
    inti._wajib_admin(request)
    with inti.db() as conn:
        q = _qris_aktif(conn)
    return {"aktif": {"merchant": q["merchant"], "nmid": q["nmid"], "kota": q["kota"], "oleh": q["oleh"], "pada": q["pada"]}
            if q else None, "boleh_ubah": request.state.peran == "owner"}


class QrisIn(BaseModel):
    payload: str


@router.put("/api/qris")
def qris_simpan(d: QrisIn, request: Request):
    inti._wajib_owner(request, "QRIS pembayaran hanya bisa diganti owner.")
    try:
        info = _baca_qris(d.payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Kode QRIS ditolak: {e}.")
    if info["nominal_tetap"]:
        raise HTTPException(status_code=400, detail="Pakai QRIS statis tanpa nominal (QRIS toko), bukan QRIS sekali bayar.")
    with inti.db() as conn:
        lama = _qris_aktif(conn)
        conn.execute("UPDATE qris_bayar SET aktif = 0 WHERE aktif = 1")
        conn.execute("INSERT INTO qris_bayar (payload, merchant, nmid, kota, oleh, pada) VALUES (?,?,?,?,?,?)",
                     (d.payload.strip(), info["merchant"], info["nmid"], info["kota"], inti._oleh(request),
                      datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    ket = (f"{lama['merchant']} ({lama['nmid'] or '-'}) → " if lama else "") + f"{info['merchant']} ({info['nmid'] or '-'})"
    inti.catat_aktivitas(inti._oleh(request), "qris", f"QRIS pembayaran diganti: {ket}")
    inti._kabar_owner(f"🔐 <b>QRIS pembayaran diganti</b> oleh {_esc(inti._oleh(request))}\n{_esc(ket)}\n"
                 "Bila Anda tidak melakukannya, segera ganti sandi owner dan pasang ulang QRIS yang benar.")
    return {"ok": True, **{k: info[k] for k in ("merchant", "nmid", "kota")}}


@router.delete("/api/qris")
def qris_matikan(request: Request):
    inti._wajib_owner(request, "QRIS pembayaran hanya bisa diatur owner.")
    with inti.db() as conn:
        _siapkan_qris(conn)
        n = conn.execute("UPDATE qris_bayar SET aktif = 0 WHERE aktif = 1").rowcount
        conn.commit()
    if n:
        inti.catat_aktivitas(inti._oleh(request), "qris", "QRIS pembayaran di portal dimatikan")
        inti._kabar_owner(f"🔐 QRIS pembayaran di portal dimatikan oleh {_esc(inti._oleh(request))}.")
    return {"ok": True}


def _tagihan_belum(conn, username: str, periode: Optional[str]) -> list:
    sql = "SELECT id, periode, jumlah FROM tagihan WHERE username = ? AND status = 'belum'"
    arg: list = [username]
    if periode:
        sql += " AND periode = ?"
        arg.append(periode)
    return conn.execute(sql + " ORDER BY periode", arg).fetchall()


def _tagihan_menunggu(conn, username: str) -> set:
    ids = set()
    for r in conn.execute("SELECT tagihan_ids FROM konfirmasi_bayar WHERE username = ? AND status = 'menunggu'", (username,)):
        ids.update(json.loads(r[0]))
    return ids


@router.get("/api/portal/bayar")
def portal_bayar(request: Request, periode: Optional[str] = None):
    """QRIS bernominal untuk tagihan yang belum dibayar (satu bulan, atau semua bila periode kosong)."""
    u = request.state.portal["pelanggan"]
    if periode and not _periode_valid(periode):
        raise HTTPException(status_code=400, detail="Periode tidak valid.")
    with inti.db() as conn:
        q = _qris_aktif(conn)
        tg = [t for t in _tagihan_belum(conn, u, periode) if t["id"] not in _tagihan_menunggu(conn, u)]
    if not q:
        # Tanpa QRIS pelanggan tetap bisa membayar dengan cara lain (INFO_BAYAR) lalu mengirim bukti bayar dari portal.
        return {"tersedia": False, "pesan": "Pembayaran QRIS dari portal belum tersedia. " + inti.CONFIG["INFO_BAYAR"],
                "jumlah": sum(t["jumlah"] for t in tg),
                "tagihan": [{"periode": t["periode"], "jumlah": t["jumlah"]} for t in tg]}
    if not tg:
        return {"tersedia": True, "tagihan": [], "jumlah": 0, "pesan": "Tidak ada tagihan yang perlu dibayar."}
    jumlah = sum(t["jumlah"] for t in tg)
    otomatis = inti.CONFIG["QRIS_NOMINAL"] == "ya"
    return {"tersedia": True, "merchant": q["merchant"], "nmid": q["nmid"], "kota": q["kota"], "jumlah": jumlah,
            "tagihan": [{"periode": t["periode"], "jumlah": t["jumlah"]} for t in tg], "nominal_otomatis": otomatis,
            "qris": _qris_bernominal(q["payload"], jumlah) if otomatis else q["payload"]}


@router.get("/api/qris/uji")
def qris_uji(request: Request, jumlah: int = 1000):
    """QR uji untuk owner (mis. Rp1.000) supaya QRIS bisa dicoba dibayar dari beberapa aplikasi sebelum diumumkan."""
    inti._wajib_owner(request, "Uji QRIS hanya untuk owner.")
    if not 1 <= jumlah <= 1_000_000:
        raise HTTPException(status_code=400, detail="Nominal uji Rp1–Rp1.000.000.")
    with inti.db() as conn:
        q = _qris_aktif(conn)
    if not q:
        raise HTTPException(status_code=400, detail="Pasang QRIS dulu.")
    return {"merchant": q["merchant"], "nmid": q["nmid"], "jumlah": jumlah,
            "dengan_nominal": _qris_bernominal(q["payload"], jumlah), "tanpa_nominal": q["payload"]}


@router.post("/api/portal/konfirmasi")
async def portal_konfirmasi(request: Request, periode: str = "", catatan: str = ""):
    """Pelanggan mengirim bukti bayar (foto/PDF di badan permintaan) untuk tagihan periode (dipisah koma, kosong =
    semua yang belum dibayar). Tagihan belum lunas sampai admin memeriksa bukti dan mutasi rekeningnya."""
    u = request.state.portal["pelanggan"]
    daftar = [p.strip() for p in periode.split(",") if p.strip()]
    if any(not _periode_valid(p) for p in daftar):
        raise HTTPException(status_code=400, detail="Periode tidak valid.")
    batas = inti.CONFIG["NOTA_MAKS_MB"] * 1024 * 1024
    if int(request.headers.get("content-length") or 0) > batas:
        raise HTTPException(status_code=413, detail=f"Foto maksimal {inti.CONFIG['NOTA_MAKS_MB']} MB.")
    isi = await request.body()
    if not isi or len(isi) > batas:
        raise HTTPException(status_code=400, detail="Lampirkan foto bukti bayar.")
    jenis = inti._jenis_berkas(isi)
    if not jenis:
        raise HTTPException(status_code=415, detail="Bukti bayar harus foto (JPG/PNG/WEBP) atau PDF.")
    catatan = catatan.strip()[:200] or None

    def simpan():
        with inti.db() as conn:
            _siapkan_qris(conn)
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT COUNT(*) FROM konfirmasi_bayar WHERE username = ? AND status = 'menunggu'",
                            (u,)).fetchone()[0] >= 3:
                raise HTTPException(status_code=429, detail="Masih ada konfirmasi yang menunggu diperiksa. Mohon tunggu.")
            menunggu = _tagihan_menunggu(conn, u)
            tg = [t for p in (daftar or [None]) for t in _tagihan_belum(conn, u, p)]
            if not tg:
                raise HTTPException(status_code=400, detail="Tidak ada tagihan belum dibayar untuk dikonfirmasi.")
            if any(t["id"] in menunggu for t in tg):
                raise HTTPException(status_code=409, detail="Bukti untuk tagihan ini sudah dikirim dan sedang diperiksa.")
            berkas = f"konfirmasi/{datetime.now().strftime('%Y%m')}/{secrets.token_hex(12)}.{jenis[1]}"
            path = os.path.join(inti._folder_nota(), berkas)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(isi)
            nama = conn.execute("SELECT nama FROM pelanggan WHERE username = ?", (u,)).fetchone()
            cur = conn.execute("INSERT INTO konfirmasi_bayar (username, tagihan_ids, periode, jumlah, catatan, berkas, mime, "
                               "dibuat_pada) VALUES (?,?,?,?,?,?,?,?)",
                               (u, json.dumps([t["id"] for t in tg]), ",".join(t["periode"] for t in tg),
                                sum(t["jumlah"] for t in tg), catatan, berkas, jenis[0],
                                datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            return cur.lastrowid, tg, (nama["nama"] if nama else u)

    kid, tg, nama = await asyncio.to_thread(simpan)
    jumlah = sum(t["jumlah"] for t in tg)
    inti.catat_aktivitas(request.state.user, "konfirmasi_bayar", f"#K{kid} {u} {_rp(jumlah)} ({', '.join(t['periode'] for t in tg)})")
    try:
        inti.send_telegram_message(f"💳 <b>Konfirmasi bayar</b> #K{kid}\n{_esc(nama)} ({_esc(u)}): {_rp(jumlah)}\n"
                              f"Tagihan: {_esc(', '.join(_nama_periode(t['periode']) for t in tg))}\n"
                              "Cocokkan dengan mutasi rekening/QRIS, lalu Terima di dashboard → Penagihan → Konfirmasi bayar.")
    except Exception:
        pass
    return {"ok": True, "id": kid, "jumlah": jumlah}


@router.get("/api/portal/konfirmasi")
def portal_konfirmasi_daftar(request: Request):
    u = request.state.portal["pelanggan"]
    with inti.db() as conn:
        _siapkan_qris(conn)
        rows = conn.execute("SELECT id, periode, jumlah, status, alasan, dibuat_pada, diperiksa_pada FROM konfirmasi_bayar "
                            "WHERE username = ? ORDER BY id DESC LIMIT 10", (u,)).fetchall()
    return {"konfirmasi": [dict(r) for r in rows]}


@router.get("/api/konfirmasi-bayar")
def konfirmasi_daftar(request: Request, status: str = "menunggu"):
    inti._wajib_admin(request)
    with inti.db() as conn:
        _siapkan_qris(conn)
        sql = (f"SELECT k.*, p.nama, p.lokasi, {inti._SQL_ID} AS id_pelanggan FROM konfirmasi_bayar k "
               "LEFT JOIN pelanggan p ON p.username = k.username")
        arg: list = []
        if status != "semua":
            sql += " WHERE k.status = ?"
            arg.append(status)
        rows = conn.execute(sql + " ORDER BY k.id DESC LIMIT 100", arg).fetchall()
        n = conn.execute("SELECT COUNT(*) FROM konfirmasi_bayar WHERE status = 'menunggu'").fetchone()[0]
    return {"menunggu": n, "konfirmasi": [{k: r[k] for k in r.keys() if k not in ("berkas", "tagihan_ids")} for r in rows]}


@router.get("/api/konfirmasi-bayar/{kid}/bukti")
def konfirmasi_bukti(kid: int, request: Request):
    inti._wajib_admin(request)
    with inti.db() as conn:
        _siapkan_qris(conn)
        r = conn.execute("SELECT berkas, mime FROM konfirmasi_bayar WHERE id = ?", (kid,)).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Konfirmasi tidak ditemukan.")
    path = os.path.realpath(os.path.join(inti._folder_nota(), r["berkas"]))
    if not path.startswith(os.path.realpath(inti._folder_nota()) + os.sep) or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Berkas bukti tidak ada di server.")
    return FileResponse(path, media_type=r["mime"], headers={"Cache-Control": "private, max-age=3600"})


class PeriksaKonfirmasiIn(BaseModel):
    terima: bool
    alasan: Optional[str] = None
    tanggal: Optional[str] = None      # tanggal uang masuk (bawaan: tanggal bukti dikirim)
    metode: str = "qris"


@router.post("/api/konfirmasi-bayar/{kid}/periksa")
def konfirmasi_periksa(kid: int, d: PeriksaKonfirmasiIn, request: Request):
    """Terima: semua tagihan dalam konfirmasi ditandai lunas (uang masuk ke rekening/QRIS Pusat, bukan kas wilayah);
    isolir dibuka otomatis di putaran pemantauan berikutnya. Tolak: alasan wajib, tampil di portal pelanggan."""
    inti._wajib_admin(request)
    alasan = (d.alasan or "").strip()
    if not d.terima and len(alasan) < 3:
        raise HTTPException(status_code=400, detail="Tuliskan alasan penolakan (tampil ke pelanggan).")
    if d.metode not in inti._METODE:
        raise HTTPException(status_code=400, detail="Metode tidak valid.")
    sekarang = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with inti.db() as conn:
        _siapkan_qris(conn)
        conn.execute("BEGIN IMMEDIATE")
        k = conn.execute("SELECT * FROM konfirmasi_bayar WHERE id = ?", (kid,)).fetchone()
        if not k:
            raise HTTPException(status_code=404, detail="Konfirmasi tidak ditemukan.")
        if k["status"] != "menunggu":
            raise HTTPException(status_code=400, detail="Konfirmasi ini sudah diperiksa.")
        lunas, sudah = [], []
        if d.terima:
            tanggal = d.tanggal or k["dibuat_pada"][:10]
            inti._cek_tanggal_lalu(tanggal, "Tanggal bayar")
            if tanggal > sekarang[:10]:
                raise HTTPException(status_code=400, detail="Tanggal bayar tidak boleh di masa depan.")
            inti._wajib_buku_terbuka(conn, tanggal)
            for tid in json.loads(k["tagihan_ids"]):
                # lokasi_kas = 0: uangnya masuk rekening/QRIS Pusat, tidak menambah wajib setor admin wilayah.
                cur = conn.execute("UPDATE tagihan SET status='lunas', tgl_bayar=?, metode_bayar=?, lunas_oleh=?, lunas_pada=?, "
                                   "lokasi_kas=0, catatan=TRIM(COALESCE(catatan || '; ', '') || ?) "
                                   "WHERE id=? AND status != 'lunas'",
                                   (tanggal, d.metode, inti._oleh(request), sekarang, f"konfirmasi #K{kid}", tid))
                (lunas if cur.rowcount else sudah).append(tid)
            conn.execute("UPDATE konfirmasi_bayar SET status='diterima', diperiksa_oleh=?, diperiksa_pada=?, metode=?, "
                         "tgl_bayar=? WHERE id=?", (inti._oleh(request), sekarang, d.metode, tanggal, kid))
        else:
            conn.execute("UPDATE konfirmasi_bayar SET status='ditolak', alasan=?, diperiksa_oleh=?, diperiksa_pada=? WHERE id=?",
                         (alasan[:200], inti._oleh(request), sekarang, kid))
        conn.commit()
    inti.catat_aktivitas(inti._oleh(request), "konfirmasi_bayar",
                    f"#K{kid} {k['username']} {_rp(k['jumlah'])}: " + (f"DITERIMA ({d.metode}), {len(lunas)} tagihan lunas"
                                                                       + (f", {len(sudah)} sudah lunas sebelumnya" if sudah else "")
                                                                       if d.terima else f"ditolak: {alasan[:100]}"))
    return {"ok": True, "status": "diterima" if d.terima else "ditolak", "lunas": len(lunas), "sudah_lunas": len(sudah)}
