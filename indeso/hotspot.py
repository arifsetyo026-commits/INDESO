"""Hotspot: voucher WiFi terbuka yang dititipkan & dijual warung/reseller, paket, cetakan, setoran, skrip MikroTik,
dan halaman reseller.

Voucher = user hotspot MikroTik (username = password = kode) dengan batas TOTAL waktu pakai (limit-uptime) yang
ditegakkan MikroTik sendiri, jadi tetap berlaku walau MikroTik tidak tersambung ke INDESO. Kecepatan & jumlah HP
per voucher lewat user profile INDESO-HS-<id paket>. Login lewat QR: http://<dns>/login?username=..&password=..
(server profile hotspot perlu login-by http-pap). Lokasi yang jaringannya tersambung ke INDESO: user langsung
dibuat lewat API; lokasi lain: INDESO mencetak skrip untuk ditempel di Terminal MikroTik lokasi itu (v6 & v7).
Alur uang: voucher dititipkan ke reseller (status stok) → terjual saat reseller menampilkan QR-nya → reseller
menyetor (harga reseller × terjual) → kas masuk "Pendapatan voucher hotspot".

Dipindah dari main.py (Tahap 4, docs/rencana-perbaikan.md). Pembantu inti dipanggil lewat `inti.<nama>`;
jalur API didaftarkan main.py lewat `app.include_router(router)`.
"""
import html
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from indeso import inti
from indeso.format import _rp, _sekarang_str, _teks_durasi

router = APIRouter()


_KAT_HOTSPOT = "Pendapatan voucher hotspot"


_HURUF_VOUCHER = "abcdefghjkmnpqrstuvwxyz23456789"     # tanpa 0/o dan 1/l/i: tidak tertukar saat dibaca dari kertas


_KOMENTAR_HS = "INDESO voucher"


@inti._skema_sekali
def _skema_hotspot(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS hs_paket (
        id INTEGER PRIMARY KEY AUTOINCREMENT, nama TEXT NOT NULL, lokasi INTEGER NOT NULL DEFAULT 1,
        menit INTEGER NOT NULL, mbps REAL NOT NULL, perangkat INTEGER NOT NULL DEFAULT 1, harga INTEGER NOT NULL,
        harga_reseller INTEGER NOT NULL, aktif INTEGER NOT NULL DEFAULT 1, dibuat TEXT, UNIQUE (nama, lokasi))""")
    # Harga & lama pakai disalin ke batch saat dibuat: mengubah paket tidak mengubah voucher yang sudah dicetak.
    conn.execute("""CREATE TABLE IF NOT EXISTS hs_batch (
        id INTEGER PRIMARY KEY AUTOINCREMENT, paket_id INTEGER NOT NULL, lokasi INTEGER NOT NULL, reseller TEXT,
        jumlah INTEGER NOT NULL, menit INTEGER NOT NULL, harga INTEGER NOT NULL, harga_reseller INTEGER NOT NULL,
        status TEXT NOT NULL, dibuat TEXT NOT NULL, oleh TEXT, terpasang TEXT, catatan TEXT)""")
    # status voucher: menunggu (skrip belum ditempel) → stok → terjual; atau batal.
    conn.execute("""CREATE TABLE IF NOT EXISTS hs_voucher (
        kode TEXT PRIMARY KEY, batch_id INTEGER NOT NULL, paket_id INTEGER NOT NULL, reseller TEXT,
        status TEXT NOT NULL, terjual_pada TEXT, setoran_id INTEGER)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hs_voucher ON hs_voucher(reseller, status)")
    conn.execute("""CREATE TABLE IF NOT EXISTS hs_setoran (
        id INTEGER PRIMARY KEY AUTOINCREMENT, reseller TEXT NOT NULL, lokasi INTEGER NOT NULL, waktu TEXT NOT NULL,
        jumlah_voucher INTEGER NOT NULL, nominal INTEGER NOT NULL, metode TEXT, transaksi_id INTEGER, oleh TEXT,
        status TEXT NOT NULL DEFAULT 'aktif', batal_alasan TEXT)""")
    if "hotspot_setoran_id" not in [r[1] for r in conn.execute("PRAGMA table_info(transaksi)")]:
        conn.execute("ALTER TABLE transaksi ADD COLUMN hotspot_setoran_id INTEGER")


@inti._skema_sekali
def _siapkan_hotspot(conn) -> None:
    """Tabel hotspot + kategori kas "Hotspot". Kategori baru dibuat saat fitur hotspot pertama kali dipakai, supaya
    usaha yang tidak berjualan voucher tidak melihat kategori itu di Keuangan."""
    _skema_hotspot(conn)
    conn.execute("INSERT OR IGNORE INTO kategori_kas (nama, jenis, pengaruh_laba) VALUES (?, 'masuk', 1)", (_KAT_HOTSPOT,))


def _waktu_mikrotik(menit: int) -> str:
    """Format waktu RouterOS (v6 & v7): 3h, 1d, 1h30m."""
    hari, sisa = divmod(int(menit), 1440)
    jam, mnt = divmod(sisa, 60)
    return "".join(f"{n}{h}" for n, h in ((hari, "d"), (jam, "h"), (mnt, "m")) if n) or "0m"


def _rate_mikrotik(mbps: float) -> str:
    mbps = float(mbps)
    return f"{int(mbps)}M" if mbps.is_integer() else f"{int(round(mbps * 1000))}k"


def _skrip_masuk_hs(kunci_hp: bool) -> str:
    """Skrip on-login profil voucher (RouterOS v6/v7). Berjalan setiap voucher masuk, tapi hanya bertindak saat
    PERTAMA kali masuk (komentar user belum diawali "AKTIF:"):
    - masa pakai dihitung SEJAK PERTAMA MASUK, tidak berhenti saat HP terputus: dibuat penjadwal (scheduler)
      yang menghapus voucher, sesi aktif, dan cookie-nya ketika masa pakai habis (sisa = limit-uptime - uptime);
    - voucher 1 HP dikunci ke MAC HP pertama, jadi QR/kode yang difoto/diteruskan tidak bisa dipakai HP lain.
    HP yang sama tersambung lagi otomatis lewat MAC cookie (login-by=mac-cookie) tanpa memasukkan kode."""
    kunci = '/ip hotspot user set $h mac-address=$"mac-address"; ' if kunci_hp else ""
    return (':local u $user; :local h [/ip hotspot user find where name=$u]; '
            ':if ([:len $h] > 0) do={ :local c [/ip hotspot user get $h comment]; '
            ':if ([:pick $c 0 6] != "AKTIF:") do={ '
            ':local t ([/ip hotspot user get $h limit-uptime] - [/ip hotspot user get $h uptime]); '
            '/ip hotspot user set $h comment=("AKTIF: " . [/system clock get date] . " " . [/system clock get time] . " | " . $c); '
            + kunci +
            ':local n ("INDESO-HS-" . $u); /system scheduler remove [find where name=$n]; '
            '/system scheduler add name=$n interval=$t start-date=[/system clock get date] comment="INDESO: hapus voucher saat masa pakai habis" '
            'on-event=("/ip hotspot active remove [find where user=\\"" . $u . "\\"]; '
            '/ip hotspot cookie remove [find where user=\\"" . $u . "\\"]; '
            '/ip hotspot user remove [find where name=\\"" . $u . "\\"]; '
            '/system scheduler remove [find where name=\\"" . $n . "\\"]") } }')


def _profil_hs(p) -> tuple[str, dict]:
    rate = _rate_mikrotik(p["mbps"])
    return f"INDESO-HS-{p['id']}", {"rate-limit": f"{rate}/{rate}", "shared-users": str(int(p["perangkat"])),
                                    "add-mac-cookie": "yes", "mac-cookie-timeout": _waktu_mikrotik(p["menit"]),
                                    "on-login": _skrip_masuk_hs(int(p["perangkat"]) == 1)}


def _hs_lokasi(conn, kode: int) -> dict:
    r = conn.execute("SELECT kode, nama, jaringan, hs_ssid, hs_dns FROM lokasi WHERE kode = ?", (kode,)).fetchone()
    if not r:
        raise HTTPException(status_code=400, detail="Lokasi tidak ditemukan.")
    return {"kode": r["kode"], "nama": r["nama"], "jaringan": bool(r["jaringan"]),
            "ssid": r["hs_ssid"] or "INTERNET DESA HOTSPOT", "dns": r["hs_dns"] or "hotspot.desa"}


def _url_login(dns: str, kode: str) -> str:
    return f"http://{dns}/login?username={kode}&password={kode}"


def _kutip_rsc(t: str) -> str:
    return '"' + str(t).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def _skrip_profil(p) -> list[str]:
    nama, nilai = _profil_hs(p)
    return ["/ip hotspot user profile",
            f":if ([:len [find name={_kutip_rsc(nama)}]] = 0) do={{ add name={_kutip_rsc(nama)} }}",
            f"set [find name={_kutip_rsc(nama)}] " + " ".join(
                f"{k}={_kutip_rsc(v) if k == 'on-login' else v}" for k, v in nilai.items())]


@router.get("/api/hotspot")
def hotspot_ringkas(request: Request, lokasi: int = 1):
    inti._wajib_admin(request)
    with inti.db() as conn:
        _siapkan_hotspot(conn)
        lok = _hs_lokasi(conn, lokasi)
        daftar_lokasi = [dict(r) for r in conn.execute("SELECT kode, nama FROM lokasi ORDER BY kode")]
        paket = [dict(r, durasi=_teks_durasi(r["menit"])) for r in conn.execute(
            "SELECT * FROM hs_paket WHERE lokasi = ? ORDER BY aktif DESC, menit, harga", (lokasi,))]
        reseller = []
        for a in conn.execute("SELECT username, aktif FROM akun WHERE peran = 'reseller' AND COALESCE(lokasi, 1) = ? "
                              "ORDER BY username COLLATE NOCASE", (lokasi,)):
            st = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM hs_voucher WHERE reseller = ? GROUP BY status", (a["username"],))}
            belum = conn.execute("SELECT COUNT(*), COALESCE(SUM(b.harga_reseller), 0) FROM hs_voucher v JOIN hs_batch b "
                                 "ON b.id = v.batch_id WHERE v.reseller = ? AND v.status = 'terjual' AND v.setoran_id IS NULL",
                                 (a["username"],)).fetchone()
            stok = [dict(r) for r in conn.execute(
                "SELECT p.nama, COUNT(*) AS n FROM hs_voucher v JOIN hs_paket p ON p.id = v.paket_id WHERE v.reseller = ? "
                "AND v.status = 'stok' GROUP BY p.id ORDER BY p.menit", (a["username"],))]
            reseller.append({"username": a["username"], "aktif": bool(a["aktif"]), "stok": st.get("stok", 0),
                             "stok_paket": stok, "menunggu": st.get("menunggu", 0), "terjual": st.get("terjual", 0),
                             "belum_setor": belum[0], "nominal_belum_setor": belum[1]})
        batch = [dict(r, durasi=_teks_durasi(r["menit"])) for r in conn.execute(
            "SELECT b.*, p.nama AS paket, (SELECT COUNT(*) FROM hs_voucher v WHERE v.batch_id = b.id AND v.status = 'terjual') "
            "AS terjual, (SELECT COUNT(*) FROM hs_voucher v WHERE v.batch_id = b.id AND v.status IN ('stok','menunggu')) AS sisa "
            "FROM hs_batch b JOIN hs_paket p ON p.id = b.paket_id WHERE b.lokasi = ? ORDER BY b.id DESC LIMIT 50", (lokasi,))]
        setoran = [dict(r) for r in conn.execute(
            "SELECT s.*, t.nomor FROM hs_setoran s LEFT JOIN transaksi t ON t.id = s.transaksi_id WHERE s.lokasi = ? "
            "ORDER BY s.id DESC LIMIT 30", (lokasi,))]
        conn.commit()
    return {"peran": request.state.peran, "lokasi": lok, "daftar_lokasi": daftar_lokasi, "paket": paket,
            "reseller": reseller, "batch": batch, "setoran": setoran}


class HsLokasiIn(BaseModel):
    ssid: str
    dns: str


@router.put("/api/hotspot/lokasi/{kode}")
def hotspot_atur_lokasi(kode: int, d: HsLokasiIn, request: Request):
    inti._wajib_owner(request, "Pengaturan hotspot hanya bisa diubah owner.")
    ssid, dns = d.ssid.strip(), d.dns.strip().lower()
    if not (1 <= len(ssid) <= 32 and all(32 <= ord(c) < 127 for c in ssid)):
        raise HTTPException(status_code=400, detail="Nama WiFi 1–32 huruf/angka biasa.")
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+|\d{1,3}(\.\d{1,3}){3}", dns):
        raise HTTPException(status_code=400, detail="Nama DNS hotspot tidak valid (contoh: hotspot.desa atau 10.5.50.1).")
    with inti.db() as conn:
        if not conn.execute("UPDATE lokasi SET hs_ssid = ?, hs_dns = ? WHERE kode = ?", (ssid, dns, kode)).rowcount:
            raise HTTPException(status_code=404, detail="Lokasi tidak ditemukan.")
        conn.commit()
    inti.catat_aktivitas(inti._oleh(request), "hotspot", f"lokasi {kode}: WiFi {ssid}, DNS {dns}")
    return {"ok": True}


class HsPaketIn(BaseModel):
    nama: str
    lokasi: int = 1
    menit: int
    mbps: float
    perangkat: int = 1
    harga: int
    harga_reseller: int
    aktif: bool = True


def _simpan_hs_paket(pid: Optional[int], d: HsPaketIn, request: Request) -> dict:
    inti._wajib_owner(request, "Paket hotspot (harga, lama pakai) hanya bisa diatur owner.")
    nama = d.nama.strip()
    if not (1 <= len(nama) <= 40 and nama.isprintable()):
        raise HTTPException(status_code=400, detail="Nama paket 1–40 huruf.")
    if not 30 <= d.menit <= 90 * 1440:
        raise HTTPException(status_code=400, detail="Lama pakai 30 menit sampai 90 hari.")
    if not 0.5 <= d.mbps <= 1000 or not 1 <= d.perangkat <= 10:
        raise HTTPException(status_code=400, detail="Kecepatan 0,5–1000 Mbps; perangkat per voucher 1–10.")
    if not 0 <= d.harga_reseller <= d.harga <= 10_000_000:
        raise HTTPException(status_code=400, detail="Harga reseller harus 0 sampai harga eceran.")
    with inti.db() as conn:
        _siapkan_hotspot(conn)
        inti._cek_lokasi(conn, d.lokasi)
        nilai = (nama, d.lokasi, d.menit, d.mbps, d.perangkat, d.harga, d.harga_reseller, int(d.aktif))
        try:
            if pid is None:
                pid = conn.execute("INSERT INTO hs_paket (nama, lokasi, menit, mbps, perangkat, harga, harga_reseller, aktif, "
                                   "dibuat) VALUES (?,?,?,?,?,?,?,?,?)", nilai + (_sekarang_str(),)).lastrowid
            elif not conn.execute("UPDATE hs_paket SET nama=?, lokasi=?, menit=?, mbps=?, perangkat=?, harga=?, harga_reseller=?, "
                                  "aktif=? WHERE id=?", nilai + (pid,)).rowcount:
                raise HTTPException(status_code=404, detail="Paket tidak ditemukan.")
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Nama paket sudah dipakai di lokasi ini.")
        p = conn.execute("SELECT * FROM hs_paket WHERE id = ?", (pid,)).fetchone()
        lok = _hs_lokasi(conn, d.lokasi)
        conn.commit()
    inti.catat_aktivitas(inti._oleh(request), "hotspot", f"paket {nama} (lokasi {d.lokasi}): {_teks_durasi(d.menit)}, "
                    f"{_rp(d.harga)} / reseller {_rp(d.harga_reseller)}, {d.mbps:g} Mbps, {d.perangkat} HP")
    # Profil kecepatan diselaraskan langsung bila MikroTik lokasi ini tersambung; selain itu lewat skrip.
    sinkron = None
    if lok["jaringan"]:
        try:
            pool = inti._mikrotik_pool()
            try:
                res = pool.get_api().get_resource("/ip/hotspot/user/profile")
                nama_p, nilai_p = _profil_hs(p)
                ada = res.get(name=nama_p)
                if ada:
                    res.set(id=ada[0]["id"], **nilai_p)
                else:
                    res.add(name=nama_p, **nilai_p)
                sinkron = True
            finally:
                pool.disconnect()
        except Exception as e:
            sinkron = f"Profil belum terpasang di MikroTik ({e}); terpasang otomatis saat voucher pertama dibuat."
    return {"ok": True, "id": pid, "mikrotik": sinkron}


@router.post("/api/hotspot/paket")
def hotspot_paket_tambah(d: HsPaketIn, request: Request):
    return _simpan_hs_paket(None, d, request)


@router.put("/api/hotspot/paket/{pid}")
def hotspot_paket_ubah(pid: int, d: HsPaketIn, request: Request):
    return _simpan_hs_paket(pid, d, request)


class HsBatchIn(BaseModel):
    paket_id: int
    jumlah: int
    reseller: str


def _kode_voucher_baru(conn, jumlah: int) -> list[str]:
    kode = set()
    while len(kode) < jumlah:
        k = "".join(secrets.choice(_HURUF_VOUCHER) for _ in range(8))
        if k not in kode and not conn.execute("SELECT 1 FROM hs_voucher WHERE kode = ?", (k,)).fetchone():
            kode.add(k)
    return sorted(kode)


def _skrip_user(b, p, kode: list[str]) -> str:
    nama_p, _ = _profil_hs(p)
    baris = [f"# INDESO: voucher {p['nama']} x{len(kode)} (batch B{b['id']}) untuk {b['reseller']}",
             "# Tempel di Terminal MikroTik (RouterOS v6/v7). Aman ditempel ulang: user yang sudah ada dilewati."]
    baris += _skrip_profil(p) + ["/ip hotspot user"]
    komentar = _kutip_rsc(_KOMENTAR_HS + " " + str(b["reseller"]))
    lama = _waktu_mikrotik(b["menit"])
    for k in kode:
        baris.append(f":if ([:len [find name={_kutip_rsc(k)}]] = 0) do={{ add name={_kutip_rsc(k)} password={_kutip_rsc(k)} "
                     f"profile={_kutip_rsc(nama_p)} limit-uptime={lama} comment={komentar} }}")
    return "\n".join(baris) + "\n"


def _hapus_user_hs(kode: list[str]) -> None:
    pool = inti._mikrotik_pool()
    try:
        api = pool.get_api()
        res, aktif = api.get_resource("/ip/hotspot/user"), api.get_resource("/ip/hotspot/active")
        for k in kode:
            for a in aktif.get(user=k):
                aktif.remove(id=a["id"])
            for u in res.get(name=k):
                if str(u.get("comment", "")).startswith(_KOMENTAR_HS):
                    res.remove(id=u["id"])
    finally:
        pool.disconnect()


@router.post("/api/hotspot/batch")
def hotspot_batch(d: HsBatchIn, request: Request):
    """Cetak & titipkan voucher ke reseller. Lokasi berjaringan: user hotspot dibuat langsung di MikroTik (gagal di
    tengah = semua dibersihkan, tidak ada yang tercatat). Lokasi lain: voucher menunggu skripnya ditempel."""
    inti._wajib_admin(request)
    if not 1 <= d.jumlah <= 500:
        raise HTTPException(status_code=400, detail="Jumlah voucher 1–500 per cetakan.")
    with inti.db() as conn:
        _siapkan_hotspot(conn)
        p = conn.execute("SELECT * FROM hs_paket WHERE id = ? AND aktif = 1", (d.paket_id,)).fetchone()
        if not p:
            raise HTTPException(status_code=404, detail="Paket tidak ditemukan atau nonaktif.")
        r = conn.execute("SELECT username FROM akun WHERE username = ? AND peran = 'reseller' AND aktif = 1 "
                         "AND COALESCE(lokasi, 1) = ?", (d.reseller, p["lokasi"])).fetchone()
        if not r:
            raise HTTPException(status_code=400, detail="Reseller tidak ditemukan di lokasi paket ini.")
        lok = _hs_lokasi(conn, p["lokasi"])
        kode = _kode_voucher_baru(conn, d.jumlah)
        conn.commit()
    status = "stok" if lok["jaringan"] else "menunggu"
    b = {"id": 0, "reseller": r["username"], "menit": p["menit"]}
    if lok["jaringan"]:
        nama_p, nilai_p = _profil_hs(p)
        dibuat = []
        try:
            pool = inti._mikrotik_pool()
            try:
                api = pool.get_api()
                prof = api.get_resource("/ip/hotspot/user/profile")
                ada = prof.get(name=nama_p)
                if ada:
                    prof.set(id=ada[0]["id"], **nilai_p)
                else:
                    prof.add(name=nama_p, **nilai_p)
                res = api.get_resource("/ip/hotspot/user")
                for k in kode:
                    res.add(name=k, password=k, profile=nama_p, **{"limit-uptime": _waktu_mikrotik(p["menit"])},
                            comment=f"{_KOMENTAR_HS} {r['username']}")
                    dibuat.append(k)
            finally:
                pool.disconnect()
        except Exception as e:
            if dibuat:
                try:
                    _hapus_user_hs(dibuat)
                except Exception as e2:
                    inti.logger.error(f"Gagal membersihkan user hotspot setelah gagal: {e2}")
            raise HTTPException(status_code=502, detail=f"Gagal membuat voucher di MikroTik: {e}. Tidak ada yang tercatat.")
    with inti.db() as conn:
        bid = conn.execute("INSERT INTO hs_batch (paket_id, lokasi, reseller, jumlah, menit, harga, harga_reseller, status, "
                           "dibuat, oleh, terpasang) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (p["id"], p["lokasi"], r["username"], d.jumlah, p["menit"], p["harga"], p["harga_reseller"],
                            "siap" if lok["jaringan"] else "menunggu", _sekarang_str(), inti._oleh(request),
                            _sekarang_str() if lok["jaringan"] else None)).lastrowid
        conn.executemany("INSERT INTO hs_voucher (kode, batch_id, paket_id, reseller, status) VALUES (?,?,?,?,?)",
                         [(k, bid, p["id"], r["username"], status) for k in kode])
        conn.commit()
    b["id"] = bid
    inti.catat_aktivitas(inti._oleh(request), "hotspot", f"B{bid}: {d.jumlah} voucher {p['nama']} dititipkan ke {r['username']}"
                    + ("" if lok["jaringan"] else " (menunggu skrip ditempel di MikroTik)"))
    return {"ok": True, "id": bid, "status": "siap" if lok["jaringan"] else "menunggu",
            "skrip": None if lok["jaringan"] else _skrip_user(b, p, kode)}


def _batch(conn, bid: int):
    _siapkan_hotspot(conn)
    b = conn.execute("SELECT b.*, p.nama AS paket FROM hs_batch b JOIN hs_paket p ON p.id = b.paket_id WHERE b.id = ?",
                     (bid,)).fetchone()
    if not b:
        raise HTTPException(status_code=404, detail="Cetakan voucher tidak ditemukan.")
    return b


@router.get("/api/hotspot/batch/{bid}")
def hotspot_batch_rinci(bid: int, request: Request):
    """Isi kartu voucher (kode & alamat login untuk QR) dan skrip MikroTik-nya."""
    inti._wajib_admin(request)
    with inti.db() as conn:
        b = _batch(conn, bid)
        p = conn.execute("SELECT * FROM hs_paket WHERE id = ?", (b["paket_id"],)).fetchone()
        lok = _hs_lokasi(conn, b["lokasi"])
        v = conn.execute("SELECT kode, status FROM hs_voucher WHERE batch_id = ? ORDER BY kode", (bid,)).fetchall()
    aktif = [x["kode"] for x in v if x["status"] in ("stok", "menunggu")]
    return {"batch": dict(b, durasi=_teks_durasi(b["menit"])), "lokasi": lok, "usaha": inti.CONFIG["NAMA_USAHA"],
            "voucher": [{"kode": x["kode"], "status": x["status"], "login": _url_login(lok["dns"], x["kode"])} for x in v],
            "skrip": _skrip_user(b, dict(p, menit=b["menit"]), aktif) if aktif else None}


@router.post("/api/hotspot/batch/{bid}/terpasang")
def hotspot_batch_terpasang(bid: int, request: Request):
    """Admin memastikan skripnya sudah ditempel di MikroTik: voucher masuk stok reseller dan bisa dijual."""
    inti._wajib_admin(request)
    with inti.db() as conn:
        b = _batch(conn, bid)
        if b["status"] != "menunggu":
            raise HTTPException(status_code=400, detail="Cetakan ini tidak sedang menunggu skrip.")
        conn.execute("UPDATE hs_batch SET status = 'siap', terpasang = ? WHERE id = ?", (_sekarang_str(), bid))
        conn.execute("UPDATE hs_voucher SET status = 'stok' WHERE batch_id = ? AND status = 'menunggu'", (bid,))
        conn.commit()
    inti.catat_aktivitas(inti._oleh(request), "hotspot", f"B{bid}: skrip sudah ditempel di MikroTik, voucher siap dijual")
    return {"ok": True}


@router.post("/api/hotspot/batch/{bid}/batal")
def hotspot_batch_batal(bid: int, d: inti.BatalIn, request: Request):
    """Tarik sisa voucher yang belum terjual (mis. reseller berhenti). Voucher terjual tidak disentuh."""
    inti._wajib_admin(request)
    alasan = d.alasan.strip()
    if len(alasan) < 3:
        raise HTTPException(status_code=400, detail="Tuliskan alasan pembatalan.")
    with inti.db() as conn:
        b = _batch(conn, bid)
        lok = _hs_lokasi(conn, b["lokasi"])
        sisa = [r[0] for r in conn.execute("SELECT kode FROM hs_voucher WHERE batch_id = ? AND status IN ('stok','menunggu')",
                                           (bid,))]
    if not sisa:
        raise HTTPException(status_code=400, detail="Tidak ada sisa voucher di cetakan ini.")
    skrip = None
    if lok["jaringan"] and b["status"] == "siap":
        try:
            _hapus_user_hs(sisa)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Gagal menghapus voucher di MikroTik: {e}")
    elif b["status"] == "siap":
        skrip = "/ip hotspot user\n" + "\n".join(f"remove [find name={_kutip_rsc(k)}]" for k in sisa) + "\n"
    with inti.db() as conn:
        conn.execute(f"UPDATE hs_voucher SET status = 'batal' WHERE kode IN ({','.join('?' * len(sisa))})", sisa)
        conn.execute("UPDATE hs_batch SET catatan = ?, status = CASE WHEN status = 'menunggu' THEN 'batal' ELSE status END "
                     "WHERE id = ?", (f"{len(sisa)} sisa ditarik: {alasan[:200]}", bid))
        conn.commit()
    inti.catat_aktivitas(inti._oleh(request), "hotspot", f"B{bid}: {len(sisa)} voucher sisa ditarik ({alasan[:120]})")
    return {"ok": True, "ditarik": len(sisa), "skrip_hapus": skrip}


class HsSetoranIn(BaseModel):
    reseller: str
    metode: str = "tunai"
    tanggal: Optional[str] = None


@router.post("/api/hotspot/setoran")
def hotspot_setoran(d: HsSetoranIn, request: Request):
    """Terima setoran reseller untuk SEMUA voucher terjual yang belum disetor: harga reseller × jumlah → kas masuk."""
    inti._wajib_admin(request)
    if d.metode not in inti._METODE:
        raise HTTPException(status_code=400, detail="Metode tidak valid.")
    tanggal = d.tanggal or datetime.now().strftime("%Y-%m-%d")
    inti._cek_tanggal_lalu(tanggal)
    with inti.db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _siapkan_hotspot(conn)
        inti._wajib_buku_terbuka(conn, tanggal)
        a = conn.execute("SELECT username, COALESCE(lokasi, 1) AS lokasi FROM akun WHERE username = ? AND peran = 'reseller'",
                         (d.reseller,)).fetchone()
        if not a:
            raise HTTPException(status_code=404, detail="Reseller tidak ditemukan.")
        v = conn.execute("SELECT v.kode, b.harga_reseller FROM hs_voucher v JOIN hs_batch b ON b.id = v.batch_id "
                         "WHERE v.reseller = ? AND v.status = 'terjual' AND v.setoran_id IS NULL", (a["username"],)).fetchall()
        if not v:
            raise HTTPException(status_code=400, detail="Tidak ada voucher terjual yang belum disetor.")
        nominal = sum(x["harga_reseller"] for x in v)
        kat = conn.execute("SELECT id FROM kategori_kas WHERE nama = ?", (_KAT_HOTSPOT,)).fetchone()[0]
        sid = conn.execute("INSERT INTO hs_setoran (reseller, lokasi, waktu, jumlah_voucher, nominal, metode, oleh) "
                           "VALUES (?,?,?,?,?,?,?)", (a["username"], a["lokasi"], _sekarang_str(), len(v), nominal,
                                                      d.metode, inti._oleh(request))).lastrowid
        tid = conn.execute(
            "INSERT INTO transaksi (tanggal, jenis, kategori_id, jumlah, metode, keterangan, dicatat_oleh, dicatat_pada, "
            "hotspot_setoran_id, lokasi) VALUES (?, 'masuk', ?, ?, ?, ?, ?, ?, ?, ?)",
            (tanggal, kat, nominal, d.metode, f"Setoran voucher hotspot {a['username']}: {len(v)} voucher",
             inti._oleh(request), _sekarang_str(), sid, a["lokasi"])).lastrowid
        nomor = inti._beri_nomor(conn, tid)
        conn.execute("UPDATE hs_setoran SET transaksi_id = ? WHERE id = ?", (tid, sid))
        conn.execute(f"UPDATE hs_voucher SET setoran_id = ? WHERE kode IN ({','.join('?' * len(v))})",
                     [sid] + [x["kode"] for x in v])
        conn.commit()
    inti.catat_aktivitas(inti._oleh(request), "hotspot", f"{nomor} setoran {a['username']}: {len(v)} voucher {_rp(nominal)} ({d.metode})")
    return {"ok": True, "id": sid, "nomor": nomor, "jumlah_voucher": len(v), "nominal": nominal}


@router.post("/api/hotspot/setoran/{sid}/batal")
def hotspot_setoran_batal(sid: int, d: inti.BatalIn, request: Request):
    """Batalkan setoran (mis. salah catat): kas ikut batal, voucher kembali 'belum disetor'."""
    inti._wajib_owner(request, "Pembatalan setoran hanya oleh owner.")
    alasan = d.alasan.strip()
    if len(alasan) < 3:
        raise HTTPException(status_code=400, detail="Tuliskan alasan pembatalan.")
    with inti.db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _siapkan_hotspot(conn)
        st = conn.execute("SELECT * FROM hs_setoran WHERE id = ? AND status = 'aktif'", (sid,)).fetchone()
        if not st:
            raise HTTPException(status_code=404, detail="Setoran tidak ditemukan.")
        t = conn.execute("SELECT tanggal FROM transaksi WHERE id = ?", (st["transaksi_id"],)).fetchone()
        inti._wajib_buku_terbuka(conn, t["tanggal"])
        conn.execute("UPDATE transaksi SET status='batal', batal_oleh=?, batal_pada=?, batal_alasan=? WHERE id=?",
                     (inti._oleh(request), _sekarang_str(), alasan[:300], st["transaksi_id"]))
        conn.execute("UPDATE hs_setoran SET status = 'batal', batal_alasan = ? WHERE id = ?", (alasan[:300], sid))
        conn.execute("UPDATE hs_voucher SET setoran_id = NULL WHERE setoran_id = ?", (sid,))
        conn.commit()
    inti.catat_aktivitas(inti._oleh(request), "hotspot", f"setoran #{sid} {st['reseller']} {_rp(st['nominal'])} dibatalkan: {alasan[:120]}")
    return {"ok": True}


@router.get("/api/hotspot/skrip-pengaturan")
def hotspot_skrip_pengaturan(request: Request, lokasi: int = 1):
    """Skrip pengaturan hotspot untuk MikroTik lokasi ini: DNS & login lewat QR (http-pap) di server profile, dan
    profil kecepatan semua paket. Server hotspot, pool IP, dan interface-nya dibuat sendiri sekali di MikroTik."""
    inti._wajib_admin(request)
    with inti.db() as conn:
        _siapkan_hotspot(conn)
        lok = _hs_lokasi(conn, lokasi)
        paket = conn.execute("SELECT * FROM hs_paket WHERE lokasi = ? ORDER BY id", (lokasi,)).fetchall()
    baris = [f"# INDESO: pengaturan hotspot {lok['nama']} (RouterOS v6/v7)",
             "# Ganti hsprof1 dengan nama server profile hotspot Anda (lihat /ip hotspot profile print).",
             f"/ip hotspot profile set [find name=\"hsprof1\"] dns-name={_kutip_rsc(lok['dns'])}",
             "# Masuk dengan kode/QR; HP yang sama tersambung lagi otomatis (mac-cookie) selama masa pakai voucher.",
             ':do { /ip hotspot profile set [find name="hsprof1"] login-by=http-chap,http-pap,cookie,mac-cookie } '
             'on-error={ /ip hotspot profile set [find name="hsprof1"] login-by=http-chap,http-pap,cookie }',
             "# Anti berbagi voucher lewat hotspot/tethering HP: paket ke pelanggan hotspot dikirim dengan TTL 1, jadi HP",
             "# pembeli tetap internetan tetapi HP lain yang menumpang ke HP itu tidak bisa (TTL habis saat diteruskan).",
             "# Hanya interface server hotspot ini; pelanggan PPPoE tidak terpengaruh. Aman ditempel ulang.",
             ':if ([:len [/ip firewall mangle find comment="INDESO: anti berbagi hotspot"]] = 0) do={ '
             '/ip firewall mangle add chain=postrouting out-interface=[/ip hotspot get [find profile="hsprof1"] interface] '
             'action=change-ttl new-ttl=set:1 passthrough=yes comment="INDESO: anti berbagi hotspot" }',
             f"# Nama WiFi (SSID) terbuka tanpa sandi: {lok['ssid']}"]
    for p in paket:
        baris += _skrip_profil(p)
    baris += ["# Halaman login INDESO (kode voucher, daftar paket & harga, nomor WA): diunduh ke folder hotspot.",
              "# Jalankan ulang baris ini setiap kali paket/harga/nomor WA diubah.",
              _skrip_unduh_halaman(_url_dasar(request), lokasi)]
    return {"skrip": "\n".join(baris) + "\n", "lokasi": lok}


# --- Halaman login hotspot (disajikan INDESO, diunduh MikroTik ke folder html-directory) -------------------------

_FOLDER_HALAMAN = Path(__file__).resolve().parent.parent / "deploy" / "hotspot"
_HALAMAN_HS = ("login.html", "alogin.html", "status.html", "logout.html", "error.html", "errors.txt")


def _url_dasar(request: Request) -> str:
    """Alamat INDESO seperti yang dilihat dari luar (di VPS lewat Caddy/terowongan: https://app.…)."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _skrip_unduh_halaman(dasar: str, lokasi: int) -> str:
    daftar = ";".join(f'"{f}"' for f in _HALAMAN_HS)
    return ('{:local d [/ip hotspot profile get [find name="hsprof1"] html-directory]; '
            f':foreach f in={{{daftar}}} do={{/tool fetch url=("{dasar}/hotspot/{int(lokasi)}/" . $f) '
            'dst-path=("$d/" . $f)}}')


def _aman(t) -> str:
    """Teks dari database untuk halaman MikroTik: di-escape HTML, dan `$` dijadikan &#36; supaya tidak dibaca
    MikroTik sebagai variabel $(...)."""
    return html.escape(str(t or "")).replace("$", "&#36;")


def _angka(x) -> str:
    return f"{float(x):g}"


def _html_paket(paket) -> str:
    if not paket:
        return ""
    baris = "".join(
        f'<div class="paket"><div><b>{_aman(p["nama"])}</b><small>{_aman(_teks_durasi(p["menit"]))} · '
        f'{_aman(_angka(p["mbps"]))} Mbps · {int(p["perangkat"])} HP</small></div>'
        f'<span class="harga">{_aman(_rp(p["harga"]))}</span></div>' for p in paket)
    return f'<div class="pemisah">Paket voucher</div>{baris}'


def _html_kontak() -> str:
    wa = (inti.CONFIG.get("USAHA_WA") or "").strip()
    link = inti._wa_link(wa)
    if not link:
        return ""
    return f'Butuh bantuan atau ingin jadi agen voucher?<br><a href="{_aman(link)}">WhatsApp {_aman(wa)}</a>'


@router.get("/hotspot/{lokasi}/{berkas}")
def hotspot_halaman(lokasi: int, berkas: str):
    """Halaman login/status/logout hotspot untuk lokasi ini. Publik (dibaca MikroTik lewat /tool fetch); isinya hanya
    nama usaha, nama lokasi, paket & harga, dan nomor WA usaha. Variabel MikroTik $(...) dibiarkan utuh."""
    if berkas not in _HALAMAN_HS:
        raise HTTPException(status_code=404, detail="Halaman tidak ada.")
    with inti.db() as conn:
        _siapkan_hotspot(conn)
        lok = _hs_lokasi(conn, lokasi)
        paket = conn.execute("SELECT * FROM hs_paket WHERE lokasi = ? AND aktif = 1 ORDER BY menit, harga",
                             (lokasi,)).fetchall()
    isi = (_FOLDER_HALAMAN / berkas).read_text(encoding="utf-8")
    ganti = {"{{CSS}}": (_FOLDER_HALAMAN / "gaya.css").read_text(encoding="utf-8"),
             "{{USAHA}}": _aman(inti.CONFIG.get("NAMA_USAHA") or "Internet Desa"),
             "{{LOKASI}}": _aman(lok["nama"]),
             "{{PAKET}}": _html_paket(paket),
             "{{KONTAK}}": _html_kontak()}
    for k, v in ganti.items():
        isi = isi.replace(k, v)
    jenis = "text/plain; charset=utf-8" if berkas.endswith(".txt") else "text/html; charset=utf-8"
    return Response(content=isi, media_type=jenis,
                    headers={"Cache-Control": "no-store"})


def _reseller(request: Request) -> dict:
    with inti.db() as conn:
        r = conn.execute("SELECT username, COALESCE(lokasi, 1) AS lokasi FROM akun WHERE username = ? AND peran = 'reseller'",
                         (request.state.user,)).fetchone()
    if not r:
        raise HTTPException(status_code=403, detail="Khusus akun reseller.")
    return dict(r)


@router.get("/api/reseller/ringkas")
def reseller_ringkas(request: Request):
    me = _reseller(request)
    hari = datetime.now().strftime("%Y-%m-%d")
    with inti.db() as conn:
        _siapkan_hotspot(conn)
        lok = _hs_lokasi(conn, me["lokasi"])
        stok = [dict(r, durasi=_teks_durasi(r["menit"])) for r in conn.execute(
            "SELECT p.id, p.nama, p.menit, p.harga, p.mbps, p.perangkat, COUNT(*) AS stok FROM hs_voucher v JOIN hs_paket p ON p.id = v.paket_id "
            "WHERE v.reseller = ? AND v.status = 'stok' GROUP BY p.id ORDER BY p.menit", (me["username"],))]
        belum = conn.execute("SELECT COUNT(*), COALESCE(SUM(b.harga_reseller), 0), COALESCE(SUM(b.harga), 0) FROM hs_voucher v "
                             "JOIN hs_batch b ON b.id = v.batch_id WHERE v.reseller = ? AND v.status = 'terjual' "
                             "AND v.setoran_id IS NULL", (me["username"],)).fetchone()
        hari_ini, hari_ini_rp, hari_ini_untung = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(b.harga), 0), COALESCE(SUM(b.harga - b.harga_reseller), 0) FROM hs_voucher v "
            "JOIN hs_batch b ON b.id = v.batch_id WHERE v.reseller = ? AND v.status = 'terjual' AND v.terjual_pada >= ?",
            (me["username"], hari)).fetchone()
        # grafik pemasukan 14 hari terakhir (harga jual & untung warung per hari, termasuk hari tanpa penjualan)
        awal = (datetime.now() - timedelta(days=13)).strftime("%Y-%m-%d")
        per_hari = {r[0]: r for r in conn.execute(
            "SELECT substr(v.terjual_pada, 1, 10), COUNT(*), SUM(b.harga), SUM(b.harga - b.harga_reseller) FROM hs_voucher v "
            "JOIN hs_batch b ON b.id = v.batch_id WHERE v.reseller = ? AND v.status = 'terjual' AND v.terjual_pada >= ? "
            "GROUP BY 1", (me["username"], awal))}
        grafik = []
        for i in range(13, -1, -1):
            t = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            r = per_hari.get(t)
            grafik.append({"tanggal": t, "voucher": r[1] if r else 0, "rp": r[2] if r else 0, "untung": r[3] if r else 0})
        st = conn.execute("SELECT waktu, nominal FROM hs_setoran WHERE reseller = ? AND status = 'aktif' ORDER BY id DESC LIMIT 1",
                          (me["username"],)).fetchone()
        riwayat = [dict(r) for r in conn.execute(
            "SELECT v.kode, v.terjual_pada, p.nama AS paket, b.harga, b.menit, (v.setoran_id IS NOT NULL) AS disetor FROM hs_voucher v "
            "JOIN hs_paket p ON p.id = v.paket_id JOIN hs_batch b ON b.id = v.batch_id WHERE v.reseller = ? "
            "AND v.status = 'terjual' ORDER BY v.terjual_pada DESC LIMIT 30", (me["username"],))]
        conn.commit()
    return {"akun": me["username"], "usaha": inti.CONFIG["NAMA_USAHA"], "lokasi": lok["nama"], "ssid": lok["ssid"],
            "stok": stok, "hari_ini": hari_ini, "hari_ini_rp": hari_ini_rp, "hari_ini_untung": hari_ini_untung, "grafik": grafik, "setor_terakhir": dict(st) if st else None, "belum_setor": {"voucher": belum[0], "setor": belum[1], "eceran": belum[2]},
            "riwayat": [dict(r, login=_url_login(lok["dns"], r["kode"]), durasi=_teks_durasi(r["menit"])) for r in riwayat]}


class ResellerJualIn(BaseModel):
    paket_id: int


@router.post("/api/reseller/jual")
def reseller_jual(d: ResellerJualIn, request: Request):
    """Ambil satu voucher dari stok reseller ini dan tandai terjual; QR-nya ditampilkan untuk dipindai pembeli."""
    me = _reseller(request)
    with inti.db() as conn:
        conn.execute("BEGIN IMMEDIATE")      # dua tombol jual bersamaan tidak mendapat voucher yang sama
        _siapkan_hotspot(conn)
        lok = _hs_lokasi(conn, me["lokasi"])
        v = conn.execute("SELECT v.kode, p.nama, b.menit, b.harga FROM hs_voucher v JOIN hs_paket p ON p.id = v.paket_id "
                         "JOIN hs_batch b ON b.id = v.batch_id WHERE v.reseller = ? AND v.paket_id = ? AND v.status = 'stok' "
                         "ORDER BY v.batch_id, v.kode LIMIT 1", (me["username"], d.paket_id)).fetchone()
        if not v:
            raise HTTPException(status_code=409, detail="Stok voucher paket ini habis. Minta admin menitipkan lagi.")
        conn.execute("UPDATE hs_voucher SET status = 'terjual', terjual_pada = ? WHERE kode = ?", (_sekarang_str(), v["kode"]))
        conn.commit()
    inti.catat_aktivitas(me["username"], "hotspot_jual", f"1 voucher {v['nama']} terjual")
    return {"kode": v["kode"], "paket": v["nama"], "durasi": _teks_durasi(v["menit"]), "harga": v["harga"],
            "ssid": lok["ssid"], "login": _url_login(lok["dns"], v["kode"])}
