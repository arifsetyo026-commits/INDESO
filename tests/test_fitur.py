"""Tes menyeluruh semua fitur dengan MikroTik, OLT, dan Telegram tiruan.

Jalankan dari folder project:
    pip install pytest httpx
    python -m pytest -q tests/

Setiap tes memakai folder sementara (database, backup, nota) sendiri, jadi data
asli di billing.db / isp_history.db tidak tersentuh.
"""

import importlib
import io
import os
import sqlite3
import sys
import zipfile
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ------------------------------------------------------------------
# Perangkat tiruan
# ------------------------------------------------------------------

class FakeRes:
    def __init__(self, mt, path):
        self.mt, self.path = mt, path

    def get(self, **kw):
        if self.path == "/ppp/secret":
            rows = [dict(v, name=k) for k, v in self.mt.secret.items()]
        elif self.path == "/ppp/active":
            rows = [dict(v, name=k) for k, vs in self.mt.active.items() for v in vs]
        elif self.path == "/ppp/profile":
            rows = [dict(getattr(self.mt, "profil_detail", {}).get(p, {}), name=p) for p in self.mt.profiles]
        elif self.path == "/ip/dns":
            rows = [dict(getattr(self.mt, "dns", {}))]
        elif self.path == "/ip/firewall/mangle":
            rows = list(getattr(self.mt, "mangle", []))
        elif self.path == "/interface/pppoe-server/server":
            rows = list(getattr(self.mt, "pppoe_server", []))
        elif self.path == "/log":
            rows = list(self.mt.log)
        elif self.path == "/system/resource":
            rows = [dict(self.mt.resource)]
        else:
            rows = []
        if "name" in kw:
            rows = [r for r in rows if r.get("name") == kw["name"]]
        return rows

    def add(self, **kw):
        if self.path == "/ppp/secret":
            self.mt.secret[kw["name"]] = dict({k: v for k, v in kw.items() if k != "name"}, id=f"*{len(self.mt.secret) + 10}",
                                              disabled="no")

    def set(self, id, **kw):
        for v in self.mt.secret.values():
            if v["id"] == id:
                v.update(kw)

    def remove(self, id):
        for k in list(self.mt.active):
            self.mt.active[k] = [x for x in self.mt.active[k] if x["id"] != id]

    def call(self, perintah, arg=None, *a, **k):
        if perintah == "monitor-traffic":
            return [dict(self.mt.traffic)] if getattr(self.mt, "traffic", None) else []
        if perintah == "ping":
            return list(getattr(self.mt, "ping", []))
        if perintah == "resolve":
            if not getattr(self.mt, "resolve", None):
                raise Exception("failure: dns server failure")
            balas = type("Balas", (list,), {})()              # seperti routeros_api: ret ada di done_message
            balas.done_message = {"ret": self.mt.resolve}
            return balas
        return []


class FakeMikroTik:
    def __init__(self):
        self.mati = False
        self.secret = {
            "budi": {"id": "*1", "profile": "paket-10mbps", "disabled": "no"},
            "ani": {"id": "*2", "profile": "paket-20mbps", "disabled": "no"},
            "server": {"id": "*3", "profile": "default", "disabled": "no"},
            "dwi": {"id": "*4", "profile": "paket-10mbps", "disabled": "no"},
        }
        self.active = {"budi": [{"id": "*A1", "address": "10.0.0.2", "uptime": "1h", "caller-id": "AA:BB:CC:00:00:01"}]}
        self.profiles = ["default", "paket-10mbps", "paket-20mbps", "ISOLIR"]
        self.log = []
        self.resource = {"cpu-load": "10", "free-memory": "80000000", "total-memory": "100000000",
                         "uptime": "3d", "version": "7.14", "board-name": "RB4011"}

    def pool(self):
        mt = self

        class Pool:
            def get_api(self_):
                if mt.mati:
                    raise ConnectionError("timeout")

                class Api:
                    def get_resource(self__, path):
                        return FakeRes(mt, path)
                return Api()

            def disconnect(self_):
                pass
        return Pool()


class FakeResp:
    def __init__(self, ok=True, data=None):
        self.ok = ok
        self._d = data if data is not None else {"ok": ok, "result": []}
        self.text = str(self._d)
        self.status_code = 200 if ok else 400

    def json(self):
        return self._d


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INDESO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MIKROTIK_PASSWORD", "x")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111111")
    for k in ("DASHBOARD_PASSWORD", "DASHBOARD_USERS", "MITRA_USERS", "TELEGRAM_OWNER_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    mt = FakeMikroTik()
    main._mikrotik_pool = mt.pool
    main.fetch_ppp_secrets = lambda: [{"name": k, "profile": v["profile"]} for k, v in mt.secret.items()]
    terkirim = []

    def fake_post(url, data=None, files=None, timeout=None, **kw):
        terkirim.append({"url": url.rsplit("/", 1)[-1], "data": data or {}, "files": files})
        return FakeResp(True)
    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(main.requests, "get", lambda *a, **k: FakeResp(True, {"ok": True, "result": []}))
    for f in (main.init_history_db, main.init_event_db, main.init_billing_db, main.init_akun_db,
              main.init_keuangan_db, main.init_nota_db, main.init_tiket_db, main._muat_pengaturan):
        f()
    main._mt = mt
    main._terkirim = terkirim
    return main


def klien(main, user=None, pw=None):
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    if user:
        r = c.post("/api/login", json={"username": user, "password": pw})
        assert r.status_code == 200, r.text
    return c


@pytest.fixture()
def siap(app):
    """Owner + admin + mitra, paket, pelanggan hasil impor."""
    main = app
    o = klien(main)
    assert o.post("/api/setup", json={"username": "owner", "password": "rahasia123"}).json()["peran"] == "owner"
    assert o.post("/api/akun", json={"username": "kasir", "password": "kasirpass1", "peran": "admin"}).status_code == 200
    assert o.post("/api/akun", json={"username": "joko", "password": "mitrapass1", "peran": "mitra"}).status_code == 200
    assert o.post("/api/paket", json={"nama": "10M", "kecepatan_mbps": 10, "harga": 150000}).status_code == 200
    assert o.post("/api/paket", json={"nama": "20M", "kecepatan_mbps": 20, "harga": 200000}).status_code == 200
    imp = o.post("/api/impor-pelanggan").json()
    assert imp["baru"] == 4
    o.put("/api/data-pelanggan/server", json={"nama": "Server", "paket_id": 1, "gratis": True, "status": "aktif",
                                              "tampil_mitra": False, "atur_secret": False})
    return main, o, klien(main, "kasir", "kasirpass1"), klien(main, "joko", "mitrapass1")


# ------------------------------------------------------------------
# Login, akun, keamanan
# ------------------------------------------------------------------

def test_halaman_dan_login(app):
    main = app
    c = klien(main)
    c.follow_redirects = False
    assert c.get("/").status_code == 303 and c.get("/").headers["location"] == "/login"
    assert c.get("/login").status_code == 200
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/setup-status").json()["perlu_setup"] is True
    assert c.post("/api/setup", json={"username": "owner", "password": "pendek"}).status_code == 400
    r = c.post("/api/setup", json={"username": "owner", "password": "rahasia123"})
    assert r.status_code == 200
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=strict" in r.headers["set-cookie"]
    assert klien(main).post("/api/setup", json={"username": "penyusup", "password": "rahasia123"}).status_code == 409
    assert c.get("/api/me").json() == {"username": "owner", "peran": "owner"}
    h = c.get("/api/me").headers
    assert h["x-frame-options"] == "DENY" and "default-src 'self'" in h["content-security-policy"]
    assert c.post("/api/logout").status_code == 200 and c.get("/api/me").status_code == 401


def test_brute_force_dan_csrf(siap):
    main, o, k, m = siap
    x = klien(main)
    kode = [x.post("/api/login", json={"username": "kasir", "password": "salah"}).status_code for _ in range(6)]
    assert kode[:5] == [401] * 5 and kode[5] == 429
    main._gagal_login.clear()
    r = o.post("/api/paket", json={"nama": "X", "kecepatan_mbps": 1, "harga": 1}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_peran_mitra_dibatasi(siap):
    main, o, k, m = siap
    for p in ("/api/status", "/api/tagihan?periode=2026-10", "/api/data-pelanggan", "/api/aktivitas",
              "/api/keuangan/ringkasan?periode=2026-10", "/api/akun", "/api/pengaturan", "/api/event"):
        assert m.get(p).status_code == 403, p
    assert m.post("/api/isolir/budi").status_code == 403
    assert "Mitra" in m.get("/").text[:400]
    assert m.get("/api/mitra/pelanggan?periode=2026-10").status_code == 200


def test_kelola_akun_dan_owner(siap):
    main, o, k, m = siap
    assert k.put("/api/akun/kasir", json={"peran": "owner"}).status_code == 403
    assert k.post("/api/akun/owner/password", json={"baru": "xxxxxxxx1"}).status_code == 403
    assert o.put("/api/akun/owner", json={"aktif": False}).status_code == 400
    assert o.delete("/api/akun/owner").status_code == 400
    assert o.put("/api/akun/kasir", json={"aktif": False}).status_code == 200
    assert k.get("/api/me").status_code == 401                                   # sesi langsung dicabut
    o.put("/api/akun/kasir", json={"aktif": True})
    k2 = klien(main, "kasir", "kasirpass1")
    assert k2.post("/api/akun/saya/password", json={"lama": "salah", "baru": "abcdefgh1"}).status_code == 400
    assert k2.post("/api/akun/saya/password", json={"lama": "kasirpass1", "baru": "abcdefgh1"}).status_code == 200
    # sesi kedaluwarsa
    c = sqlite3.connect("billing.db")
    c.execute("UPDATE sesi SET terakhir_aktif=?", ((datetime.now() - timedelta(hours=13)).isoformat(timespec="seconds"),))
    c.commit()
    assert o.get("/api/me").status_code == 401


def test_migrasi_akun_dari_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k, v in {"INDESO_DATA_DIR": str(tmp_path), "MIKROTIK_PASSWORD": "x", "TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "1",
                 "DASHBOARD_USERS": "pemilik:lama12345", "MITRA_USERS": "joko:mitra12345"}.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    main.init_billing_db()
    main.init_akun_db()
    c = klien(main, "pemilik", "lama12345")
    assert {a["username"]: a["peran"] for a in c.get("/api/akun").json()["akun"]} == {"pemilik": "admin", "joko": "mitra"}


# ------------------------------------------------------------------
# Pelanggan, paket, billing, isolir
# ------------------------------------------------------------------

def test_paket_dan_pelanggan(siap):
    main, o, k, m = siap
    pk = {p["nama"]: p for p in o.get("/api/paket").json()["paket"]}
    assert pk["10M"]["jumlah_pelanggan"] == 3          # budi, dwi, server (paket-10mbps / diisi manual)
    assert o.delete(f"/api/paket/{pk['10M']['id']}").status_code == 409
    assert o.put(f"/api/paket/{pk['20M']['id']}", json={"nama": "20M", "kecepatan_mbps": 20, "harga": 250000}).status_code == 200
    data = {p["username"]: p for p in o.get("/api/data-pelanggan").json()["pelanggan"]}
    assert data["server"]["gratis"] == 1 and data["server"]["tampil_mitra"] == 0
    main._mt.secret.pop("dwi")
    hilang = o.post("/api/impor-pelanggan").json()["tidak_ada_di_mikrotik"]
    assert [h["username"] for h in hilang] == ["dwi"]


def test_berhenti_nonaktifkan_secret(siap):
    main, o, k, m = siap
    mt = main._mt
    r = o.put("/api/data-pelanggan/budi", json={"nama": "Budi", "paket_id": 1, "status": "berhenti"}).json()
    assert r["secret"]["ok"] and mt.secret["budi"]["disabled"] == "yes" and mt.active["budi"] == []
    r = o.put("/api/data-pelanggan/budi", json={"nama": "Budi", "paket_id": 1, "status": "aktif"}).json()
    assert r["secret"]["ok"] and mt.secret["budi"]["disabled"] == "no"
    mt.mati = True
    r = o.put("/api/data-pelanggan/ani", json={"nama": "Ani", "paket_id": 2, "status": "berhenti"}).json()
    assert r["secret"]["tertunda"]
    main.proses_sinkron_secret()
    assert mt.secret["ani"]["disabled"] == "no"
    mt.mati = False
    main.proses_sinkron_secret()
    assert mt.secret["ani"]["disabled"] == "yes"


def test_tagihan_isolir(siap):
    main, o, k, m = siap
    hasil = o.post("/api/tagihan/buat?periode=2026-10").json()
    assert hasil["baru"] == 3                              # server gratis tidak ditagih
    t = o.get("/api/tagihan?periode=2026-10").json()
    assert {x["username"] for x in t["tagihan"]} == {"budi", "ani", "dwi"}
    assert [x["username"] for x in t["tanpa_tagihan"]] == ["server"]
    # isolir otomatis (lewat jatuh tempo 5 Okt)
    main.proses_isolir(datetime(2026, 10, 11, 9))
    iso = {x["username"] for x in o.get("/api/isolir").json()["terisolir"]}
    assert iso == {"budi", "ani", "dwi"}
    assert main._mt.secret["budi"]["profile"] == "ISOLIR"
    # lunas -> buka otomatis
    tid = [x["id"] for x in t["tagihan"] if x["username"] == "budi"][0]
    assert o.post(f"/api/tagihan/{tid}/lunas").status_code == 200
    main.proses_isolir(datetime(2026, 10, 11, 10))
    assert main._mt.secret["budi"]["profile"] == "paket-10mbps"
    # isolir manual pelanggan gratis tidak dibuka otomatis
    assert o.post("/api/isolir/server").status_code == 200
    main.proses_isolir(datetime(2026, 10, 11, 11))
    assert "server" in {x["username"] for x in o.get("/api/isolir").json()["terisolir"]}
    assert o.post("/api/isolir/server/buka").status_code == 200


def test_tagihan_otomatis_akhir_bulan(siap):
    main, o, k, m = siap
    main.proses_tagihan_harian(datetime(2026, 10, 31, 9))
    assert len(o.get("/api/tagihan?periode=2026-11").json()["tagihan"]) == 3
    assert any("Tagihan November 2026" in (x["data"].get("text") or "") for x in main._terkirim)


# ------------------------------------------------------------------
# Mitra & insentif
# ------------------------------------------------------------------

def test_mitra_dan_insentif(siap):
    main, o, k, m = siap
    o.post("/api/tagihan/buat?periode=2026-10")
    for t in o.get("/api/tagihan?periode=2026-10").json()["tagihan"]:
        if t["username"] in ("budi", "ani"):
            o.post(f"/api/tagihan/{t['id']}/lunas")
    d = m.get("/api/mitra/pelanggan?periode=2026-10").json()
    assert "server" not in {p["username"] for p in d["pelanggan"]}
    assert d["insentif"]["total"] == 20000 and d["insentif"]["sisa"] == 20000
    assert m.put("/api/mitra/pelanggan/budi", json={"nama": "Budi S", "no_hp": "0812"}).status_code == 200
    assert m.put("/api/mitra/pelanggan/server", json={"nama": "x"}).status_code == 404
    # gratis yang tetap dihitung insentif
    o.put("/api/data-pelanggan/server", json={"nama": "Server", "paket_id": 1, "gratis": True, "status": "aktif",
                                              "insentif_gratis": True, "atur_secret": False})
    d = m.get("/api/mitra/pelanggan?periode=2026-10").json()
    assert d["insentif"]["total"] == 30000
    # di HP mitra tampil seperti pelanggan biasa: lunas tgl 1, tanpa tanda gratis, rumus 3 lunas + 0 gratis
    sv = next(p for p in d["pelanggan"] if p["username"] == "server")
    assert sv["status_bayar"] == "lunas" and sv["tgl_bayar"] == "2026-10-01" and sv["gratis"] == 0
    assert "insentif_gratis" not in sv
    assert d["insentif"]["jumlah_lunas"] == 3 and d["insentif"]["jumlah_gratis"] == 0
    depan = m.get("/api/mitra/pelanggan?periode=2999-01").json()
    assert next(p for p in depan["pelanggan"] if p["username"] == "server")["status_bayar"] is None
    # bayar insentif, terikat ke periode walau dibayar bulan berikutnya
    hari = datetime.now().strftime("%Y-%m-%d")
    assert o.post("/api/keuangan/insentif/2026-10/bayar", json={"tanggal": hari, "jumlah": 99999999}).status_code == 400
    r = k.post("/api/keuangan/insentif/2026-10/bayar", json={"tanggal": hari, "jumlah": 10000, "metode": "transfer"}).json()
    assert r["sisa"] == 20000
    rek = {b["periode"]: b for b in o.get("/api/keuangan/insentif?sampai=2026-10").json()["rekap"]}
    assert rek["2026-10"]["status"] == "sebagian"
    assert m.get("/api/mitra/pelanggan?periode=2026-10").json()["insentif"]["pembayaran"][0]["jumlah"] == 10000


# ------------------------------------------------------------------
# Keuangan
# ------------------------------------------------------------------

def _kat(o):
    return {x["nama"]: x["id"] for x in o.get("/api/keuangan/kategori").json()["kategori"]}


def test_keuangan_lengkap(siap):
    main, o, k, m = siap
    kat = _kat(o)
    hari = datetime.now().strftime("%Y-%m-%d")
    per = hari[:7]
    o.post(f"/api/tagihan/buat?periode={per}")
    t = o.get(f"/api/tagihan?periode={per}").json()["tagihan"]
    o.post(f"/api/tagihan/{t[0]['id']}/lunas")
    lunas = t[0]["jumlah"]
    trx = lambda c, jenis, nama, jml: c.post("/api/keuangan/transaksi", json={
        "tanggal": hari, "jenis": jenis, "kategori_id": kat[nama], "jumlah": jml}).json()
    trx(o, "masuk", "Setoran modal", 1_000_000)
    t1 = trx(k, "keluar", "Beban listrik", 200_000)["id"]
    trx(o, "masuk", "Pendapatan jasa pemasangan", 100_000)
    assert k.post("/api/keuangan/transaksi", json={"tanggal": "2099-01-01", "jenis": "keluar",
                                                    "kategori_id": kat["Beban listrik"], "jumlah": 1}).status_code == 400
    assert k.post("/api/keuangan/transaksi", json={"tanggal": hari, "jenis": "keluar",
                                                    "kategori_id": kat["Beban komisi mitra"], "jumlah": 1}).status_code == 400
    # utang kredit
    u = k.post("/api/keuangan/utang", json={"tanggal": hari, "pemasok": "Toko A", "jumlah": 600_000, "uang_muka": 100_000,
                                            "kategori_id": kat["Beban bahan habis pakai (konektor, pigtail, isolasi)"]}).json()["id"]
    assert k.post(f"/api/keuangan/utang/{u}/bayar", json={"tanggal": hari, "jumlah": 600_000}).status_code == 400
    assert k.post(f"/api/keuangan/utang/{u}/bayar", json={"tanggal": hari, "jumlah": 200_000}).json()["sisa"] == 300_000
    r = o.get(f"/api/keuangan/ringkasan?periode={per}").json()
    assert r["pendapatan"] == lunas + 100_000
    assert r["beban"] == 200_000 + 600_000 and r["beban_kredit"] == 600_000
    assert r["laba"] == lunas + 100_000 - 800_000
    assert r["saldo_akhir"] == lunas + 1_000_000 + 100_000 - 200_000 - 100_000 - 200_000
    assert r["utang"]["sisa"] == 300_000
    # admin tidak boleh mengubah catatan owner; batal wajib alasan
    milik_owner = [x for x in o.get(f"/api/keuangan/transaksi?periode={per}").json()["transaksi"]
                   if x["dicatat_oleh"] == "owner"][0]["id"]
    assert k.post(f"/api/keuangan/transaksi/{milik_owner}/batal", json={"alasan": "coba"}).status_code == 403
    assert o.post(f"/api/keuangan/transaksi/{t1}/batal", json={"alasan": ""}).status_code == 400
    assert o.post(f"/api/keuangan/transaksi/{t1}/batal", json={"alasan": "salah input"}).status_code == 200
    assert o.get(f"/api/keuangan/ringkasan?periode={per}").json()["beban"] == 600_000
    # nota
    jpg = b"\xff\xd8\xff\xe0" + b"0" * 500
    hdr = {"Content-Type": "image/jpeg", "X-Nama-Berkas": "nota.jpg"}
    nid = k.post(f"/api/keuangan/utang/{u}/nota", content=jpg, headers=hdr).json()["id"]
    assert k.post(f"/api/keuangan/utang/{u}/nota", content=b"<html>", headers=hdr).status_code == 415
    assert m.get(f"/api/keuangan/nota/{nid}").status_code == 403
    assert o.get(f"/api/keuangan/nota/{nid}").content == jpg
    # ekspor CSV + formula injection
    tx = o.post("/api/keuangan/transaksi", json={"tanggal": hari, "jenis": "keluar", "kategori_id": kat["Beban listrik"],
                                                 "jumlah": 5000, "keterangan": "=HYPERLINK(1)"})
    assert tx.status_code == 200
    csv = o.get(f"/api/keuangan/ekspor?periode={per}").text
    assert "'=HYPERLINK(1)" in csv and "kredit (liabilitas)" in csv


def test_laporan_bulanan_dan_backup(siap):
    main, o, k, m = siap
    main.CONFIG["TELEGRAM_OWNER_CHAT_ID"] = "222222,-1003333333"
    teks = main.susun_laporan_bulanan("2026-10")
    assert "Laporan Keuangan Oktober 2026" in teks and len(teks) < 4096
    main._terkirim.clear()
    main.proses_laporan_bulanan(datetime(2026, 11, 1, 9))
    tujuan = [x["data"].get("chat_id") for x in main._terkirim]
    assert tujuan.count("222222") == 2 and tujuan.count("-1003333333") == 2     # teks + CSV ke tiap chat
    main._terkirim.clear()
    main.proses_laporan_bulanan(datetime(2026, 11, 2, 9))
    assert main._terkirim == []                                                 # tidak dobel
    assert k.put("/api/pengaturan", json={"nilai": {"TELEGRAM_OWNER_CHAT_ID": "999999"}}).status_code == 403
    # backup database + nota ZIP ke tujuan owner
    kat = _kat(o)
    tid = o.post("/api/keuangan/transaksi", json={"tanggal": datetime.now().strftime("%Y-%m-%d"), "jenis": "keluar",
                                                  "kategori_id": kat["Beban listrik"], "jumlah": 150000}).json()["id"]
    o.post(f"/api/keuangan/transaksi/{tid}/nota", content=b"%PDF-1.4 x", headers={"Content-Type": "application/pdf"})
    main._terkirim.clear()
    zip_isi = []
    asli = main.kirim_dokumen_telegram

    def tangkap(path, caption, chat_id=None, **kw):
        if path.endswith(".zip"):
            zip_isi.append(zipfile.ZipFile(io.BytesIO(open(path, "rb").read())).namelist())
        return asli(path, caption, chat_id, **kw)
    main.kirim_dokumen_telegram = tangkap
    minggu = datetime(2026, 10, 11, 3)
    assert minggu.weekday() == main.CONFIG["BACKUP_HARI_KIRIM"]
    main.proses_backup_harian(minggu)
    assert {x["data"]["chat_id"] for x in main._terkirim} == {"222222", "-1003333333"}
    assert zip_isi and "daftar-nota.csv" in zip_isi[0]
    assert main._hitung_nota_belum_backup() == 0


# ------------------------------------------------------------------
# Monitoring, Alert & Event, Telegram
# ------------------------------------------------------------------

def test_alert_dan_event(siap):
    main, o, k, m = siap
    sekarang = datetime.now()
    assert main._waktu_log_ros("oct/10 22:00:00", sekarang) is not None
    assert main._klasifikasi_log_ros("system,error,critical", "login failure for user admin from 1.2.3.4 via winbox")[0] == "kritis"
    main._mt.log = [{"time": sekarang.strftime("%H:%M:%S"), "topics": "interface,info", "message": "ether2 link down"}]

    def onu(i, online=True, status="OK", user=None):
        return main.ONUStatus(pon="0/0/1", onu_id=i, snmp_index=i, olt_rx_dbm=-20.0 if online else None,
                              online=online, status=status, pppoe_username=user)
    main.proses_event([onu(1, user="budi"), onu(2)], None)
    main.proses_event([onu(1, online=False), onu(2, status="CRITICAL")], None)
    main.proses_event([], "[ConnectionError] timeout")
    ev = o.get("/api/event?jam=24").json()
    judul = {e["judul"] for e in ev["event"]}
    assert {"Interface link down", "ONU offline", "Redaman kritis", "OLT tidak merespons SNMP",
            "API MikroTik tidak terhubung"} <= judul
    main._latest_data = main._build_dashboard([], "[x] down", [onu(2, status="CRITICAL")])
    al = o.get("/api/alert").json()
    assert al["ringkasan"]["kritis"] >= 2
    assert o.get("/api/status").status_code == 200
    assert o.get("/api/kesehatan-jaringan").status_code == 200
    assert o.get("/api/riwayat-putus/budi").status_code == 200
    assert o.get("/api/boncos").status_code == 200 and o.get("/api/putus").status_code == 200


def test_perintah_telegram(siap):
    main, o, k, m = siap
    main._latest_data = main._build_dashboard([], None, [])
    main.CONFIG["TELEGRAM_OWNER_CHAT_ID"] = "222222"
    for cmd in ("/help", "/masalah", "/boncos", "/putus", "/cek budi", "/cek", "/tidakada", "/cek@NamaBot budi"):
        main._terkirim.clear()
        main._handle_telegram_command(cmd)
        assert main._terkirim, cmd
    main._terkirim.clear()
    assert main._handle_perintah_owner("222222", "/laporan 2026-10") is True
    assert any(x["data"].get("chat_id") == "222222" for x in main._terkirim)
    assert main._handle_perintah_owner("222222", "/cek budi") is False


def test_siklus_otomasi_tanpa_error(siap, caplog):
    """Semua proses di loop pemantauan berjalan tanpa exception, baik MikroTik hidup maupun mati."""
    main, o, k, m = siap
    caplog.set_level("ERROR")
    for mati in (False, True):
        main._mt.mati = mati
        users, err = main.fetch_pppoe_active()
        assert (err is None) == (not mati)
        main.check_and_send_onu_alerts([])
        main.proses_event([], err)
        main.proses_putus_pelanggan(users, err, [])
        main.proses_sinkron_secret()
        main.proses_tagihan_harian(datetime(2026, 10, 31, 9))
        main.proses_isolir(datetime(2026, 10, 11, 9))
        main.proses_laporan_bulanan(datetime(2026, 11, 1, 9))
        main.proses_pengingat_utang(datetime(2026, 10, 11, 9))
        main.proses_backup_harian(datetime(2026, 10, 11, 3))
        main.log_history_snapshot(main._build_dashboard(users, err, []))
        main._build_daily_report_text()
    tak_terduga = [r for r in caplog.records if "Traceback" in r.getMessage()]
    assert not tak_terduga


def test_pengaturan(siap):
    main, o, k, m = siap
    assert o.put("/api/pengaturan", json={"nilai": {"JATUH_TEMPO_TANGGAL": 31}}).status_code == 400
    assert o.put("/api/pengaturan", json={"nilai": {"MIKROTIK_PASSWORD": "x"}}).status_code == 400
    assert k.put("/api/pengaturan", json={"nilai": {"ISOLIR_MODE": "simulasi"}}).status_code == 200
    main.CONFIG["ISOLIR_MODE"] = "aktif"
    main._muat_pengaturan()
    assert main.CONFIG["ISOLIR_MODE"] == "simulasi"
    s = o.get("/api/pengaturan").json()["sistem"]
    assert "backup_ke" in s and "nota_mb" in s
    assert o.post("/api/backup").json()["integritas"] == "ok"


def test_token_tidak_bocor_ke_log(app, caplog):
    main = app
    main.CONFIG["TELEGRAM_BOT_TOKEN"] = "123456789:AAH-rahasiaSekaliTokenBot_abcdefghij"
    caplog.set_level("ERROR")
    main.logger.error("Gagal: Max retries exceeded with url: /bot123456789:AAH-rahasiaSekaliTokenBot_abcdefghij/sendMessage")
    teks = " ".join(r.getMessage() for r in caplog.records)
    assert "rahasiaSekali" not in teks and "bot***" in teks


def test_laporan_harian_tidak_hilang_saat_telegram_gagal(app, monkeypatch):
    import asyncio
    main = app
    monkeypatch.setattr(main, "kirim_telegram_ke", lambda cid, t: False)
    main.CONFIG["DAILY_REPORT_HOUR"], main.CONFIG["DAILY_REPORT_MINUTE"] = 0, 0

    async def satu_putaran():
        tugas = asyncio.ensure_future(main.daily_report_loop())
        await asyncio.sleep(0.5)
        tugas.cancel()
    asyncio.run(satu_putaran())
    assert not main._laporan_sudah_terkirim(datetime.now().strftime("%Y-%m-%d"))


def test_utang_cicilan_bulanan(siap):
    """PayLater: harga 1.200.000, uang muka 0, 3x cicilan 420.000 (bunga 60.000)."""
    main, o, k, m = siap
    kat = _kat(o)
    hari = datetime.now()
    tgl = hari.strftime("%Y-%m-%d")
    per = tgl[:7]
    pertama = (hari - timedelta(days=40)).strftime("%Y-%m-%d")      # cicilan pertama sudah lewat
    body = {"tanggal": (hari - timedelta(days=45)).strftime("%Y-%m-%d"), "pemasok": "Shopee PayLater",
            "kategori_id": kat["Beban bahan habis pakai (konektor, pigtail, isolasi)"], "jumlah": 1_200_000,
            "jatuh_tempo": pertama, "tenor": 3, "cicilan": 420_000}
    assert k.post("/api/keuangan/utang", json={**body, "cicilan": 100_000}).status_code == 400   # kurang dari harga
    assert k.post("/api/keuangan/utang", json={**body, "tenor": 1}).status_code == 400
    r = k.post("/api/keuangan/utang", json=body).json()
    assert r["total"] == 1_260_000
    u = [x for x in o.get("/api/keuangan/utang?status=semua").json()["utang"] if x["id"] == r["id"]][0]
    assert [c["jatuh_tempo"] for c in u["jadwal"]] == [pertama, main._tambah_bulan(pertama, 1), main._tambah_bulan(pertama, 2)]
    assert u["bunga"] == 60_000 and u["cicilan_ke"] == 1 and u["tagihan_berikut"] == 420_000
    assert u["lewat_tempo"] and u["tertunggak"] >= 420_000
    # bayar cicilan pertama -> berikutnya ke-2
    k.post(f"/api/keuangan/utang/{r['id']}/bayar", json={"tanggal": tgl, "jumlah": 420_000})
    u = [x for x in o.get("/api/keuangan/utang?status=semua").json()["utang"] if x["id"] == r["id"]][0]
    assert u["cicilan_ke"] == 2 and u["cicilan_lunas"] == 1 and u["sisa"] == 840_000
    # laba: pokok di kategori perangkat + bunga di kategori bunga, pada bulan pembelian
    per_beli = body["tanggal"][:7]
    rk = {x["nama"]: x["total"] for x in o.get(f"/api/keuangan/ringkasan?periode={per_beli}").json()["per_kategori"]}
    assert rk["Beban bahan habis pakai (konektor, pigtail, isolasi)"] == 1_200_000
    # bunga 60.000 diakui 20.000 per cicilan, di bulan jatuh tempo masing-masing (akrual)
    jadwal_bln = [c["jatuh_tempo"][:7] for c in u["jadwal"]]
    for p in sorted(set(jadwal_bln)):
        rkp = {x["nama"]: x["total"] for x in o.get(f"/api/keuangan/ringkasan?periode={p}").json()["per_kategori"]}
        assert rkp.get("Beban bunga & biaya angsuran", 0) == 20_000 * jadwal_bln.count(p)
    if per_beli not in jadwal_bln:
        assert "Beban bunga & biaya angsuran" not in rk
    # kas hanya berkurang sebesar cicilan yang dibayar
    rs = o.get(f"/api/keuangan/ringkasan?periode={per}").json()
    assert rs["bayar_utang"] == 420_000
    # tanggal akhir bulan
    assert main._tambah_bulan("2026-01-31", 1) == "2026-02-28" and main._tambah_bulan("2026-12-15", 1) == "2027-01-15"


def test_hapus_transaksi_dan_utang(siap):
    main, o, k, m = siap
    kat = _kat(o)
    tgl = datetime.now().strftime("%Y-%m-%d")
    per = tgl[:7]
    kid = kat["Beban bahan habis pakai (konektor, pigtail, isolasi)"]
    tid = k.post("/api/keuangan/transaksi", json={"tanggal": tgl, "jenis": "keluar", "kategori_id": kid,
                                                  "jumlah": 50_000, "metode": "tunai"}).json()["id"]
    # hapus permanen hanya untuk transaksi yang sudah dihapus, dan hanya owner
    assert o.delete(f"/api/keuangan/transaksi/{tid}").status_code == 400
    assert k.post(f"/api/keuangan/transaksi/{tid}/batal", json={"alasan": "salah input"}).status_code == 200
    ids = [t["id"] for t in o.get(f"/api/keuangan/transaksi?periode={per}").json()["transaksi"]]
    assert tid not in ids                                           # tidak tampil di daftar biasa
    d = o.get(f"/api/keuangan/transaksi?periode={per}&batal=true").json()
    assert tid in [t["id"] for t in d["transaksi"]] and d["ringkasan"]["dibatalkan"] == 1
    assert k.delete(f"/api/keuangan/transaksi/{tid}").status_code == 403
    assert o.delete(f"/api/keuangan/transaksi/{tid}").status_code == 200
    assert tid not in [t["id"] for t in o.get(f"/api/keuangan/transaksi?periode={per}&batal=true").json()["transaksi"]]

    # utang yang sudah ada pembayarannya tetap bisa dihapus, pembayarannya ikut terhapus
    uid = k.post("/api/keuangan/utang", json={"tanggal": tgl, "pemasok": "Toko", "kategori_id": kid,
                                              "jumlah": 300_000, "uang_muka": 100_000}).json()["id"]
    k.post(f"/api/keuangan/utang/{uid}/bayar", json={"tanggal": tgl, "jumlah": 50_000})
    assert o.get(f"/api/keuangan/ringkasan?periode={per}").json()["bayar_utang"] > 0
    r = k.post(f"/api/keuangan/utang/{uid}/batal", json={"alasan": "salah input"}).json()
    assert r["pembayaran_ikut_dihapus"] >= 1
    rs = o.get(f"/api/keuangan/ringkasan?periode={per}").json()
    assert rs["bayar_utang"] == 0 and rs["beban"] == 0 and rs["arus_kas"] == 0
    assert uid not in [u["id"] for u in o.get("/api/keuangan/utang?status=semua").json()["utang"]]
    assert uid in [u["id"] for u in o.get("/api/keuangan/utang?status=dihapus").json()["utang"]]
    assert k.delete(f"/api/keuangan/utang/{uid}").status_code == 403
    assert o.delete(f"/api/keuangan/utang/{uid}").status_code == 200
    assert uid not in [u["id"] for u in o.get("/api/keuangan/utang?status=dihapus").json()["utang"]]


def test_tiket_gangguan(siap):
    main, o, k, m = siap
    main._terkirim.clear()
    # mitra melapor untuk pelanggannya; server (tidak tampil ke mitra) ditolak
    assert m.post("/api/mitra/tiket", json={"username": "server", "kategori": "Internet mati"}).status_code == 404
    assert m.post("/api/mitra/tiket", json={"username": "budi", "kategori": "Ngawur"}).status_code == 400
    r = m.post("/api/mitra/tiket", json={"username": "budi", "kategori": "Internet mati", "keterangan": "LOS merah"})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    assert any("Tiket baru" in x["data"].get("text", "") for x in main._terkirim)
    # satu pelanggan satu tiket terbuka
    assert k.post("/api/tiket", json={"username": "budi", "kategori": "Internet lambat"}).status_code == 409
    # mitra tidak bisa memakai API admin
    assert m.get("/api/tiket").status_code == 403
    d = k.get("/api/tiket").json()
    assert d["ringkasan"]["baru"] == 1 and d["tiket"][0]["id"] == tid and set(d["teknisi"]) == {"owner", "kasir"}
    det = k.get(f"/api/tiket/{tid}").json()
    assert det["diagnosa"] is not None and det["log"][0]["aksi"] == "buat"
    # ambil, tugaskan, selesai tanpa solusi ditolak, lalu selesai
    assert k.post(f"/api/tiket/{tid}/aksi", json={"aksi": "ambil"}).status_code == 200
    assert k.post(f"/api/tiket/{tid}/aksi", json={"aksi": "tugaskan", "teknisi": "joko"}).status_code == 400  # mitra bukan teknisi
    assert k.post(f"/api/tiket/{tid}/aksi", json={"aksi": "tugaskan", "teknisi": "owner"}).status_code == 200
    assert k.get("/api/tiket?status=saya").json()["tiket"] == []
    assert o.get("/api/tiket?status=saya").json()["tiket"][0]["id"] == tid
    assert o.post(f"/api/tiket/{tid}/aksi", json={"aksi": "selesai"}).status_code == 400
    main._terkirim.clear()
    assert o.post(f"/api/tiket/{tid}/aksi", json={"aksi": "selesai", "catatan": "Konektor ODP diganti"}).status_code == 200
    assert any("selesai" in x["data"].get("text", "") for x in main._terkirim)
    t = [x for x in m.get("/api/mitra/tiket").json()["tiket"] if x["id"] == tid][0]
    assert t["status"] == "selesai" and t["solusi"] == "Konektor ODP diganti" and t["teknisi"] == "owner"
    assert k.get("/api/tiket?status=terbuka").json()["ringkasan"]["selesai_30hari"] == 1
    # buka lagi, lalu batal
    assert k.post(f"/api/tiket/{tid}/aksi", json={"aksi": "buka", "catatan": "masih mati"}).status_code == 200
    assert k.post(f"/api/tiket/{tid}/aksi", json={"aksi": "batal", "catatan": "dobel"}).status_code == 200
    # tiket umum harus ada keterangan
    assert k.post("/api/tiket", json={"kategori": "Lainnya"}).status_code == 400
    t2 = k.post("/api/tiket", json={"kategori": "Kabel / ONU rusak", "keterangan": "Tiang roboh di RT 3",
                                   "prioritas": "tinggi", "teknisi": "kasir"}).json()["id"]
    assert k.get(f"/api/tiket/{t2}").json()["tiket"]["status"] == "dikerjakan"
    # pengingat: tiket lama diingatkan sekali per periode
    conn = main._billing_conn()
    conn.execute("UPDATE tiket SET dibuat_pada='2020-01-01 08:00:00' WHERE id=?", (t2,))
    conn.commit(); conn.close()
    main._terkirim.clear()
    main.proses_pengingat_tiket()
    main.proses_pengingat_tiket()
    assert sum("belum selesai" in x["data"].get("text", "") for x in main._terkirim) == 1
    assert "#T" in main._format_tiket_list()
    assert "Tiket gangguan terbuka" in main._build_daily_report_text()


def test_keuangan_sejak_awal(siap):
    main, o, k, m = siap
    kat = _kat(o)
    hari = datetime.now()
    lalu = (hari.replace(day=1) - timedelta(days=40)).strftime("%Y-%m-%d")   # ~2 bulan lalu
    tgl = hari.strftime("%Y-%m-%d")
    trx = lambda t, jenis, nama, jml: k.post("/api/keuangan/transaksi", json={
        "tanggal": t, "jenis": jenis, "kategori_id": kat[nama], "jumlah": jml}).json()
    trx(lalu, "masuk", "Setoran modal", 2_000_000)
    trx(lalu, "keluar", "Beban listrik", 300_000)
    trx(tgl, "masuk", "Pendapatan jasa pemasangan", 150_000)
    trx(tgl, "keluar", "Beban listrik", 100_000)
    assert m.get("/api/keuangan/keseluruhan").status_code == 403
    d = k.get("/api/keuangan/keseluruhan").json()
    assert d["mulai"] == lalu[:7] and d["sampai"] == tgl[:7] and d["jumlah_bulan"] >= 2
    assert d["total"]["modal_masuk"] == 2_000_000 and d["total"]["beban"] == 400_000
    assert d["total"]["laba"] == 150_000 - 400_000
    assert d["saldo_kas"] == 2_000_000 - 300_000 + 150_000 - 100_000 == d["bulanan"][-1]["saldo_akhir"]
    assert d["bulanan"][-1]["laba_kumulatif"] == d["total"]["laba"]
    assert {x["nama"]: x["total"] for x in d["per_kategori"]}["Beban listrik"] == 400_000
    assert d["terburuk"] == lalu[:7] and d["modal_kembali_persen"] == round(-250_000 * 100 / 2_000_000)


def test_riwayat_transaksi_lengkap(siap):
    """Tab Transaksi memuat juga pembayaran langganan dan pembelian kredit bulan itu."""
    main, o, k, m = siap
    kat = _kat(o)
    hari = datetime.now().strftime("%Y-%m-%d")
    per = hari[:7]
    o.post(f"/api/tagihan/buat?periode={per}")
    t = o.get(f"/api/tagihan?periode={per}").json()["tagihan"][0]
    o.post(f"/api/tagihan/{t['id']}/lunas")
    k.post("/api/keuangan/transaksi", json={"tanggal": hari, "jenis": "keluar", "kategori_id": kat["Beban listrik"], "jumlah": 100_000})
    k.post("/api/keuangan/utang", json={"tanggal": hari, "pemasok": "Toko Fiber", "jumlah": 500_000,
                                        "kategori_id": kat["Beban bahan habis pakai (konektor, pigtail, isolasi)"]})
    d = k.get(f"/api/keuangan/transaksi?periode={per}").json()
    sumber = sorted(x["sumber"] for x in d["transaksi"])
    assert sumber == ["kredit", "langganan", "manual"]
    r = d["ringkasan"]
    assert r["masuk"] == t["jumlah"] and r["langganan"] == t["jumlah"] and r["keluar"] == 100_000 and r["kredit"] == 500_000
    assert [x["sumber"] for x in k.get(f"/api/keuangan/transaksi?periode={per}&jenis=langganan").json()["transaksi"]] == ["langganan"]
    assert [x["sumber"] for x in k.get(f"/api/keuangan/transaksi?periode={per}&jenis=kredit").json()["transaksi"]] == ["kredit"]
    assert {x["sumber"] for x in k.get(f"/api/keuangan/transaksi?periode={per}&jenis=keluar").json()["transaksi"]} == {"manual", "kredit"}
    assert [x["sumber"] for x in k.get(f"/api/keuangan/transaksi?periode={per}&cari=Fiber").json()["transaksi"]] == ["kredit"]
    nama = t.get("nama") or t["username"]
    assert any(x["sumber"] == "langganan" for x in k.get(f"/api/keuangan/transaksi?periode={per}&cari={nama}").json()["transaksi"])
    # tampilan yang dihapus tidak memuat baris otomatis
    assert all(x["sumber"] == "manual" for x in k.get(f"/api/keuangan/transaksi?periode={per}&batal=true").json()["transaksi"])


def test_qty_transaksi(siap):
    main, o, k, m = siap
    kat = _kat(o)
    hari = datetime.now().strftime("%Y-%m-%d")
    per = hari[:7]
    kp = kat["Beban bahan habis pakai (konektor, pigtail, isolasi)"]
    base = {"tanggal": hari, "jenis": "keluar", "kategori_id": kp, "jumlah": 2_500_000}
    assert k.post("/api/keuangan/transaksi", json={**base, "qty": -1}).status_code == 400
    tid = k.post("/api/keuangan/transaksi", json={**base, "qty": 10}).json()["id"]              # satuan bawaan pcs
    t2 = k.post("/api/keuangan/transaksi", json={**base, "jumlah": 300_000, "qty": 150.5, "satuan": "meter"}).json()["id"]
    t3 = k.post("/api/keuangan/transaksi", json=base).json()["id"]                             # qty opsional
    rows = {x["id"]: x for x in k.get(f"/api/keuangan/transaksi?periode={per}&jenis=keluar").json()["transaksi"] if x["sumber"] == "manual"}
    assert (rows[tid]["qty"], rows[tid]["satuan"]) == (10, "pcs")
    assert (rows[t2]["qty"], rows[t2]["satuan"]) == (150.5, "meter")
    assert rows[t3]["qty"] is None and rows[t3]["satuan"] is None
    # ubah: qty ikut tersimpan dan tercatat di log
    assert k.put(f"/api/keuangan/transaksi/{tid}", json={**base, "qty": 12, "satuan": "unit"}).status_code == 200
    r = [x for x in k.get(f"/api/keuangan/transaksi?periode={per}").json()["transaksi"] if x["sumber"] == "manual" and x["id"] == tid][0]
    assert (r["qty"], r["satuan"]) == (12, "unit")
    assert any("qty: 10 pcs → 12 unit" in (a["detail"] or "") for a in o.get("/api/aktivitas").json()["aktivitas"])
    # kredit
    uid = k.post("/api/keuangan/utang", json={"tanggal": hari, "pemasok": "Toko", "kategori_id": kp, "jumlah": 900_000,
                                              "qty": 3}).json()["id"]
    u = [x for x in k.get("/api/keuangan/utang?status=semua").json()["utang"] if x["id"] == uid][0]
    assert (u["qty"], u["satuan"]) == (3, "pcs")
    csv = k.get(f"/api/keuangan/ekspor?periode={per}").text
    assert ";Qty;Satuan;" in csv and ";150.5;meter;" in csv and ";3;pcs;" in csv


def test_sop_keuangan(siap):
    """Pelunasan, tutup buku, nomor bukti, neraca seimbang, jejak hapus permanen."""
    main, o, k, m = siap
    kat = _kat(o)
    hari = datetime.now()
    tgl, per = hari.strftime("%Y-%m-%d"), hari.strftime("%Y-%m")
    kemarin = (hari - timedelta(days=1)).strftime("%Y-%m-%d")
    # --- pelunasan: tanggal & metode, tidak menimpa tanggal lama, batal wajib alasan
    o.post(f"/api/tagihan/buat?periode={per}")
    t = o.get(f"/api/tagihan?periode={per}").json()["tagihan"][0]
    assert k.post(f"/api/tagihan/{t['id']}/lunas", json={"tanggal": "2999-01-01", "metode": "tunai"}).status_code == 400
    assert k.post(f"/api/tagihan/{t['id']}/lunas", json={"tanggal": kemarin, "metode": "transfer"}).status_code == 200
    assert k.post(f"/api/tagihan/{t['id']}/lunas").json().get("sudah_lunas")
    t2 = [x for x in o.get(f"/api/tagihan?periode={per}").json()["tagihan"] if x["id"] == t["id"]][0]
    assert (t2["tgl_bayar"], t2["metode_bayar"], t2["lunas_oleh"]) == (kemarin, "transfer", "kasir")
    assert k.post(f"/api/tagihan/{t['id']}/batal", json={"alasan": ""}).status_code == 400
    # --- nomor bukti berurutan per jenis & bulan
    a = k.post("/api/keuangan/transaksi", json={"tanggal": tgl, "jenis": "masuk", "kategori_id": kat["Setoran modal"], "jumlah": 5_000_000}).json()
    b = k.post("/api/keuangan/transaksi", json={"tanggal": tgl, "jenis": "keluar", "kategori_id": kat["Beban listrik"], "jumlah": 200_000}).json()
    c = k.post("/api/keuangan/transaksi", json={"tanggal": tgl, "jenis": "keluar", "kategori_id": kat["Beban listrik"], "jumlah": 50_000}).json()
    yymm = per[2:4] + per[5:7]
    assert a["nomor"] == f"BKM-{yymm}-0001" and b["nomor"] == f"BKK-{yymm}-0001" and c["nomor"] == f"BKK-{yymm}-0002"
    k.post("/api/keuangan/utang", json={"tanggal": tgl, "pemasok": "Toko", "kategori_id": kat["Beban jasa & pemeliharaan"],
                                        "jumlah": 600_000, "jatuh_tempo": "2099-01-01"})
    # --- neraca seimbang: kas = utang + modal - prive + laba
    d = o.get("/api/keuangan/keseluruhan").json()
    n = d["neraca"]
    assert n["seimbang"] and n["total_aset"] == n["total_pasiva"] and n["kewajiban"]["utang_usaha"] == 600_000
    assert n["aset"]["kas"] == t["jumlah"] + 5_000_000 - 250_000
    # --- tutup buku: hanya owner; setelah ditutup semua perubahan bulan itu ditolak
    assert k.post(f"/api/keuangan/tutup-buku/{per}", json={}).status_code == 403
    assert o.post("/api/keuangan/tutup-buku/2999-01", json={}).status_code == 400
    assert o.post(f"/api/keuangan/tutup-buku/{per}", json={"catatan": "sudah dicek"}).status_code == 200
    assert o.get(f"/api/keuangan/ringkasan?periode={per}").json()["tutup_buku"]["oleh"] == "owner"
    body = {"tanggal": tgl, "jenis": "keluar", "kategori_id": kat["Beban listrik"], "jumlah": 1000}
    assert k.post("/api/keuangan/transaksi", json=body).status_code == 409
    assert o.post("/api/keuangan/transaksi", json=body).status_code == 409          # owner pun harus buka buku dulu
    assert k.put(f"/api/keuangan/transaksi/{b['id']}", json={**body, "jumlah": 300_000}).status_code == 409
    assert k.post(f"/api/keuangan/transaksi/{b['id']}/batal", json={"alasan": "salah"}).status_code == 409
    assert k.post(f"/api/tagihan/{t['id']}/batal", json={"alasan": "salah input"}).status_code == 409
    assert k.post("/api/keuangan/utang", json={"tanggal": tgl, "pemasok": "X", "kategori_id": kat["Beban listrik"], "jumlah": 1}).status_code == 409
    # buka kembali wajib alasan, lalu bisa dikoreksi lagi
    assert o.post(f"/api/keuangan/tutup-buku/{per}/buka", json={"alasan": ""}).status_code == 400
    assert o.post(f"/api/keuangan/tutup-buku/{per}/buka", json={"alasan": "koreksi listrik"}).status_code == 200
    assert k.post(f"/api/keuangan/transaksi/{c['id']}/batal", json={"alasan": "dobel"}).status_code == 200
    # --- hapus permanen menyimpan salinan lengkap di log
    assert o.delete(f"/api/keuangan/transaksi/{c['id']}").status_code == 200
    log = [x["detail"] for x in o.get("/api/aktivitas").json()["aktivitas"] if x["aksi"] == "kas_hapus_permanen"][0]
    assert "salinan data" in log and f"BKK-{yymm}-0002" in log
    aksi = [x["aksi"] for x in o.get("/api/aktivitas").json()["aktivitas"]]
    assert "tutup_buku" in aksi and "buka_buku" in aksi


def test_riwayat_semua_waktu(siap):
    main, o, k, m = siap
    kat = _kat(o)
    hari = datetime.now()
    lama = (hari.replace(day=1) - timedelta(days=70)).strftime("%Y-%m-%d")
    tgl = hari.strftime("%Y-%m-%d")
    a = k.post("/api/keuangan/transaksi", json={"tanggal": tgl, "jenis": "keluar", "kategori_id": kat["Beban listrik"], "jumlah": 1000}).json()["id"]
    b = k.post("/api/keuangan/transaksi", json={"tanggal": lama, "jenis": "keluar", "kategori_id": kat["Beban listrik"], "jumlah": 2000}).json()["id"]
    assert o.get("/api/keuangan/transaksi?periode=ngawur").status_code == 400
    d = k.get("/api/keuangan/transaksi?periode=semua").json()
    ids = [x["id"] for x in d["transaksi"] if x["sumber"] == "manual"]
    assert ids == [a, b] and d["ringkasan"]["keluar"] == 3000                    # urut tanggal terbaru dulu
    d = k.get("/api/keuangan/transaksi?periode=semua&urut=dicatat").json()
    assert [x["id"] for x in d["transaksi"] if x["sumber"] == "manual"] == [b, a]  # yang terakhir diinput di atas
    assert k.get(f"/api/keuangan/transaksi?periode={tgl[:7]}").json()["ringkasan"]["keluar"] == 1000
    csv = k.get("/api/keuangan/ekspor?periode=semua").text
    assert lama in csv and tgl in csv


class FakeACS:
    """GenieACS NBI tiruan: cukup untuk query perangkat dan tugas."""
    def __init__(self):
        wlan = {"1": {"SSID": {"_value": "RUMAH_BUDI"}, "Enable": {"_value": True}, "Channel": {"_value": 6},
                      "TotalAssociations": {"_value": 3}, "PreSharedKey": {"1": {"KeyPassphrase": {"_value": ""}}}},
                "5": {"SSID": {"_value": "RUMAH_BUDI_5G"}, "Enable": {"_value": True}, "Channel": {"_value": 149},
                      "KeyPassphrase": {"_value": ""}}}
        hosts = {str(i): {"HostName": {"_value": f"hp{i}"}, "IPAddress": {"_value": f"192.168.1.{i + 1}"},
                          "MACAddress": {"_value": "aa"}, "Active": {"_value": True}} for i in range(1, 13)}
        self.dev = {"ZTEGC1234567": {
            "_id": "ZTE-F609-ZTEGC1234567", "_lastInform": "2026-01-01T00:00:00.000Z",
            "_deviceId": {"_Manufacturer": "ZTE", "_ProductClass": "F609", "_SerialNumber": "ZTEGC1234567"},
            "InternetGatewayDevice": {
                "DeviceInfo": {"SoftwareVersion": {"_value": "V9.0"}, "UpTime": {"_value": 600}},
                "LANDevice": {"1": {"WLANConfiguration": wlan, "Hosts": {"Host": hosts}}},
                "WANDevice": {"1": {"WANConnectionDevice": {"1": {"WANPPPConnection": {"1": {
                    "Username": {"_value": "budi"}, "ExternalIPAddress": {"_value": "10.0.0.2"}}}}}}}}}}
        self.tugas, self.antre = [], False
        self.antrean, self.faults = {}, []

    def _cocok(self, doc, q):
        if "$or" in q:
            return any(self._cocok(doc, x) for x in q["$or"])
        for jalur, nilai in q.items():
            cur = doc
            for p in jalur.split("."):
                cur = cur.get(p) if isinstance(cur, dict) else None
            if cur != nilai:
                return False
        return True

    def request(self, method, url, params=None, json=None, auth=None, timeout=None):
        import json as js
        if url.endswith("/devices/") and method == "GET":
            q = js.loads((params or {}).get("query", "{}"))
            return FakeResp(True, [d for d in self.dev.values() if self._cocok(d, q)])
        if "/tasks" in url and method == "POST":
            self.tugas.append(json)
            tid = f"{len(self.tugas):024x}"
            if self.antre:
                self.antrean[tid] = json
            r = FakeResp(True, dict(json, _id=tid))
            r.status_code = 202 if self.antre else 200
            return r
        if url.endswith("/tasks/") and method == "GET":
            return FakeResp(True, [dict(t, _id=k) for k, t in self.antrean.items()])
        if url.endswith("/faults/") and method == "GET":
            return FakeResp(True, list(self.faults))
        if method == "DELETE":
            tid = url.rsplit("/", 1)[-1]
            self.antrean.pop(tid, None)
            self.faults = [f for f in self.faults if not url.endswith(f["_id"].replace(":", "%3A"))]
            return FakeResp(True, {})
        r = FakeResp(False, {})
        r.status_code = 404
        return r


def test_acs_dan_diagnosa(siap, monkeypatch):
    main, o, k, m = siap
    for r in (k.get("/api/acs/budi"), k.post("/api/acs/budi/reboot")):
        assert r.status_code == 503                                  # belum diatur
    acs = FakeACS()
    main.CONFIG["GENIEACS_URL"] = "http://acs.test/nbi"
    monkeypatch.setattr(main.requests, "request", acs.request)
    assert m.get("/api/acs/budi").status_code == 403                 # mitra tidak boleh
    # ONU ditemukan lewat username PPPoE (belum ada data OLT), id tersimpan
    d = k.get("/api/acs/budi").json()
    p = d["perangkat"]
    assert d["terhubung"] and p["model"] == "F609" and p["perangkat_aktif"] == 12
    assert [(w["index"], w["band"], w["bisa_ganti_password"]) for w in p["wifi"]] == [(1, "2,4 GHz", True), (5, "5 GHz", True)]
    conn = main._billing_conn()
    assert conn.execute("SELECT acs_id FROM pelanggan WHERE username='budi'").fetchone()[0] == "ZTE-F609-ZTEGC1234567"
    conn.close()
    assert k.get("/api/acs/ani").json()["terhubung"] is False
    # ganti WiFi: validasi, parameter yang benar per model, password tidak masuk log
    assert k.post("/api/acs/budi/wifi", json={"index": 1, "password": "pendek"}).status_code == 400
    assert k.post("/api/acs/budi/wifi", json={"index": 9, "password": "rahasia123"}).status_code == 404
    assert k.post("/api/acs/budi/wifi", json={"index": 1, "ssid": "BUDI_BARU", "password": "rahasia123"}).json()["status"] == "selesai"
    assert acs.tugas[-1]["parameterValues"] == [
        ["InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID", "BUDI_BARU", "xsd:string"],
        ["InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.PreSharedKey.1.KeyPassphrase", "rahasia123", "xsd:string"]]
    k.post("/api/acs/budi/wifi", json={"index": 5, "password": "rahasia123"})
    assert acs.tugas[-1]["parameterValues"][0][0].endswith("WLANConfiguration.5.KeyPassphrase")
    log = " ".join(x["detail"] for x in o.get("/api/aktivitas").json()["aktivitas"])
    assert "password WiFi diganti" in log and "rahasia123" not in log
    acs.antre = True
    assert k.post("/api/acs/budi/reboot").json()["status"] == "antre"
    assert acs.tugas[-1] == {"name": "reboot"}
    # diagnosa: online, pemakaian penuh, banyak perangkat, ONU baru restart
    main._latest_data = main._build_dashboard(main.fetch_pppoe_active()[0], None, [])
    main._mt.traffic = {"tx-bits-per-second": "9800000", "rx-bits-per-second": "500000"}
    dg = k.get("/api/diagnosa/budi").json()
    assert dg["pppoe"]["online"] and dg["lalu_lintas"]["download_mbps"] == 9.8
    teks = " ".join(x["teks"] for x in dg["kesimpulan"])
    assert "penuh" in teks and "12 perangkat" in teks and "restart" in teks
    main._mt.ping = [{"time": "2ms500us", "sent": "1", "received": "1"},
                     {"status": "timeout", "sent": "2", "received": "1", "packet-loss": "50", "avg-rtt": "2ms500us",
                      "min-rtt": "2ms500us", "max-rtt": "2ms500us"}]
    pg = k.post("/api/diagnosa/budi/ping").json()
    assert pg["hilang_persen"] == 50 and pg["rata_ms"] == 2.5
    assert k.post("/api/diagnosa/ani/ping").status_code == 400     # tidak online
    dg = k.get("/api/diagnosa/ani").json()
    assert not dg["pppoe"]["online"] and "tidak online" in dg["kesimpulan"][0]["teks"]
    assert k.get("/api/diagnosa/tidakada").status_code == 404


def test_keamanan_dasar(siap):
    main, o, k, m = siap
    # dokumentasi API otomatis tidak tersedia
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert o.get(p).status_code == 404
    # hash password lama (iterasi lebih kecil) di-upgrade saat login
    conn = main._billing_conn()
    import hashlib, secrets
    g = secrets.token_hex(16)
    lama = f"pbkdf2_sha256$200000${g}${hashlib.pbkdf2_hmac('sha256', b'kasirpass1', bytes.fromhex(g), 200000).hex()}"
    conn.execute("UPDATE akun SET password_hash=? WHERE username='kasir'", (lama,))
    conn.commit()
    assert klien(main, "kasir", "kasirpass1")
    baru = conn.execute("SELECT password_hash FROM akun WHERE username='kasir'").fetchone()[0]
    conn.close()
    assert baru.startswith(f"pbkdf2_sha256${main._PBKDF2_ITERASI}$") and main._cek_password("kasirpass1", baru)
    # pembatas login: catatan kedaluwarsa dibersihkan
    from datetime import datetime, timedelta
    lama_t = datetime.now() - timedelta(hours=1)
    for i in range(5001):
        main._gagal_login[f"ip:x{i}"] = [lama_t]
    main._catat_gagal("ip:baru", datetime.now())
    assert len(main._gagal_login) < 100


def test_ringkas_perangkat_fiberhome(app):
    """Slot WiFi nonaktif tanpa channel tetap dikenali band-nya; jumlah klien tidak dilaporkan → None."""
    main = app
    v = lambda x: {"_value": x}
    doc = {"_id": "000AC2-HG6145D2-FHTT9D2674A0",
           "_deviceId": {"_Manufacturer": "FiberHome", "_ProductClass": "HG6145D2", "_SerialNumber": "FHTT9D2674A0"},
           "InternetGatewayDevice": {"LANDevice": {"1": {
               "WLANConfiguration": {
                   "1": {"SSID": v("Awas ada SULE"), "Enable": v(True), "Channel": v(6), "TotalAssociations": v(0),
                         "PreSharedKey": {"1": {"KeyPassphrase": v("")}}},
                   "6": {"SSID": v("fh_5G_ssid2"), "Enable": v(False)},
                   "5": {"SSID": v("KANJENG MAMI"), "Enable": v(True), "X_FH_Band": v("5G")}},
               "Hosts": {"Host": {"1": {"HostName": v("hp"), "Active": v(True), "InterfaceType": v("802.11")}}}}}}}
    r = main._ringkas_perangkat(doc)
    w = {x["ssid"]: x for x in r["wifi"]}
    assert w["fh_5G_ssid2"]["band"] == "5 GHz" and w["KANJENG MAMI"]["band"] == "5 GHz"
    assert w["Awas ada SULE"]["band"] == "2,4 GHz"
    assert r["perangkat_wifi"] == 1 and all(x["klien"] is None for x in r["wifi"])


def test_ringkas_perangkat_zte(app):
    """ZTE: X_ZTE-COM_BandWidth (lebar kanal) tidak boleh dikira frekuensi; channel 153 = 5 GHz."""
    main = app
    v = lambda x: {"_value": x}
    doc = {"_id": "B08B92-F670L-ZTEGD08EDDF5",
           "_deviceId": {"_Manufacturer": "ZTE", "_ProductClass": "F670L", "_SerialNumber": "ZTEGD08EDDF5"},
           "InternetGatewayDevice": {"LANDevice": {"1": {"WLANConfiguration": {
               "1": {"SSID": v("GHARIL"), "Enable": v(True), "Channel": v(4), "X_ZTE-COM_BandWidth": v("Auto")},
               "5": {"SSID": v("ZTE_5G_2zNUEZ"), "Enable": v(True), "Channel": v(153),
                     "X_ZTE-COM_BandWidth": v("20MHz/40MHz/80MHz")},
               "6": {"SSID": v("Tamu"), "Enable": v(True), "Channel": v(0), "X_ZTE-COM_WlanStandard": v("a,n,ac"),
                     "Standard": v("ac")}}}}}}
    w = {x["ssid"]: x["band"] for x in main._ringkas_perangkat(doc)["wifi"]}
    assert w == {"GHARIL": "2,4 GHz", "ZTE_5G_2zNUEZ": "5 GHz", "Tamu": "5 GHz"}


def test_acs_pantau_tugas(siap, monkeypatch):
    main, o, k, m = siap
    acs = FakeACS()
    main.CONFIG["GENIEACS_URL"] = "http://acs.test/nbi"
    monkeypatch.setattr(main.requests, "request", acs.request)
    r = k.post("/api/acs/budi/wifi", json={"index": 1, "password": "rahasia123"}).json()
    assert r["status"] == "selesai" and len(r["tugas_id"]) == 24
    acs.antre = True
    r = k.post("/api/acs/budi/wifi", json={"index": 1, "password": "rahasia456"}).json()
    tid = r["tugas_id"]
    assert r["status"] == "antre"
    assert k.get(f"/api/acs/budi/tugas/{tid}").json()["status"] == "antre"
    assert k.get("/api/acs/budi/tugas/bukan-id").status_code == 400
    assert m.get(f"/api/acs/budi/tugas/{tid}").status_code == 403
    # ONU menolak -> gagal dengan alasan, lalu bisa dibatalkan
    acs.faults.append({"_id": f"ZTE-F609-ZTEGC1234567:task_{tid}", "channel": f"task_{tid}",
                       "device": "ZTE-F609-ZTEGC1234567", "code": "cwmp.9007", "message": "Invalid parameter value"})
    g = k.get(f"/api/acs/budi/tugas/{tid}").json()
    assert g["status"] == "gagal" and "9007" in g["pesan"]
    assert k.post(f"/api/acs/budi/tugas/{tid}/batal").json()["ok"]
    assert k.get(f"/api/acs/budi/tugas/{tid}").json()["status"] == "selesai" and not acs.faults
    # tugas antre yang kemudian dijalankan ONU -> selesai
    r = k.post("/api/acs/budi/reboot").json()
    assert k.get(f"/api/acs/budi/tugas/{r['tugas_id']}").json()["status"] == "antre"
    acs.antrean.clear()
    assert k.get(f"/api/acs/budi/tugas/{r['tugas_id']}").json()["status"] == "selesai"


def test_acs_ping_dari_onu(siap, monkeypatch):
    main, o, k, m = siap
    acs = FakeACS()
    main.CONFIG["GENIEACS_URL"] = "http://acs.test/nbi"
    monkeypatch.setattr(main.requests, "request", acs.request)
    # ONU tanpa IPPingDiagnostics -> ditolak dengan pesan jelas
    assert k.post("/api/acs/budi/ping", json={}).status_code == 400
    igd = acs.dev["ZTEGC1234567"]["InternetGatewayDevice"]
    igd["IPPingDiagnostics"] = {"DiagnosticsState": {"_value": "None", "_timestamp": "2020-01-01T00:00:00.000Z"}}
    assert k.post("/api/acs/budi/ping", json={"host": "google.com; rm -rf /"}).status_code == 400
    r = k.post("/api/acs/budi/ping", json={"jumlah": 99}).json()
    assert r["jumlah"] == 30 and r["host"] == "google.com"
    nilai = {p[0].rsplit(".", 1)[-1]: p[1] for p in acs.tugas[-1]["parameterValues"]}
    assert nilai["Host"] == "google.com" and nilai["DiagnosticsState"] == "Requested"
    assert m.get(f"/api/acs/budi/ping?mulai={r['mulai']}").status_code == 403
    assert k.get(f"/api/acs/budi/ping?mulai={r['mulai']}").json()["status"] == "menunggu"
    # ONU selesai: nilai diagnostik baru masuk
    baru = "2999-01-01T00:00:00.000Z"
    igd["IPPingDiagnostics"].update({
        "DiagnosticsState": {"_value": "Complete", "_timestamp": baru}, "Host": {"_value": "google.com"},
        "SuccessCount": {"_value": 9}, "FailureCount": {"_value": 1}, "AverageResponseTime": {"_value": 23},
        "MinimumResponseTime": {"_value": 18}, "MaximumResponseTime": {"_value": 41}})
    h = k.get(f"/api/acs/budi/ping?mulai={r['mulai']}&segarkan=true").json()
    assert h["status"] == "selesai" and h["hilang_persen"] == 10 and h["rata_ms"] == 23
    assert acs.tugas[-1]["name"] == "getParameterValues"


def test_koreksi_jam_olt(app):
    """Jam OLT maju 2 jam 24 menit: waktu dying-gasp dikoreksi ke jam server."""
    main = app
    from datetime import datetime, timedelta
    geser = timedelta(hours=2, minutes=24)
    t0 = datetime(2026, 10, 12, 12, 50)

    def onu(i, online, ev_waktu):
        return main.ONUStatus(pon="0/0/1", onu_id=i, snmp_index=i, olt_rx_dbm=-20.0 if online else None,
                              online=online, status="OK", last_event="DyingGasp",
                              last_event_time=f'"{ev_waktu}"' if ev_waktu else None)

    lama = (t0 - timedelta(days=3) + geser).strftime("%Y-%m-%d %H:%M:%S")
    main._koreksi_jam_olt([onu(1, True, lama), onu(2, True, lama)], sekarang=t0)
    assert main._jam_olt_geser == timedelta(0)       # event lama, belum ada bukti selisih
    # ONU 1 baru saja mati pukul 12:59 (jam OLT: 15:23)
    t1 = t0 + timedelta(minutes=9)
    daftar = [onu(1, False, (t1 + geser).strftime("%Y-%m-%d %H:%M:%S")), onu(2, True, lama)]
    main._koreksi_jam_olt(daftar, sekarang=t1 + timedelta(seconds=40))
    assert abs(main._jam_olt_geser - geser) <= timedelta(minutes=1)   # meleset ≤ 1 siklus pemantauan
    putus = datetime.strptime(daftar[0].last_event_time, "%Y-%m-%d %H:%M:%S")
    assert abs(putus - t1) <= timedelta(minutes=1)
    # Siklus berikutnya: tetap terkoreksi, tanpa sampel baru
    d2 = [onu(1, False, (t1 + geser).strftime("%Y-%m-%d %H:%M:%S"))]
    main._koreksi_jam_olt(d2, sekarang=t1 + timedelta(minutes=2))
    assert d2[0].last_event_time == daftar[0].last_event_time


def test_aset_tetap_dan_laporan(siap):
    """Pembelian aset tidak langsung jadi beban: disusutkan garis lurus; laporan standar seimbang."""
    main, o, k, m = siap
    kat = _kat(o)
    ka = kat["Aset: perangkat jaringan (OLT, router, switch, ONU, AP)"]
    kb = kat["Aset: kabel fiber & instalasi (tiang, ODP, ODC, splitter)"]
    assert k.post("/api/keuangan/transaksi", json={"tanggal": "2026-01-05", "jenis": "masuk",
                  "kategori_id": kat["Setoran modal"], "jumlah": 20_000_000}).status_code == 200
    # OLT 4.800.050 tunai (umur 48 bln -> 100.001/bln, sisa 2 di bulan terakhir)
    assert k.post("/api/keuangan/transaksi", json={"tanggal": "2026-01-10", "jenis": "keluar", "kategori_id": ka,
                  "jumlah": 4_800_050, "keterangan": "OLT 4 port"}).status_code == 200
    # Kabel fiber kredit 9.600.000 dengan cicilan 3x 3.300.000 (bunga 300.000)
    assert k.post("/api/keuangan/utang", json={"tanggal": "2026-02-01", "pemasok": "Toko Fiber", "kategori_id": kb,
                  "jumlah": 9_600_000, "jatuh_tempo": "2026-03-01", "tenor": 3, "cicilan": 3_300_000}).status_code == 200
    assert k.post("/api/keuangan/transaksi", json={"tanggal": "2026-02-03", "jenis": "keluar", "kategori_id": kat["Beban listrik"],
                  "jumlah": 200_000}).status_code == 200

    jan = o.get("/api/keuangan/ringkasan?periode=2026-01").json()
    assert jan["beban"] == 100_001 and jan["beli_aset"] == 4_800_050 and jan["laba"] == -100_001
    assert jan["arus_kas"] == 20_000_000 - 4_800_050
    feb = o.get("/api/keuangan/ringkasan?periode=2026-02").json()
    assert feb["penyusutan"] == 100_001 + 100_000 and feb["beli_aset_kredit"] == 9_600_000
    assert feb["beban"] == 200_000 + 200_001 and feb["beban_kredit"] == 0

    lap = o.get("/api/keuangan/laporan?periode=2026-03").json()
    lr = lap["laba_rugi"]
    nama = {x["nama"]: x["jumlah"] for x in lr["beban_usaha"]}
    assert nama["Beban penyusutan aset tetap"] == 200_001
    assert lr["total_beban_lain"] == 100_000          # bunga cicilan ke-1 jatuh tempo Maret
    ak = lap["arus_kas"]
    assert ak["cocok"] and ak["bersih"]["investasi"] == 0 and ak["bersih"]["operasi"] == 0   # cicilan belum dibayar
    p = lap["posisi"]
    assert p["seimbang"], p
    assert p["aset"]["aset_tetap"] == 4_800_050 + 9_600_000
    assert p["aset"]["akumulasi_penyusutan"] == -(3 * 100_001 + 2 * 100_000)
    assert p["kewajiban"]["utang_usaha"] == 9_600_000 + 100_000      # pokok + bunga yang sudah jatuh tempo
    # Bayar cicilan pertama -> arus kas investasi
    u = o.get("/api/keuangan/utang?status=belum").json()["utang"][0]
    assert k.post(f"/api/keuangan/utang/{u['id']}/bayar", json={"tanggal": "2026-03-01", "jumlah": 3_300_000}).status_code == 200
    lap = o.get("/api/keuangan/laporan?periode=2026-03").json()
    assert lap["arus_kas"]["bersih"]["investasi"] == -3_300_000 and lap["arus_kas"]["cocok"] and lap["posisi"]["seimbang"]
    th = o.get("/api/keuangan/laporan?periode=2026").json()
    assert th["posisi"]["seimbang"] and th["arus_kas"]["cocok"]
    assert len(th["aset_tetap"]["daftar"]) == 2
    assert o.get("/api/keuangan/keseluruhan").json()["neraca"]["seimbang"]
    assert m.get("/api/keuangan/laporan?periode=2026-03").status_code == 403
    assert o.get("/api/keuangan/laporan?periode=x").status_code == 400


def test_kosongkan_keuangan(siap):
    """Owner bisa mengosongkan catatan kas & utang; cadangan dibuat dulu; billing tidak tersentuh."""
    main, o, k, m = siap
    import os, sqlite3
    kat = _kat(o)
    per = datetime.now().strftime("%Y-%m")
    tgl = datetime.now().strftime("%Y-%m-%d")
    o.post(f"/api/tagihan/buat?periode={per}")
    t = o.get(f"/api/tagihan?periode={per}").json()["tagihan"][0]
    k.post(f"/api/tagihan/{t['id']}/lunas", json={"tanggal": tgl, "metode": "tunai"})
    k.post("/api/keuangan/transaksi", json={"tanggal": tgl, "jenis": "keluar", "kategori_id": kat["Beban listrik"], "jumlah": 50_000})
    k.post("/api/keuangan/utang", json={"tanggal": tgl, "pemasok": "Toko", "kategori_id": kat["Beban listrik"], "jumlah": 90_000,
                                        "uang_muka": 10_000})
    o.post(f"/api/keuangan/tutup-buku/{per}", json={})
    body = {"password": "rahasia123", "konfirmasi": "HAPUS DATA KEUANGAN"}
    assert k.post("/api/keuangan/kosongkan", json=body).status_code == 403
    assert o.post("/api/keuangan/kosongkan", json={**body, "konfirmasi": "hapus"}).status_code == 400
    assert o.post("/api/keuangan/kosongkan", json={**body, "password": "salah"}).status_code == 400
    r = o.post("/api/keuangan/kosongkan", json=body).json()
    assert r["dihapus"] == {"transaksi": 2, "utang": 1, "tutup_buku": 1}
    cad = os.path.join(main._folder_backup(), r["cadangan"])
    c = sqlite3.connect(cad)
    assert c.execute("SELECT COUNT(*) FROM transaksi").fetchone()[0] == 2
    c.close()
    rk = o.get(f"/api/keuangan/ringkasan?periode={per}").json()
    assert rk["beban"] == 0 and rk["utang"]["sisa"] == 0 and rk["tutup_buku"] is None
    assert rk["pendapatan_langganan"] == t["jumlah"]          # pembayaran pelanggan tetap
    # bisa langsung dicatat ulang, nomor bukti mulai dari 0001 lagi
    x = k.post("/api/keuangan/transaksi", json={"tanggal": tgl, "jenis": "keluar", "kategori_id": kat["Beban listrik"], "jumlah": 1}).json()
    assert x["nomor"].endswith("-0001")


def test_nama_kategori_baku(app):
    """Database lama dengan nama kategori tidak baku diganti nama otomatis; id & transaksi tetap."""
    main = app
    import sqlite3
    c = sqlite3.connect(main.CONFIG["BILLING_DB_PATH"])
    c.execute("UPDATE kategori_kas SET nama='Listrik' WHERE nama='Beban listrik'")
    c.execute("UPDATE kategori_kas SET nama='Pembayaran utang' WHERE nama='Pelunasan liabilitas'")
    kid = c.execute("SELECT id FROM kategori_kas WHERE nama='Listrik'").fetchone()[0]
    c.commit(); c.close()
    main.init_keuangan_db()
    c = sqlite3.connect(main.CONFIG["BILLING_DB_PATH"])
    nama = {r[0] for r in c.execute("SELECT nama FROM kategori_kas")}
    assert c.execute("SELECT nama FROM kategori_kas WHERE id=?", (kid,)).fetchone()[0] == "Beban listrik"
    c.close()
    assert "Pelunasan liabilitas" in nama and "Pembayaran utang" not in nama and "Listrik" not in nama
    assert not any("utang" in n.lower() or "cicilan" in n.lower() for n in nama)


def test_acs_onu_diganti_dan_ping_lewat_wan(siap, monkeypatch):
    """ONU pelanggan diganti: nomor seri dari OLT menang atas id lama. Ping diarahkan ke PPPoE;
    bila ONU menolak perintahnya, status 'gagal' langsung dilaporkan."""
    import copy
    main, o, k, m = siap
    acs = FakeACS()
    main.CONFIG["GENIEACS_URL"] = "http://acs.test/nbi"
    monkeypatch.setattr(main.requests, "request", acs.request)
    assert k.get("/api/acs/budi").json()["perangkat"]["id"] == "ZTE-F609-ZTEGC1234567"   # id tersimpan
    baru = copy.deepcopy(acs.dev["ZTEGC1234567"])
    baru.update(_id="ZTE-F670L-ZTEGDBARU0001", _lastInform="2026-02-01T00:00:00.000Z",
                _deviceId={"_Manufacturer": "ZTE", "_ProductClass": "F670L", "_SerialNumber": "ZTEGDBARU0001"})
    acs.dev["ZTEGDBARU0001"] = baru
    # Belum ada data OLT: dua ONU sama-sama menyimpan username budi -> id tersimpan (ONU lama) dipakai
    assert k.get("/api/acs/budi").json()["perangkat"]["id"] == "ZTE-F609-ZTEGC1234567"
    # OLT kini melihat ONU baru untuk budi -> ONU baru yang dipakai dan id tersimpan diperbarui
    onu = main.ONUStatus(pon="0/0/1", onu_id=1, snmp_index=1, olt_rx_dbm=-20.0, online=True, status="OK",
                         serial_number="ZTEGDBARU0001", pppoe_username="budi")
    main._latest_data = main._build_dashboard([], None, [onu])
    assert k.get("/api/acs/budi").json()["perangkat"]["id"] == "ZTE-F670L-ZTEGDBARU0001"
    main._latest_data = None
    assert k.get("/api/acs/budi").json()["perangkat"]["id"] == "ZTE-F670L-ZTEGDBARU0001"

    igd = baru["InternetGatewayDevice"]
    igd["IPPingDiagnostics"] = {"DiagnosticsState": {"_value": "None"}, "Interface": {"_value": ""}}
    r = k.post("/api/acs/budi/ping", json={}).json()
    nilai = {p[0].rsplit(".", 1)[-1]: p[1] for p in acs.tugas[-1]["parameterValues"]}
    assert r["lewat_wan"] and nilai["Interface"] == "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1"
    r2 = k.post("/api/acs/budi/ping", json={"lewat_wan": False}).json()
    assert not r2["lewat_wan"] and "Interface" not in {p[0].rsplit(".", 1)[-1] for p in acs.tugas[-1]["parameterValues"]}
    # ONU menolak (fault) -> gagal, bukan menunggu terus
    acs.antre = True
    r3 = k.post("/api/acs/budi/ping", json={}).json()
    tid = r3["tugas_id"]
    acs.faults.append({"_id": f"{baru['_id']}:task_{tid}", "channel": f"task_{tid}", "device": baru["_id"],
                       "code": "cwmp.9007", "message": "Invalid parameter value"})
    h = k.get(f"/api/acs/budi/ping?mulai={r3['mulai']}&tugas={tid}").json()
    assert h["status"] == "gagal" and "9007" in h["pesan"]


def test_acs_ping_minimal_dan_detail_fault(siap, monkeypatch):
    """ZTE F609 menolak ping (9003): mode minimal hanya mengirim Host/jumlah/mulai; kolom yang ditolak disebut."""
    main, o, k, m = siap
    acs = FakeACS()
    main.CONFIG["GENIEACS_URL"] = "http://acs.test/nbi"
    monkeypatch.setattr(main.requests, "request", acs.request)
    acs.dev["ZTEGC1234567"]["InternetGatewayDevice"]["IPPingDiagnostics"] = {"DiagnosticsState": {"_value": "None"}}
    r = k.post("/api/acs/budi/ping", json={"minimal": True}).json()
    assert r["minimal"] and [p[0].rsplit(".", 1)[-1] for p in acs.tugas[-1]["parameterValues"]] == [
        "Host", "NumberOfRepetitions", "DiagnosticsState"]
    acs.antre = True
    tid = k.post("/api/acs/budi/ping", json={}).json()["tugas_id"]
    acs.faults.append({"_id": f"ZTE-F609-ZTEGC1234567:task_{tid}", "channel": f"task_{tid}", "device": "ZTE-F609-ZTEGC1234567",
                       "code": "cwmp.9003", "message": "Invalid arguments",
                       "detail": {"faultCode": "9003", "setParameterValuesFault": [
                           {"parameterName": "InternetGatewayDevice.IPPingDiagnostics.DataBlockSize", "faultCode": "9007"}]}})
    st = k.get(f"/api/acs/budi/tugas/{tid}").json()
    assert st["status"] == "gagal" and st["parameter"] == ["DataBlockSize"] and "DataBlockSize" in st["pesan"]


def test_acs_ping_ikut_tipe_onu(siap, monkeypatch):
    main, o, k, m = siap
    acs = FakeACS()
    main.CONFIG["GENIEACS_URL"] = "http://acs.test/nbi"
    monkeypatch.setattr(main.requests, "request", acs.request)
    acs.dev["ZTEGC1234567"]["InternetGatewayDevice"]["IPPingDiagnostics"] = {
        "DiagnosticsState": {"_value": "None", "_type": "xsd:string"},
        "NumberOfRepetitions": {"_value": 1, "_type": "xsd:int"}}
    k.post("/api/acs/budi/ping", json={"minimal": True})
    tipe = {p[0].rsplit(".", 1)[-1]: p[2] for p in acs.tugas[-1]["parameterValues"]}
    assert tipe == {"Host": "xsd:string", "NumberOfRepetitions": "xsd:int", "DiagnosticsState": "xsd:string"}


def test_acs_ping_bersihkan_tugas_tertahan(siap, monkeypatch):
    """Perintah ping lama yang tertahan (fault 9003) dihapus sebelum ping baru dikirim."""
    main, o, k, m = siap
    acs = FakeACS()
    main.CONFIG["GENIEACS_URL"] = "http://acs.test/nbi"
    monkeypatch.setattr(main.requests, "request", acs.request)
    acs.dev["ZTEGC1234567"]["InternetGatewayDevice"]["IPPingDiagnostics"] = {"DiagnosticsState": {"_value": "None"}}
    lama = "a" * 24
    acs.antrean[lama] = {"name": "setParameterValues", "parameterValues": [
        ["InternetGatewayDevice.IPPingDiagnostics.Host", "google.com", "xsd:string"]]}
    acs.antrean["b" * 24] = {"name": "reboot"}
    acs.faults.append({"_id": f"ZTE-F609-ZTEGC1234567:task_{lama}", "channel": f"task_{lama}",
                       "device": "ZTE-F609-ZTEGC1234567", "code": "cwmp.9003", "message": "Invalid arguments"})
    r = k.post("/api/acs/budi/ping", json={}).json()
    assert r["status"] == "selesai"
    assert lama not in acs.antrean and "b" * 24 in acs.antrean and not acs.faults   # tugas lain tidak disentuh
    kolom = [p[0].rsplit(".", 1)[-1] for p in acs.tugas[-1]["parameterValues"]]
    assert "Timeout" not in kolom and "DataBlockSize" not in kolom and kolom[-1] == "DiagnosticsState"


def test_acs_ping_segarkan_tidak_menumpuk(siap, monkeypatch):
    """Permintaan hasil ping diulang berkala dari browser, tapi tidak ditumpuk bila ONU belum menjawab."""
    main, o, k, m = siap
    acs = FakeACS()
    main.CONFIG["GENIEACS_URL"] = "http://acs.test/nbi"
    monkeypatch.setattr(main.requests, "request", acs.request)
    acs.dev["ZTEGC1234567"]["InternetGatewayDevice"]["IPPingDiagnostics"] = {"DiagnosticsState": {"_value": "None"}}
    r = k.post("/api/acs/budi/ping", json={}).json()
    acs.antre = True
    for _ in range(3):
        assert k.get(f"/api/acs/budi/ping?mulai={r['mulai']}&segarkan=true").json()["status"] == "menunggu"
    assert sum(1 for t in acs.tugas if t["name"] == "getParameterValues") == 1


def test_kelola_kategori(siap):
    """Kategori buatan sendiri: bisa aset tetap; owner bisa menghapus & memindahkan catatannya."""
    main, o, k, m = siap
    kat = _kat(o)
    r = k.post("/api/keuangan/kategori", json={"nama": "OLT C DATA", "jenis": "keluar"}).json()
    salah = r["id"]
    a = k.post("/api/keuangan/kategori", json={"nama": "perangkat server", "jenis": "keluar", "umur_bulan": 48}).json()
    assert a["nama"] == "Aset: perangkat server"
    assert k.post("/api/keuangan/kategori", json={"nama": "x", "jenis": "masuk", "umur_bulan": 48}).status_code == 400
    assert k.post("/api/keuangan/kategori", json={"nama": "y", "jenis": "keluar", "umur_bulan": 12}).status_code == 400
    t = k.post("/api/keuangan/transaksi", json={"tanggal": "2026-10-05", "jenis": "keluar", "kategori_id": salah,
                                                "jumlah": 6_500_000, "keterangan": "OLT C-Data 4 port"}).json()
    assert o.get("/api/keuangan/ringkasan?periode=2026-10").json()["beban"] == 6_500_000   # salah: jadi beban
    daftar = {x["nama"]: x for x in o.get("/api/keuangan/kategori").json()["kategori"]}
    assert daftar["OLT C DATA"]["dipakai"] == 1 and not daftar["OLT C DATA"]["bawaan"] and daftar["Beban listrik"]["bawaan"]
    aset = kat["Aset: perangkat jaringan (OLT, router, switch, ONU, AP)"]
    assert k.delete(f"/api/keuangan/kategori/{salah}?pindah_ke={aset}").status_code == 403          # admin
    assert o.delete(f"/api/keuangan/kategori/{salah}").status_code == 400                            # dipakai
    assert o.delete(f"/api/keuangan/kategori/{salah}?pindah_ke={kat['Setoran modal']}").status_code == 400  # beda jenis
    assert o.delete(f"/api/keuangan/kategori/{kat['Beban listrik']}").status_code == 400             # bawaan
    assert o.delete(f"/api/keuangan/kategori/{salah}?pindah_ke={aset}").json()["dipindah"] == 1
    rk = o.get("/api/keuangan/ringkasan?periode=2026-10").json()
    assert rk["beli_aset"] == 6_500_000 and rk["beban"] == 6_500_000 // 48
    assert "OLT C DATA" not in _kat(o)
    assert o.delete(f"/api/keuangan/kategori/{a['id']}").json()["dipindah"] == 0


def test_transaksi_kembar_ditolak(siap):
    """Tombol Simpan tertekan dua kali: kiriman kedua yang persis sama ditolak, bukan dicatat dobel."""
    main, o, k, m = siap
    kat = _kat(o)
    body = {"tanggal": "2026-10-05", "jenis": "keluar", "kategori_id": kat["Beban listrik"], "jumlah": 50_000,
            "keterangan": "token"}
    assert k.post("/api/keuangan/transaksi", json=body).status_code == 200
    r = k.post("/api/keuangan/transaksi", json=body)
    assert r.status_code == 409 and "baru saja tercatat" in r.json()["detail"]
    assert k.post("/api/keuangan/transaksi", json={**body, "jumlah": 50_001}).status_code == 200   # beda nominal: boleh
    assert o.post("/api/keuangan/transaksi", json=body).status_code == 200                        # beda pencatat: boleh
    n = len([t for t in o.get("/api/keuangan/transaksi?periode=2026-10").json()["transaksi"] if t["sumber"] == "manual"])
    assert n == 3


def test_riwayat_transaksi_filter(siap):
    """Riwayat transaksi: rentang tanggal, metode, kategori, dan urutan terlama/terbaru."""
    main, o, k, m = siap
    kat = _kat(o)
    for tgl, jml, met in [("2026-08-02", 10_000, "tunai"), ("2026-09-15", 20_000, "transfer"), ("2026-10-01", 30_000, "tunai"),
                          ("2026-10-09", 40_000, "qris")]:
        assert k.post("/api/keuangan/transaksi", json={"tanggal": tgl, "jenis": "keluar", "kategori_id": kat["Beban listrik"],
                                                       "jumlah": jml, "metode": met}).status_code == 200
    k.post("/api/keuangan/transaksi", json={"tanggal": "2026-09-20", "jenis": "keluar", "kategori_id": kat["Beban gaji & upah"],
                                            "jumlah": 99_000})
    jml = lambda **q: [t["jumlah"] for t in o.get("/api/keuangan/transaksi", params=q).json()["transaksi"] if t["sumber"] == "manual"]
    assert jml() == [40_000, 30_000, 99_000, 20_000, 10_000]                       # bawaan: semua waktu, terbaru dulu
    assert jml(urut="lama") == [10_000, 20_000, 99_000, 30_000, 40_000]
    assert jml(dari="2026-09-01", sampai="2026-10-01") == [30_000, 99_000, 20_000]
    assert jml(sampai="2026-09-30", dari="2026-09-16") == [99_000]
    assert jml(metode="tunai") == [30_000, 99_000, 10_000]          # 99.000: metode bawaan tunai
    assert jml(kategori_id=kat["Beban listrik"], metode="qris") == [40_000]
    r = o.get("/api/keuangan/transaksi", params={"dari": "2026-10-01"}).json()
    assert r["ringkasan"]["keluar"] == 70_000
    assert o.get("/api/keuangan/transaksi", params={"dari": "2026-13-01"}).status_code == 400


def test_dana_talangan_pemilik(siap):
    """Uang pribadi pemilik yang dipinjamkan ke usaha: kas & neraca benar, bukan modal/pendapatan;
    saldo kas terendah harian menunjukkan kapan kas sempat minus."""
    main, o, k, m = siap
    kat = _kat(o)
    t = lambda tgl, j, n, x: k.post("/api/keuangan/transaksi", json={"tanggal": tgl, "jenis": j, "kategori_id": kat[n], "jumlah": x})
    assert t("2026-08-01", "masuk", "Setoran modal", 1_000_000).status_code == 200
    assert t("2026-08-05", "keluar", "Beban bandwidth (upstream)", 1_500_000).status_code == 200    # kas sempat -500 rb
    rk = o.get("/api/keuangan/ringkasan?periode=2026-08").json()
    assert rk["kas_negatif"] and rk["kas_terendah"] == {"tanggal": "2026-08-05", "saldo": -500_000}
    # catat talangan pemilik di tanggal yang sama -> kas tidak lagi minus
    assert t("2026-08-05", "masuk", "Dana talangan pemilik (pinjaman)", 600_000).status_code == 200
    rk = o.get("/api/keuangan/ringkasan?periode=2026-08").json()
    assert not rk["kas_negatif"] and rk["saldo_akhir"] == 100_000
    assert rk["talangan_masuk"] == 600_000 and rk["modal_masuk"] == 1_000_000 and rk["pendapatan"] == 0
    assert rk["talangan_sisa"] == 600_000
    # dikembalikan sebagian bulan berikutnya
    assert t("2026-09-02", "masuk", "Pendapatan jasa pemasangan", 900_000).status_code == 200
    assert t("2026-09-03", "keluar", "Pengembalian dana talangan pemilik", 400_000).status_code == 200
    rk = o.get("/api/keuangan/ringkasan?periode=2026-09").json()
    assert rk["prive"] == 0 and rk["talangan_kembali"] == 400_000 and rk["talangan_sisa"] == 200_000
    assert rk["laba"] == 900_000 and rk["saldo_akhir"] == 100_000 + 900_000 - 400_000
    lap = o.get("/api/keuangan/laporan?periode=2026-09").json()
    p = lap["posisi"]
    assert p["seimbang"] and p["kewajiban"]["utang_pemilik"] == 200_000 and p["ekuitas"]["modal_disetor"] == 1_000_000
    assert lap["arus_kas"]["cocok"] and lap["arus_kas"]["bersih"]["pendanaan"] == -400_000
    assert o.get("/api/keuangan/keseluruhan").json()["neraca"]["seimbang"]


def test_valuasi(siap):
    """Bahan valuasi: MRR dari pelanggan aktif berbayar, nilai buku dari neraca, EBITDA bulan lengkap."""
    main, o, k, m = siap
    assert k.get("/api/keuangan/valuasi").status_code == 403          # admin tidak boleh
    v = o.get("/api/keuangan/valuasi").json()
    assert v["pelanggan_berbayar"] >= 1 and v["mrr"] > 0 and v["arr"] == v["mrr"] * 12
    assert v["arpu"] == round(v["mrr"] / v["pelanggan_berbayar"])
    assert v["nilai_buku"] == v["kas"] + v["aset_tetap_buku"] - v["utang"]
    assert len(v["bulan_dihitung"]) >= 1 and v["ebitda_tahunan"] == v["rata_bulanan"]["ebitda"] * 12


def test_keandalan_backup_harian_healthz_dan_bayar_ganda(siap):
    main, o, k, m = siap
    main.CONFIG["TELEGRAM_OWNER_CHAT_ID"] = "222222"
    # database memakai WAL; salinan backup berupa satu berkas biasa
    c = sqlite3.connect(main.CONFIG["BILLING_DB_PATH"])
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c.close()
    main.proses_backup_harian(datetime(2026, 10, 11, 3))          # Minggu: berbunyi + nota
    main._terkirim.clear()
    main.proses_backup_harian(datetime(2026, 10, 12, 3))          # Senin: tetap dikirim, senyap
    dok = [x["data"] for x in main._terkirim if "caption" in x["data"]]
    assert dok and all(d.get("disable_notification") == "true" for d in dok)
    b = sqlite3.connect(os.path.join(main._folder_backup(), "billing-2026-10-12.db"))
    assert b.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    b.close()
    main.CONFIG["BACKUP_KIRIM_HARIAN"] = False
    main._terkirim.clear()
    main.proses_backup_harian(datetime(2026, 10, 13, 3))
    assert not [x for x in main._terkirim if "caption" in x["data"]]
    # /healthz publik: sehat setelah putaran, 503 bila loop macet
    x = klien(main)
    main._tandai_putaran()
    assert x.get("/healthz").json()["ok"] is True
    main._putaran["terakhir"] -= 3600
    assert x.get("/healthz").status_code == 503
    main._tandai_putaran()
    # tandai lunas dua kali: tanggal bayar pertama tidak tertimpa
    o.post("/api/tagihan/buat?periode=2026-10")
    tid = o.get("/api/tagihan?periode=2026-10").json()["tagihan"][0]["id"]
    assert o.post(f"/api/tagihan/{tid}/lunas", json={"tanggal": "2026-10-05", "metode": "tunai"}).json() == {"ok": True}
    r = o.post(f"/api/tagihan/{tid}/lunas", json={"tanggal": "2026-10-06", "metode": "qris"}).json()
    assert r.get("sudah_lunas") is True
    c = sqlite3.connect(main.CONFIG["BILLING_DB_PATH"])
    assert c.execute("SELECT tgl_bayar, metode_bayar FROM tagihan WHERE id=?", (tid,)).fetchone() == ("2026-10-05", "tunai")
    c.close()


def test_tunggakan_dan_kwitansi(siap):
    main, o, k, m = siap
    for per in ("2026-08", "2026-09", "2026-10"):
        assert o.post(f"/api/tagihan/buat?periode={per}").status_code == 200
    t10 = {x["username"]: x for x in o.get("/api/tagihan?periode=2026-10").json()["tagihan"]}
    t08 = {x["username"]: x for x in o.get("/api/tagihan?periode=2026-08").json()["tagihan"]}
    # ani bayar Agustus; budi bayar semuanya
    o.post(f"/api/tagihan/{t08['ani']['id']}/lunas", json={"tanggal": "2026-08-03", "metode": "transfer"})
    for per in ("2026-08", "2026-09", "2026-10"):
        b = next(x for x in o.get(f"/api/tagihan?periode={per}").json()["tagihan"] if x["username"] == "budi")
        o.post(f"/api/tagihan/{b['id']}/lunas", json={"tanggal": "2026-10-01", "metode": "tunai"})
    d = k.get("/api/tagihan/tunggakan").json()
    per = {g["username"]: g for g in d["pelanggan"]}
    assert "budi" not in per and "server" not in per
    assert per["ani"]["bulan"] == 2 and per["dwi"]["bulan"] == 3
    assert per["dwi"]["total"] == sum(t["jumlah"] for t in per["dwi"]["tagihan"])
    assert per["dwi"]["hari_telat"] >= per["ani"]["hari_telat"]          # umur dari jatuh tempo tertua
    assert d["pelanggan"][0]["username"] == "dwi"                         # paling lama di atas
    assert d["total"] == per["ani"]["total"] + per["dwi"]["total"]
    assert sum(v["pelanggan"] for v in d["kelompok"].values()) == 2
    assert m.get("/api/tagihan/tunggakan").status_code == 403             # mitra tidak boleh
    # kwitansi: hanya untuk yang lunas
    assert o.get(f"/api/tagihan/{t10['ani']['id']}/kwitansi").status_code == 400
    kw = o.get(f"/api/tagihan/{t08['ani']['id']}/kwitansi").json()
    assert kw["nomor"] == f"KW/202608/{t08['ani']['id']:05d}" and kw["metode_bayar"] == "transfer"
    assert kw["sisa_bulan"] == 2 and kw["sisa_total"] == per["ani"]["total"]
    h = o.get(f"/kwitansi/{t08['ani']['id']}")
    assert h.status_code == 200 and "KWITANSI" in h.text and "Agustus 2026" in h.text and "LUNAS" in h.text
    x = klien(main)
    x.follow_redirects = False
    assert x.get(f"/kwitansi/{t08['ani']['id']}").status_code == 303     # wajib login
    assert m.get(f"/api/tagihan/{t08['ani']['id']}/kwitansi").status_code == 403


def test_pengingat_wa_massal(siap):
    main, o, k, m = siap
    o.post("/api/tagihan/buat?periode=2026-09")
    # pengaturan teks pesan
    r = o.put("/api/pengaturan", json={"nilai": {"NAMA_USAHA": "Indeso Net",
                                                 "INFO_BAYAR": "Transfer BRI 1234 a.n. Arif\nKirim bukti ke nomor ini.",
                                                 "TEMPLATE_PENGINGAT": "Halo {nama}, tagihan {usaha}:\n{rincian}\nTotal {total}\n{cara_bayar}"}})
    assert r.json()["berubah"] == 3
    assert o.put("/api/pengaturan", json={"nilai": {"INFO_BAYAR": "   "}}).status_code == 400
    assert o.put("/api/pengaturan", json={"nilai": {"NAMA_USAHA": "x" * 61}}).status_code == 400
    p = k.get("/api/pesan-wa").json()
    assert p == {"usaha": "Indeso Net", "cara_bayar": "Transfer BRI 1234 a.n. Arif\nKirim bukti ke nomor ini.",
                 "template": "Halo {nama}, tagihan {usaha}:\n{rincian}\nTotal {total}\n{cara_bayar}"}
    assert m.get("/api/pesan-wa").status_code == 403
    # tersimpan permanen (dimuat ulang saat restart)
    main.CONFIG["NAMA_USAHA"] = "lain"
    main._muat_pengaturan()
    assert main.CONFIG["NAMA_USAHA"] == "Indeso Net"
    # catatan pengingat muncul di tunggakan
    d = o.get("/api/tagihan/tunggakan").json()
    assert d["jeda_pengingat_hari"] == 3 and all(g["diingatkan"] is None for g in d["pelanggan"])
    assert k.post("/api/tagihan/pengingat", json={"username": "ani", "total": 150000, "bulan": 1}).status_code == 200
    assert k.post("/api/tagihan/pengingat", json={"username": "tidakada"}).status_code == 404
    assert m.post("/api/tagihan/pengingat", json={"username": "ani"}).status_code == 403
    per = {g["username"]: g for g in o.get("/api/tagihan/tunggakan").json()["pelanggan"]}
    assert per["ani"]["diingatkan"] and per["dwi"]["diingatkan"] is None


def test_abaikan_peringatan_kas_minus(siap):
    main, o, k, m = siap
    kat = _kat(o)
    assert k.post("/api/keuangan/transaksi", json={"tanggal": "2026-08-05", "jenis": "keluar",
                                                     "kategori_id": kat["Beban bandwidth (upstream)"], "jumlah": 500_000}).status_code == 200
    assert o.get("/api/keuangan/ringkasan?periode=2026-08").json()["kas_negatif"]       # untuk cek saat tutup buku
    # buku masih terbuka: belum diperingatkan di ikhtisar kumulatif
    assert not any("negatif" in p for p in o.get("/api/keuangan/keseluruhan").json()["peringatan"])
    assert o.post("/api/keuangan/tutup-buku/2026-08", json={"catatan": ""}).status_code == 200
    assert any("Agustus 2026" in p for p in o.get("/api/keuangan/keseluruhan").json()["peringatan"])
    assert k.post("/api/keuangan/abaikan-kas-minus", json={"periode": "2026-08"}).status_code == 403   # hanya owner
    assert o.post("/api/keuangan/abaikan-kas-minus", json={"periode": "2999-01"}).status_code == 400
    assert o.post("/api/keuangan/abaikan-kas-minus", json={"periode": "2026-08"}).status_code == 200
    rk = o.get("/api/keuangan/ringkasan?periode=2026-08").json()
    assert not rk["kas_negatif"] and rk["kas_negatif_diabaikan"] and rk["saldo_akhir"] == -500_000   # angka tetap
    assert not any("Agustus 2026" in p for p in o.get("/api/keuangan/keseluruhan").json()["peringatan"])
    # bulan sesudahnya tetap diperingatkan
    assert o.get("/api/keuangan/ringkasan?periode=2026-09").json()["kas_negatif"]
    assert o.post("/api/keuangan/abaikan-kas-minus", json={"periode": ""}).status_code == 200
    assert o.get("/api/keuangan/ringkasan?periode=2026-08").json()["kas_negatif"]


class FakeOLTTelnet:
    """Server telnet tiruan mirip CLI C-Data FD1602S-B1: catat perintah yang diterima."""
    def __init__(self, tolak_onu=None, save_di_enable=False):
        import socket, threading
        self.perintah, self.tolak_onu, self.save_di_enable = [], tolak_onu, save_di_enable
        self.srv = socket.socket(); self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0)); self.srv.listen(1); self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._layani, daemon=True).start()

    def _layani(self):
        while True:
            c, _ = self.srv.accept()
            try:
                self._sesi(c)
            except SystemExit:
                pass
            finally:
                c.close()

    def _sesi(self, c):
        f = c.makefile("rb")
        kirim = lambda t: c.sendall(t.encode())
        c.sendall(bytes([255, 251, 1, 255, 251, 3]) + b"User Access Verification\r\n\r\nUser name:")
        def baris():
            b = f.readline()
            if not b:
                raise SystemExit
            while b and b[:1] == b"\xff":
                b = b[3:]
            return b.decode(errors="replace").strip()
        baris(); kirim("Password:")
        if baris() != "rahasiaOLT":
            kirim("\r\n% Authentication failed.\r\nUser name:"); c.close(); return
        prompt = "FD1602S-B1>"
        kirim("\r\n" + prompt)
        while True:
            b = baris()
            if not b and f.closed:
                return
            self.perintah.append(b)
            out = ""
            if b == "enable": prompt = "FD1602S-B1#"
            elif b == "config": prompt = "FD1602S-B1(config)#"
            elif b.startswith("interface gpon"): prompt = "FD1602S-B1(config-interface-gpon-0/0)#"
            elif b == "exit": prompt = "FD1602S-B1(config)#"
            elif b == "end": prompt = "FD1602S-B1#"
            elif b == "save" and self.save_di_enable and prompt != "FD1602S-B1#": out = "% Unknown command.\r\n"
            elif b == "save":
                kirim("\r\nAre you sure to save the configuration? (y/n):")
                self.perintah.append(baris()); out = "Saving current configuration...\r\n--More ( Press 'Q' to quit )--"
            elif self.tolak_onu and b.startswith(f"ont tr069 {self.tolak_onu}"): out = "% ONT does not exist.\r\n"
            elif b == "show current-config":
                out = ("Current configuration:\r\n!\r\nont-line-profile gpon profile-id 0 profile-name line-profile_0\r\n #tcont 0 dba-profile-id 0\r\n"
                       "  commit\r\n exit\r\n!\r\n--More ( Press 'Q' to quit )--")
                kirim("\r\n" + out); f.read(1)
                out = ("#dot1p-mapping-table table-id 0\r\ninterface gpon 0/0\r\n"
                       ' ont add 1 1 sn-auth "ZTEGD33C6275" ont-lineprofile-id 0 ont-srvprofile-id 0\r\n'
                       "  ont tr069 1 1 tr069-management enable\r\n"
                       "  ont tr069 1 1 acs-url http://10.99.0.1:7547  acs-username X acs-password Y\r\n"
                       ' ont add 1 2 sn-auth "ZTEGD5D276C6" mult-srv-profile profile-id 0\r\n'
                       ' ont add 2 1 sn-auth "FHTT9D2674A0" mult-srv-profile profile-id 0\r\n'
                       ' ont add 2 2 sn-auth "ZTEGD08EDDF5" mult-srv-profile profile-id 0\r\n'
                       "exit\r\n!\r\ninterface vlanif 1\r\n ip address 192.168.2.100 24\r\nexit\r\nend\r\n")
            elif b.startswith("show ont info"):
                out = "  Run state            : Online\r\n  Config state         : success\r\n"
            if "--More" in out:
                kirim("\r\n" + out); f.read(1); out = "OK\r\n"
            try:
                kirim("\r\n" + out + prompt)
            except OSError:
                return


def test_olt_kirim_tr069(siap):
    main, o, k, m = siap
    assert o.get("/api/olt/tr069").json()["aktif"] is False
    assert o.post("/api/olt/tr069", json={"onu": [{"pon": "0/0/1", "onu_id": 3}]}).status_code == 503
    olt = FakeOLTTelnet(tolak_onu="1 5")
    main.CONFIG.update(OLT_HOST="127.0.0.1", OLT_TELNET_PORT=olt.port, OLT_TELNET_USER="root", OLT_TELNET_PASS="rahasiaOLT",
                       ACS_ONU_USER="INDESO", ACS_ONU_PASS="Sandi26")
    r = k.post("/api/olt/tr069", json={"onu": [{"pon": "0/0/1", "onu_id": 3}, {"pon": "0/0/1", "onu_id": 5},
                                               {"pon": "0/0/2", "onu_id": 1}]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [h["ok"] for h in d["hasil"]] == [True, False, True] and "does not exist" in d["hasil"][1]["pesan"]
    assert d["tersimpan"] is True
    p = olt.perintah
    assert p[:3] == ["enable", "config", "interface gpon 0/0"]
    assert "ont tr069 1 3 tr069-management enable" in p
    assert "ont tr069 1 3 acs-url http://10.99.0.1:7547 acs-username INDESO acs-password Sandi26" in p
    assert "ont tr069 2 1 inform enable interval 300" in p
    assert not any(x.startswith("ont tr069 1 5 acs-url") for x in p)      # berhenti setelah ditolak
    assert p[-3:] == ["save", "y", "end"]                                 # selalu disimpan, konfirmasi dijawab
    # firmware yang menerima save hanya di mode enable: dicoba ulang setelah end
    olt3 = FakeOLTTelnet(save_di_enable=True)
    main.CONFIG.update(OLT_TELNET_PORT=olt3.port)
    d = k.post("/api/olt/tr069", json={"onu": [{"pon": "0/0/1", "onu_id": 3}]}).json()
    assert d["tersimpan"] is True and olt3.perintah[-5:] == ["save", "end", "save", "y", "end"]
    # alamat aneh ditolak sebelum telnet (tidak bisa menyisipkan perintah)
    assert k.post("/api/olt/tr069", json={"onu": [{"pon": "0/0/1\nreboot", "onu_id": 3}]}).status_code == 400
    assert m.post("/api/olt/tr069", json={"onu": [{"pon": "0/0/1", "onu_id": 3}]}).status_code == 403
    # sandi telnet salah
    olt2 = FakeOLTTelnet()
    main.CONFIG.update(OLT_TELNET_PORT=olt2.port, OLT_TELNET_PASS="salah")
    r = k.post("/api/olt/tr069", json={"onu": [{"pon": "0/0/1", "onu_id": 3}]})
    assert r.status_code == 502 and "ditolak" in r.json()["detail"]



def test_skrip_push_tr069_semua_onu(tmp_path):
    """deploy/indeso/push-tr069.py: baca ONU dari show current-config, lewati yang sudah ada / bukan ZTE,
    kirim, save, lalu cek Config state. Tanpa --kirim tidak mengubah apa pun."""
    import subprocess
    skrip = os.path.join(ROOT, "deploy", "indeso", "push-tr069.py")
    env = dict(os.environ, MIKROTIK_PASSWORD="x", TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHAT_ID="1", OLT_HOST="127.0.0.1",
               OLT_TELNET_USER="root", OLT_TELNET_PASS="rahasiaOLT", ACS_ONU_USER="INDESO", ACS_ONU_PASS="Sandi26",
               GENIEACS_URL="")
    olt = FakeOLTTelnet()
    env["OLT_TELNET_PORT"] = str(olt.port)
    r = subprocess.run([sys.executable, skrip], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "4 ONU di OLT · 2 akan dikirim" in r.stdout and "dilewati (sudah ada)" in r.stdout and "bukan ZTE" in r.stdout
    assert not any(p.startswith("ont tr069") for p in olt.perintah)          # tanpa --kirim: hanya membaca
    olt.perintah.clear()
    r = subprocess.run([sys.executable, skrip, "--kirim"], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    p = olt.perintah
    assert "ont tr069 1 2 acs-url http://10.99.0.1:7547 acs-username INDESO acs-password Sandi26" in p
    assert "ont tr069 2 2 inform enable interval 300" in p
    assert not any(x.startswith("ont tr069 1 1 ") or x.startswith("ont tr069 2 1 ") for x in p)   # sudah ada / Fiberhome
    assert "save" in p and "show ont info 1 2" in p and "show ont info 2 2" in p
    assert "2 dari 2 berhasil" in r.stdout and "TERSIMPAN" in r.stdout and "config success" in r.stdout
    # hanya satu ONU tertentu
    olt.perintah.clear()
    r = subprocess.run([sys.executable, skrip, "--kirim", "--onu", "2/1"], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert "1 akan dikirim" in r.stdout and any(x.startswith("ont tr069 2 1 acs-url") for x in olt.perintah)


def test_ekspor_csv_pelanggan_tagihan_tunggakan(siap):
    main, o, k, m = siap
    o.post("/api/tagihan/buat?periode=2026-09")
    o.post("/api/tagihan/buat?periode=2026-10")
    o.put("/api/data-pelanggan/ani", json={"nama": "=HYPERLINK(\"x\")", "paket_id": 1, "status": "aktif", "atur_secret": False})
    r = k.get("/api/ekspor/pelanggan")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    teks = r.content.decode("utf-8-sig")
    baris = teks.splitlines()
    assert baris[0].startswith("Username;Nama;Alamat;No. HP;Paket")
    import csv as _csv
    ani = next(x for x in _csv.reader(baris, delimiter=";") if x[0] == "ani")
    assert ani[1] == "'=HYPERLINK(\"x\")"                                 # formula dinetralkan
    assert any(b.startswith("budi;") and b.endswith(";2;300000") for b in baris)
    t = k.get("/api/ekspor/tagihan?periode=2026-10").content.decode("utf-8-sig").splitlines()
    assert t[0].startswith("Username;Nama;No. HP;Periode;Jumlah") and len(t) == 4
    assert k.get("/api/ekspor/tagihan?periode=salah").status_code == 400
    g = k.get("/api/ekspor/tunggakan").content.decode("utf-8-sig").splitlines()
    assert g[0].startswith("Username;Nama") and any(";2026-09, 2026-10;" in b for b in g)
    for url in ("/api/ekspor/pelanggan", "/api/ekspor/tagihan?periode=2026-10", "/api/ekspor/tunggakan"):
        assert m.get(url).status_code == 403



def test_prorata_diskon_dan_pasang_baru(siap):
    main, o, k, m = siap
    h = main._hitung_tagihan
    assert h(150000, 0, None, "2026-10") == (150000, "2026-10-05", None)
    assert h(150000, 0, "2026-10-01", "2026-10")[0] == 150000                      # pasang tgl 1 = penuh
    j, jt, cat = h(150000, 0, "2026-10-20", "2026-10")                              # 12 dari 31 hari
    assert j == 58000 and jt == "2026-10-27" and "prorata 12/31" in cat
    assert h(150000, 10000, "2026-10-20", "2026-11") == (140000, "2026-11-05", "diskon Rp 10.000")
    assert h(150000, 0, "2026-11-03", "2026-10") is None                            # belum berlangganan
    assert h(150000, 150000, None, "2026-10") is None                               # diskon penuh
    # diskon & tanggal pasang lewat ubah pelanggan
    assert o.put("/api/data-pelanggan/budi", json={"nama": "Budi", "paket_id": 1, "diskon": 25000, "tgl_pasang": "2026-08-15",
                                                   "atur_secret": False}).status_code == 200
    o.post("/api/tagihan/buat?periode=2026-10")
    tb = next(t for t in o.get("/api/tagihan?periode=2026-10").json()["tagihan"] if t["username"] == "budi")
    assert tb["jumlah"] == 125000 and tb["catatan"] == "diskon Rp 25.000"
    assert o.put("/api/data-pelanggan/budi", json={"nama": "Budi", "paket_id": 1, "diskon": -5, "atur_secret": False}).status_code == 400
    # pasang baru lengkap: akun PPPoE, tagihan prorata, biaya pasang ke keuangan
    hari = datetime.now()
    tgl = hari.strftime("%Y-%m-%d")
    r = k.post("/api/pasang-baru", json={"username": "eko", "nama": "Eko Baru", "no_hp": "0812", "paket_id": 2,
                                         "tgl_pasang": tgl, "buat_secret": True, "password_pppoe": "rahasia9",
                                         "biaya_pasang": 250000, "metode_bayar": "tunai"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["profil"] == "paket-20mbps" and main._mt.secret["eko"]["password"] == "rahasia9"
    assert main._mt.secret["eko"]["service"] == "pppoe"
    if hari.day > 1:
        assert d["tagihan"] and "prorata" in d["tagihan"]["catatan"] and d["tagihan"]["jumlah"] < 200000
    trx = o.get("/api/keuangan/transaksi?periode=" + tgl[:7]).json()["transaksi"]
    assert any(t["jumlah"] == 250000 and "Pasang baru Eko Baru" in (t["keterangan"] or "") for t in trx)
    # ditolak: username sudah ada, secret sudah ada di MikroTik, tanggal masa depan, mitra
    assert k.post("/api/pasang-baru", json={"username": "eko", "nama": "X", "paket_id": 1, "tgl_pasang": tgl}).status_code == 409
    assert k.post("/api/pasang-baru", json={"username": "ani2", "nama": "X", "paket_id": 1, "tgl_pasang": tgl,
                                            "buat_secret": True, "password_pppoe": "a b"}).status_code == 400
    besok = (hari + timedelta(days=1)).strftime("%Y-%m-%d")
    assert k.post("/api/pasang-baru", json={"username": "fani", "nama": "X", "paket_id": 1, "tgl_pasang": besok}).status_code == 400
    assert k.post("/api/pasang-baru", json={"username": "a b", "nama": "X", "paket_id": 1, "tgl_pasang": tgl}).status_code == 400
    assert m.post("/api/pasang-baru", json={"username": "gani", "nama": "X", "paket_id": 1, "tgl_pasang": tgl}).status_code == 403
    main._mt.secret["hadi"] = {"id": "*99", "profile": "default", "disabled": "no"}
    assert k.post("/api/pasang-baru", json={"username": "hadi", "nama": "Hadi", "paket_id": 1, "tgl_pasang": tgl,
                                            "buat_secret": True, "password_pppoe": "rahasia9"}).status_code == 409
    assert k.get("/api/mikrotik/profil").json()["profil"][1] == {"nama": "paket-10mbps", "mbps": 10.0}


def test_perangkat_wifi_dicocokkan_dengan_mac_tersambung(app):
    """Fiberhome: tabel host (DHCP) masih 'aktif' untuk HP yang sudah pergi; yang dihitung tersambung
    hanya MAC yang ada di daftar AssociatedDevice WiFi."""
    main = app
    v = lambda x: {"_value": x}
    igd = {"LANDevice": {"1": {
        "WLANConfiguration": {
            "1": {"SSID": v("Awas ada JULE"), "Enable": v(True), "TotalAssociations": v("0"), "AssociatedDevice": {}},
            "5": {"SSID": v("KANJENG MAMI"), "Enable": v(True), "TotalAssociations": v("1"),
                  "AssociatedDevice": {"1": {"AssociatedDeviceMACAddress": v("2e:77:f7:b0:08:08")}}}},
        "Hosts": {"Host": {
            "1": {"HostName": v("realme-C11"), "IPAddress": v("192.168.1.2"), "MACAddress": v("9A:0F:D3:C1:45:29"),
                  "Active": v(True), "InterfaceType": v("802.11")},
            "2": {"HostName": v("OPPO-A57"), "IPAddress": v("192.168.1.9"), "MACAddress": v("2E:77:F7:B0:08:08"),
                  "Active": v(True), "InterfaceType": v("802.11")},
            "3": {"HostName": v("PC"), "IPAddress": v("192.168.1.5"), "MACAddress": v("AA:AA:AA:AA:AA:AA"),
                  "Active": v(True), "InterfaceType": v("Ethernet")}}}}}}
    r = main._ringkas_perangkat({"_id": "x", "_deviceId": {}, "InternetGatewayDevice": igd})
    per = {h["nama"]: h for h in r["perangkat"]}
    assert per["OPPO-A57"]["aktif"] and per["OPPO-A57"]["wifi"] == "KANJENG MAMI"
    assert not per["realme-C11"]["aktif"] and "tidak sedang tersambung" in per["realme-C11"]["catatan"]
    assert per["PC"]["aktif"]                                  # kabel tidak terpengaruh
    assert r["perangkat_aktif"] == 2 and r["perangkat_wifi"] == 1
    # ONU yang tidak memberi daftar MAC WiFi: tetap memakai status dari tabel host
    for w in igd["LANDevice"]["1"]["WLANConfiguration"].values():
        w["AssociatedDevice"] = {}
    r = main._ringkas_perangkat({"_id": "x", "_deviceId": {}, "InternetGatewayDevice": igd})
    assert r["perangkat_aktif"] == 3


def test_kunci_komisi_mitra(siap):
    """Komisi = hasil kerja bulan X (dibayar awal X+1); terkunci saat tutup buku X atau manual oleh owner."""
    main, o, k, m = siap
    o.post("/api/tagihan/buat?periode=2026-09")
    tg = {t["username"]: t["id"] for t in o.get("/api/tagihan?periode=2026-09").json()["tagihan"]}
    o.post(f"/api/tagihan/{tg['budi']}/lunas", json={"tanggal": "2026-09-05", "metode": "tunai"})
    assert m.get("/api/mitra/pelanggan?periode=2026-09").json()["insentif"]["total"] == 10000
    # owner kunci manual dengan angka disepakati; admin & mitra tidak boleh
    assert k.post("/api/keuangan/insentif/2026-09/kunci", json={"total": 260000}).status_code == 403
    assert o.post("/api/keuangan/insentif/2999-01/kunci", json={}).status_code == 400
    assert o.post("/api/keuangan/insentif/2026-09/kunci", json={"total": 260000}).json()["total"] == 260000
    # pembayaran susulan tidak mengubah komisi yang sudah dikunci
    o.post(f"/api/tagihan/{tg['ani']}/lunas", json={"tanggal": "2026-09-20", "metode": "tunai"})
    d = m.get("/api/mitra/pelanggan?periode=2026-09").json()["insentif"]
    assert d["total"] == 260000 and d["final"] is True and d["dibayar_bulan"] == "2026-10" and "pelanggan" not in d
    rk = next(b for b in o.get("/api/keuangan/insentif?sampai=2026-10").json()["rekap"] if b["periode"] == "2026-09")
    assert rk["terhitung"] == 260000 and rk["terkunci"]["sumber"] == "manual"
    # buka kunci -> dihitung ulang (budi + ani lunas = 20.000)
    assert o.delete("/api/keuangan/insentif/2026-09/kunci").status_code == 200
    assert m.get("/api/mitra/pelanggan?periode=2026-09").json()["insentif"]["total"] == 20000
    # tutup buku mengunci otomatis; pembayaran susulan setelahnya tidak menambah; buka buku melepas kunci otomatis
    assert o.post("/api/keuangan/tutup-buku/2026-09", json={"catatan": ""}).status_code == 200
    o.post(f"/api/tagihan/{tg['dwi']}/lunas", json={"tanggal": "2026-10-02", "metode": "tunai"})
    ins = m.get("/api/mitra/pelanggan?periode=2026-09").json()["insentif"]
    assert ins["total"] == 20000 and ins["final"]
    assert o.post("/api/keuangan/tutup-buku/2026-09/buka", json={"alasan": "koreksi"}).status_code == 200
    assert m.get("/api/mitra/pelanggan?periode=2026-09").json()["insentif"]["total"] == 30000


def test_komisi_bulan_sebelum_penagihan(siap):
    """Data nyata: Penagihan baru dipakai (hanya ada tagihan bulan depan), transaksi keuangan sudah sejak
    September. Daftar komisi tetap menampilkan September s/d bulan tagihan; tutup buku bulan tanpa
    tagihan tidak mengunci di Rp0; owner kunci manual Rp260.000."""
    main, o, k, m = siap
    kat = _kat(o)
    assert k.post("/api/keuangan/transaksi", json={"tanggal": "2026-09-03", "jenis": "masuk", "kategori_id": kat["Setoran modal"],
                                                     "jumlah": 1_000_000}).status_code == 200
    o.post("/api/tagihan/buat?periode=2026-11")
    rk = {b["periode"]: b for b in o.get("/api/keuangan/insentif?sampai=2026-10").json()["rekap"]}
    assert {"2026-09", "2026-10", "2026-11"} <= set(rk)
    assert rk["2026-09"]["ada_tagihan"] is False and rk["2026-11"]["ada_tagihan"] is True
    assert o.post("/api/keuangan/tutup-buku/2026-09", json={"catatan": ""}).status_code == 200
    rk = {b["periode"]: b for b in o.get("/api/keuangan/insentif?sampai=2026-10").json()["rekap"]}
    assert rk["2026-09"]["terkunci"] is None                       # tidak terkunci otomatis di Rp0
    assert o.post("/api/keuangan/insentif/2026-09/kunci", json={"total": 260000}).status_code == 200
    rk = {b["periode"]: b for b in o.get("/api/keuangan/insentif?sampai=2026-10").json()["rekap"]}
    assert rk["2026-09"]["terhitung"] == 260000 and rk["2026-09"]["sisa"] == 260000


def test_komisi_mulai_bulan(siap):
    main, o, k, m = siap
    kat = _kat(o)
    k.post("/api/keuangan/transaksi", json={"tanggal": "2026-08-03", "jenis": "masuk", "kategori_id": kat["Setoran modal"], "jumlah": 5000})
    o.post("/api/tagihan/buat?periode=2026-11")
    per = lambda: [b["periode"] for b in o.get("/api/keuangan/insentif?sampai=2026-10").json()["rekap"]]
    assert "2026-08" in per()
    assert k.post("/api/keuangan/insentif/2026-10/mulai").status_code == 403
    assert o.post("/api/keuangan/insentif/2026-10/mulai").status_code == 200
    assert per() == ["2026-11", "2026-10"]
    o.post("/api/keuangan/insentif/2026-10/kunci", json={"total": 260000})
    d = o.get("/api/keuangan/insentif?sampai=2026-10").json()
    assert d["ringkasan"]["tertunggak"] == 260000 + next(b["sisa"] for b in d["rekap"] if b["periode"] == "2026-11")
    # tersimpan permanen & bisa dikosongkan lagi lewat Pengaturan
    main.CONFIG["KOMISI_MULAI"] = ""
    main._muat_pengaturan()
    assert main.CONFIG["KOMISI_MULAI"] == "2026-10"
    assert o.put("/api/pengaturan", json={"nilai": {"KOMISI_MULAI": "2026-13"}}).status_code == 400
    assert o.put("/api/pengaturan", json={"nilai": {"KOMISI_MULAI": ""}}).status_code == 200
    assert "2026-08" in per()


def test_acs_ping_tertahan_dibersihkan_berkala_dan_pesan_jelas(siap, monkeypatch):
    """Ping yang ditolak '9002 Internal error' pada objek IPPingDiagnostics. (ONU sibuk) dihapus berkala
    bila tertahan > 10 menit, dan pesan gagal menyebut bagian & alasannya."""
    main, o, k, m = siap
    acs = FakeACS()
    main.CONFIG["GENIEACS_URL"] = "http://acs.test/nbi"
    monkeypatch.setattr(main.requests, "request", acs.request)
    detail = {"faultCode": "9003", "faultString": "Invalid arguments", "setParameterValuesFault": [
        {"parameterName": "InternetGatewayDevice.IPPingDiagnostics.", "faultCode": "9002", "faultString": "Internal error"}]}
    assert main._rincian_fault(detail) == ["IPPingDiagnostics: Internal error (9002)"]
    lama, baru = "c" * 24, "d" * 24
    jam_lalu = (datetime.utcnow() - timedelta(hours=1)).isoformat() + "Z"
    pv = [["InternetGatewayDevice.IPPingDiagnostics.DiagnosticsState", "Requested", "xsd:string"]]
    acs.antrean[lama] = {"name": "setParameterValues", "parameterValues": pv, "device": "DEV1", "timestamp": jam_lalu}
    acs.antrean[baru] = {"name": "setParameterValues", "parameterValues": pv, "device": "DEV1",
                         "timestamp": datetime.utcnow().isoformat() + "Z"}
    acs.antrean["e" * 24] = {"name": "reboot", "device": "DEV1", "timestamp": jam_lalu}
    acs.faults.append({"_id": f"DEV1:task_{lama}", "channel": f"task_{lama}", "device": "DEV1", "code": "cwmp.9003",
                       "message": "Invalid arguments", "detail": detail})
    main._bersih_ping["terakhir"] = None
    assert main.proses_bersih_ping_tertahan() == 1
    assert lama not in acs.antrean and baru in acs.antrean and "e" * 24 in acs.antrean and not acs.faults
    assert main.proses_bersih_ping_tertahan() == 0            # dibatasi tiap 10 menit
    # pesan gagal saat tugas ditolak
    acs.faults.append({"_id": f"DEV1:task_{baru}", "channel": f"task_{baru}", "device": "DEV1", "code": "cwmp.9003",
                       "message": "Invalid arguments", "detail": detail})
    st = main._status_tugas("DEV1", baru)
    assert st["status"] == "gagal" and st["sibuk"] and "IPPingDiagnostics: Internal error (9002)" in st["pesan"]
    assert "Reboot ONU" in st["pesan"]



def test_cek_dns_mtu_mikrotik(siap):
    main, o, k, m = siap
    mt = main._mt
    mt.dns = {"servers": "", "dynamic-servers": "", "allow-remote-requests": "true"}
    mt.profil_detail = {"default": {"change-tcp-mss": "yes"},
                        "paket-10mbps": {"local-address": "10.10.0.1", "change-tcp-mss": "default"},
                        "paket-20mbps": {"dns-server": "8.8.8.8", "change-tcp-mss": "no"}}
    mt.pppoe_server = [{"service-name": "desa", "interface": "ether2", "max-mtu": "1500"}]
    d = k.get("/api/mikrotik/cek-dns-mtu").json()
    judul = {c["judul"]: c for c in d["cek"]}
    assert judul["MikroTik tidak punya server DNS"]["perintah"] == "/ip dns set servers=8.8.8.8,1.1.1.1"
    assert "Uji DNS dari MikroTik gagal" in judul
    p10 = judul["Profil 'paket-10mbps' tidak membagikan DNS (2 pelanggan)"]
    assert p10["perintah"] == '/ppp profile set [find name="paket-10mbps"] dns-server=10.10.0.1'
    assert "Profil 'paket-20mbps': penyesuaian MSS mati (1 pelanggan)" in judul
    assert not any("paket-10mbps': penyesuaian" in j for j in judul)        # default profil = yes
    assert "max-mtu=1480" in judul["MTU server PPPoE 'desa' 1500"]["perintah"]
    assert d["masalah"] >= 5
    # setelah diperbaiki: normal
    mt.dns = {"servers": "8.8.8.8", "allow-remote-requests": "true"}
    mt.resolve = "142.250.4.100"
    mt.profil_detail["paket-10mbps"]["dns-server"] = "10.10.0.1"
    mt.profil_detail["paket-20mbps"]["change-tcp-mss"] = "yes"
    mt.profil_detail["default"]["dns-server"] = "8.8.8.8"
    mt.pppoe_server[0]["max-mtu"] = "1480"
    d = k.get("/api/mikrotik/cek-dns-mtu").json()
    assert d["masalah"] == 0, d
    assert m.get("/api/mikrotik/cek-dns-mtu").status_code == 403


def test_acs_ping_gagal_langsung_dihapus_tanpa_ulang(siap, monkeypatch):
    """Ping yang ditolak ONU langsung dihapus (tugas + fault) supaya tidak menumpuk; ping berikutnya
    untuk ONU itu otomatis memakai cara tanpa memilih WAN."""
    main, o, k, m = siap
    acs = FakeACS()
    main.CONFIG["GENIEACS_URL"] = "http://acs.test/nbi"
    monkeypatch.setattr(main.requests, "request", acs.request)
    dev = acs.dev["ZTEGC1234567"]
    dev["InternetGatewayDevice"]["IPPingDiagnostics"] = {"DiagnosticsState": {"_value": "None"},
                                                         "Interface": {"_value": ""}}
    main._ping_tanpa_wan.discard(dev["_id"])
    acs.antre = True
    r = k.post("/api/acs/budi/ping", json={}).json()
    tid = r["tugas_id"]
    assert r["lewat_wan"] and tid in acs.antrean
    acs.faults.append({"_id": f"{dev['_id']}:task_{tid}", "channel": f"task_{tid}", "device": dev["_id"],
                       "code": "cwmp.9003", "message": "Invalid arguments"})
    h = k.get(f"/api/acs/budi/ping?mulai={r['mulai']}&tugas={tid}").json()
    assert h["status"] == "gagal" and h["dihapus"] and h["cara_berikutnya"] == "tanpa_wan"
    assert tid not in acs.antrean and not acs.faults
    jumlah = len(acs.tugas)
    r2 = k.post("/api/acs/budi/ping", json={}).json()          # hanya bila diminta lagi
    assert len(acs.tugas) == jumlah + 1 and not r2["lewat_wan"]
    assert "Interface" not in {p[0].rsplit(".", 1)[-1] for p in acs.tugas[-1]["parameterValues"]}
    main._ping_tanpa_wan.discard(dev["_id"])


def test_olt_tr069_daftar_cocok_lewat_pppoe(siap, monkeypatch):
    """ONU yang melapor ke ACS dengan nomor seri berbeda dari SN di OLT tetap terbaca 'sudah di ACS'
    lewat username PPPoE-nya."""
    main, o, k, m = siap
    acs = FakeACS()
    main.CONFIG["GENIEACS_URL"] = "http://acs.test/nbi"
    monkeypatch.setattr(main.requests, "request", acs.request)
    acs.dev["ZTEGC1234567"]["_deviceId"]["_SerialNumber"] = "ZTEEQJNQ1F19726"     # SN versi TR-069
    onu = [main.ONUStatus(pon="0/0/1", onu_id=1, snmp_index=1, olt_rx_dbm=-20.0, online=True, status="OK",
                          serial_number="ZTEGC1234567", pppoe_username="budi"),
           main.ONUStatus(pon="0/0/1", onu_id=2, snmp_index=2, olt_rx_dbm=-20.0, online=True, status="OK",
                          serial_number="ZTEGD0000002", pppoe_username="ani")]
    main._latest_data = main._build_dashboard([], None, onu)
    try:
        d = {x["onu_id"]: x["terdaftar_acs"] for x in k.get("/api/olt/tr069").json()["onu"]}
    finally:
        main._latest_data = None
    assert d == {1: True, 2: False}


def test_sambung_ulang_dihitung_per_sesi(siap):
    """Satu sambung ulang yang terekam di dua snapshot (uptime 2 & 7 menit) dihitung sekali."""
    main, o, k, m = siap
    t = datetime(2026, 10, 19, 10, 0)
    baris = [("budi", (t + timedelta(minutes=2)).isoformat(), 120), ("budi", (t + timedelta(minutes=7)).isoformat(), 420),
             ("budi", (t + timedelta(hours=3, minutes=1)).isoformat(), 60), ("ani", t.isoformat(), 30)]
    assert main._hitung_sambung_ulang(baris) == {"budi": 2, "ani": 1}


def test_alert_putus_jenis_dan_alasan(siap, monkeypatch):
    """Listrik ONU mati: senyap di siang hari, tidak dikabarkan di jam malam (masuk laporan harian).
    Putus lain menyertakan alasan dari log PPPoE MikroTik."""
    main, o, k, m = siap
    main.CONFIG.update(PUTUS_TUNGGU_MENIT=10, PUTUS_MASSAL_MIN=5, PUTUS_SENYAP_MALAM="21-06")
    data = main._data_pelanggan()
    a, b = sorted(data)[:2]
    for x in (main._putus_aktif, main._pernah_aktif, main._peta_onu):
        x.clear()
    main._listrik_malam.clear()
    monkeypatch.setattr(main, "_putus_siap", True)
    main._pernah_aktif.update({a, b})

    def onu(i, user, online, ev=None, waktu=None):
        return main.ONUStatus(pon="0/0/1", onu_id=i, snmp_index=i, olt_rx_dbm=-20.0, online=online, status="OK",
                              pppoe_username=user, last_event=ev, last_event_time=waktu and waktu.strftime("%Y-%m-%d %H:%M:%S"))

    def jalan(t0):
        main._terkirim.clear()
        main._putus_aktif.clear()
        onus = [onu(1, a, False, "DyingGasp", t0), onu(2, b, True)]
        main.simpan_event([main._ev("mikrotik", "info", "pppoe", "PPPoE terputus",
                                    f"<pppoe-{b}>: terminating... - peer is not responding [pppoe,ppp,info]",
                                    pelanggan=b, waktu=t0.strftime("%Y-%m-%d %H:%M:%S"))])
        main.proses_putus_pelanggan([], None, onus, t0)
        main.proses_putus_pelanggan([], None, onus, t0 + timedelta(minutes=11))
        return [x["data"] for x in main._terkirim]

    siang = jalan(datetime(2026, 10, 19, 14, 0))
    listrik = [d for d in siang if "Listrik ONU mati" in d["text"]]
    putus = [d for d in siang if "PUTUS lebih dari" in d["text"]]
    assert len(listrik) == 1 and listrik[0].get("disable_notification") == "true"
    assert len(putus) == 1 and "ONU tidak menjawab MikroTik" in putus[0]["text"] and "disable_notification" not in putus[0]

    malam = jalan(datetime(2026, 10, 19, 23, 0))
    assert not any("Listrik ONU mati" in d["text"] for d in malam)
    assert [u for u, _ in main._listrik_malam] == [a]
    assert "Listrik ONU mati di jam malam (1)" in main._build_daily_report_text()
    main._terkirim.clear()
    main.proses_putus_pelanggan([main.PPPoEUser(username=a, ip_address="10.0.0.9", uptime="1m", uptime_seconds=60),
                                 main.PPPoEUser(username=b, ip_address="10.0.0.8", uptime="1m", uptime_seconds=60)],
                                None, [], datetime(2026, 10, 20, 6, 0))
    kembali = [x["data"]["text"] for x in main._terkirim]
    assert len(kembali) == 1 and b in kembali[0]           # yang senyap tidak dikabari kembali
    for x in (main._putus_aktif, main._pernah_aktif, main._peta_onu):
        x.clear()
    main._listrik_malam.clear()
