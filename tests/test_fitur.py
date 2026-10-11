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
            rows = [{"name": p} for p in self.mt.profiles]
        elif self.path == "/log":
            rows = list(self.mt.log)
        elif self.path == "/system/resource":
            rows = [dict(self.mt.resource)]
        else:
            rows = []
        if "name" in kw:
            rows = [r for r in rows if r.get("name") == kw["name"]]
        return rows

    def set(self, id, **kw):
        for v in self.mt.secret.values():
            if v["id"] == id:
                v.update(kw)

    def remove(self, id):
        for k in list(self.mt.active):
            self.mt.active[k] = [x for x in self.mt.active[k] if x["id"] != id]

    def call(self, *a, **k):
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
        self._d = data or {"ok": ok, "result": []}
        self.text = str(self._d)

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
              main.init_keuangan_db, main.init_nota_db, main._muat_pengaturan):
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
    assert m.get("/api/mitra/pelanggan?periode=2026-10").json()["insentif"]["total"] == 30000
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
    trx(o, "masuk", "Setoran modal pemilik", 1_000_000)
    t1 = trx(k, "keluar", "Listrik", 200_000)["id"]
    trx(o, "masuk", "Biaya pemasangan baru", 100_000)
    assert k.post("/api/keuangan/transaksi", json={"tanggal": "2099-01-01", "jenis": "keluar",
                                                    "kategori_id": kat["Listrik"], "jumlah": 1}).status_code == 400
    assert k.post("/api/keuangan/transaksi", json={"tanggal": hari, "jenis": "keluar",
                                                    "kategori_id": kat["Insentif mitra"], "jumlah": 1}).status_code == 400
    # utang kredit
    u = k.post("/api/keuangan/utang", json={"tanggal": hari, "pemasok": "Toko A", "jumlah": 600_000, "uang_muka": 100_000,
                                            "kategori_id": kat["Pembelian perangkat (ONT, router, kabel, dll.)"]}).json()["id"]
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
    tx = o.post("/api/keuangan/transaksi", json={"tanggal": hari, "jenis": "keluar", "kategori_id": kat["Listrik"],
                                                 "jumlah": 5000, "keterangan": "=HYPERLINK(1)"})
    assert tx.status_code == 200
    csv = o.get(f"/api/keuangan/ekspor?periode={per}").text
    assert "'=HYPERLINK(1)" in csv and "kredit (utang)" in csv


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
                                                  "kategori_id": kat["Listrik"], "jumlah": 150000}).json()["id"]
    o.post(f"/api/keuangan/transaksi/{tid}/nota", content=b"%PDF-1.4 x", headers={"Content-Type": "application/pdf"})
    main._terkirim.clear()
    zip_isi = []
    asli = main.kirim_dokumen_telegram

    def tangkap(path, caption, chat_id=None):
        if path.endswith(".zip"):
            zip_isi.append(zipfile.ZipFile(io.BytesIO(open(path, "rb").read())).namelist())
        return asli(path, caption, chat_id)
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
            "kategori_id": kat["Pembelian perangkat (ONT, router, kabel, dll.)"], "jumlah": 1_200_000,
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
    assert rk["Pembelian perangkat (ONT, router, kabel, dll.)"] == 1_200_000 and rk["Bunga & biaya cicilan"] == 60_000
    # kas hanya berkurang sebesar cicilan yang dibayar
    rs = o.get(f"/api/keuangan/ringkasan?periode={per}").json()
    assert rs["bayar_utang"] == 420_000
    # tanggal akhir bulan
    assert main._tambah_bulan("2026-01-31", 1) == "2026-02-28" and main._tambah_bulan("2026-12-15", 1) == "2027-01-15"
