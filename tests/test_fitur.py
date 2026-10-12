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

    def call(self, perintah, arg=None, *a, **k):
        if perintah == "monitor-traffic":
            return [dict(self.mt.traffic)] if getattr(self.mt, "traffic", None) else []
        if perintah == "ping":
            return list(getattr(self.mt, "ping", []))
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
    assert rk["Pembelian perangkat (ONT, router, kabel, dll.)"] == 1_200_000
    # bunga 60.000 diakui 20.000 per cicilan, di bulan jatuh tempo masing-masing (akrual)
    jadwal_bln = [c["jatuh_tempo"][:7] for c in u["jadwal"]]
    for p in sorted(set(jadwal_bln)):
        rkp = {x["nama"]: x["total"] for x in o.get(f"/api/keuangan/ringkasan?periode={p}").json()["per_kategori"]}
        assert rkp.get("Bunga & biaya cicilan", 0) == 20_000 * jadwal_bln.count(p)
    if per_beli not in jadwal_bln:
        assert "Bunga & biaya cicilan" not in rk
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
    kid = kat["Pembelian perangkat (ONT, router, kabel, dll.)"]
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
    trx(lalu, "masuk", "Setoran modal pemilik", 2_000_000)
    trx(lalu, "keluar", "Listrik", 300_000)
    trx(tgl, "masuk", "Biaya pemasangan baru", 150_000)
    trx(tgl, "keluar", "Listrik", 100_000)
    assert m.get("/api/keuangan/keseluruhan").status_code == 403
    d = k.get("/api/keuangan/keseluruhan").json()
    assert d["mulai"] == lalu[:7] and d["sampai"] == tgl[:7] and d["jumlah_bulan"] >= 2
    assert d["total"]["modal_masuk"] == 2_000_000 and d["total"]["beban"] == 400_000
    assert d["total"]["laba"] == 150_000 - 400_000
    assert d["saldo_kas"] == 2_000_000 - 300_000 + 150_000 - 100_000 == d["bulanan"][-1]["saldo_akhir"]
    assert d["bulanan"][-1]["laba_kumulatif"] == d["total"]["laba"]
    assert {x["nama"]: x["total"] for x in d["per_kategori"]}["Listrik"] == 400_000
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
    k.post("/api/keuangan/transaksi", json={"tanggal": hari, "jenis": "keluar", "kategori_id": kat["Listrik"], "jumlah": 100_000})
    k.post("/api/keuangan/utang", json={"tanggal": hari, "pemasok": "Toko Fiber", "jumlah": 500_000,
                                        "kategori_id": kat["Pembelian perangkat (ONT, router, kabel, dll.)"]})
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
    kp = kat["Pembelian perangkat (ONT, router, kabel, dll.)"]
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
    a = k.post("/api/keuangan/transaksi", json={"tanggal": tgl, "jenis": "masuk", "kategori_id": kat["Setoran modal pemilik"], "jumlah": 5_000_000}).json()
    b = k.post("/api/keuangan/transaksi", json={"tanggal": tgl, "jenis": "keluar", "kategori_id": kat["Listrik"], "jumlah": 200_000}).json()
    c = k.post("/api/keuangan/transaksi", json={"tanggal": tgl, "jenis": "keluar", "kategori_id": kat["Listrik"], "jumlah": 50_000}).json()
    yymm = per[2:4] + per[5:7]
    assert a["nomor"] == f"BKM-{yymm}-0001" and b["nomor"] == f"BKK-{yymm}-0001" and c["nomor"] == f"BKK-{yymm}-0002"
    k.post("/api/keuangan/utang", json={"tanggal": tgl, "pemasok": "Toko", "kategori_id": kat["Jasa & perbaikan"],
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
    body = {"tanggal": tgl, "jenis": "keluar", "kategori_id": kat["Listrik"], "jumlah": 1000}
    assert k.post("/api/keuangan/transaksi", json=body).status_code == 409
    assert o.post("/api/keuangan/transaksi", json=body).status_code == 409          # owner pun harus buka buku dulu
    assert k.put(f"/api/keuangan/transaksi/{b['id']}", json={**body, "jumlah": 300_000}).status_code == 409
    assert k.post(f"/api/keuangan/transaksi/{b['id']}/batal", json={"alasan": "salah"}).status_code == 409
    assert k.post(f"/api/tagihan/{t['id']}/batal", json={"alasan": "salah input"}).status_code == 409
    assert k.post("/api/keuangan/utang", json={"tanggal": tgl, "pemasok": "X", "kategori_id": kat["Listrik"], "jumlah": 1}).status_code == 409
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
    a = k.post("/api/keuangan/transaksi", json={"tanggal": tgl, "jenis": "keluar", "kategori_id": kat["Listrik"], "jumlah": 1000}).json()["id"]
    b = k.post("/api/keuangan/transaksi", json={"tanggal": lama, "jenis": "keluar", "kategori_id": kat["Listrik"], "jumlah": 2000}).json()["id"]
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
