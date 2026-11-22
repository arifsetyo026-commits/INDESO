"""Admin wilayah (peran "wilayah"): mengurus pelanggan SATU lokasi dan melihat keuangannya tanpa bisa mengubah.

Bisa: tambah pelanggan, hapus salah input yang baru dicatat (maks. 30 hari, tanpa tagihan bulan lain), lihat keuangan.
Berhenti/aktif, isolir, & penagihan hanya oleh Pusat (supaya pelanggan yang menunggak tidak bisa diaktifkan atau dihapus
tunggakannya dari wilayah). Middleware main.py hanya membuka /api/wilayah/* untuk peran ini, dan setiap jalur di sini
menyaring ke lokasi akunnya, jadi data lokasi lain, pengaturan, akun, MikroTik/OLT, dan keuangan Pusat tidak terjangkau.
Permintaan material wilayah ada di indeso/gudang.py.

Dipindah dari main.py (Tahap 4, docs/rencana-perbaikan.md). Pembantu inti dipanggil lewat `inti.<nama>`;
jalur API didaftarkan main.py lewat `app.include_router(router)`.
"""
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from indeso import inti
from indeso.format import _geser_periode

router = APIRouter()


def _lokasi_wilayah(request: Request) -> dict:
    if request.state.peran != "wilayah":
        raise HTTPException(status_code=403, detail="Khusus akun admin wilayah.")
    kode = inti._lokasi_mitra(request)
    with inti.db() as conn:
        r = conn.execute("SELECT kode, nama FROM lokasi WHERE kode = ?", (kode,)).fetchone()
    if not r:
        raise HTTPException(status_code=403, detail="Lokasi akun ini sudah tidak ada. Hubungi Pusat.")
    return dict(r)


def _pelanggan_wilayah(conn, username: str, lokasi: int) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM pelanggan WHERE username = ? AND COALESCE(lokasi, 1) = ?", (username, lokasi)).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Pelanggan tidak ditemukan di wilayah Anda.")
    return r


@router.get("/api/wilayah/data")
def wilayah_data(request: Request):
    lok = _lokasi_wilayah(request)
    bln = datetime.now().strftime("%Y-%m")
    online = {(u.username or "").lower() for u in (inti._latest_data.pppoe_users if inti._latest_data else [])}
    with inti.db() as conn:
        paket = [dict(r) for r in conn.execute("SELECT id, nama, kecepatan_mbps, harga FROM paket ORDER BY harga, nama")]
        try:
            isolir = {r[0].lower() for r in conn.execute("SELECT username FROM isolir")}
        except sqlite3.OperationalError:
            isolir = set()
        rows = conn.execute(
            f"SELECT p.username, p.nama, p.alamat, p.no_hp, p.status, p.tgl_pasang, p.berhenti_sejak, p.gratis, "
            f"COALESCE(p.koneksi, 'pppoe') AS koneksi, k.nama AS paket, k.harga, {inti._SQL_ID} AS id_pelanggan, "
            f"(SELECT t.status FROM tagihan t WHERE t.username = p.username AND t.periode = ?) AS tagihan_bulan_ini, "
            f"(SELECT COUNT(*) FROM tagihan t WHERE t.username = p.username AND t.status = 'belum') AS n_tunggak, "
            f"(SELECT COALESCE(SUM(t.jumlah), 0) FROM tagihan t WHERE t.username = p.username AND t.status = 'belum') AS tunggak, "
            f"(SELECT COUNT(*) FROM tagihan t WHERE t.username = p.username "
            f"AND t.periode != substr(COALESCE(p.tgl_pasang, ''), 1, 7)) AS n_tagihan_lain "
            f"FROM pelanggan p LEFT JOIN paket k ON k.id = p.paket_id WHERE COALESCE(p.lokasi, 1) = ? "
            f"ORDER BY p.status, p.nama COLLATE NOCASE", (bln, lok["kode"])).fetchall()
    pel = [dict(r, online=r["username"].lower() in online, isolir=r["username"].lower() in isolir) for r in rows]
    return {"akun": request.state.user, "lokasi": lok, "periode": bln, "paket": paket, "pelanggan": pel}


class TambahWilayahIn(BaseModel):
    username: str
    nama: str
    alamat: Optional[str] = None
    no_hp: Optional[str] = None
    paket_id: int
    tgl_pasang: str
    koneksi: str = "pppoe"


@router.post("/api/wilayah/pelanggan")
def wilayah_tambah(d: TambahWilayahIn, request: Request):
    """Pelanggan baru selalu masuk lokasi akun ini. Tagihan bulan pertama dihitung prorata seperti biasa; bila perlu
    diubah (diskon, gratis, tagihan penuh) diurus Pusat."""
    lok = _lokasi_wilayah(request)
    return inti._tambah_pelanggan(inti.TambahPelangganIn(**d.model_dump(), lokasi=lok["kode"]), request)


@router.delete("/api/wilayah/pelanggan/{username}")
def wilayah_hapus(username: str, request: Request, hapus_secret: bool = False):
    """Hapus pelanggan SALAH INPUT wilayah ini: hanya yang dicatat <= 30 hari lalu dan tagihannya cuma tagihan bulan
    pasang (belum dibayar). Menghapus pelanggan ikut menghapus tagihan belum bayarnya, jadi pelanggan lama / yang
    menunggak hanya bisa dihapus atau diberhentikan oleh Pusat (supaya tunggakan tidak bisa dihapus dari wilayah)."""
    lok = _lokasi_wilayah(request)
    with inti.db() as conn:
        p = _pelanggan_wilayah(conn, username, lok["kode"])
        tgl = p["tgl_pasang"] or ""
        baru = bool(tgl) and tgl >= (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        lain = conn.execute("SELECT COUNT(*) FROM tagihan WHERE username = ? AND periode != ?",
                            (username, tgl[:7])).fetchone()[0]
    if p["status"] != "aktif" or not baru or lain:
        raise HTTPException(status_code=403, detail="Hanya pelanggan salah input yang baru dicatat (maks. 30 hari, tanpa "
                            "tagihan bulan lain) yang bisa dihapus dari wilayah. Untuk memberhentikan atau menghapus "
                            "pelanggan lain, hubungi Pusat.")
    return inti._hapus_pelanggan(username, request, hapus_secret)


@router.get("/api/wilayah/keuangan")
def wilayah_keuangan(request: Request, bulan: int = 12):
    """Keuangan wilayah ini (lihat saja): tagihan per bulan, uang masuk, tunggakan per pelanggan, transaksi kas wilayah."""
    lok = _lokasi_wilayah(request)
    kode = lok["kode"]
    n = max(1, min(int(bulan), 24))
    akhir = datetime.now().strftime("%Y-%m")
    periode = [_geser_periode(akhir, -i) for i in range(n)]
    awal = periode[-1]
    with inti.db() as conn:
        tag = {r["periode"]: dict(r) for r in conn.execute(
            "SELECT t.periode, COUNT(*) AS n, COALESCE(SUM(t.jumlah), 0) AS ditagih, "
            "COALESCE(SUM(t.status = 'lunas'), 0) AS n_lunas, "
            "COALESCE(SUM(CASE WHEN t.status = 'lunas' THEN t.jumlah END), 0) AS lunas, "
            "COALESCE(SUM(t.status = 'belum'), 0) AS n_belum, "
            "COALESCE(SUM(CASE WHEN t.status = 'belum' THEN t.jumlah END), 0) AS belum "
            "FROM tagihan t JOIN pelanggan p ON p.username = t.username "
            "WHERE COALESCE(p.lokasi, 1) = ? AND t.periode >= ? GROUP BY t.periode", (kode, awal))}
        masuk = dict(conn.execute(
            "SELECT substr(t.tgl_bayar, 1, 7), COALESCE(SUM(t.jumlah), 0) FROM tagihan t "
            "JOIN pelanggan p ON p.username = t.username WHERE t.status = 'lunas' AND COALESCE(p.lokasi, 1) = ? "
            "AND t.tgl_bayar >= ? GROUP BY 1", (kode, f"{awal}-01")).fetchall())
        tunggakan = [dict(r) for r in conn.execute(
            f"SELECT p.username, p.nama, {inti._SQL_ID} AS id_pelanggan, COUNT(*) AS n, COALESCE(SUM(t.jumlah), 0) AS jumlah, "
            "MIN(t.periode) AS sejak FROM tagihan t JOIN pelanggan p ON p.username = t.username "
            "WHERE t.status = 'belum' AND COALESCE(p.lokasi, 1) = ? GROUP BY p.username ORDER BY jumlah DESC, p.nama",
            (kode,))]
        transaksi = [dict(r) for r in conn.execute(
            "SELECT t.tanggal, t.jenis, k.nama AS kategori, t.jumlah, t.keterangan FROM transaksi t "
            "LEFT JOIN kategori_kas k ON k.id = t.kategori_id WHERE t.status = 'aktif' AND t.lokasi = ? "
            "ORDER BY t.tanggal DESC, t.id DESC LIMIT 50", (kode,))]
        kas = inti._posisi_kas_wilayah(conn, kode)
    kosong = {"n": 0, "ditagih": 0, "n_lunas": 0, "lunas": 0, "n_belum": 0, "belum": 0}
    per_bulan = [{**kosong, **tag.get(p, {}), "periode": p, "uang_masuk": masuk.get(p, 0)} for p in periode]
    return {"lokasi": lok, "per_bulan": per_bulan, "tunggakan": tunggakan,
            "total_tunggakan": sum(x["jumlah"] for x in tunggakan), "transaksi": transaksi, "kas": kas}
