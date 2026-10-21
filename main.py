"""
Dashboard Monitoring ISP - Backend
FastAPI + routeros-api (MikroTik) + snmpwalk binary (OLT C-Data) + Telegram Alert
Didesain untuk berjalan 24/7 di Termux (Android) sebagai local server.

Jalankan dengan:
    uvicorn main:app --host 0.0.0.0 --port 8000

CATATAN OID (hasil investigasi manual + validasi silang ke web GUI OLT):
- OLT-RX (daya optik yang OLT terima dari ONU — indikator utama kondisi fiber
  pelanggan, dipakai untuk alert): 17409.2.3.3.6.1.2.<snmp_index>
- ONU-RX/TX/bias/voltage/temperature (parameter DOM sisi ONU):
  17409.2.8.4.4.1.<field>.<snmp_index>.<idx2>.<idx3>
  field: 4=RX 5=TX 6=bias 7=voltage 8=temperature
- Lokasi (PON/ONU ID): 17409.2.8.4.1.1.2   | Vendor: .5   | Model: .6
- Firmware: 17409.2.8.4.2.1.2   | Last event: .103   | Event time: .102
- MAC address per ONU: 17409.2.3.2.4.2.1.4.6.<b1>.<b2>.<b3>.<b4>.<b5>.<b6>.1
  = Gauge32: <snmp_index>  (dipetakan terbalik: value -> snmp_index, key -> MAC)
- Semua nilai optik/numerik di-scale x100 (dibagi 100 untuk dapat dBm/V/°C asli)

Script TIDAK butuh OLT_ONU_COUNT manual — daftar ONU di-auto-discover dari OLT
setiap siklus (walk ke subtree lokasi), otomatis ikut kalau ada ONU baru
ditambah/dicabut tanpa perlu edit config.

MAC address ONU dicocokkan ke caller-id PPPoE active MikroTik, jadi dashboard
bisa langsung tunjukin "ONU ini dipakai pelanggan siapa" tanpa perlu tabel
mapping manual.
"""

import asyncio
import csv
import io
import hashlib
import json
import logging
import os
import secrets
import sys
import re
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Optional

from html import escape as _esc

import requests
from dotenv import load_dotenv

load_dotenv()
import routeros_api
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

# ============================================================
# 1. KONFIGURASI — SESUAIKAN DENGAN JARINGAN ANDA
# ============================================================
# Rahasia (password, token) dan alamat perangkat dibaca dari berkas .env
# (lihat .env.example). Nilai lain cukup diubah langsung di bawah.

# Semua berkas dirujuk dari folder main.py, bukan folder tempat perintah dijalankan,
# supaya aman bila uvicorn dijalankan dari folder lain (mis. skrip autostart Termux).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _env(nama: str, default: str) -> str:
    """Baris kosong di .env (mis. OLT_SNMP_COMMUNITY=) dianggap belum diisi, bukan string kosong."""
    return os.environ.get(nama, "").strip() or default


def _env_wajib(nama: str) -> str:
    nilai = os.environ.get(nama, "").strip()
    if not nilai:
        sys.exit(f"Variabel {nama} belum diisi. Salin .env.example menjadi .env lalu lengkapi isinya.")
    return nilai


CONFIG = {
    # --- MikroTik RouterOS API ---
    "MIKROTIK_HOST": _env("MIKROTIK_HOST", "192.168.50.1"),
    "MIKROTIK_PORT": int(_env("MIKROTIK_PORT", "8728")),
    "MIKROTIK_USER": _env("MIKROTIK_USER", "INDESO"),
    "MIKROTIK_PASSWORD": _env_wajib("MIKROTIK_PASSWORD"),
    "MIKROTIK_USE_SSL": False,

    # --- OLT C-Data via SNMP ---
    "OLT_HOST": _env("OLT_HOST", "192.168.2.100"),   # subnet Wilayah 2, tervalidasi lewat snmpwalk manual
    "OLT_SNMP_PORT": int(_env("OLT_SNMP_PORT", "161")),
    "OLT_SNMP_COMMUNITY": _env("OLT_SNMP_COMMUNITY", "public"),
    "OLT_SNMP_VERSION": "2c",      # dipakai langsung sebagai flag -v snmpwalk

    # --- OID (tervalidasi manual via snmpwalk + cross-check web GUI OLT) ---
    "OID_LOCATION": "1.3.6.1.4.1.17409.2.8.4.1.1.2",
    "OID_VENDOR": "1.3.6.1.4.1.17409.2.8.4.1.1.5",
    "OID_MODEL": "1.3.6.1.4.1.17409.2.8.4.1.1.6",
    "OID_SN_HEX": "1.3.6.1.4.1.17409.2.8.4.1.1.3",
    "OID_EVENT_TIME": "1.3.6.1.4.1.17409.2.8.4.1.1.102",
    "OID_EVENT": "1.3.6.1.4.1.17409.2.8.4.1.1.103",
    "OID_FIRMWARE": "1.3.6.1.4.1.17409.2.8.4.2.1.2",
    "OID_OLT_RX": "1.3.6.1.4.1.17409.2.3.3.6.1.2",     # redaman OLT<-ONU, metrik utama alert
    "OID_ONU_OPTICAL": "1.3.6.1.4.1.17409.2.8.4.4.1",  # field 4=RX 5=TX 6=bias 7=volt 8=temp
    "OID_MAC_TABLE": "1.3.6.1.4.1.17409.2.3.2.4.2.1.4.6",

    # --- Threshold Alert (berlaku untuk OLT-RX, bukan ONU-RX) ---
    "RX_POWER_CRITICAL_DBM": -27.0,
    "RX_POWER_WARNING_DBM": -23.0,

    # --- Threshold Hardware Health (voltage/bias/temperature) ---
    # Ini TIDAK memicu notifikasi Telegram — cuma menandai status di data
    # sebagai "PERLU_CEK", buat ditampilkan (mis. warna merah) kalau nanti
    # ada dashboard visual. Kalau ada satu yang keluar dari rentang ini,
    # indikasi hardware ONT mulai bermasalah, bukan soal redaman fiber.
    "VOLTAGE_MIN_V": 3.0,
    "VOLTAGE_MAX_V": 3.6,
    "BIAS_MAX_MA": 50.0,
    "TEMP_MAX_C": 70.0,

    # --- Telegram Bot ---
    "TELEGRAM_BOT_TOKEN": _env_wajib("TELEGRAM_BOT_TOKEN"),
    "TELEGRAM_CHAT_ID": _env_wajib("TELEGRAM_CHAT_ID"),
    "TELEGRAM_ENABLED": True,
    "TELEGRAM_POLL_INTERVAL_SECONDS": 3,   # seberapa sering cek perintah masuk
    # Chat pribadi owner untuk laporan keuangan (pisahkan dari grup operasional).
    # Bisa beberapa, dipisah koma. Bisa juga diubah owner dari Pengaturan -> Sistem.
    "TELEGRAM_OWNER_CHAT_ID": os.environ.get("TELEGRAM_OWNER_CHAT_ID", "").strip(),
    "LAPORAN_BULANAN_AKTIF": "ya",
    "LAPORAN_BULANAN_JAM": 8,          # tanggal 1, setelah jam ini, laporan bulan lalu dikirim

    # --- Login. Akun dikelola dari menu Pengaturan (tersimpan di billing.db).
    # Isian akun di .env (DASHBOARD_*, MITRA_USERS) hanya diimpor SEKALI saat
    # tabel akun masih kosong, untuk peralihan dari versi lama. ---
    "SESI_IDLE_JAM": 12,       # sesi berakhir bila tidak dipakai selama ini
    "SESI_MAKS_HARI": 7,       # dan paling lama sekian hari sejak login
    "DASHBOARD_USER": _env("DASHBOARD_USER", "admin"),
    "DASHBOARD_PASSWORD": os.environ.get("DASHBOARD_PASSWORD", ""),
    "DASHBOARD_USERS": os.environ.get("DASHBOARD_USERS", ""),
    "MITRA_USERS": os.environ.get("MITRA_USERS", ""),
    "MITRA_INSENTIF_PER_PELANGGAN": int(_env("MITRA_INSENTIF_PER_PELANGGAN", "10000")),

    # --- History Database (SQLite) ---
    # Folder data (database, backup, nota). Default: folder main.py; bisa diganti
    # lewat INDESO_DATA_DIR (dipakai juga oleh tes otomatis agar data asli tak tersentuh).
    "HISTORY_DB_PATH": os.path.join(_env("INDESO_DATA_DIR", BASE_DIR), "isp_history.db"),
    "BILLING_DB_PATH": os.path.join(_env("INDESO_DATA_DIR", BASE_DIR), "billing.db"),
    "JATUH_TEMPO_TANGGAL": 5,
    "TAGIHAN_JAM": 8,                       # otomasi tagihan baru jalan setelah jam ini
    "TAGIHAN_PENGINGAT_HARI": [3, 5, 10],   # tanggal kirim daftar belum bayar ke Telegram
    "ISOLIR_MODE": "aktif",            # "aktif" = isolir sungguhan, "simulasi" = hanya kirim daftar ke Telegram
    "ISOLIR_PROFILE": "ISOLIR",
    "ISOLIR_JAM": 8,                   # isolir dijalankan setelah jam ini
    "ISOLIR_TOLERANSI_HARI": 0,        # 0 = isolir mulai sehari setelah jatuh tempo, 2 = beri keringanan 2 hari
    "ISOLIR_MAKS_SEKALI": 8,           # lebih dari ini, isolir ditahan dan minta konfirmasi
    "BACKUP_FOLDER": "backup",          # folder backup, di samping main.py
    "NOTA_FOLDER": "nota",              # foto/PDF nota transaksi, di samping main.py
    "NOTA_MAKS_MB": 5,
    "NOTA_MAKS_PER_ITEM": 6,
    "NOTA_WAJIB_DI_ATAS": 100_000,      # pengeluaran >= ini tanpa nota ditandai di laporan
    "BACKUP_JAM": 2,                    # backup dibuat setelah jam ini tiap hari
    "BACKUP_SIMPAN": 14,                # jumlah backup harian yang disimpan
    "BACKUP_KIRIM_TELEGRAM": True,      # kirim berkas backup ke Telegram (ke tujuan laporan owner bila diisi)
    "BACKUP_NOTA_TELEGRAM": True,       # backup mingguan ikut mengirim nota baru (ZIP)
    "BACKUP_NOTA_MAKS_MB": 45,          # batas per ZIP (batas Telegram 50 MB per berkas)
    "NAMA_USAHA": _env("NAMA_USAHA", "Internet Desa"),   # dipakai di kwitansi & pesan WA
    # Pesan WA ke pelanggan (bisa diubah di Pengaturan). Penanda {..} diisi otomatis.
    "INFO_BAYAR": "Pembayaran dapat dilakukan langsung ke admin atau lewat QRIS.",
    "TEMPLATE_PENGINGAT": ("Yth. Bapak/Ibu {nama},\n\nKami informasikan tagihan internet {usaha} yang belum terbayar:\n"
                           "{rincian}\n*Total: {total}*\n\n{cara_bayar}\nBila sudah membayar, mohon abaikan pesan ini.\n\n"
                           "Terima kasih."),
    "PENGINGAT_JEDA_HARI": 3,           # pengingat massal melewati pelanggan yang baru diingatkan
    "KOMISI_MULAI": "",                 # YYYY-MM: komisi mitra dihitung mulai bulan ini (kosong = sejak data pertama)
    "BACKUP_HARI_KIRIM": 6,             # hari pengiriman mingguan: 0=Senin ... 6=Minggu
    # Database (kecil) dikirim ke Telegram TIAP HARI tanpa bunyi notifikasi, supaya bila VPS
    # rusak data yang hilang paling lama 1 hari. Nota (besar) tetap mingguan.
    "BACKUP_KIRIM_HARIAN": _env("BACKUP_KIRIM_HARIAN", "1") != "0",
    "HISTORY_INTERVAL_SECONDS": 300,   # simpan snapshot tiap 5 menit, bukan tiap 30 detik
    "HISTORY_RETENTION_DAYS": 30,      # data lebih lama dari ini dibuang otomatis
    "EVENT_RETENTION_DAYS": 90,        # riwayat Alert & Event disimpan sekian hari
    "EVENT_TELEGRAM_KRITIS": True,     # teruskan event kritis MikroTik (login gagal, reboot) ke Telegram
    "MIKROTIK_CPU_PERINGATAN": 90,     # CPU MikroTik >= ini (%) -> peringatan

    # --- Deteksi "boncos" / kandidat upgrade paket ---
    # Kalau MikroTik melaporkan rate-limit pelanggan, itu dipakai sebagai acuan
    # (rasio pemakaian terhadap limit). Kalau tidak ada info rate-limit,
    # fallback ke angka absolut BONCOS_DEFAULT_MBPS.
    "BONCOS_RATIO_THRESHOLD": 0.85,
    "BONCOS_DEFAULT_MBPS": 8.0,
    "BONCOS_MIN_SAMPLES": 10,           # minimal jumlah data sebelum dianggap valid, bukan kebetulan sesaat
    "BONCOS_LOOKBACK_DAYS": 7,

    # --- Deteksi pelanggan sering putus-nyambung ---
    # Dihitung dari sesi PPPoE baru (uptime pendek) di snapshot tiap 5 menit. Satu sesi yang
    # terekam di dua snapshot dihitung sekali. Sambung ulang beruntun di antara dua snapshot
    # terhitung satu, jadi angkanya batas bawah.
    "SHORT_UPTIME_THRESHOLD_SECONDS": 600,
    "SHORT_UPTIME_MIN_COUNT": 5,      # sambung ulang (bukan jumlah snapshot) per PUTUS_LOOKBACK_DAYS
    "PUTUS_LOOKBACK_DAYS": 1,

    # --- Laporan Harian ---
    "DAILY_REPORT_ENABLED": True,
    "DAILY_REPORT_HOUR": 7,     # jam kirim (waktu HP/server, 24 jam)
    "DAILY_REPORT_MINUTE": 0,

    # --- Interval ---
    "POLL_INTERVAL_SECONDS": 120,
    "ALERT_COOLDOWN_MINUTES": 720,   # 12 jam: satu alert per ONU, lalu diam sampai 12 jam kemudian
    "PUTUS_TUNGGU_MENIT": 10,         # tunggu sekian menit sebelum kirim peringatan putus
    "PUTUS_MASSAL_MIN": 5,
    # --- Pantau internet pusat (MikroTik -> ISP) ---
    "UPSTREAM_AKTIF": True,
    "UPSTREAM_TUJUAN": "8.8.8.8,1.1.1.1",   # tujuan ping internet; gateway ISP ditambah otomatis
    "UPSTREAM_WAN": "",                      # interface WAN; kosong = deteksi dari default route
    "UPSTREAM_KAPASITAS_MBPS": 0,            # download langganan ISP; 0 = alert bandwidth mati
    "UPSTREAM_INTERVAL_DETIK": 120,
    "UPSTREAM_LOSS_PERSEN": 20,              # packet loss ke semua tujuan internet >= ini = bermasalah
    "UPSTREAM_LATENSI_MS": 200,              # rata-rata latensi internet >= ini = lambat
    "UPSTREAM_PENUH_PERSEN": 90,             # pemakaian WAN >= ini dari kapasitas = penuh
    "PUTUS_SENYAP_MALAM": "21-06",    # jam (mulai-selesai) listrik ONU mati tidak dikabarkan per pelanggan; kosong = mati
    "TIKET_PENGINGAT_JAM": 24,        # tiket belum selesai sekian jam -> pengingat ke grup Telegram (0 = mati)            # sekian pelanggan putus bersamaan = satu pesan gangguan massal
    "ALERT_OFFLINE_LANGSUNG": False,  # False = matikan peringatan 'ONU OFFLINE' langsung yang lama
    "SNMP_TIMEOUT_SECONDS": 15,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("isp-dashboard")


class _SamarkanRahasia(logging.Filter):
    """Pesan error library HTTP menyertakan URL lengkap Telegram (.../bot<TOKEN>/...).
    Token & password disamarkan sebelum ditulis ke log supaya tidak bocor."""
    def filter(self, record):
        pesan = record.getMessage()
        bersih = _samarkan(pesan)
        if bersih != pesan:
            record.msg, record.args = bersih, ()
        return True


def _samarkan(teks: str) -> str:
    for rahasia in (CONFIG.get("TELEGRAM_BOT_TOKEN"), CONFIG.get("MIKROTIK_PASSWORD"), CONFIG.get("GENIEACS_PASS"),
                    CONFIG.get("OLT_TELNET_PASS"), CONFIG.get("ACS_ONU_PASS")):
        if rahasia:
            teks = teks.replace(f"bot{rahasia}", "bot***")
            if len(rahasia) >= 6:
                teks = teks.replace(rahasia, "***")
    return re.sub(r"bot\d+:[A-Za-z0-9_-]{20,}", "bot***", teks)


logger.addFilter(_SamarkanRahasia())


# ============================================================
# 2. MODELS
# ============================================================

class PPPoEUser(BaseModel):
    username: str
    ip_address: str
    uptime: str
    uptime_seconds: Optional[int] = None
    caller_id: Optional[str] = None  # MAC, dipakai untuk cross-reference ke ONU
    service: Optional[str] = None
    rx_mbps: Optional[float] = None        # kecepatan sesaat, hasil monitor-traffic
    tx_mbps: Optional[float] = None
    rate_limit_raw: Optional[str] = None   # mis. "5M/10M", apa adanya dari MikroTik
    rate_limit_mbps: Optional[float] = None  # angka terbesar dari rate_limit_raw, buat perbandingan
    nama: Optional[str] = None
    paket: Optional[str] = None
    kecepatan_paket: Optional[float] = None

class ONUStatus(BaseModel):
    pon: str                       # "0/0/1"
    onu_id: int                    # nomor ONU dalam PON tsb (sesuai web GUI)
    snmp_index: int                # index SNMP internal, untuk debug
    vendor: Optional[str] = None
    model: Optional[str] = None
    firmware: Optional[str] = None
    serial_number: Optional[str] = None
    mac_address: Optional[str] = None
    pppoe_username: Optional[str] = None   # hasil cross-reference MAC -> PPPoE active
    olt_rx_dbm: Optional[float]            # redaman diterima OLT dari ONU ini
    onu_rx_dbm: Optional[float] = None     # redaman diterima ONU dari OLT
    onu_tx_dbm: Optional[float] = None
    bias_current_ma: Optional[float] = None
    voltage_v: Optional[float] = None
    temperature_c: Optional[float] = None
    hardware_status: str = "OK"            # "OK" atau "PERLU_CEK" — tidak memicu alert
    hardware_notes: list[str] = []
    last_event: Optional[str] = None
    last_event_time: Optional[str] = None
    online: bool
    status: str                    # "OK", "WARNING", "CRITICAL", "UNKNOWN" — dari redaman ONU/OLT, INI yang memicu alert


class DashboardData(BaseModel):
    timestamp: str
    mikrotik_online: bool
    mikrotik_error: Optional[str] = None
    olt_online: bool
    pppoe_active_count: int
    pppoe_users: list[PPPoEUser]
    onu_list: list[ONUStatus]
    onu_total: int
    onu_online_count: int
    onu_critical_count: int
    onu_warning_count: int


# ============================================================
# 3. FETCHER — MIKROTIK (routeros-api)
# ============================================================

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


def fetch_pppoe_active() -> tuple[list[PPPoEUser], Optional[str]]:
    """Ambil daftar PPPoE active dari MikroTik via RouterOS API, sekalian
    bandwidth sesaat (monitor-traffic, satu kali panggil buat semua interface
    biar hemat) dan rate-limit tiap pelanggan kalau tersedia.
    Return (daftar_user, pesan_error). error None jika sukses — dipakai
    untuk status mikrotik_online yang akurat, bukan ditebak dari isi list."""
    connection = None
    try:
        connection = _mikrotik_pool()
        api = connection.get_api()
        active_list = api.get_resource("/ppp/active").get()

        # Bandwidth sesaat — panggil monitor-traffic SEKALI untuk semua interface
        # PPPoE sekaligus (bukan satu-satu per user, biar nggak lambat/berat).
        # Kalau ini gagal, bandwidth cuma jadi None — data PPPoE inti tetap utuh.
        bw_by_username: dict[str, tuple[Optional[float], Optional[float]]] = {}
        iface_names = [f"<pppoe-{item.get('name')}>" for item in active_list if item.get("name")]
        if iface_names:
            try:
                traffic = api.get_resource("/interface").call(
                    "monitor-traffic", {"interface": ",".join(iface_names), "once": ""}
                )
                for row in traffic:
                    name = (row.get("name") or "").strip("<>")
                    if name.startswith("pppoe-"):
                        username = name[len("pppoe-"):]
                        rx_bps = row.get("tx-bits-per-second")
                        tx_bps = row.get("rx-bits-per-second")
                        rx_mbps = round(int(rx_bps) / 1_000_000, 2) if rx_bps else 0.0
                        tx_mbps = round(int(tx_bps) / 1_000_000, 2) if tx_bps else 0.0
                        bw_by_username[username] = (rx_mbps, tx_mbps)
            except Exception as e:
                logger.warning(f"Gagal ambil bandwidth (monitor-traffic): {e}")

        users = []
        for item in active_list:
            username = item.get("name", "-")
            rx_mbps, tx_mbps = bw_by_username.get(username, (None, None))
            rate_raw, rate_mbps = _parse_rate_limit(item.get("rate-limit"))
            uptime = item.get("uptime", "-")
            users.append(
                PPPoEUser(
                    username=username,
                    ip_address=item.get("address", "-"),
                    uptime=uptime,
                    uptime_seconds=parse_uptime_to_seconds(uptime),
                    caller_id=item.get("caller-id", "-"),
                    service=item.get("service", "pppoe"),
                    rx_mbps=rx_mbps,
                    tx_mbps=tx_mbps,
                    rate_limit_raw=rate_raw,
                    rate_limit_mbps=rate_mbps,
                )
            )
        return users, None
    except Exception as e:
        err = f"[{type(e).__name__}] {e}"
        logger.error(f"Gagal konek MikroTik: {err}")
        return [], err
    finally:
        if connection:
            try:
                connection.disconnect()
            except Exception:
                pass


# ============================================================
# 4. FETCHER — OLT C-Data (via binary snmpwalk, bukan pysnmp)
# ============================================================

_SNMP_LINE_RE = re.compile(r"^\.?([\d.]+)\s*=\s*(\S+):\s*(.*)$")


async def snmp_walk(base_oid: str) -> dict[str, tuple[str, str]]:
    """Jalankan `snmpwalk` di base_oid, return {suffix_oid: (tipe, nilai_mentah)}.
    Gagal total (device down / binary tidak ada) -> dict kosong + log warning,
    TIDAK melempar exception ke pemanggil — ini yang bikin fetch tahan banting."""
    cmd = [
        "snmpwalk",
        f"-v{CONFIG['OLT_SNMP_VERSION']}",
        "-c", CONFIG["OLT_SNMP_COMMUNITY"],
        "-O", "n",
        "-r", "1",
        "-t", "3",
        f"{CONFIG['OLT_HOST']}:{CONFIG['OLT_SNMP_PORT']}",
        base_oid,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CONFIG["SNMP_TIMEOUT_SECONDS"]
        )
    except FileNotFoundError:
        logger.error("Binary 'snmpwalk' tidak ditemukan — pastikan net-snmp terpasang (pkg install net-snmp).")
        return {}
    except asyncio.TimeoutError:
        logger.warning(f"SNMP walk timeout untuk OID {base_oid}")
        return {}
    except Exception as e:
        logger.error(f"SNMP walk gagal ({base_oid}): {e}")
        return {}

    if not stdout and stderr:
        logger.warning(f"snmpwalk tidak ada output untuk {base_oid}: {stderr.decode(errors='ignore')[:200]}")
        return {}

    result: dict[str, tuple[str, str]] = {}
    for line in stdout.decode(errors="ignore").splitlines():
        m = _SNMP_LINE_RE.match(line.strip())
        if not m:
            continue
        oid, vtype, value = m.groups()
        prefix = base_oid + "."
        suffix = oid[len(prefix):] if oid.startswith(prefix) else oid
        result[suffix] = (vtype, value.strip())
    return result


def _parse_pon_onu(location_raw: str) -> Optional[tuple[str, int]]:
    """'\"0/0/1 onu 3\"' -> ("0/0/1", 3)"""
    text = location_raw.strip('"')
    m = re.search(r"(\d+/\d+/\d+)\s+onu\s+(\d+)", text, re.IGNORECASE)
    return (m.group(1), int(m.group(2))) if m else None


def _to_scaled_float(raw_value: Optional[str], scale: float = 0.01) -> Optional[float]:
    if raw_value is None:
        return None
    try:
        return round(int(raw_value) * scale, 2)
    except (ValueError, TypeError):
        return None


def _decode_serial(hex_raw: Optional[str]) -> Optional[str]:
    """SN OLT C-Data: 4 byte pertama = vendor ID ASCII, sisanya hex uppercase."""
    if not hex_raw:
        return None
    try:
        parts = hex_raw.strip().split()
        if len(parts) < 5:
            return None
        vendor = "".join(chr(int(b, 16)) for b in parts[:4])
        return vendor + "".join(b.upper() for b in parts[4:])
    except Exception:
        return None


def _build_mac_by_index(mac_table: dict[str, tuple[str, str]]) -> dict[str, str]:
    """OID ini kebalik: suffix-nya adalah byte-byte MAC (desimal), value-nya
    snmp_index si ONU. Kita balik jadi {snmp_index: "AA:BB:CC:DD:EE:FF"}."""
    mac_by_index: dict[str, str] = {}
    for suffix, (_, snmp_index_val) in mac_table.items():
        parts = suffix.split(".")
        if len(parts) != 7 or parts[6] != "1":
            continue
        try:
            mac_hex = ":".join(f"{int(b):02X}" for b in parts[:6])
        except ValueError:
            continue
        mac_by_index[snmp_index_val] = mac_hex
    return mac_by_index


def classify_rx_power(rx: Optional[float]) -> str:
    if rx is None:
        return "UNKNOWN"
    if rx <= CONFIG["RX_POWER_CRITICAL_DBM"]:
        return "CRITICAL"
    if rx <= CONFIG["RX_POWER_WARNING_DBM"]:
        return "WARNING"
    return "OK"


_SEVERITY_ORDER = {"OK": 0, "UNKNOWN": 0, "WARNING": 1, "CRITICAL": 2}


def combine_status(olt_rx: Optional[float], onu_rx: Optional[float]) -> str:
    """Status akhir yang memicu alert — diambil dari yang paling parah antara
    redaman OLT (OLT<-ONU) dan redaman ONU (ONU<-OLT). ONU offline (olt_rx
    None) selalu UNKNOWN terlepas dari onu_rx."""
    if olt_rx is None:
        return "UNKNOWN"
    olt_status = classify_rx_power(olt_rx)
    onu_status = classify_rx_power(onu_rx) if onu_rx is not None else "OK"
    return olt_status if _SEVERITY_ORDER[olt_status] >= _SEVERITY_ORDER[onu_status] else onu_status


def classify_hardware(bias: Optional[float], voltage: Optional[float], temperature: Optional[float]) -> tuple[str, list[str]]:
    """Tidak memicu alert Telegram — cuma penanda 'PERLU_CEK' buat ditampilkan
    (mis. warna merah) kalau nanti ada dashboard visual."""
    notes: list[str] = []
    if voltage is not None and not (CONFIG["VOLTAGE_MIN_V"] <= voltage <= CONFIG["VOLTAGE_MAX_V"]):
        notes.append(f"Voltage {voltage}V di luar rentang normal ({CONFIG['VOLTAGE_MIN_V']}-{CONFIG['VOLTAGE_MAX_V']}V)")
    if bias is not None and bias > CONFIG["BIAS_MAX_MA"]:
        notes.append(f"Bias current {bias}mA tinggi (>{CONFIG['BIAS_MAX_MA']}mA) — indikasi laser ONT mulai degradasi")
    if temperature is not None and temperature > CONFIG["TEMP_MAX_C"]:
        notes.append(f"Suhu {temperature}°C tinggi (>{CONFIG['TEMP_MAX_C']}°C)")
    return ("PERLU_CEK" if notes else "OK"), notes


async def fetch_all_onu() -> list[ONUStatus]:
    """Auto-discover semua ONU terdaftar di OLT lalu ambil semua data pendukung.
    Setiap sub-fetch dibungkus supaya kegagalan satu jenis data (mis. firmware
    tidak terbaca) tidak menghapus data lain yang berhasil (mis. redaman) untuk
    ONU yang sama."""

    location_map = await snmp_walk(CONFIG["OID_LOCATION"])
    if not location_map:
        logger.warning("Tidak ada data lokasi ONU dari OLT — OLT mungkin down atau SNMP tidak terjangkau.")
        return []

    vendor_map, model_map, fw_map, sn_map, event_map, event_time_map, olt_rx_map, optical_map, mac_table = await asyncio.gather(
        snmp_walk(CONFIG["OID_VENDOR"]),
        snmp_walk(CONFIG["OID_MODEL"]),
        snmp_walk(CONFIG["OID_FIRMWARE"]),
        snmp_walk(CONFIG["OID_SN_HEX"]),
        snmp_walk(CONFIG["OID_EVENT"]),
        snmp_walk(CONFIG["OID_EVENT_TIME"]),
        snmp_walk(CONFIG["OID_OLT_RX"]),
        snmp_walk(CONFIG["OID_ONU_OPTICAL"]),
        snmp_walk(CONFIG["OID_MAC_TABLE"]),
    )
    mac_by_index = _build_mac_by_index(mac_table)

    results: list[ONUStatus] = []
    for snmp_index_str, (_, loc_raw) in location_map.items():
        try:
            snmp_index = int(snmp_index_str)
        except ValueError:
            continue

        parsed = _parse_pon_onu(loc_raw)
        if parsed is None:
            logger.warning(f"Lokasi tak terparsing untuk snmp_index {snmp_index}: {loc_raw!r}")
            continue
        pon, onu_id = parsed

        onu_rx = onu_tx = bias = voltage = temperature = None
        for suffix, (_, val) in optical_map.items():
            parts = suffix.split(".")
            if len(parts) != 4 or parts[1] != snmp_index_str:
                continue
            if parts[0] == "4":
                onu_rx = _to_scaled_float(val)
            elif parts[0] == "5":
                onu_tx = _to_scaled_float(val)
            elif parts[0] == "6":
                bias = _to_scaled_float(val)
            elif parts[0] == "7":
                voltage = _to_scaled_float(val)
            elif parts[0] == "8":
                temperature = _to_scaled_float(val)

        olt_rx = _to_scaled_float(olt_rx_map.get(snmp_index_str, (None, None))[1])
        mac_addr = mac_by_index.get(snmp_index_str)

        # Slot ONU "hantu" — biasanya sisa dari ONU yang pernah dicabut/belum
        # benar aktif: tidak ada MAC sama sekali DAN redaman 0.0 dBm (angka ini
        # secara fisik mustahil untuk penerimaan optik nyata, selalu negatif).
        # Ditandai UNKNOWN, BUKAN CRITICAL, supaya tidak memicu alert palsu.
        is_ghost_slot = mac_addr is None and olt_rx == 0.0

        online = olt_rx is not None and not is_ghost_slot
        hw_status, hw_notes = classify_hardware(bias, voltage, temperature)
        final_status = "UNKNOWN" if is_ghost_slot else combine_status(olt_rx, onu_rx)

        results.append(
            ONUStatus(
                pon=pon,
                onu_id=onu_id,
                snmp_index=snmp_index,
                vendor=vendor_map.get(snmp_index_str, (None, None))[1],
                model=model_map.get(snmp_index_str, (None, None))[1],
                firmware=fw_map.get(snmp_index_str, (None, None))[1],
                serial_number=_decode_serial(sn_map.get(snmp_index_str, (None, None))[1]),
                mac_address=mac_addr,
                olt_rx_dbm=None if is_ghost_slot else olt_rx,
                onu_rx_dbm=None if is_ghost_slot else onu_rx,
                onu_tx_dbm=None if is_ghost_slot else onu_tx,
                bias_current_ma=bias,
                voltage_v=voltage,
                temperature_c=temperature,
                hardware_status=hw_status,
                hardware_notes=hw_notes,
                last_event=event_map.get(snmp_index_str, (None, None))[1],
                last_event_time=event_time_map.get(snmp_index_str, (None, None))[1],
                online=online,
                status=final_status,
            )
        )

    results.sort(key=lambda o: (o.pon, o.onu_id))
    _koreksi_jam_olt(results)
    return results


# --- Koreksi jam OLT ---------------------------------------------------------
# Waktu event (dying-gasp, LOS) berasal dari jam internal OLT. Kalau jam OLT tidak
# sinkron, waktu itu meleset (mis. dying-gasp "15:23" untuk putus pukul 12:59).
# Selisihnya diperkirakan dari ONU yang baru saja offline dengan event baru:
# event itu pasti terjadi dalam satu siklus terakhir, jadi waktu OLT - waktu server
# = selisih jam OLT.
_FMT_WAKTU = "%Y-%m-%d %H:%M:%S"
_jam_olt_lalu: dict = {}           # (pon, onu) -> (online, waktu event mentah)
_jam_olt_sampel: list = []         # selisih detik, maks 9 sampel terakhir
_jam_olt_geser = timedelta(0)


def _parse_waktu_olt(teks):
    try:
        return datetime.strptime((teks or "").strip().strip('"').strip(), _FMT_WAKTU)
    except ValueError:
        return None


def _koreksi_jam_olt(onu_list: list[ONUStatus], sekarang: Optional[datetime] = None) -> None:
    global _jam_olt_geser
    sekarang = sekarang or datetime.now()
    terbaru = None
    for o in onu_list:
        key = (o.pon, o.onu_id)
        w = _parse_waktu_olt(o.last_event_time)
        lalu = _jam_olt_lalu.get(key)
        if w is not None:
            if lalu is not None and lalu[0] and not o.online and lalu[1] != o.last_event_time:
                selisih = (w - sekarang).total_seconds()
                if abs(selisih) < 86400:
                    _jam_olt_sampel.append(selisih)
                    del _jam_olt_sampel[:-9]
            terbaru = w if terbaru is None or w > terbaru else terbaru
        _jam_olt_lalu[key] = (o.online, o.last_event_time)

    if _jam_olt_sampel:
        urut = sorted(_jam_olt_sampel)
        geser = urut[len(urut) // 2]
    elif terbaru is not None and terbaru > sekarang + timedelta(minutes=5):
        geser = (terbaru - sekarang).total_seconds()   # event "di masa depan": jam OLT pasti maju
    else:
        geser = _jam_olt_geser.total_seconds()
    baru = timedelta(seconds=round(geser / 60) * 60) if abs(geser) >= 180 else timedelta(0)
    if abs((baru - _jam_olt_geser).total_seconds()) >= 180:
        logger.warning(f"Jam OLT selisih {int(baru.total_seconds() // 60)} menit dari server; waktu event OLT dikoreksi.")
    _jam_olt_geser = baru

    if _jam_olt_geser:
        for o in onu_list:
            w = _parse_waktu_olt(o.last_event_time)
            if w is not None:
                o.last_event_time = (w - _jam_olt_geser).strftime(_FMT_WAKTU)


def _attach_pppoe_username(onu_list: list[ONUStatus], pppoe_users: list[PPPoEUser]) -> None:
    """Cocokkan ONU <-> pelanggan aktif lewat MAC address (caller-id di PPPoE)."""
    by_mac = {u.caller_id.upper(): u.username for u in pppoe_users if u.caller_id and u.caller_id != "-"}
    for onu in onu_list:
        if onu.mac_address:
            onu.pppoe_username = by_mac.get(onu.mac_address.upper())


# ============================================================
# 5. TELEGRAM ALERT
# ============================================================

_last_alert_sent: dict[tuple[str, int], datetime] = {}
_previous_online: dict[tuple[str, int], bool] = {}


def _chat_pindah(r) -> Optional[str]:
    """Grup yang di-upgrade jadi supergroup mendapat chat ID baru; Telegram
    memberitahunya lewat parameters.migrate_to_chat_id."""
    try:
        baru = (r.json().get("parameters") or {}).get("migrate_to_chat_id")
        return str(baru) if baru else None
    except Exception:
        return None


def _ganti_chat_owner(lama: str, baru: str) -> None:
    daftar = [baru if c == lama else c for c in _chat_owner()]
    CONFIG["TELEGRAM_OWNER_CHAT_ID"] = ",".join(daftar)
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS pengaturan (kunci TEXT PRIMARY KEY, nilai TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO pengaturan (kunci, nilai) VALUES ('TELEGRAM_OWNER_CHAT_ID', ?)",
                     (CONFIG["TELEGRAM_OWNER_CHAT_ID"],))
        conn.commit()
    finally:
        conn.close()
    logger.warning(f"Grup owner {lama} di-upgrade jadi supergroup, chat ID diganti otomatis ke {baru}.")
    catat_aktivitas("sistem", "pengaturan_ubah", f"Chat ID grup owner {lama} → {baru} (grup di-upgrade Telegram)")


def kirim_telegram_ke(chat_id: str, text: str) -> bool:
    """Kirim ke chat tertentu dan kembalikan apakah Telegram menerimanya."""
    if not CONFIG["TELEGRAM_ENABLED"]:
        return False
    try:
        for _ in range(2):
            r = requests.post(
                f"https://api.telegram.org/bot{CONFIG['TELEGRAM_BOT_TOKEN']}/sendMessage",
                data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10,
            )
            baru = None if r.ok else _chat_pindah(r)
            if not baru:
                break
            if chat_id in _chat_owner():
                _ganti_chat_owner(chat_id, baru)
            chat_id = baru
        if not r.ok:
            logger.error(f"Telegram menolak pesan ke {chat_id}: {r.text[:200]}")
        return r.ok
    except Exception as e:
        logger.error(f"Gagal kirim Telegram ke {chat_id}: {e}")
        return False


def send_telegram_message(text: str, senyap: bool = False) -> None:
    if not CONFIG["TELEGRAM_ENABLED"]:
        return
    url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_BOT_TOKEN']}/sendMessage"
    data = {"chat_id": CONFIG["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML"}
    if senyap:
        data["disable_notification"] = "true"
    try:
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        logger.error(f"Gagal kirim Telegram: {e}")


def _cooldown_ok(key: tuple[str, int], now: datetime) -> bool:
    last_sent = _last_alert_sent.get(key)
    cooldown = timedelta(minutes=CONFIG["ALERT_COOLDOWN_MINUTES"])
    return not last_sent or (now - last_sent) >= cooldown


def check_and_send_onu_alerts(onu_list: list[ONUStatus]) -> None:
    """Dua kondisi alert: (1) redaman OLT-RX kritis, (2) ONU tadinya online
    tiba-tiba offline (indikasi fiber putus / ONT mati/dicabut)."""
    now = datetime.now()
    seen_keys: set[tuple[str, int]] = set()

    for onu in onu_list:
        key = (onu.pon, onu.onu_id)
        seen_keys.add(key)
        was_online = _previous_online.get(key)
        pelanggan = f" ({_esc(onu.pppoe_username)})" if onu.pppoe_username else ""

        if CONFIG["ALERT_OFFLINE_LANGSUNG"] and was_online is True and not onu.online:
            if _cooldown_ok(key, now):
                send_telegram_message(
                    f"⚠️ <b>ONU OFFLINE</b>\n"
                    f"PON {onu.pon} / ONU {onu.onu_id}{pelanggan}\n"
                    f"Model: {_esc(onu.model or '-')}\n"
                    f"SN: <code>{onu.serial_number or '-'}</code>\n"
                    f"MAC: <code>{onu.mac_address or '-'}</code>\n"
                    f"Event terakhir: {_esc(onu.last_event or '-')} ({_esc(onu.last_event_time or '-')})\n"
                    f"Waktu: {now.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                _last_alert_sent[key] = now
                logger.warning(f"Alert OFFLINE terkirim untuk PON {onu.pon} ONU {onu.onu_id}")

        elif onu.status == "CRITICAL" and onu.online:
            if _cooldown_ok(key, now):
                send_telegram_message(
                    f"🔴 <b>Redaman Kritis</b>\n"
                    f"PON {onu.pon} / ONU {onu.onu_id}{pelanggan}\n"
                    f"OLT-RX: <b>{onu.olt_rx_dbm} dBm</b> | ONU-RX: <b>{onu.onu_rx_dbm} dBm</b> (threshold: {CONFIG['RX_POWER_CRITICAL_DBM']} dBm)\n"
                    f"Model: {_esc(onu.model or '-')}\n"
                    f"SN: <code>{onu.serial_number or '-'}</code>\n"
                    f"MAC: <code>{onu.mac_address or '-'}</code>\n"
                    f"Waktu: {now.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                _last_alert_sent[key] = now
                logger.warning(f"Alert CRITICAL terkirim untuk PON {onu.pon} ONU {onu.onu_id} ({onu.olt_rx_dbm} dBm)")

        _previous_online[key] = onu.online

    for stale_key in set(_previous_online) - seen_keys:
        _previous_online.pop(stale_key, None)


# ============================================================
# 6. BACKGROUND MONITOR LOOP
# ============================================================

_latest_data: Optional[DashboardData] = None


def _build_dashboard(pppoe_users: list[PPPoEUser], mikrotik_error: Optional[str], onu_list: list[ONUStatus]) -> DashboardData:
    return DashboardData(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        mikrotik_online=mikrotik_error is None,
        mikrotik_error=mikrotik_error,
        olt_online=len(onu_list) > 0,
        pppoe_active_count=len(pppoe_users),
        pppoe_users=pppoe_users,
        onu_list=onu_list,
        onu_total=len(onu_list),
        onu_online_count=sum(1 for o in onu_list if o.online),
        onu_critical_count=sum(1 for o in onu_list if o.status == "CRITICAL"),
        onu_warning_count=sum(1 for o in onu_list if o.status == "WARNING"),
    )


# ============================================================
# 6a. ALERT & EVENT — log MikroTik, event OLT/ONU, kondisi perangkat
# ============================================================
# Event disimpan di isp_history.db tabel "event". Tingkat: kritis / peringatan / info.
# kunci_dedup UNIQUE supaya log yang sama tidak tersimpan dua kali walau
# dibaca ulang tiap siklus.

_BULAN_ROS = {b: i for i, b in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def init_event_db() -> None:
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                waktu TEXT NOT NULL,
                sumber TEXT NOT NULL,
                tingkat TEXT NOT NULL,
                kategori TEXT NOT NULL,
                judul TEXT NOT NULL,
                detail TEXT,
                pelanggan TEXT,
                kunci_dedup TEXT UNIQUE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_event_waktu ON event(waktu)")
        conn.commit()
    finally:
        conn.close()


def simpan_event(daftar: list[dict]) -> list[dict]:
    """Simpan event; kembalikan yang benar-benar baru (belum pernah tersimpan)."""
    if not daftar:
        return []
    baru = []
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
    try:
        for e in daftar:
            kunci = e.get("kunci") or hashlib.sha1(
                f"{e['waktu']}|{e['sumber']}|{e['judul']}|{e.get('detail', '')}".encode()).hexdigest()
            cur = conn.execute(
                "INSERT OR IGNORE INTO event (waktu, sumber, tingkat, kategori, judul, detail, pelanggan, kunci_dedup) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (e["waktu"], e["sumber"], e["tingkat"], e["kategori"], e["judul"][:200],
                 (e.get("detail") or "")[:1000], e.get("pelanggan"), kunci),
            )
            if cur.rowcount:
                baru.append(e)
        conn.commit()
    finally:
        conn.close()
    return baru


def _ev(sumber, tingkat, kategori, judul, detail="", pelanggan=None, waktu=None, kunci=None) -> dict:
    return {"waktu": waktu or datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "sumber": sumber,
            "tingkat": tingkat, "kategori": kategori, "judul": judul, "detail": detail,
            "pelanggan": pelanggan, "kunci": kunci}


def _waktu_log_ros(teks: str, sekarang: datetime) -> Optional[datetime]:
    """Format waktu log RouterOS: 'HH:MM:SS' (hari ini), 'mon/dd HH:MM:SS' (tahun ini),
    'mon/dd/yyyy HH:MM:SS', 'yyyy-mm-dd HH:MM:SS' atau 'mm-dd HH:MM:SS' (v7)."""
    teks = (teks or "").strip()
    try:
        if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", teks):
            t = datetime.combine(sekarang.date(), datetime.strptime(teks, "%H:%M:%S").time())
            return t - timedelta(days=1) if t > sekarang + timedelta(minutes=5) else t
        m = re.fullmatch(r"([a-z]{3})/(\d{1,2})(?:/(\d{4}))? (\d{1,2}:\d{2}:\d{2})", teks.lower())
        if m:
            bln, tgl, thn, jam = m.groups()
            t = datetime.strptime(f"{thn or sekarang.year}-{_BULAN_ROS[bln]}-{tgl} {jam}", "%Y-%m-%d %H:%M:%S")
            if not thn and t > sekarang + timedelta(days=1):
                t = t.replace(year=t.year - 1)
            return t
        if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}:\d{2}", teks):
            return datetime.strptime(teks, "%Y-%m-%d %H:%M:%S")
        m = re.fullmatch(r"(\d{2})-(\d{2}) (\d{1,2}:\d{2}:\d{2})", teks)
        if m:
            t = datetime.strptime(f"{sekarang.year}-{m.group(1)}-{m.group(2)} {m.group(3)}", "%Y-%m-%d %H:%M:%S")
            return t.replace(year=t.year - 1) if t > sekarang + timedelta(days=1) else t
    except (ValueError, KeyError):
        return None
    return None


def _klasifikasi_log_ros(topics: str, pesan: str) -> Optional[tuple[str, str, str]]:
    """(tingkat, kategori, judul) atau None bila log tidak perlu disimpan (mis. dhcp info)."""
    t = set((topics or "").split(","))
    p = (pesan or "").lower()
    if "login failure" in p:
        return "kritis", "keamanan", "Login gagal ke MikroTik"
    if "critical" in t:
        return "kritis", "router", "Log critical MikroTik"
    if "link down" in p:
        return "peringatan", "interface", "Interface link down"
    if "error" in t:
        return "peringatan", "router", "Log error MikroTik"
    if "warning" in t:
        return "peringatan", "router", "Log warning MikroTik"
    if "link up" in p:
        return "info", "interface", "Interface link up"
    if "account" in t or "logged in" in p or "logged out" in p:
        return "info", "akses", "Akses ke MikroTik"
    if "reboot" in p or "router was rebooted" in p:
        return "kritis", "router", "Router reboot"
    if "changed by" in p or "added by" in p or "removed by" in p:
        return "info", "konfigurasi", "Konfigurasi MikroTik diubah"
    if "pppoe" in t or "ppp" in t:
        if "disconnected" in p or "terminating" in p:
            return "info", "pppoe", "PPPoE terputus"
        if "connected" in p or "authenticated" in p:
            return "info", "pppoe", "PPPoE tersambung"
        return None
    return None


_PPPOE_USER_RE = re.compile(r"<pppoe-([^>]+)>|^([^\s:]+): (?:connected|disconnected|authenticated|terminating)")
_mikrotik_pertama = True        # pembacaan log pertama tidak dikirim ke Telegram (isi buffer lama)
_mikrotik_uptime_lalu: Optional[int] = None
_mikrotik_resource: dict = {}   # kondisi terakhir router untuk alert aktif


def fetch_mikrotik_event() -> list[dict]:
    """Baca /log dan /system/resource dari MikroTik, ubah menjadi event."""
    global _mikrotik_uptime_lalu, _mikrotik_resource
    sekarang = datetime.now()
    hasil = []
    pool = _mikrotik_pool()
    try:
        api = pool.get_api()
        log = api.get_resource("/log").get()
        res = (api.get_resource("/system/resource").get() or [{}])[0]
    finally:
        pool.disconnect()

    for row in log:
        kelas = _klasifikasi_log_ros(row.get("topics", ""), row.get("message", ""))
        if not kelas:
            continue
        waktu = _waktu_log_ros(row.get("time", ""), sekarang)
        if waktu is None:
            continue
        tingkat, kategori, judul = kelas
        pesan = row.get("message", "")
        m = _PPPOE_USER_RE.search(pesan)
        hasil.append(_ev("mikrotik", tingkat, kategori, judul, f"{pesan} [{row.get('topics', '')}]",
                         pelanggan=(m.group(1) or m.group(2)) if m and kategori == "pppoe" else None,
                         waktu=waktu.strftime("%Y-%m-%d %H:%M:%S")))

    # Kondisi router: CPU, memori, reboot
    try:
        cpu = int(res.get("cpu-load", 0))
        mem_free, mem_total = int(res.get("free-memory", 0)), int(res.get("total-memory", 0))
        uptime = parse_uptime_to_seconds(res.get("uptime", ""))
    except (TypeError, ValueError):
        cpu, mem_free, mem_total, uptime = 0, 0, 0, None
    mem_persen = round(100 - mem_free * 100 / mem_total, 1) if mem_total else None
    lalu = _mikrotik_resource
    _mikrotik_resource = {"cpu": cpu, "memori_persen": mem_persen, "uptime": res.get("uptime"),
                          "versi": res.get("version"), "board": res.get("board-name"), "waktu": sekarang}
    if cpu >= CONFIG["MIKROTIK_CPU_PERINGATAN"] and lalu.get("cpu", 0) < CONFIG["MIKROTIK_CPU_PERINGATAN"]:
        hasil.append(_ev("mikrotik", "peringatan", "router", "CPU MikroTik tinggi", f"CPU {cpu}%"))
    elif cpu < CONFIG["MIKROTIK_CPU_PERINGATAN"] - 10 and lalu.get("cpu", 0) >= CONFIG["MIKROTIK_CPU_PERINGATAN"]:
        hasil.append(_ev("mikrotik", "info", "router", "CPU MikroTik normal kembali", f"CPU {cpu}%"))
    if mem_persen is not None and mem_persen >= 90 and (lalu.get("memori_persen") or 0) < 90:
        hasil.append(_ev("mikrotik", "peringatan", "router", "Memori MikroTik hampir penuh", f"Terpakai {mem_persen}%"))
    if uptime is not None and _mikrotik_uptime_lalu is not None and uptime < _mikrotik_uptime_lalu:
        hasil.append(_ev("mikrotik", "kritis", "router", "MikroTik reboot terdeteksi",
                         f"Uptime kembali ke {res.get('uptime')}"))
    if uptime is not None:
        _mikrotik_uptime_lalu = uptime
    return hasil


def _db(v) -> str:
    return "-" if v is None else f"{v}"


# Kondisi terakhir tiap ONU, untuk mendeteksi perubahan antar siklus
_onu_kondisi: dict[tuple[str, int], dict] = {}
_olt_pertama = True
_olt_online_lalu: Optional[bool] = None
_mikrotik_online_lalu: Optional[bool] = None


def deteksi_event_perangkat(onu_list: list[ONUStatus], mikrotik_error: Optional[str]) -> list[dict]:
    """Bandingkan kondisi sekarang dengan siklus sebelumnya -> event perubahan."""
    global _olt_pertama, _olt_online_lalu, _mikrotik_online_lalu
    hasil = []
    olt_online = len(onu_list) > 0
    if _olt_online_lalu is not None and olt_online != _olt_online_lalu:
        hasil.append(_ev("olt", "kritis" if not olt_online else "info", "olt",
                         "OLT tidak merespons SNMP" if not olt_online else "OLT merespons kembali",
                         f"{CONFIG['OLT_HOST']}:{CONFIG['OLT_SNMP_PORT']}"))
    _olt_online_lalu = olt_online
    mik_online = mikrotik_error is None
    if _mikrotik_online_lalu is not None and mik_online != _mikrotik_online_lalu:
        hasil.append(_ev("sistem", "kritis" if not mik_online else "info", "mikrotik",
                         "API MikroTik tidak terhubung" if not mik_online else "API MikroTik terhubung kembali",
                         mikrotik_error or f"{CONFIG['MIKROTIK_HOST']}:{CONFIG['MIKROTIK_PORT']}"))
    _mikrotik_online_lalu = mik_online
    if not olt_online:
        return hasil      # data ONU kosong karena OLT tak terbaca, jangan anggap semua ONU hilang

    for o in onu_list:
        key = (o.pon, o.onu_id)
        lokasi = f"PON {o.pon} / ONU {o.onu_id}"
        lama = _onu_kondisi.get(key)
        # ONU yang offline tidak punya PPPoE aktif: pakai pelanggan terakhir yang diketahui
        siapa = o.pppoe_username or (lama or {}).get("pelanggan")
        ev_waktu = (o.last_event_time or "").strip().strip('"')
        ev_teks = (o.last_event or "").strip().strip('"')
        # Event bawaan OLT (dying-gasp, LOS, dll.) beserta waktunya
        if ev_teks and ev_teks != "--" and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", ev_waktu):
            low = ev_teks.lower()
            tingkat = "kritis" if "los" in low else ("peringatan" if "dying" in low or "power" in low else "info")
            hasil.append(_ev("olt", tingkat, "onu", f"Event OLT: {ev_teks}", lokasi, pelanggan=siapa,
                             waktu=ev_waktu, kunci=f"olt|{o.pon}|{o.onu_id}|{ev_waktu}|{ev_teks}"))
        if lama is not None:
            if lama["online"] and not o.online:
                hasil.append(_ev("olt", "peringatan", "onu", "ONU offline",
                                 f"{lokasi}" + (f" · event terakhir: {ev_teks}" if ev_teks and ev_teks != "--" else ""),
                                 pelanggan=siapa))
            elif not lama["online"] and o.online:
                hasil.append(_ev("olt", "info", "onu", "ONU online kembali", lokasi, pelanggan=siapa))
            if o.online and lama["online"] and o.status != lama["status"] and o.status in ("OK", "WARNING", "CRITICAL"):
                judul = {"CRITICAL": "Redaman kritis", "WARNING": "Redaman mendekati batas",
                         "OK": "Redaman normal kembali"}[o.status]
                hasil.append(_ev("olt", {"CRITICAL": "kritis", "WARNING": "peringatan", "OK": "info"}[o.status],
                                 "redaman", judul, f"{lokasi} · OLT-RX {_db(o.olt_rx_dbm)} / ONU-RX {_db(o.onu_rx_dbm)} dBm",
                                 pelanggan=siapa))
            if o.online and o.hardware_status == "PERLU_CEK" and lama["hardware"] != "PERLU_CEK":
                hasil.append(_ev("olt", "peringatan", "hardware", "Hardware ONT perlu dicek",
                                 f"{lokasi} · " + "; ".join(o.hardware_notes), pelanggan=siapa))
        elif not _olt_pertama:
            hasil.append(_ev("olt", "info", "onu", "ONU baru terdeteksi",
                             f"{lokasi} · {o.model or '-'} · SN {o.serial_number or '-'}", pelanggan=siapa))
        _onu_kondisi[key] = {"online": o.online, "status": o.status, "hardware": o.hardware_status,
                             "pelanggan": siapa}
    _olt_pertama = False
    return hasil


_event_telegram_terakhir: dict[str, datetime] = {}


def kirim_event_penting(baru: list[dict], dari_log_awal: bool = False) -> None:
    """Event kritis dari log MikroTik (mis. login gagal, reboot) diteruskan ke Telegram,
    maksimal sekali per 30 menit per jenis. Event ONU/redaman sudah punya alert sendiri."""
    if dari_log_awal or not CONFIG["EVENT_TELEGRAM_KRITIS"]:
        return
    sekarang = datetime.now()
    for e in baru:
        diteruskan = e["sumber"] in ("mikrotik", "sistem") or e["kategori"] == "olt"
        if e["tingkat"] != "kritis" or not diteruskan:
            continue
        try:
            if sekarang - datetime.strptime(e["waktu"], "%Y-%m-%d %H:%M:%S") > timedelta(minutes=15):
                continue    # log lama yang baru terbaca, tidak perlu dikabarkan
        except ValueError:
            pass
        jenis = e["judul"]
        if sekarang - _event_telegram_terakhir.get(jenis, datetime.min) < timedelta(minutes=30):
            continue
        _event_telegram_terakhir[jenis] = sekarang
        send_telegram_message(f"🚨 <b>{_esc(e['judul'])}</b>\n{_esc(e['detail'] or '')}\nWaktu: {e['waktu'][11:16]}")


def proses_event(onu_list: list[ONUStatus], mikrotik_error: Optional[str]) -> None:
    """Dipanggil tiap siklus pemantauan."""
    global _mikrotik_pertama
    event = deteksi_event_perangkat(onu_list, mikrotik_error)
    kirim_event_penting(simpan_event(event))
    if mikrotik_error is None:
        try:
            ev_mik = fetch_mikrotik_event()
        except Exception as e:
            logger.warning(f"Gagal baca log MikroTik: {e}")
            return
        kirim_event_penting(simpan_event(ev_mik), dari_log_awal=_mikrotik_pertama)
        _mikrotik_pertama = False


async def background_monitor_loop():
    """Setiap tahap dibungkus try/except sendiri supaya satu sumber data gagal
    (mis. MikroTik down) tidak menghentikan pengambilan data lain (OLT tetap
    terus dipantau, dan sebaliknya)."""
    global _latest_data
    while True:
        pppoe_users: list[PPPoEUser] = []
        mikrotik_error: Optional[str] = None
        onu_list: list[ONUStatus] = []

        try:
            pppoe_users, mikrotik_error = await asyncio.to_thread(fetch_pppoe_active)
        except Exception as e:
            mikrotik_error = f"[{type(e).__name__}] {e}"
            logger.error(f"Error tak terduga saat fetch MikroTik: {mikrotik_error}")

        try:
            onu_list = await fetch_all_onu()
        except Exception as e:
            logger.error(f"Error tak terduga saat fetch OLT: {e}")

        try:
            _attach_pppoe_username(onu_list, pppoe_users)
        except Exception as e:
            logger.error(f"Error saat cross-reference MAC->PPPoE: {e}")

        # Tahap-tahap di bawah memanggil Telegram/MikroTik/SQLite secara blocking,
        # jadi dijalankan di thread supaya API dashboard tetap responsif.
        try:
            await asyncio.to_thread(check_and_send_onu_alerts, onu_list)
        except Exception as e:
            logger.error(f"Error saat proses alert: {e}")
        try:
            await asyncio.to_thread(proses_event, onu_list, mikrotik_error)
        except Exception as e:
            logger.error(f"Error saat proses event: {e}")
        try:
            await asyncio.to_thread(proses_putus_pelanggan, pppoe_users, mikrotik_error, onu_list)
        except Exception as e:
            logger.error(f"Error saat proses putus pelanggan: {e}")
        try:
            await asyncio.to_thread(proses_tagihan_harian)
        except Exception as e:
            logger.error(f"Error saat proses tagihan harian: {e}")
        try:
            if mikrotik_error is None:
                await asyncio.to_thread(proses_sinkron_secret)
        except Exception as e:
            logger.error(f"Error saat sinkron secret PPPoE: {e}")
        try:
            await asyncio.to_thread(proses_isolir)
        except Exception as e:
            logger.error(f"Error saat proses isolir: {e}")
        try:
            await asyncio.to_thread(proses_laporan_bulanan)
        except Exception as e:
            logger.error(f"Error saat laporan bulanan: {e}")
        try:
            await asyncio.to_thread(proses_pengingat_utang)
        except Exception as e:
            logger.error(f"Error saat pengingat liabilitas: {e}")
        try:
            await asyncio.to_thread(proses_pengingat_tiket)
        except Exception as e:
            logger.error(f"Error saat pengingat tiket: {e}")
        try:
            await asyncio.to_thread(proses_backup_harian)
        except Exception as e:
            logger.error(f"Error saat proses backup harian: {e}")
        try:
            await asyncio.to_thread(proses_bersih_harian)
        except Exception as e:
            logger.error(f"Error saat bersih riwayat lama: {e}")
        try:
            await asyncio.to_thread(proses_cek_acs)
        except Exception as e:
            logger.error(f"Error saat cek GenieACS: {e}")
        try:
            await asyncio.to_thread(proses_bersih_ping_tertahan)
        except Exception as e:
            logger.error(f"Error saat bersih ping tertahan: {e}")

        # Dibungkus juga: bila ini error tanpa try, seluruh loop (isolir, tagihan, backup) berhenti diam-diam.
        try:
            _latest_data = _build_dashboard(pppoe_users, mikrotik_error, onu_list)
        except Exception as e:
            logger.exception(f"Error saat menyusun data dashboard: {e}")
        _tandai_putaran()

        await asyncio.sleep(CONFIG["POLL_INTERVAL_SECONDS"])


# --- Detak loop untuk /healthz, bersih riwayat harian, dan pantauan GenieACS ---
_putaran = {"terakhir": time.monotonic(), "mulai": time.monotonic(), "jumlah": 0}
_bersih_hari: Optional[str] = None
_acs_cek = {"terakhir": 0.0, "gagal": 0, "dilaporkan": False}


def _tandai_putaran() -> None:
    _putaran["terakhir"] = time.monotonic()
    _putaran["jumlah"] += 1


def proses_bersih_harian() -> None:
    """Hapus riwayat lewat masa simpan sekali sehari, terlepas dari laporan harian
    (dulu hanya ikut laporan harian, jadi tidak jalan bila laporan dimatikan)."""
    global _bersih_hari
    hari = datetime.now().strftime("%Y-%m-%d")
    if _bersih_hari != hari:
        cleanup_old_history()
        _bersih_hari = hari


_bersih_ping = {"terakhir": None}


def proses_bersih_ping_tertahan(umur_menit: int = 10) -> int:
    """Perintah ping yang tertahan > umur_menit dihapus beserta fault-nya. Tanpa ini GenieACS mengulangnya
    setiap ONU lapor, ONU terus 'sibuk diagnosa', dan ping berikutnya ikut ditolak (9002 Internal error)."""
    if not CONFIG.get("GENIEACS_URL") or (
            _bersih_ping["terakhir"] is not None and time.monotonic() - _bersih_ping["terakhir"] < 600):
        return 0
    _bersih_ping["terakhir"] = time.monotonic()
    from urllib.parse import quote
    batas = (datetime.utcnow() - timedelta(minutes=umur_menit)).isoformat()
    try:
        r = _acs("GET", "/tasks/", params={"query": json.dumps({"name": "setParameterValues"})}, timeout=15)
    except HTTPException:
        return 0
    n = 0
    for t in (r.json() if r.ok else []):
        pv = t.get("parameterValues") or []
        if not any(str(p[0]).startswith(_PING_ONU) for p in pv if p):
            continue
        if str(t.get("timestamp") or "")[:19] > batas[:19]:
            continue
        tid, dev = str(t.get("_id")), str(t.get("device") or "")
        try:
            _acs("DELETE", f"/tasks/{tid}")
            if dev:
                _acs("DELETE", f"/faults/{quote(dev + ':task_' + tid, safe='')}")
            n += 1
        except HTTPException:
            pass
    if n:
        logger.warning(f"{n} perintah ping tertahan > {umur_menit} menit dihapus dari GenieACS")
    return n


def proses_cek_acs() -> None:
    """Cek GenieACS tiap 5 menit. Lapor ke Telegram setelah 3 kali gagal berturut-turut
    (±15 menit) dan sekali lagi saat pulih, supaya gangguan sesaat tidak berisik."""
    if not CONFIG.get("GENIEACS_URL") or time.monotonic() - _acs_cek["terakhir"] < 300:
        return
    _acs_cek["terakhir"] = time.monotonic()
    auth = (CONFIG["GENIEACS_USER"], CONFIG["GENIEACS_PASS"]) if CONFIG.get("GENIEACS_USER") else None
    try:
        ok = requests.get(CONFIG["GENIEACS_URL"] + "/devices/?projection=_id&limit=1", auth=auth, timeout=8).ok
    except requests.RequestException:
        ok = False
    if ok:
        if _acs_cek["dilaporkan"]:
            send_telegram_message("✅ <b>Server ACS (GenieACS) pulih</b> — fitur ONU (WiFi, ping, reboot) bisa dipakai lagi.")
        _acs_cek.update(gagal=0, dilaporkan=False)
        return
    _acs_cek["gagal"] += 1
    if _acs_cek["gagal"] >= 3 and not _acs_cek["dilaporkan"]:
        send_telegram_message("⚠️ <b>Server ACS (GenieACS) tidak merespons</b> ±15 menit.\n"
                              "Fitur ONU (ganti WiFi, ping, reboot) tidak bisa dipakai. Cek di VPS:\n"
                              "<code>sudo systemctl status genieacs-cwmp genieacs-nbi mongod</code>")
        _acs_cek["dilaporkan"] = True


# ============================================================
# 6b. HISTORY DATABASE (SQLite) — bandwidth & redaman
# ============================================================
# Kenapa SQLite: satu file, tidak perlu server database terpisah, cukup
# ringan buat 24/7 di Termux. Ditulis tiap HISTORY_INTERVAL_SECONDS (bukan
# tiap siklus 30 detik) supaya ukuran file tidak membengkak cepat.

def init_history_db() -> None:
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS onu_history (
                ts TEXT NOT NULL,
                pon TEXT NOT NULL,
                onu_id INTEGER NOT NULL,
                pppoe_username TEXT,
                olt_rx_dbm REAL,
                onu_rx_dbm REAL,
                status TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage_history (
                ts TEXT NOT NULL,
                username TEXT NOT NULL,
                rx_mbps REAL,
                tx_mbps REAL,
                rate_limit_mbps REAL,
                uptime_seconds INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS upstream_history (
                ts TEXT NOT NULL,
                jenis TEXT NOT NULL,
                tujuan TEXT NOT NULL,
                rata_ms REAL,
                maks_ms REAL,
                hilang_persen INTEGER
            )
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS upstream_trafik (ts TEXT NOT NULL, rx_mbps REAL, tx_mbps REAL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_upstream_ts ON upstream_history(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_upstream_trafik_ts ON upstream_trafik(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_onu_hist_ts ON onu_history(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_hist_ts_user ON usage_history(ts, username)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_onu_hist_onu ON onu_history(pon, onu_id, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_onu_hist_user ON onu_history(pppoe_username, ts)")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    finally:
        conn.close()

def init_billing_db() -> None:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        # WAL: pembaca tidak menunggu penulis, jadi simpan pembayaran tidak gagal
        # "database is locked" saat monitor/backup sedang jalan. Tersimpan permanen di file.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paket (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nama TEXT NOT NULL UNIQUE,
                kecepatan_mbps REAL NOT NULL,
                harga INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pelanggan (
                username TEXT PRIMARY KEY,
                nama TEXT NOT NULL,
                alamat TEXT,
                no_hp TEXT,
                paket_id INTEGER REFERENCES paket(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tagihan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL REFERENCES pelanggan(username),
                periode TEXT NOT NULL,
                jumlah INTEGER NOT NULL,
                jatuh_tempo TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'belum',
                tgl_bayar TEXT,
                UNIQUE (username, periode)
            )
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS pengingat (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, "
                     "waktu TEXT NOT NULL, oleh TEXT, total INTEGER, bulan INTEGER)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pengingat_user ON pengingat(username, waktu)")
        kolom = [r[1] for r in conn.execute("PRAGMA table_info(pelanggan)").fetchall()]
        if "gratis" not in kolom:
            conn.execute("ALTER TABLE pelanggan ADD COLUMN gratis INTEGER NOT NULL DEFAULT 0")
        # status: 'aktif' atau 'berhenti'. Pelanggan berhenti tidak ditagih,
        # tidak diisolir otomatis, dan tidak dipantau putus-sambungnya.
        if "status" not in kolom:
            conn.execute("ALTER TABLE pelanggan ADD COLUMN status TEXT NOT NULL DEFAULT 'aktif'")
        if "berhenti_sejak" not in kolom:
            conn.execute("ALTER TABLE pelanggan ADD COLUMN berhenti_sejak TEXT")
        # tampil_mitra = 0: disembunyikan dari akun mitra dan tidak dihitung insentif
        # (mis. akun server/kantor). Saat kolom dibuat, pelanggan gratis disembunyikan.
        if "tampil_mitra" not in kolom:
            conn.execute("ALTER TABLE pelanggan ADD COLUMN tampil_mitra INTEGER NOT NULL DEFAULT 1")
            conn.execute("UPDATE pelanggan SET tampil_mitra = 0 WHERE gratis = 1")
        # insentif_gratis = 1: pelanggan gratis yang tetap dihitung insentif mitra
        # tiap bulan walau tidak punya tagihan.
        if "insentif_gratis" not in kolom:
            conn.execute("ALTER TABLE pelanggan ADD COLUMN insentif_gratis INTEGER NOT NULL DEFAULT 0")
        # Sinkron status secret PPPoE di MikroTik saat pelanggan berhenti/aktif lagi.
        # secret_target: 'nonaktif' / 'aktif' bila belum berhasil diterapkan (dicoba ulang otomatis).
        # secret_nonaktif: 1 bila secret sudah dinonaktifkan oleh sistem ini.
        if "secret_target" not in kolom:
            conn.execute("ALTER TABLE pelanggan ADD COLUMN secret_target TEXT")
        if "secret_nonaktif" not in kolom:
            conn.execute("ALTER TABLE pelanggan ADD COLUMN secret_nonaktif INTEGER NOT NULL DEFAULT 0")
        # ID perangkat di GenieACS (TR-069), diisi otomatis saat ONU pertama kali ditemukan.
        if "acs_id" not in kolom:
            conn.execute("ALTER TABLE pelanggan ADD COLUMN acs_id TEXT")
        # Pasang baru & prorata: tanggal mulai berlangganan, potongan tetap per bulan (Rp).
        if "tgl_pasang" not in kolom:
            conn.execute("ALTER TABLE pelanggan ADD COLUMN tgl_pasang TEXT")
        if "diskon" not in kolom:
            conn.execute("ALTER TABLE pelanggan ADD COLUMN diskon INTEGER NOT NULL DEFAULT 0")
        # Pelunasan: metode (untuk rekonsiliasi kas vs bank), siapa & kapan mencatat.
        kol_tg = [r[1] for r in conn.execute("PRAGMA table_info(tagihan)").fetchall()]
        for kol in ("metode_bayar", "lunas_oleh", "lunas_pada", "catatan"):
            if kol not in kol_tg:
                conn.execute(f"ALTER TABLE tagihan ADD COLUMN {kol} TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS aktivitas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                waktu TEXT NOT NULL,
                oleh TEXT NOT NULL,
                aksi TEXT NOT NULL,
                detail TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_aktivitas_waktu ON aktivitas(waktu)")
        conn.commit()
    finally:
        conn.close()

def log_history_snapshot(dashboard: DashboardData) -> None:
    """Simpan satu snapshot ke SQLite. Dipanggil dari thread terpisah
    (blocking I/O), tidak boleh menahan event loop utama."""
    now_iso = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
    try:
        conn.executemany(
            "INSERT INTO onu_history (ts, pon, onu_id, pppoe_username, olt_rx_dbm, onu_rx_dbm, status) VALUES (?,?,?,?,?,?,?)",
            [
                (now_iso, o.pon, o.onu_id, o.pppoe_username, o.olt_rx_dbm, o.onu_rx_dbm, o.status)
                for o in dashboard.onu_list
            ],
        )
        conn.executemany(
            "INSERT INTO usage_history (ts, username, rx_mbps, tx_mbps, rate_limit_mbps, uptime_seconds) VALUES (?,?,?,?,?,?)",
            [
                (now_iso, u.username, u.rx_mbps, u.tx_mbps, u.rate_limit_mbps, u.uptime_seconds)
                for u in dashboard.pppoe_users
            ],
        )
        conn.commit()
    finally:
        conn.close()


def cleanup_old_history() -> None:
    cutoff = (datetime.now() - timedelta(days=CONFIG["HISTORY_RETENTION_DAYS"])).isoformat(timespec="seconds")
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
    try:
        conn.execute("DELETE FROM onu_history WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM usage_history WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM upstream_history WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM upstream_trafik WHERE ts < ?", (cutoff,))
        batas_event = (datetime.now() - timedelta(days=CONFIG["EVENT_RETENTION_DAYS"])).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("DELETE FROM event WHERE waktu < ?", (batas_event,))
        conn.commit()
    finally:
        conn.close()


async def history_logger_loop():
    """Loop independen — cukup baca _latest_data yang sudah ada (hasil
    background_monitor_loop), TIDAK fetch ulang ke SNMP/MikroTik sendiri,
    biar beban ke OLT/MikroTik tidak dobel."""
    await asyncio.sleep(10)  # kasih waktu siklus pertama selesai dulu
    while True:
        try:
            if _latest_data is not None:
                await asyncio.to_thread(log_history_snapshot, _latest_data)
        except Exception as e:
            logger.error(f"Gagal simpan history: {e}")
        await asyncio.sleep(CONFIG["HISTORY_INTERVAL_SECONDS"])


# ============================================================
# 6c. DETEKSI KANDIDAT UPGRADE ("boncos") & SERING PUTUS
# ============================================================

def get_boncos_candidates() -> list[dict]:
    """Pelanggan yang pemakaiannya konsisten mendekati/menyentuh rate-limit
    paket mereka selama BONCOS_LOOKBACK_DAYS terakhir — kandidat ditawari
    upgrade. Kalau rate-limit tidak diketahui, dibandingkan ke
    BONCOS_DEFAULT_MBPS sebagai fallback (ditandai di hasil)."""
    since = (datetime.now() - timedelta(days=CONFIG["BONCOS_LOOKBACK_DAYS"])).isoformat(timespec="seconds")
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT username,
                   AVG(MAX(rx_mbps, tx_mbps)) AS avg_usage,
                   MAX(MAX(rx_mbps, tx_mbps)) AS peak_usage,
                   AVG(rate_limit_mbps) AS rate_limit_mbps,
                   COUNT(*) AS sample_count
            FROM usage_history
            WHERE ts >= ? AND rx_mbps IS NOT NULL
            GROUP BY username
            """,
            (since,),
        ).fetchall()
    finally:
        conn.close()

    results = []
    for r in rows:
        if r["sample_count"] < CONFIG["BONCOS_MIN_SAMPLES"]:
            continue
        limit = r["rate_limit_mbps"] or CONFIG["BONCOS_DEFAULT_MBPS"]
        limit_is_fallback = r["rate_limit_mbps"] is None
        ratio = (r["avg_usage"] or 0) / limit if limit else 0
        if ratio >= CONFIG["BONCOS_RATIO_THRESHOLD"]:
            results.append({
                "username": r["username"],
                "avg_usage_mbps": round(r["avg_usage"] or 0, 2),
                "peak_usage_mbps": round(r["peak_usage"] or 0, 2),
                "limit_mbps": round(limit, 2),
                "limit_is_fallback": limit_is_fallback,
                "ratio": round(ratio, 2),
                "sample_count": r["sample_count"],
            })
    results.sort(key=lambda x: x["ratio"], reverse=True)
    return results


def _hitung_sambung_ulang(rows) -> dict:
    """Jumlah sambung ulang per pelanggan dari baris (username, ts, uptime) yang uptime-nya pendek.
    Satu sesi bisa terekam di beberapa snapshot (uptime 2 menit, lalu 7 menit), jadi yang dihitung
    adalah waktu mulai sesinya (ts - uptime), bukan jumlah snapshot."""
    sesi: dict = {}
    for u, ts, up in rows:
        try:
            mulai = datetime.fromisoformat(ts) - timedelta(seconds=int(up))
        except (TypeError, ValueError):
            continue
        daftar = sesi.setdefault(u, [])
        if not any(abs((mulai - m).total_seconds()) <= 90 for m in daftar):
            daftar.append(mulai)
    return {u: len(d) for u, d in sesi.items()}


def get_frequent_disconnect_candidates() -> list[dict]:
    """Pelanggan yang PPPoE-nya tersambung ulang berkali-kali (sesi baru) dalam PUTUS_LOOKBACK_DAYS.
    Masih batas bawah: sambung ulang beruntun di antara dua snapshot terhitung satu."""
    since = (datetime.now() - timedelta(days=CONFIG["PUTUS_LOOKBACK_DAYS"])).isoformat(timespec="seconds")
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
    try:
        rows = conn.execute(
            "SELECT username, ts, uptime_seconds FROM usage_history "
            "WHERE ts >= ? AND uptime_seconds IS NOT NULL AND uptime_seconds < ?",
            (since, CONFIG["SHORT_UPTIME_THRESHOLD_SECONDS"]),
        ).fetchall()
    finally:
        conn.close()
    jumlah = _hitung_sambung_ulang(rows)
    hasil = [{"username": u, "short_uptime_count": n} for u, n in jumlah.items()
             if n >= CONFIG["SHORT_UPTIME_MIN_COUNT"]]
    return sorted(hasil, key=lambda x: -x["short_uptime_count"])


def _build_daily_report_text() -> str:
    lines = ["📋 <b>Laporan Harian</b>", f"({datetime.now().strftime('%Y-%m-%d %H:%M')})", ""]

    if _latest_data is not None:
        lines.append(f"Pelanggan aktif: {_latest_data.pppoe_active_count}")
        lines.append(f"ONU online: {_latest_data.onu_online_count}/{_latest_data.onu_total}")
        lines.append(f"⚠️ Warning: {_latest_data.onu_warning_count} | 🔴 Critical: {_latest_data.onu_critical_count}")
    else:
        lines.append("(data monitoring belum tersedia)")

    try:
        boncos = get_boncos_candidates()
        if boncos:
            lines.append("")
            lines.append(f"📈 <b>Kandidat upgrade ({len(boncos)})</b> — pemakaian mendekati limit:")
            for b in boncos[:5]:
                tanda = " (perkiraan, limit tidak terbaca)" if b["limit_is_fallback"] else ""
                lines.append(f"- {_esc(b['username'])}: rata² {b['avg_usage_mbps']} / limit {b['limit_mbps']} Mbps ({int(b['ratio']*100)}%){tanda}")
    except Exception as e:
        logger.error(f"Gagal hitung kandidat boncos untuk laporan: {e}")

    try:
        conn = _billing_conn()
        try:
            terbuka = _tiket_terbuka(conn)
        finally:
            conn.close()
        if terbuka:
            tanpa = sum(1 for t in terbuka if not t["teknisi"])
            lines.append("")
            lines.append(f"🛠 <b>Tiket gangguan terbuka: {len(terbuka)}</b>" + (f" ({tanpa} belum ada teknisi)" if tanpa else ""))
            for t in terbuka[:5]:
                lines.append(f"- #T{t['id']} {_esc(t['nama'] or t['username'] or 'umum')}: {_esc(t['kategori'])}, {_durasi_teks(t['dibuat_pada'])}")
    except Exception as e:
        logger.error(f"Gagal hitung tiket untuk laporan: {e}")

    try:
        malam = _ringkasan_listrik_malam()
        if malam:
            lines.append("")
            lines.extend(malam)
    except Exception as e:
        logger.error(f"Gagal hitung listrik ONU mati malam untuk laporan: {e}")

    try:
        putus = get_frequent_disconnect_candidates()
        if putus:
            lines.append("")
            lines.append(f"🔁 <b>Sering sambung ulang ({len(putus)})</b> — tiap kali internet terhenti beberapa detik:")
            for p in putus[:5]:
                lines.append(f"- {_esc(p['username'])}: {p['short_uptime_count']}x sambung ulang")
    except Exception as e:
        logger.error(f"Gagal hitung kandidat putus untuk laporan: {e}")

    return "\n".join(lines)


_last_report_date: Optional[str] = None


def _laporan_sudah_terkirim(hari: str) -> bool:
    """Dicatat di otomasi_log supaya laporan tidak terkirim ulang saat program dinyalakan ulang."""
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        return conn.execute(
            "SELECT 1 FROM otomasi_log WHERE tanggal=? AND jenis='laporan'", (hari,)
        ).fetchone() is not None
    finally:
        conn.close()


def _tandai_laporan_terkirim(hari: str) -> None:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        conn.execute("INSERT OR IGNORE INTO otomasi_log VALUES (?, 'laporan')", (hari,))
        conn.commit()
    finally:
        conn.close()


async def daily_report_loop():
    global _last_report_date
    if not CONFIG["DAILY_REPORT_ENABLED"]:
        return
    while True:
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        if _last_report_date != today_str:
            try:
                if await asyncio.to_thread(_laporan_sudah_terkirim, today_str):
                    _last_report_date = today_str
            except Exception as e:
                logger.error(f"Gagal cek log laporan harian: {e}")
        # Pakai ">=" bukan "==": sleep 60 detik bisa saja melompati menit tepatnya.
        if (
            (now.hour, now.minute) >= (CONFIG["DAILY_REPORT_HOUR"], CONFIG["DAILY_REPORT_MINUTE"])
            and _last_report_date != today_str
        ):
            try:
                text = await asyncio.to_thread(_build_daily_report_text)
                await asyncio.to_thread(cleanup_old_history)
                # Tandai terkirim hanya bila Telegram benar-benar menerima; kalau gagal
                # dicoba lagi menit berikutnya (sebelumnya laporan hilang diam-diam).
                if await asyncio.to_thread(kirim_telegram_ke, str(CONFIG["TELEGRAM_CHAT_ID"]), text):
                    await asyncio.to_thread(_tandai_laporan_terkirim, today_str)
                    _listrik_malam.clear()
                    _last_report_date = today_str
                    logger.info("Laporan harian terkirim.")
            except Exception as e:
                logger.error(f"Gagal kirim laporan harian: {e}")
        await asyncio.sleep(60)


# ============================================================
# 6d. TELEGRAM COMMAND LISTENER — "/cek", "/masalah", "/boncos", "/putus"
# ============================================================

_telegram_offset: Optional[int] = None
_telegram_first_poll = True   # supaya backlog pesan lama tidak diproses ulang saat restart


def find_onu_by_name(query: str) -> list[ONUStatus]:
    """Cari ONU berdasarkan nama pelanggan PPPoE (substring, case-insensitive)."""
    if _latest_data is None:
        return []
    q = query.strip().lower()
    return [o for o in _latest_data.onu_list if o.pppoe_username and q in o.pppoe_username.lower()]


def _format_onu_report(onu: ONUStatus) -> str:
    status_icon = {"OK": "✅", "WARNING": "🟡", "CRITICAL": "🔴", "UNKNOWN": "⚫"}.get(onu.status, "❓")
    pppoe = None
    if _latest_data is not None:
        pppoe = next((u for u in _latest_data.pppoe_users if u.username == onu.pppoe_username), None)

    lines = [
        f"👤 <b>{_esc(onu.pppoe_username or '(tanpa PPPoE aktif)')}</b>",
        f"PON {onu.pon} / ONU {onu.onu_id}  {status_icon} {onu.status}",
        f"OLT-RX: {onu.olt_rx_dbm} dBm | ONU-RX: {onu.onu_rx_dbm} dBm",
        f"Model: {_esc(onu.model or '-')} | SN: <code>{_esc(onu.serial_number or '-')}</code>",
        f"MAC: <code>{onu.mac_address or '-'}</code>",
        f"Online: {'Ya' if onu.online else 'Tidak'}",
    ]
    if onu.hardware_status != "OK":
        lines.append(f"⚠️ Hardware: {_esc('; '.join(onu.hardware_notes))}")
    if pppoe:
        lines.append(f"IP: {pppoe.ip_address} | Uptime: {pppoe.uptime}")
        if pppoe.rx_mbps is not None:
            lines.append(f"Bandwidth saat ini: ⬇{pppoe.rx_mbps} / ⬆{pppoe.tx_mbps} Mbps")
        if pppoe.rate_limit_raw:
            lines.append(f"Rate-limit paket: {_esc(pppoe.rate_limit_raw)}")
    return "\n".join(lines)


def _format_masalah_list() -> str:
    if _latest_data is None:
        return "Data belum tersedia."
    bermasalah = [o for o in _latest_data.onu_list if o.status in ("WARNING", "CRITICAL")]
    if not bermasalah:
        return "✅ Semua ONU normal, tidak ada yang WARNING/CRITICAL saat ini."
    bermasalah.sort(key=lambda o: (o.status != "CRITICAL", o.olt_rx_dbm or 0))
    lines = [f"📋 <b>{len(bermasalah)} ONU bermasalah:</b>"]
    for o in bermasalah:
        icon = "🔴" if o.status == "CRITICAL" else "🟡"
        nama = _esc(o.pppoe_username or f"PON {o.pon} ONU {o.onu_id}")
        lines.append(f"{icon} {nama}: OLT-RX {o.olt_rx_dbm} / ONU-RX {o.onu_rx_dbm} dBm")
    return "\n".join(lines)


def _format_boncos_list() -> str:
    candidates = get_boncos_candidates()
    if not candidates:
        return f"Tidak ada kandidat upgrade dalam {CONFIG['BONCOS_LOOKBACK_DAYS']} hari terakhir (belum ada yang konsisten mendekati limit)."
    lines = [f"📈 <b>{len(candidates)} kandidat upgrade</b> (pemakaian ≥{int(CONFIG['BONCOS_RATIO_THRESHOLD']*100)}% dari limit, {CONFIG['BONCOS_LOOKBACK_DAYS']} hari terakhir):"]
    for b in candidates[:15]:
        tanda = " ⚠️(limit tidak terbaca, dibanding default)" if b["limit_is_fallback"] else ""
        lines.append(f"- {_esc(b['username'])}: rata² {b['avg_usage_mbps']} Mbps / limit {b['limit_mbps']} Mbps ({int(b['ratio']*100)}%){tanda}")
    return "\n".join(lines)


def _format_putus_list() -> str:
    candidates = get_frequent_disconnect_candidates()
    if not candidates:
        return (f"Tidak ada pelanggan yang tersambung ulang {CONFIG['SHORT_UPTIME_MIN_COUNT']}x atau lebih "
                f"dalam {CONFIG['PUTUS_LOOKBACK_DAYS']} hari terakhir.")
    lines = [
        f"🔁 <b>{len(candidates)} pelanggan sering sambung ulang</b> ({CONFIG['PUTUS_LOOKBACK_DAYS']} hari terakhir):",
    ]
    for p in candidates[:15]:
        lines.append(f"- {_esc(p['username'])}: {p['short_uptime_count']}x sambung ulang")
    lines.append("\n<i>Sambung ulang = internet terhenti beberapa detik (ONU restart / PPPoE dial ulang). "
                 "Sering tidak terasa pelanggan, tapi tanda ONU, adaptor, atau kabel mulai bermasalah.</i>")
    return "\n".join(lines)


_HELP_TEXT = (
    "Perintah yang tersedia:\n"
    "<code>/cek nama</code> — detail satu pelanggan\n"
    "<code>/masalah</code> — daftar ONU WARNING/CRITICAL saat ini\n"
    "<code>/boncos</code> — kandidat upgrade paket\n"
    "<code>/putus</code> — pelanggan yang sering putus-nyambung\n"
    "<code>/tiket</code> — tiket gangguan yang belum selesai\n"
    "<code>/id</code> — tampilkan chat ID (untuk chat owner)\n"
    "<code>/laporan</code> — laporan keuangan bulan lalu (hanya di chat owner)\n"
    "<code>/help</code> — tampilkan pesan ini"
)


def _handle_telegram_command(text: str) -> None:
    text = (text or "").strip()
    if not text.startswith("/"):
        return

    command, _, rest = text.partition(" ")
    command = command.lower().split("@")[0]     # "/cek@NamaBot" di grup

    if command in ("/help", "/start"):
        send_telegram_message(_HELP_TEXT)
        return

    if command == "/masalah":
        send_telegram_message(_format_masalah_list())
        return

    if command == "/boncos":
        send_telegram_message(_format_boncos_list())
        return

    if command == "/putus":
        send_telegram_message(_format_putus_list())
        return

    if command == "/tiket":
        send_telegram_message(_format_tiket_list())
        return

    if command == "/cek":
        query = rest.strip()
        if not query:
            send_telegram_message("Format: <code>/cek nama pelanggan</code>\nContoh: <code>/cek NUR AS</code>")
            return
        if _latest_data is None:
            send_telegram_message("Data belum tersedia, coba lagi beberapa saat.")
            return
        matches = find_onu_by_name(query)
        if not matches:
            send_telegram_message(f"Tidak ditemukan pelanggan dengan nama mengandung '{_esc(query)}'.")
            return
        if len(matches) > 1:
            daftar = "\n".join(f"- {_esc(m.pppoe_username)} (PON {m.pon} ONU {m.onu_id})" for m in matches)
            send_telegram_message(f"Ditemukan {len(matches)} pelanggan cocok, sebutkan lebih spesifik:\n{daftar}")
            return
        send_telegram_message(_format_onu_report(matches[0]))
        return

    send_telegram_message(f"Perintah '{_esc(command)}' tidak dikenal.\n\n{_HELP_TEXT}")


def _poll_telegram_updates_once() -> None:
    """Satu kali polling getUpdates (long-poll). Dipanggil dari thread terpisah
    (blocking request) supaya tidak menahan event loop utama."""
    global _telegram_offset, _telegram_first_poll

    if not CONFIG["TELEGRAM_ENABLED"]:
        return

    url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_BOT_TOKEN']}/getUpdates"
    params = {"timeout": 10}
    if _telegram_offset is not None:
        params["offset"] = _telegram_offset

    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
    except Exception as e:
        logger.warning(f"Gagal polling Telegram: {e}")
        return

    if not data.get("ok"):
        logger.warning(f"Telegram getUpdates gagal: {data}")
        return

    for update in data.get("result", []):
        _telegram_offset = update["update_id"] + 1

        if _telegram_first_poll:
            # Lewati backlog pesan lama dari sebelum script ini dijalankan,
            # supaya tidak "meledak" balas ulang perintah lama pas restart.
            continue

        message = update.get("message") or {}
        chat_id = str(message.get("chat", {}).get("id", ""))
        perintah = (message.get("text") or "").strip().split(" ")[0].lower().split("@")[0]
        if perintah == "/id":
            # Siapa pun boleh tahu chat ID-nya sendiri (dipakai untuk mengisi chat ID owner)
            kirim_telegram_ke(chat_id, f"Chat ID ini: <code>{_esc(chat_id)}</code>")
            continue
        if chat_id in _chat_owner():
            try:
                if _handle_perintah_owner(chat_id, message.get("text", "")):
                    continue
            except Exception as e:
                logger.error(f"Error saat proses perintah owner: {e}")
                continue
        if chat_id != str(CONFIG["TELEGRAM_CHAT_ID"]):
            continue  # abaikan chat lain — data pelanggan tidak boleh bocor ke chat lain

        try:
            _handle_telegram_command(message.get("text", ""))
        except Exception as e:
            logger.error(f"Error saat proses command Telegram: {e}")

    _telegram_first_poll = False


async def telegram_command_listener():
    """Loop independen dari background_monitor_loop — polling perintah masuk
    tanpa mengganggu siklus fetch data SNMP/MikroTik."""
    while True:
        try:
            await asyncio.to_thread(_poll_telegram_updates_once)
        except Exception as e:
            logger.error(f"Error tak terduga di telegram_command_listener: {e}")
        await asyncio.sleep(CONFIG["TELEGRAM_POLL_INTERVAL_SECONDS"])


# ============================================================
# 7. FASTAPI APP
# ============================================================

# /docs, /redoc, /openapi.json dimatikan: dashboard terbuka ke internet, peta API tidak perlu diumbar.
app = FastAPI(title="ISP Monitoring Dashboard", docs_url=None, redoc_url=None, openapi_url=None)

# Dashboard dan API dilayani dari origin yang sama, jadi CORS tidak dibuka:
# dengan CORS "*" situs lain yang dibuka admin di browser bisa ikut memanggil
# API (mis. mengisolir pelanggan).


# ============================================================
# 7a. AKUN & SESI LOGIN
# ============================================================
# Akun disimpan di billing.db (tabel akun), password di-hash PBKDF2-SHA256.
# Login membuat sesi acak; yang disimpan di database hanya hash token-nya,
# jadi salinan database/backup tidak bisa dipakai untuk masuk.
# Cookie sesi: HttpOnly (tidak terbaca JavaScript), SameSite=Strict (tidak
# terkirim dari situs lain -> aman dari CSRF), Secure bila lewat HTTPS.

_COOKIE_SESI = "indeso_sesi"
_PBKDF2_ITERASI = 600_000   # rekomendasi OWASP untuk PBKDF2-SHA256; hash lama di-upgrade saat login
_POLA_USERNAME = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
_PERAN = ("owner", "admin", "mitra")
_PERAN_PENUH = ("owner", "admin")     # akses seluruh dashboard

# Alamat yang boleh dibuka tanpa login, dan yang boleh dibuka akun mitra.
# Selain itu: tanpa login -> 401/ke halaman login, mitra -> 403.
_JALUR_PUBLIK = re.compile(r"^/(login|favicon\.ico|static/inter\.woff2|api/login|api/setup|api/setup-status|healthz)$")
_JALUR_MITRA = re.compile(r"^/(|api/me|api/logout|api/akun/saya/password|api/mitra/.*)$")


def _hash_password(pw: str) -> str:
    garam = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(garam), _PBKDF2_ITERASI).hex()
    return f"pbkdf2_sha256${_PBKDF2_ITERASI}${garam}${h}"


def _cek_password(pw: str, tersimpan: Optional[str]) -> bool:
    try:
        _alg, iterasi, garam, h = (tersimpan or "").split("$")
        hitung = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(garam), int(iterasi)).hex()
        return secrets.compare_digest(hitung, h)
    except Exception:
        return False


# Hash palsu untuk username yang tidak ada, supaya lama prosesnya sama
# (penyerang tidak bisa menebak username dari waktu respons).
_HASH_PALSU = _hash_password(secrets.token_hex(8))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _cek_password_baru(pw: str) -> None:
    if len(pw) < 8:
        raise HTTPException(status_code=400, detail="Password minimal 8 karakter.")
    if len(pw) > 128:
        raise HTTPException(status_code=400, detail="Password maksimal 128 karakter.")


def init_akun_db() -> None:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS akun (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                peran TEXT NOT NULL,
                aktif INTEGER NOT NULL DEFAULT 1,
                dibuat TEXT NOT NULL,
                login_terakhir TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sesi (
                token_hash TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                dibuat TEXT NOT NULL,
                terakhir_aktif TEXT NOT NULL,
                ip TEXT,
                perangkat TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sesi_user ON sesi(username)")
        # Impor sekali dari .env (cara lama). Setelah tabel akun terisi, akun
        # dikelola dari menu Pengaturan dan isian akun di .env tidak dipakai lagi.
        if conn.execute("SELECT COUNT(*) FROM akun").fetchone()[0] == 0:
            def parse(teks):
                for item in (teks or "").split(","):
                    nama, _, pw = item.strip().partition(":")
                    if nama.strip() and pw:
                        yield nama.strip(), pw
            akun = {n: (pw, "mitra") for n, pw in parse(CONFIG["MITRA_USERS"])}
            akun.update({n: (pw, "admin") for n, pw in parse(CONFIG["DASHBOARD_USERS"])})
            if CONFIG["DASHBOARD_PASSWORD"]:
                akun[CONFIG["DASHBOARD_USER"]] = (CONFIG["DASHBOARD_PASSWORD"], "admin")
            if akun and not any(p == "admin" for _pw, p in akun.values()):
                logger.warning("Akun mitra di .env tidak diimpor karena belum ada akun admin.")
                akun = {}
            sekarang = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for nama, (pw, peran) in akun.items():
                conn.execute(
                    "INSERT INTO akun (username, password_hash, peran, dibuat) VALUES (?,?,?,?)",
                    (nama, _hash_password(pw), peran, sekarang),
                )
            if akun:
                logger.warning(f"{len(akun)} akun diimpor dari .env: {', '.join(sorted(akun))}. "
                               "Selanjutnya kelola akun dari menu Pengaturan; boleh hapus isian akun di .env.")
        conn.commit()
    finally:
        conn.close()


def _jumlah_akun() -> int:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        return conn.execute("SELECT COUNT(*) FROM akun").fetchone()[0]
    finally:
        conn.close()


def _ambil_sesi(token: str) -> Optional[tuple[str, str]]:
    """Kembalikan (username, peran) untuk token sesi yang masih berlaku."""
    if not token:
        return None
    sekarang = datetime.now()
    th = _hash_token(token)
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        r = conn.execute(
            "SELECT s.username, s.dibuat, s.terakhir_aktif, a.peran FROM sesi s "
            "JOIN akun a ON a.username = s.username WHERE s.token_hash = ? AND a.aktif = 1",
            (th,),
        ).fetchone()
        if not r:
            return None
        username, dibuat, terakhir, peran = r
        if (sekarang - datetime.fromisoformat(terakhir) > timedelta(hours=CONFIG["SESI_IDLE_JAM"])
                or sekarang - datetime.fromisoformat(dibuat) > timedelta(days=CONFIG["SESI_MAKS_HARI"])):
            conn.execute("DELETE FROM sesi WHERE token_hash = ?", (th,))
            conn.commit()
            return None
        # Perbarui waktu aktif paling sering sekali per menit, supaya tidak menulis tiap request.
        if sekarang - datetime.fromisoformat(terakhir) > timedelta(minutes=1):
            conn.execute("UPDATE sesi SET terakhir_aktif = ? WHERE token_hash = ?",
                         (sekarang.isoformat(timespec="seconds"), th))
            conn.commit()
        return username, peran
    finally:
        conn.close()


def _cabut_sesi(username: str, kecuali_token: Optional[str] = None) -> int:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        if kecuali_token:
            n = conn.execute("DELETE FROM sesi WHERE username = ? AND token_hash != ?",
                             (username, _hash_token(kecuali_token))).rowcount
        else:
            n = conn.execute("DELETE FROM sesi WHERE username = ?", (username,)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def _ip(request: Request) -> str:
    return request.client.host if request.client else "-"


# --- Pembatas percobaan login (di memori): 5 gagal per IP atau 10 gagal per
# username dalam 15 menit -> dikunci 15 menit sejak gagal terakhir. ---
_gagal_login: dict[str, list[datetime]] = {}
_BATAS_GAGAL = {"ip": 5, "user": 10}
_JENDELA_GAGAL = timedelta(minutes=15)


def _terkunci(kunci: str, batas: int, sekarang: datetime) -> Optional[int]:
    daftar = [t for t in _gagal_login.get(kunci, []) if sekarang - t < _JENDELA_GAGAL]
    _gagal_login[kunci] = daftar
    if len(daftar) >= batas:
        return int((_JENDELA_GAGAL - (sekarang - daftar[-1])).total_seconds() // 60) + 1
    return None


def _catat_gagal(kunci: str, sekarang: datetime) -> None:
    # Bersihkan catatan kedaluwarsa supaya penyerang yang berganti-ganti IP/username
    # tidak bisa membuat memori server membengkak.
    if len(_gagal_login) > 5000:
        for k in [k for k, v in _gagal_login.items() if not v or sekarang - v[-1] >= _JENDELA_GAGAL]:
            _gagal_login.pop(k, None)
    _gagal_login.setdefault(kunci, []).append(sekarang)


_HEADER_KEAMANAN = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
}


def _asal_sah(request: Request) -> bool:
    """Tolak request pengubah data yang datang dari situs lain (lapisan kedua
    selain SameSite=Strict). Tanpa header Origin/Referer (mis. curl) dibiarkan."""
    asal = request.headers.get("origin") or request.headers.get("referer")
    if not asal:
        return True
    host = request.headers.get("host", "")
    return re.sub(r"^https?://", "", asal).split("/")[0] == host


@app.middleware("http")
async def wajib_login(request: Request, call_next):
    """Semua halaman dan API wajib login, kecuali halaman login itu sendiri.
    request.state.user / peran dipakai endpoint dan log aktivitas."""
    jalur = request.url.path
    request.state.user = None
    request.state.peran = None
    request.state.token = request.cookies.get(_COOKIE_SESI, "")

    if request.method not in ("GET", "HEAD", "OPTIONS") and not _asal_sah(request):
        return JSONResponse({"detail": "Permintaan ditolak (asal tidak sah)."}, status_code=403)

    if not _JALUR_PUBLIK.match(jalur):
        sesi = await asyncio.to_thread(_ambil_sesi, request.state.token)
        if sesi is None:
            if jalur.startswith("/api/"):
                return JSONResponse({"detail": "Sesi berakhir, silakan login lagi."}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        request.state.user, request.state.peran = sesi
        if request.state.peran == "mitra" and not _JALUR_MITRA.match(jalur):
            if jalur.startswith("/api/"):
                return JSONResponse({"detail": "Akun mitra tidak punya akses ke fitur ini."}, status_code=403)
            return RedirectResponse("/", status_code=303)

    response = await call_next(request)
    for k, v in _HEADER_KEAMANAN.items():
        response.headers.setdefault(k, v)
    if jalur.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def _wajib_admin(request: Request) -> None:
    if request.state.peran not in _PERAN_PENUH:
        raise HTTPException(status_code=403, detail="Khusus admin.")


def _pasang_cookie(response: Response, request: Request, token: str) -> None:
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        _COOKIE_SESI, token, httponly=True, samesite="strict", secure=https,
        max_age=CONFIG["SESI_MAKS_HARI"] * 86400, path="/",
    )


def _buat_sesi(username: str, request: Request) -> str:
    token = secrets.token_urlsafe(32)
    sekarang = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        # Bersihkan sesi kedaluwarsa sekalian
        batas_idle = (datetime.now() - timedelta(hours=CONFIG["SESI_IDLE_JAM"])).isoformat(timespec="seconds")
        conn.execute("DELETE FROM sesi WHERE terakhir_aktif < ?", (batas_idle,))
        conn.execute(
            "INSERT INTO sesi (token_hash, username, dibuat, terakhir_aktif, ip, perangkat) VALUES (?,?,?,?,?,?)",
            (_hash_token(token), username, sekarang, sekarang, _ip(request),
             (request.headers.get("user-agent") or "")[:200]),
        )
        conn.execute("UPDATE akun SET login_terakhir = ? WHERE username = ?",
                     (sekarang.replace("T", " "), username))
        conn.commit()
    finally:
        conn.close()
    return token


class LoginIn(BaseModel):
    username: str
    password: str


@app.get("/login")
async def halaman_login():
    return FileResponse(os.path.join(BASE_DIR, "login.html"), headers={"Cache-Control": "no-cache"})


@app.get("/static/inter.woff2", include_in_schema=False)
def font_inter():
    """Font Inter (SIL OFL) disajikan sendiri: CSP hanya mengizinkan sumber 'self'."""
    return FileResponse(os.path.join(BASE_DIR, "static", "inter.woff2"), media_type="font/woff2",
                        headers={"Cache-Control": "public, max-age=2592000"})


@app.get("/api/setup-status")
def setup_status():
    return {"perlu_setup": _jumlah_akun() == 0}


@app.post("/api/setup")
def setup_admin_pertama(d: LoginIn, request: Request):
    """Hanya bisa dipakai selama belum ada akun sama sekali (instalasi baru)."""
    username = d.username.strip()
    if not _POLA_USERNAME.match(username):
        raise HTTPException(status_code=400, detail="Username 3-32 karakter: huruf, angka, titik, minus, garis bawah.")
    _cek_password_baru(d.password)
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        conn.execute("BEGIN IMMEDIATE")   # cegah dua setup bersamaan
        if conn.execute("SELECT COUNT(*) FROM akun").fetchone()[0] > 0:
            conn.rollback()
            raise HTTPException(status_code=409, detail="Akun admin sudah dibuat. Silakan login.")
        conn.execute("INSERT INTO akun (username, password_hash, peran, dibuat) VALUES (?,?,?,?)",
                     (username, _hash_password(d.password), "owner", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(username, "akun_tambah", f"owner pertama dibuat dari {_ip(request)}")
    token = _buat_sesi(username, request)
    resp = JSONResponse({"ok": True, "peran": "owner"})
    _pasang_cookie(resp, request, token)
    return resp


@app.post("/api/login")
def login(d: LoginIn, request: Request):
    username = d.username.strip()
    sekarang = datetime.now()
    ip = _ip(request)
    for kunci, batas in ((f"ip:{ip}", _BATAS_GAGAL["ip"]), (f"user:{username.lower()}", _BATAS_GAGAL["user"])):
        menit = _terkunci(kunci, batas, sekarang)
        if menit:
            raise HTTPException(status_code=429, detail=f"Terlalu banyak percobaan gagal. Coba lagi dalam {menit} menit.")
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        r = conn.execute("SELECT username, password_hash, peran, aktif FROM akun WHERE username = ?",
                         (username,)).fetchone()
    finally:
        conn.close()
    ok = _cek_password(d.password, r[1] if r else _HASH_PALSU) and r is not None
    if not ok or not r[3]:
        _catat_gagal(f"ip:{ip}", sekarang)
        _catat_gagal(f"user:{username.lower()}", sekarang)
        catat_aktivitas(username[:32] or "-", "login_gagal",
                        f"dari {ip}" + (" (akun nonaktif)" if ok and r and not r[3] else ""))
        raise HTTPException(status_code=401, detail="Username atau password salah." if not ok
                            else "Akun ini sedang dinonaktifkan. Hubungi admin.")
    _gagal_login.pop(f"user:{username.lower()}", None)
    if not (r[1] or "").startswith(f"pbkdf2_sha256${_PBKDF2_ITERASI}$"):
        # Password tersimpan dengan iterasi lama: simpan ulang dengan iterasi sekarang.
        conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
        try:
            conn.execute("UPDATE akun SET password_hash=? WHERE username=?", (_hash_password(d.password), r[0]))
            conn.commit()
        finally:
            conn.close()
    token = _buat_sesi(r[0], request)
    catat_aktivitas(r[0], "login", f"dari {ip}")
    resp = JSONResponse({"ok": True, "peran": r[2]})
    _pasang_cookie(resp, request, token)
    return resp


@app.post("/api/logout")
def logout(request: Request):
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        conn.execute("DELETE FROM sesi WHERE token_hash = ?", (_hash_token(request.state.token),))
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(request.state.user, "logout", f"dari {_ip(request)}")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_COOKIE_SESI, path="/")
    return resp


@app.get("/api/me")
def akun_saya(request: Request):
    return {"username": request.state.user, "peran": request.state.peran}


class GantiPasswordIn(BaseModel):
    lama: str
    baru: str


@app.post("/api/akun/saya/password")
def ganti_password_saya(d: GantiPasswordIn, request: Request):
    _cek_password_baru(d.baru)
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        r = conn.execute("SELECT password_hash FROM akun WHERE username = ?", (request.state.user,)).fetchone()
        if not r or not _cek_password(d.lama, r[0]):
            raise HTTPException(status_code=400, detail="Password lama salah.")
        conn.execute("UPDATE akun SET password_hash = ? WHERE username = ?",
                     (_hash_password(d.baru), request.state.user))
        conn.commit()
    finally:
        conn.close()
    n = _cabut_sesi(request.state.user, kecuali_token=request.state.token)
    catat_aktivitas(request.state.user, "akun_password", f"ganti password sendiri, {n} sesi lain dicabut")
    return {"ok": True, "sesi_lain_dicabut": n}


# --- Kelola akun (khusus admin) ---

class AkunIn(BaseModel):
    username: str
    password: str
    peran: str = "admin"


class AkunUbahIn(BaseModel):
    peran: Optional[str] = None
    aktif: Optional[bool] = None


class ResetPasswordIn(BaseModel):
    baru: str


def _cek_hak_owner(conn, request: Request, peran_lama: Optional[str], peran_baru: Optional[str]) -> None:
    """Peran owner hanya boleh diberikan/dicabut oleh owner. Selama belum ada
    owner sama sekali, admin boleh menetapkan owner pertama."""
    if "owner" not in (peran_lama, peran_baru) or peran_lama == peran_baru:
        return
    ada_owner = conn.execute("SELECT 1 FROM akun WHERE peran='owner' AND aktif=1 LIMIT 1").fetchone()
    if request.state.peran != "owner" and ada_owner:
        raise HTTPException(status_code=403, detail="Hanya owner yang bisa memberi atau mencabut peran owner.")


def _jaga_akun_owner(conn, request: Request, username: str) -> None:
    """Admin tidak boleh mengutak-atik akun owner (reset password, keluarkan, hapus)."""
    r = conn.execute("SELECT peran FROM akun WHERE username = ?", (username,)).fetchone()
    if r and r[0] == "owner" and request.state.peran != "owner" and username != request.state.user:
        raise HTTPException(status_code=403, detail="Akun owner hanya bisa diubah oleh owner.")


def _admin_aktif_lain(conn, username: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM akun WHERE peran IN ('owner','admin') AND aktif=1 AND username != ?", (username,)
    ).fetchone()[0]


@app.get("/api/akun")
def list_akun(request: Request):
    _wajib_admin(request)
    batas = (datetime.now() - timedelta(hours=CONFIG["SESI_IDLE_JAM"])).isoformat(timespec="seconds")
    conn = _billing_conn()
    try:
        rows = conn.execute(
            "SELECT a.username, a.peran, a.aktif, a.dibuat, a.login_terakhir, "
            "(SELECT COUNT(*) FROM sesi s WHERE s.username = a.username AND s.terakhir_aktif >= ?) AS sesi_aktif "
            "FROM akun a ORDER BY a.peran, a.username COLLATE NOCASE", (batas,)
        ).fetchall()
    finally:
        conn.close()
    return {"saya": request.state.user, "akun": [dict(r) for r in rows]}


@app.post("/api/akun")
def tambah_akun(d: AkunIn, request: Request):
    _wajib_admin(request)
    username = d.username.strip()
    if not _POLA_USERNAME.match(username):
        raise HTTPException(status_code=400, detail="Username 3-32 karakter: huruf, angka, titik, minus, garis bawah.")
    if d.peran not in _PERAN:
        raise HTTPException(status_code=400, detail="Peran harus owner, admin, atau mitra.")
    _cek_password_baru(d.password)
    conn = _billing_conn()
    try:
        _cek_hak_owner(conn, request, None, d.peran)
        try:
            conn.execute("INSERT INTO akun (username, password_hash, peran, dibuat) VALUES (?,?,?,?)",
                         (username, _hash_password(d.password), d.peran, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Username sudah dipakai.")
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(request.state.user, "akun_tambah", f"{username} ({d.peran})")
    return {"ok": True}


@app.put("/api/akun/{username}")
def ubah_akun(username: str, d: AkunUbahIn, request: Request):
    _wajib_admin(request)
    if d.peran is not None and d.peran not in _PERAN:
        raise HTTPException(status_code=400, detail="Peran harus owner, admin, atau mitra.")
    conn = _billing_conn()
    try:
        r = conn.execute("SELECT peran, aktif FROM akun WHERE username = ?", (username,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Akun tidak ditemukan.")
        peran = d.peran if d.peran is not None else r["peran"]
        aktif = int(d.aktif) if d.aktif is not None else r["aktif"]
        _cek_hak_owner(conn, request, r["peran"], peran)
        if r["peran"] == "owner" and request.state.peran != "owner":
            raise HTTPException(status_code=403, detail="Akun owner hanya bisa diubah oleh owner.")
        if username == request.state.user and (peran not in _PERAN_PENUH or not aktif):
            raise HTTPException(status_code=400, detail="Tidak bisa menonaktifkan akun sendiri atau menjadikannya mitra.")
        if (r["peran"] in _PERAN_PENUH and r["aktif"] and (peran not in _PERAN_PENUH or not aktif)
                and _admin_aktif_lain(conn, username) == 0):
            raise HTTPException(status_code=400, detail="Harus ada minimal satu admin aktif.")
        conn.execute("UPDATE akun SET peran = ?, aktif = ? WHERE username = ?", (peran, aktif, username))
        conn.commit()
    finally:
        conn.close()
    ubah = []
    if peran != r["peran"]:
        ubah.append(f"peran {r['peran']} → {peran}")
    if aktif != r["aktif"]:
        ubah.append("diaktifkan" if aktif else "dinonaktifkan")
    if ubah:
        # Perubahan hak akses langsung berlaku: paksa login ulang.
        _cabut_sesi(username)
        catat_aktivitas(request.state.user, "akun_ubah", f"{username}: " + ", ".join(ubah))
    return {"ok": True}


@app.post("/api/akun/{username}/password")
def reset_password_akun(username: str, d: ResetPasswordIn, request: Request):
    _wajib_admin(request)
    _cek_password_baru(d.baru)
    conn = _billing_conn()
    try:
        _jaga_akun_owner(conn, request, username)
        n = conn.execute("UPDATE akun SET password_hash = ? WHERE username = ?",
                         (_hash_password(d.baru), username)).rowcount
        conn.commit()
    finally:
        conn.close()
    if not n:
        raise HTTPException(status_code=404, detail="Akun tidak ditemukan.")
    _cabut_sesi(username, kecuali_token=request.state.token if username == request.state.user else None)
    catat_aktivitas(request.state.user, "akun_password", f"reset password {username}")
    return {"ok": True}


@app.post("/api/akun/{username}/logout")
def cabut_sesi_akun(username: str, request: Request):
    _wajib_admin(request)
    conn = _billing_conn()
    try:
        _jaga_akun_owner(conn, request, username)
    finally:
        conn.close()
    n = _cabut_sesi(username, kecuali_token=request.state.token if username == request.state.user else None)
    catat_aktivitas(request.state.user, "akun_cabut_sesi", f"{username}: {n} sesi")
    return {"ok": True, "dicabut": n}


@app.delete("/api/akun/{username}")
def hapus_akun(username: str, request: Request):
    _wajib_admin(request)
    if username == request.state.user:
        raise HTTPException(status_code=400, detail="Tidak bisa menghapus akun sendiri.")
    conn = _billing_conn()
    try:
        r = conn.execute("SELECT peran, aktif FROM akun WHERE username = ?", (username,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Akun tidak ditemukan.")
        _jaga_akun_owner(conn, request, username)
        if r["peran"] in _PERAN_PENUH and r["aktif"] and _admin_aktif_lain(conn, username) == 0:
            raise HTTPException(status_code=400, detail="Harus ada minimal satu admin aktif.")
        conn.execute("DELETE FROM akun WHERE username = ?", (username,))
        conn.execute("DELETE FROM sesi WHERE username = ?", (username,))
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(request.state.user, "akun_hapus", f"{username} ({r['peran']})")
    return {"ok": True}


_background_tasks: list[asyncio.Task] = []


@app.on_event("startup")
async def on_startup():
    await asyncio.to_thread(init_history_db)
    await asyncio.to_thread(init_event_db)
    await asyncio.to_thread(init_billing_db)
    await asyncio.to_thread(init_akun_db)
    await asyncio.to_thread(init_keuangan_db)
    await asyncio.to_thread(init_nota_db)
    await asyncio.to_thread(init_tiket_db)
    await asyncio.to_thread(_muat_pengaturan)
    logger.info(
        f"Target: MikroTik {CONFIG['MIKROTIK_HOST']}:{CONFIG['MIKROTIK_PORT']}, "
        f"OLT SNMP v{CONFIG['OLT_SNMP_VERSION']} {CONFIG['OLT_HOST']}:{CONFIG['OLT_SNMP_PORT']}"
    )
    if await asyncio.to_thread(_jumlah_akun) == 0:
        logger.warning("Belum ada akun. Buka dashboard di browser untuk membuat akun admin pertama.")
    # Simpan referensinya: task tanpa referensi bisa dibersihkan garbage collector.
    _background_tasks.extend([
        asyncio.create_task(background_monitor_loop()),
        asyncio.create_task(telegram_command_listener()),
        asyncio.create_task(history_logger_loop()),
        asyncio.create_task(daily_report_loop()),
        asyncio.create_task(upstream_loop()),
    ])
    logger.info("Semua background task dimulai: monitor, telegram listener, history logger, daily report.")


@app.get("/")
async def serve_dashboard(request: Request):
    # no-cache: browser selalu cek ulang ke server, jadi update index.html langsung terpakai
    halaman = "mitra.html" if request.state.peran == "mitra" else "index.html"
    return FileResponse(os.path.join(BASE_DIR, halaman), headers={"Cache-Control": "no-cache"})

def _salin(model, **perubahan):
    """Salin objek Pydantic dengan nilai baru (jalan di Pydantic v1 maupun v2)."""
    if hasattr(model, "model_copy"):
        return model.model_copy(update=perubahan)
    return model.copy(update=perubahan)


def _perkaya_pelanggan(data: DashboardData) -> DashboardData:
    conn = _billing_conn()
    try:
        rows = conn.execute(
            "SELECT p.username, p.nama, k.nama AS paket, k.kecepatan_mbps "
            "FROM pelanggan p LEFT JOIN paket k ON k.id = p.paket_id"
        ).fetchall()
    finally:
        conn.close()
    info = {r["username"]: r for r in rows}
    users = []
    for u in data.pppoe_users:
        r = info.get(u.username)
        if r:
            u = _salin(u, nama=r["nama"], paket=r["paket"], kecepatan_paket=r["kecepatan_mbps"])
        users.append(u)
    return _salin(data, pppoe_users=users)

@app.get("/api/status", response_model=DashboardData)
async def get_status():
    if _latest_data is None:
        pppoe_users, mikrotik_error = await asyncio.to_thread(fetch_pppoe_active)
        onu_list = await fetch_all_onu()
        _attach_pppoe_username(onu_list, pppoe_users)
        data = _build_dashboard(pppoe_users, mikrotik_error, onu_list)
    else:
        data = _latest_data
    return await asyncio.to_thread(_perkaya_pelanggan, data)



# ------------------------------------------------------------
# API Alert & Event
# ------------------------------------------------------------

_URUT_TINGKAT = {"kritis": 0, "peringatan": 1, "info": 2}


@app.get("/api/alert")
def get_alert_aktif():
    """Masalah yang SEDANG terjadi (dihitung dari kondisi terkini), beda dengan
    /api/event yang berisi riwayat kejadian."""
    data = _latest_data
    alert = []
    sekarang = datetime.now()
    if data is not None:
        if not data.mikrotik_online:
            alert.append({"tingkat": "kritis", "sumber": "mikrotik", "judul": "MikroTik tidak terhubung",
                          "detail": data.mikrotik_error or "", "pelanggan": None, "sejak": None})
        if not data.olt_online:
            alert.append({"tingkat": "kritis", "sumber": "olt", "judul": "OLT tidak merespons SNMP",
                          "detail": f"{CONFIG['OLT_HOST']}:{CONFIG['OLT_SNMP_PORT']}", "pelanggan": None, "sejak": None})
        for o in data.onu_list:
            lokasi = f"PON {o.pon} / ONU {o.onu_id}"
            if o.online and o.status in ("CRITICAL", "WARNING"):
                alert.append({"tingkat": "kritis" if o.status == "CRITICAL" else "peringatan", "sumber": "olt",
                              "judul": "Redaman kritis" if o.status == "CRITICAL" else "Redaman mendekati batas",
                              "detail": f"{lokasi} · OLT-RX {o.olt_rx_dbm} / ONU-RX "
                                        f"{'-' if o.onu_rx_dbm is None else o.onu_rx_dbm} dBm",
                              "pelanggan": o.pppoe_username, "sejak": None})
            if o.online and o.hardware_status == "PERLU_CEK":
                alert.append({"tingkat": "peringatan", "sumber": "olt", "judul": "Hardware ONT perlu dicek",
                              "detail": f"{lokasi} · " + "; ".join(o.hardware_notes),
                              "pelanggan": o.pppoe_username, "sejak": None})
    # Pelanggan yang sedang putus (sudah lewat masa tunggu)
    nama = {}
    try:
        nama = {u: d.get("nama") for u, d in _data_pelanggan().items()}
    except Exception:
        pass
    for u, st in list(_putus_aktif.items()):
        if st.get("alert"):
            menit = round((sekarang - st["mulai"]).total_seconds() / 60)
            alert.append({"tingkat": "peringatan", "sumber": "mikrotik", "judul": "Pelanggan putus",
                          "detail": (f"{nama.get(u) or u} · " if nama.get(u) else "")
                          + ("sejak sebelum pemantauan dimulai" if st.get("awal") else f"±{menit} menit"),
                          "pelanggan": u, "sejak": st["mulai"].strftime("%Y-%m-%d %H:%M:%S")})
    res = _mikrotik_resource
    if res and sekarang - res.get("waktu", sekarang) < timedelta(minutes=10):
        if res.get("cpu", 0) >= CONFIG["MIKROTIK_CPU_PERINGATAN"]:
            alert.append({"tingkat": "peringatan", "sumber": "mikrotik", "judul": "CPU MikroTik tinggi",
                          "detail": f"CPU {res['cpu']}%", "pelanggan": None, "sejak": None})
        if (res.get("memori_persen") or 0) >= 90:
            alert.append({"tingkat": "peringatan", "sumber": "mikrotik", "judul": "Memori MikroTik hampir penuh",
                          "detail": f"Terpakai {res['memori_persen']}%", "pelanggan": None, "sejak": None})
    # Percobaan login gagal ke MikroTik dalam 1 jam terakhir
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
    try:
        n, terakhir = conn.execute(
            "SELECT COUNT(*), MAX(waktu) FROM event WHERE kategori='keamanan' AND waktu >= ?",
            ((sekarang - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchone()
    finally:
        conn.close()
    if n:
        alert.append({"tingkat": "kritis", "sumber": "mikrotik", "judul": "Login gagal ke MikroTik",
                      "detail": f"{n}x dalam 1 jam terakhir, terakhir {terakhir[11:16]}. Cek Riwayat untuk asal IP.",
                      "pelanggan": None, "sejak": terakhir})
    alert.sort(key=lambda a: (_URUT_TINGKAT[a["tingkat"]], a["judul"], a["pelanggan"] or ""))
    return {
        "ringkasan": {"kritis": sum(a["tingkat"] == "kritis" for a in alert),
                      "peringatan": sum(a["tingkat"] == "peringatan" for a in alert)},
        "mikrotik": {k: v for k, v in res.items() if k != "waktu"} if res else None,
        "alert": alert,
    }


@app.get("/api/event")
def get_event(jam: int = 24, sumber: str = "", tingkat: str = "", cari: str = "", limit: int = 300):
    jam = max(1, min(jam, 24 * 90))
    limit = max(1, min(limit, 1000))
    sql = "SELECT waktu, sumber, tingkat, kategori, judul, detail, pelanggan FROM event WHERE waktu >= ?"
    arg: list = [(datetime.now() - timedelta(hours=jam)).strftime("%Y-%m-%d %H:%M:%S")]
    if sumber in ("mikrotik", "olt", "sistem"):
        sql += " AND sumber = ?"
        arg.append(sumber)
    if tingkat in ("kritis", "peringatan", "info"):
        sql += " AND tingkat = ?"
        arg.append(tingkat)
    if cari.strip():
        sql += " AND (judul LIKE ? OR detail LIKE ? OR pelanggan LIKE ?)"
        arg += [f"%{cari.strip()}%"] * 3
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql + " ORDER BY waktu DESC, id DESC LIMIT ?", arg + [limit]).fetchall()
        hitung = dict(conn.execute(
            "SELECT tingkat, COUNT(*) FROM event WHERE waktu >= ? GROUP BY tingkat", (arg[0],)).fetchall())
    finally:
        conn.close()
    return {"jam": jam, "ringkasan": {t: hitung.get(t, 0) for t in ("kritis", "peringatan", "info")},
            "event": [dict(r) for r in rows]}


@app.get("/api/pelanggan/{nama}")
async def get_pelanggan(nama: str):
    """Cari pelanggan by nama (substring, case-insensitive) — versi HTTP dari
    perintah /cek di Telegram, buat dipakai lewat browser/curl."""
    matches = find_onu_by_name(nama)
    if not matches:
        return {"found": 0, "results": []}
    return {"found": len(matches), "results": [(m.model_dump() if hasattr(m, "model_dump") else m.dict()) for m in matches]}

_cache_boncos: tuple[float, list] = (0.0, [])


@app.get("/api/boncos")
async def get_boncos():
    # Riwayat pemakaian hanya bertambah tiap HISTORY_INTERVAL_SECONDS, jadi hasilnya
    # disimpan sebentar: query agregat 7 hari ini cukup berat bila diulang tiap beberapa detik di HP.
    global _cache_boncos
    if time.monotonic() - _cache_boncos[0] > 120:
        _cache_boncos = (time.monotonic(), await asyncio.to_thread(get_boncos_candidates))
    return {"candidates": _cache_boncos[1]}


@app.get("/api/putus")
async def get_putus():
    return {"candidates": await asyncio.to_thread(get_frequent_disconnect_candidates)}

class PaketIn(BaseModel):
    nama: str
    kecepatan_mbps: float
    harga: int


def _billing_conn():
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def catat_aktivitas(oleh: str, aksi: str, detail: str = "") -> None:
    """Catat aksi penting (siapa, kapan, apa) untuk audit internal. Tidak boleh
    menggagalkan aksi utamanya, jadi error cukup di-log."""
    try:
        conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
        try:
            conn.execute(
                "INSERT INTO aktivitas (waktu, oleh, aksi, detail) VALUES (?,?,?,?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), oleh, aksi, detail),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Gagal catat aktivitas {aksi}: {e}")


def _oleh(request: Request) -> str:
    return getattr(request.state, "user", None) or "-"


# --- Pengaturan yang bisa diubah admin dari dashboard (tersimpan di tabel
# pengaturan, menimpa nilai default CONFIG). Hanya daftar ini yang boleh diubah. ---
_PENGATURAN = {
    "JATUH_TEMPO_TANGGAL": {"label": "Tanggal jatuh tempo tagihan", "tipe": "int", "min": 1, "max": 28},
    "ISOLIR_MODE": {"label": "Mode isolir otomatis", "tipe": "pilihan", "pilihan": ["aktif", "simulasi"]},
    "ISOLIR_TOLERANSI_HARI": {"label": "Toleransi isolir setelah jatuh tempo (hari)", "tipe": "int", "min": 0, "max": 30},
    "ISOLIR_MAKS_SEKALI": {"label": "Maks. pelanggan diisolir otomatis sekali jalan", "tipe": "int", "min": 1, "max": 500},
    "ISOLIR_JAM": {"label": "Jam isolir otomatis (0-23)", "tipe": "int", "min": 0, "max": 23},
    "TAGIHAN_JAM": {"label": "Jam pembuatan tagihan otomatis (0-23)", "tipe": "int", "min": 0, "max": 23},
    "MITRA_INSENTIF_PER_PELANGGAN": {"label": "Komisi mitra per pelanggan lunas (Rp)", "tipe": "int", "min": 0, "max": 1_000_000},
    "PUTUS_TUNGGU_MENIT": {"label": "Tunggu sebelum alert putus (menit)", "tipe": "int", "min": 1, "max": 240},
    "UPSTREAM_KAPASITAS_MBPS": {"label": "Kapasitas download internet pusat dari ISP (Mbps, 0 = alert bandwidth mati)",
                                "tipe": "int", "min": 0, "max": 100000},
    "UPSTREAM_WAN": {"label": "Interface WAN MikroTik ke ISP (kosong = deteksi otomatis)", "tipe": "teks",
                     "pola": r"^[\w.\-<>@ ]{0,64}$", "contoh": "ether1"},
    "UPSTREAM_TUJUAN": {"label": "Tujuan ping internet pusat (pisahkan koma)", "tipe": "teks",
                        "pola": r"^\s*[A-Za-z0-9.\-]{1,253}(\s*,\s*[A-Za-z0-9.\-]{1,253}){0,4}\s*$", "contoh": "8.8.8.8,1.1.1.1"},
    "PUTUS_SENYAP_MALAM": {"label": "Jam malam: listrik ONU mati tidak dikabarkan satu per satu (mis. 21-06, kosong = selalu kabarkan)",
                           "tipe": "teks", "pola": r"^\s*((\d|1\d|2[0-3])\s*-\s*(\d|1\d|2[0-3]))?\s*$", "contoh": "21-06",
                           "bantuan": "Masuk ringkasan laporan harian. Gangguan massal tetap dikabarkan langsung."},
    "SHORT_UPTIME_MIN_COUNT": {"label": "Laporan 'sering sambung ulang': minimal sambung ulang per hari", "tipe": "int",
                               "min": 2, "max": 100},
    "TIKET_PENGINGAT_JAM": {"label": "Ingatkan tiket gangguan yang belum selesai setelah (jam, 0 = mati)", "tipe": "int",
                            "min": 0, "max": 168},
    "DAILY_REPORT_HOUR": {"label": "Jam laporan harian Telegram (0-23)", "tipe": "int", "min": 0, "max": 23},
    "LAPORAN_BULANAN_AKTIF": {"label": "Laporan keuangan bulanan ke Telegram owner", "tipe": "pilihan",
                              "pilihan": ["ya", "tidak"], "owner": True},
    "LAPORAN_BULANAN_JAM": {"label": "Jam kirim laporan bulanan, tanggal 1 (0-23)", "tipe": "int", "min": 0, "max": 23,
                            "owner": True},
    "NAMA_USAHA": {"label": "Nama usaha (tampil di pesan WA & kwitansi)", "tipe": "teksbebas", "maks": 60},
    "INFO_BAYAR": {"label": "Cara bayar di pesan WA (nomor rekening, QRIS, nama admin)", "tipe": "teksbebas", "maks": 500,
                   "baris": 3, "bantuan": "Contoh: Transfer BRI 1234-01-000123-50-6 a.n. Arif, atau QRIS di kios. "
                                          "Kirim bukti ke nomor ini."},
    "TEMPLATE_PENGINGAT": {"label": "Teks pengingat tagihan WA", "tipe": "teksbebas", "maks": 1500, "baris": 8,
                           "bantuan": "Penanda yang diisi otomatis: {nama} {usaha} {rincian} (daftar bulan & jumlah) "
                                      "{total} {cara_bayar} {username}. Huruf di antara *bintang* jadi tebal di WA."},
    "PENGINGAT_JEDA_HARI": {"label": "Pengingat massal: lewati pelanggan yang sudah diingatkan dalam (hari)", "tipe": "int",
                            "min": 0, "max": 30},
    "KOMISI_MULAI": {"label": "Komisi mitra dihitung mulai bulan (YYYY-MM, kosong = sejak data pertama)", "tipe": "teks",
                     "pola": r"^\s*(\d{4}-(0[1-9]|1[0-2]))?\s*$", "owner": True, "contoh": "2026-10",
                     "bantuan": "Bulan sebelumnya tidak ditampilkan dan tidak dihitung sebagai komisi belum dibayar."},
    "TELEGRAM_OWNER_CHAT_ID": {"label": "Chat ID tujuan laporan keuangan: grup internal owner dan/atau chat pribadi (pisahkan koma)",
                               "tipe": "teks", "pola": r"^\s*(-?\d{5,20}\s*(,\s*-?\d{5,20}\s*)*)?$", "owner": True,
                               "bantuan": "Grup: tambahkan bot ke grup internal owner, lalu kirim /id di grup itu "
                                          "(ID grup diawali tanda minus). Chat pribadi: kirim /id ke bot."},
}


def _validasi_pengaturan(kunci: str, nilai):
    meta = _PENGATURAN.get(kunci)
    if meta is None:
        raise HTTPException(status_code=400, detail=f"Pengaturan {kunci} tidak bisa diubah dari dashboard.")
    if meta["tipe"] == "int":
        try:
            nilai = int(nilai)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{meta['label']} harus angka.")
        if not meta["min"] <= nilai <= meta["max"]:
            raise HTTPException(status_code=400, detail=f"{meta['label']} harus {meta['min']}–{meta['max']}.")
        return nilai
    if meta["tipe"] == "teksbebas":
        nilai = str(nilai or "").replace("\r\n", "\n").strip()
        if not nilai:
            raise HTTPException(status_code=400, detail=f"{meta['label']} tidak boleh kosong.")
        if len(nilai) > meta["maks"]:
            raise HTTPException(status_code=400, detail=f"{meta['label']} maksimal {meta['maks']} huruf.")
        return nilai
    if meta["tipe"] == "teks":
        nilai = str(nilai or "").strip()
        if not re.fullmatch(meta["pola"], nilai):
            raise HTTPException(status_code=400, detail=f"{meta['label']}: format tidak valid.")
        return ",".join(x.strip() for x in nilai.split(",") if x.strip())
    if nilai not in meta["pilihan"]:
        raise HTTPException(status_code=400, detail=f"{meta['label']} harus salah satu: {', '.join(meta['pilihan'])}.")
    return nilai


def _muat_pengaturan() -> None:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS pengaturan (kunci TEXT PRIMARY KEY, nilai TEXT NOT NULL)")
        conn.commit()
        for kunci, nilai in conn.execute("SELECT kunci, nilai FROM pengaturan").fetchall():
            try:
                CONFIG[kunci] = _validasi_pengaturan(kunci, nilai)
            except HTTPException as e:
                logger.warning(f"Pengaturan tersimpan {kunci}={nilai!r} diabaikan: {e.detail}")
    finally:
        conn.close()


@app.get("/api/pengaturan")
def get_pengaturan(request: Request):
    _wajib_admin(request)
    folder = _folder_backup()
    backup = _daftar_backup(folder)
    return {
        "peran": request.state.peran,
        "pengaturan": [{"kunci": k, **m, "nilai": CONFIG[k]} for k, m in _PENGATURAN.items()],
        "sistem": {
            "mikrotik": f"{CONFIG['MIKROTIK_HOST']}:{CONFIG['MIKROTIK_PORT']} (user {CONFIG['MIKROTIK_USER']})",
            "olt": f"{CONFIG['OLT_HOST']}:{CONFIG['OLT_SNMP_PORT']} (SNMP v{CONFIG['OLT_SNMP_VERSION']})",
            "profil_isolir": CONFIG["ISOLIR_PROFILE"],
            "telegram": "aktif" if CONFIG["TELEGRAM_ENABLED"] else "mati",
            "backup_terakhir": backup[-1] if backup else None,
            "jumlah_backup": len(backup),
            "data_terakhir": _latest_data.timestamp if _latest_data else None,
            "sesi_idle_jam": CONFIG["SESI_IDLE_JAM"],
            "backup_nota_terakhir": _status_get("backup_nota_terakhir_pada") or None,
            "nota_belum_dibackup": _hitung_nota_belum_backup(),
            "backup_ke": "tujuan laporan owner" if _chat_owner() else "grup operasional (isi chat owner agar lebih aman)",
            "nota_mb": round(sum(os.path.getsize(os.path.join(d, f)) for d, _s, fs in os.walk(_folder_nota())
                                 for f in fs) / 1048576, 1),
        },
    }


class PengaturanIn(BaseModel):
    nilai: dict


@app.put("/api/pengaturan")
def ubah_pengaturan(d: PengaturanIn, request: Request):
    _wajib_admin(request)
    baru = {k: _validasi_pengaturan(k, v) for k, v in d.nilai.items()}
    berubah = {k: v for k, v in baru.items() if CONFIG[k] != v}
    terkunci = [k for k in berubah if _PENGATURAN[k].get("owner") and request.state.peran != "owner"]
    if terkunci:
        raise HTTPException(status_code=403, detail="Hanya owner yang bisa mengubah: "
                            + ", ".join(_PENGATURAN[k]["label"] for k in terkunci))
    if berubah:
        conn = _billing_conn()
        try:
            conn.executemany("INSERT OR REPLACE INTO pengaturan (kunci, nilai) VALUES (?,?)",
                             [(k, str(v)) for k, v in berubah.items()])
            conn.commit()
        finally:
            conn.close()
        detail = "; ".join(f"{_PENGATURAN[k]['label']}: {CONFIG[k]} → {v}" for k, v in berubah.items())
        CONFIG.update(berubah)
        catat_aktivitas(request.state.user, "pengaturan_ubah", detail)
    return {"ok": True, "berubah": len(berubah)}


@app.get("/api/aktivitas")
def list_aktivitas(request: Request, limit: int = 200, cari: str = ""):
    _wajib_admin(request)
    limit = max(1, min(limit, 1000))
    conn = _billing_conn()
    try:
        if cari.strip():
            pola = f"%{cari.strip()}%"
            rows = conn.execute(
                "SELECT waktu, oleh, aksi, detail FROM aktivitas "
                "WHERE detail LIKE ? OR aksi LIKE ? OR oleh LIKE ? ORDER BY id DESC LIMIT ?",
                (pola, pola, pola, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT waktu, oleh, aksi, detail FROM aktivitas ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return {"aktivitas": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/paket")
def list_paket():
    conn = _billing_conn()
    try:
        rows = conn.execute(
            "SELECT k.id, k.nama, k.kecepatan_mbps, k.harga, "
            "(SELECT COUNT(*) FROM pelanggan p WHERE p.paket_id = k.id AND p.status = 'aktif') AS jumlah_pelanggan "
            "FROM paket k ORDER BY k.kecepatan_mbps, k.harga"
        ).fetchall()
        return {"paket": [dict(r) for r in rows]}
    finally:
        conn.close()


def _cek_paket(p: PaketIn) -> str:
    nama = p.nama.strip()
    if not nama or p.kecepatan_mbps <= 0 or p.harga < 0:
        raise HTTPException(status_code=400, detail="Data paket tidak valid: nama wajib, kecepatan > 0, harga tidak negatif.")
    return nama


@app.post("/api/paket")
def tambah_paket(p: PaketIn, request: Request):
    nama = _cek_paket(p)
    conn = _billing_conn()
    try:
        try:
            cur = conn.execute(
                "INSERT INTO paket (nama, kecepatan_mbps, harga) VALUES (?,?,?)",
                (nama, p.kecepatan_mbps, p.harga),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Nama paket sudah ada.")
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "paket_tambah", f"{nama} {p.kecepatan_mbps:g} Mbps {_rp(p.harga)}")
    return {"id": cur.lastrowid}


@app.put("/api/paket/{paket_id}")
def ubah_paket(paket_id: int, p: PaketIn, request: Request):
    """Harga baru hanya berlaku untuk tagihan yang dibuat setelahnya; tagihan yang
    sudah ada tidak berubah."""
    nama = _cek_paket(p)
    conn = _billing_conn()
    try:
        lama = conn.execute("SELECT nama, kecepatan_mbps, harga FROM paket WHERE id=?", (paket_id,)).fetchone()
        if not lama:
            raise HTTPException(status_code=404, detail="Paket tidak ditemukan.")
        try:
            conn.execute(
                "UPDATE paket SET nama=?, kecepatan_mbps=?, harga=? WHERE id=?",
                (nama, p.kecepatan_mbps, p.harga, paket_id),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Nama paket sudah dipakai paket lain.")
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(
        _oleh(request), "paket_ubah",
        f"{lama['nama']} {lama['kecepatan_mbps']:g} Mbps {_rp(lama['harga'])} → "
        f"{nama} {p.kecepatan_mbps:g} Mbps {_rp(p.harga)}",
    )
    return {"ok": True}


@app.delete("/api/paket/{paket_id}")
def hapus_paket(paket_id: int, request: Request):
    conn = _billing_conn()
    try:
        lama = conn.execute("SELECT nama FROM paket WHERE id=?", (paket_id,)).fetchone()
        if not lama:
            raise HTTPException(status_code=404, detail="Paket tidak ditemukan.")
        dipakai = conn.execute("SELECT COUNT(*) FROM pelanggan WHERE paket_id=?", (paket_id,)).fetchone()[0]
        if dipakai:
            raise HTTPException(
                status_code=409,
                detail=f"Paket masih dipakai {dipakai} pelanggan (termasuk yang berhenti). Pindahkan dulu ke paket lain.",
            )
        conn.execute("DELETE FROM paket WHERE id=?", (paket_id,))
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "paket_hapus", lama["nama"])
    return {"ok": True}

def fetch_ppp_secrets() -> list[dict]:
    connection = _mikrotik_pool()
    try:
        api = connection.get_api()
        rows = api.get_resource("/ppp/secret").get()
        return [
            {"name": r.get("name"), "profile": r.get("profile")}
            for r in rows if r.get("name")
        ]
    finally:
        connection.disconnect()


def _kecepatan_dari_profil(profile: Optional[str]) -> Optional[float]:
    m = re.search(r"(\d+)\s*mbps", (profile or "").lower())
    return float(m.group(1)) if m else None


@app.post("/api/impor-pelanggan")
def impor_pelanggan(request: Request):
    try:
        secrets = fetch_ppp_secrets()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gagal baca MikroTik: {e}")
    conn = _billing_conn()
    try:
        paket_by_speed = {
            r["kecepatan_mbps"]: r["id"]
            for r in conn.execute("SELECT id, kecepatan_mbps FROM paket")
        }
        baru, sudah_ada, tanpa_paket = 0, 0, []
        for s in secrets:
            paket_id = paket_by_speed.get(_kecepatan_dari_profil(s["profile"]))
            if paket_id is None:
                tanpa_paket.append(f'{s["name"]} ({s["profile"]})')
            cur = conn.execute(
                "INSERT OR IGNORE INTO pelanggan (username, nama, paket_id) VALUES (?,?,?)",
                (s["name"], s["name"], paket_id),
            )
            if cur.rowcount:
                baru += 1
            else:
                sudah_ada += 1
                if paket_id is not None:
                    conn.execute(
                        "UPDATE pelanggan SET paket_id=? WHERE username=? AND paket_id IS NULL",
                        (paket_id, s["name"]),
                    )
        # Pelanggan aktif yang secret-nya sudah tidak ada di MikroTik: kemungkinan
        # sudah berhenti, perlu ditandai supaya tidak terus ditagih.
        nama_secret = {s["name"] for s in secrets}
        hilang = [
            {"username": r["username"], "nama": r["nama"]}
            for r in conn.execute("SELECT username, nama FROM pelanggan WHERE status='aktif' ORDER BY nama COLLATE NOCASE")
            if r["username"] not in nama_secret
        ]
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "impor_pelanggan",
                    f"{len(secrets)} secret, {baru} baru, {len(hilang)} tidak ada di MikroTik")
    return {"total_secret": len(secrets), "baru": baru, "sudah_ada": sudah_ada,
            "tanpa_paket": tanpa_paket, "tidak_ada_di_mikrotik": hilang}

def _cek_tgl_pasang(t: str) -> str:
    t = (t or "").strip()
    if not t:
        return ""
    try:
        datetime.strptime(t, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Tanggal pasang harus format YYYY-MM-DD.")
    return t


def _cek_diskon(n) -> int:
    n = int(n or 0)
    if not 0 <= n <= 10_000_000:
        raise HTTPException(status_code=400, detail="Diskon tidak valid.")
    return n


class PelangganIn(BaseModel):
    nama: str
    alamat: Optional[str] = None
    no_hp: Optional[str] = None
    paket_id: Optional[int] = None
    gratis: bool = False
    status: Optional[str] = None    # "aktif" / "berhenti"; None = tidak diubah
    tampil_mitra: Optional[bool] = None   # None = tidak diubah
    insentif_gratis: Optional[bool] = None   # None = tidak diubah
    atur_secret: bool = True   # saat status berubah: nonaktifkan/aktifkan secret PPPoE di MikroTik
    tgl_pasang: Optional[str] = None   # None = tidak diubah, "" = dikosongkan
    diskon: Optional[int] = None       # None = tidak diubah


@app.get("/api/data-pelanggan")
def list_data_pelanggan():
    conn = _billing_conn()
    try:
        rows = conn.execute(
            "SELECT p.username, p.nama, p.alamat, p.no_hp, p.paket_id, p.gratis, "
            "p.status, p.berhenti_sejak, p.tampil_mitra, p.insentif_gratis, p.secret_target, p.secret_nonaktif, "
            "p.tgl_pasang, p.diskon, k.nama AS paket, k.harga "
            "FROM pelanggan p LEFT JOIN paket k ON k.id = p.paket_id "
            "ORDER BY p.nama COLLATE NOCASE"
        ).fetchall()
        return {"pelanggan": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/mikrotik/profil")
def mikrotik_profil(request: Request):
    """Daftar profil PPPoE di MikroTik untuk formulir pasang baru."""
    _wajib_admin(request)
    connection = _mikrotik_pool()
    try:
        rows = connection.get_api().get_resource("/ppp/profile").get()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gagal baca MikroTik: {e}")
    finally:
        try:
            connection.disconnect()
        except Exception:
            pass
    nama = [r.get("name") for r in rows if r.get("name") and r.get("name") != CONFIG["ISOLIR_PROFILE"]]
    return {"profil": [{"nama": n, "mbps": _kecepatan_dari_profil(n)} for n in nama]}


class PasangBaruIn(BaseModel):
    username: str
    nama: str
    alamat: Optional[str] = None
    no_hp: Optional[str] = None
    paket_id: int
    tgl_pasang: str
    diskon: int = 0
    buat_secret: bool = False          # buat akun PPPoE di MikroTik sekalian
    password_pppoe: Optional[str] = None
    profil: Optional[str] = None       # profil PPPoE MikroTik; kosong = dicocokkan dari kecepatan paket
    biaya_pasang: int = 0              # uang pemasangan yang diterima (0 = tidak ada / belum dibayar)
    metode_bayar: str = "tunai"
    tagih_bulan_ini: bool = True       # langsung buat tagihan prorata bulan pasang


@app.post("/api/pasang-baru")
def pasang_baru(d: PasangBaruIn, request: Request):
    """Pelanggan baru dalam satu langkah: data pelanggan, akun PPPoE (opsional), biaya pasang ke
    Keuangan (opsional), dan tagihan bulan pertama yang dihitung prorata."""
    _wajib_admin(request)
    username = d.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9._@-]{2,40}", username):
        raise HTTPException(status_code=400, detail="Username PPPoE 2–40 huruf/angka (boleh . _ - @), tanpa spasi.")
    nama = d.nama.strip()
    if not nama:
        raise HTTPException(status_code=400, detail="Nama pelanggan wajib diisi.")
    tgl = _cek_tgl_pasang(d.tgl_pasang)
    if not tgl:
        raise HTTPException(status_code=400, detail="Tanggal pasang wajib diisi.")
    if tgl > datetime.now().strftime("%Y-%m-%d"):
        raise HTTPException(status_code=400, detail="Tanggal pasang tidak boleh di masa depan.")
    diskon = _cek_diskon(d.diskon)
    if d.biaya_pasang < 0 or d.metode_bayar not in _METODE:
        raise HTTPException(status_code=400, detail="Biaya pasang / metode bayar tidak valid.")
    conn = _billing_conn()
    try:
        paket = conn.execute("SELECT * FROM paket WHERE id = ?", (d.paket_id,)).fetchone()
        if not paket:
            raise HTTPException(status_code=400, detail="Paket tidak ditemukan.")
        if conn.execute("SELECT 1 FROM pelanggan WHERE username = ?", (username,)).fetchone():
            raise HTTPException(status_code=409, detail=f"Username {username} sudah terdaftar di INDESO.")
    finally:
        conn.close()

    # 1. Akun PPPoE di MikroTik (dulu, supaya data INDESO tidak tercatat bila MikroTik gagal)
    profil = None
    if d.buat_secret:
        sandi = (d.password_pppoe or "").strip()
        if not re.fullmatch(r"[^\s\"';\\]{4,64}", sandi):
            raise HTTPException(status_code=400, detail="Password PPPoE 4–64 karakter, tanpa spasi atau tanda kutip.")
        connection = _mikrotik_pool()
        try:
            api = connection.get_api()
            if api.get_resource("/ppp/secret").get(name=username):
                raise HTTPException(status_code=409, detail=f"Akun PPPoE {username} sudah ada di MikroTik. "
                                                            "Hilangkan centang 'buat akun PPPoE' atau pakai username lain.")
            daftar = [r.get("name") for r in api.get_resource("/ppp/profile").get() if r.get("name")]
            profil = (d.profil or "").strip() or next(
                (n for n in daftar if _kecepatan_dari_profil(n) == paket["kecepatan_mbps"]), None)
            if not profil or profil not in daftar:
                raise HTTPException(status_code=400, detail=f"Profil PPPoE untuk paket {paket['nama']} tidak ditemukan "
                                                            "di MikroTik. Pilih profilnya di formulir.")
            api.get_resource("/ppp/secret").add(name=username, password=sandi, service="pppoe", profile=profil,
                                                comment=f"INDESO {nama[:40]} pasang {tgl}")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Gagal membuat akun PPPoE di MikroTik: {e}")
        finally:
            try:
                connection.disconnect()
            except Exception:
                pass

    # 2. Data pelanggan + tagihan bulan pertama
    periode = tgl[:7]
    conn = _billing_conn()
    try:
        conn.execute("INSERT INTO pelanggan (username, nama, alamat, no_hp, paket_id, tgl_pasang, diskon) VALUES (?,?,?,?,?,?,?)",
                     (username, nama, (d.alamat or "").strip() or None, (d.no_hp or "").strip() or None,
                      d.paket_id, tgl, diskon))
        tagihan = None
        if d.tagih_bulan_ini and _buat_tagihan_satu(conn, username, periode):
            tagihan = dict(conn.execute("SELECT id, periode, jumlah, jatuh_tempo, catatan FROM tagihan "
                                        "WHERE username = ? AND periode = ?", (username, periode)).fetchone())
        conn.commit()
    finally:
        conn.close()

    # 3. Biaya pasang ke Keuangan (nomor bukti, buku tertutup, dll. lewat jalur pencatatan biasa)
    transaksi = None
    if d.biaya_pasang > 0:
        conn = _billing_conn()
        try:
            k = conn.execute("SELECT id FROM kategori_kas WHERE jenis = 'masuk' AND nama IN "
                             "('Pendapatan jasa pemasangan', 'Biaya pemasangan baru') ORDER BY id LIMIT 1").fetchone()
        finally:
            conn.close()
        if k:
            try:
                transaksi = keuangan_tambah(TransaksiIn(tanggal=tgl, jenis="masuk", kategori_id=k[0], jumlah=d.biaya_pasang,
                                                        metode=d.metode_bayar, keterangan=f"Pasang baru {nama} ({username})"),
                                            request)
            except HTTPException as e:
                transaksi = {"galat": e.detail}

    catat_aktivitas(_oleh(request), "pasang_baru",
                    f"{username} ({nama}) paket {paket['nama']} pasang {tgl}"
                    + (f", diskon {_rp(diskon)}/bln" if diskon else "")
                    + (f", akun PPPoE dibuat (profil {profil})" if profil else "")
                    + (f", tagihan {periode} {_rp(tagihan['jumlah'])}" if tagihan else "")
                    + (f", biaya pasang {_rp(d.biaya_pasang)}" if d.biaya_pasang else ""))
    return {"ok": True, "username": username, "profil": profil, "tagihan": tagihan, "transaksi": transaksi}


@app.put("/api/data-pelanggan/{username}")
def ubah_data_pelanggan(username: str, d: PelangganIn, request: Request):
    nama = d.nama.strip()
    if not nama:
        raise HTTPException(status_code=400, detail="Nama tidak boleh kosong.")
    if d.status is not None and d.status not in ("aktif", "berhenti"):
        raise HTTPException(status_code=400, detail="Status harus 'aktif' atau 'berhenti'.")
    sekarang = datetime.now()
    conn = _billing_conn()
    try:
        lama = conn.execute(
            "SELECT p.*, k.nama AS paket FROM pelanggan p LEFT JOIN paket k ON k.id = p.paket_id WHERE p.username=?",
            (username,),
        ).fetchone()
        if not lama:
            raise HTTPException(status_code=404, detail="Pelanggan tidak ditemukan.")
        paket_baru = None
        if d.paket_id is not None:
            paket_baru = conn.execute("SELECT nama FROM paket WHERE id=?", (d.paket_id,)).fetchone()
            if not paket_baru:
                raise HTTPException(status_code=400, detail="Paket tidak ditemukan.")
        status = d.status or lama["status"]
        berhenti_sejak = lama["berhenti_sejak"]
        if status == "berhenti" and lama["status"] != "berhenti":
            berhenti_sejak = sekarang.strftime("%Y-%m-%d")
        elif status == "aktif":
            berhenti_sejak = None
        tgl_pasang = lama["tgl_pasang"] if d.tgl_pasang is None else (_cek_tgl_pasang(d.tgl_pasang) or None)
        diskon = lama["diskon"] if d.diskon is None else _cek_diskon(d.diskon)
        tampil_mitra = lama["tampil_mitra"] if d.tampil_mitra is None else int(d.tampil_mitra)
        insentif_gratis = lama["insentif_gratis"] if d.insentif_gratis is None else int(d.insentif_gratis)
        if not d.gratis:
            insentif_gratis = 0          # pelanggan berbayar dihitung dari tagihan lunas
        elif insentif_gratis:
            tampil_mitra = 1             # dihitung insentif = pasti terlihat oleh mitra
        conn.execute(
            "UPDATE pelanggan SET nama=?, alamat=?, no_hp=?, paket_id=?, gratis=?, status=?, berhenti_sejak=?, "
            "tampil_mitra=?, insentif_gratis=?, tgl_pasang=?, diskon=? WHERE username=?",
            (nama, (d.alamat or "").strip() or None, (d.no_hp or "").strip() or None,
             d.paket_id, int(d.gratis), status, berhenti_sejak, tampil_mitra, insentif_gratis, tgl_pasang, diskon, username),
        )
        terhapus, hapus_rinci = 0, []
        syarat = None
        if d.gratis:
            syarat = ("username=? AND status='belum'", (username,))
        elif status == "berhenti" and lama["status"] != "berhenti":
            # Tagihan bulan-bulan setelah bulan berhenti dibatalkan; tunggakan
            # sampai bulan ini tetap ada karena layanannya sudah dipakai.
            syarat = ("username=? AND status='belum' AND periode > ?", (username, sekarang.strftime("%Y-%m")))
        if syarat:
            # Penghapusan piutang dicatat rinci (periode & nominal) sebagai jejak audit.
            hapus_rinci = conn.execute(f"SELECT periode, jumlah FROM tagihan WHERE {syarat[0]} ORDER BY periode",
                                       syarat[1]).fetchall()
            terhapus = conn.execute(f"DELETE FROM tagihan WHERE {syarat[0]}", syarat[1]).rowcount
        conn.commit()
    finally:
        conn.close()

    perubahan = []
    if lama["nama"] != nama:
        perubahan.append(f"nama {lama['nama']} → {nama}")
    if lama["paket_id"] != d.paket_id:
        perubahan.append(f"paket {lama['paket'] or '-'} → {paket_baru['nama'] if paket_baru else '-'}")
    if bool(lama["gratis"]) != d.gratis:
        perubahan.append("jadi GRATIS" if d.gratis else "tidak gratis lagi")
    if lama["status"] != status:
        perubahan.append(f"status {lama['status']} → {status}")
    if lama["tampil_mitra"] != tampil_mitra:
        perubahan.append("ditampilkan ke mitra" if tampil_mitra else "disembunyikan dari mitra")
    if lama["insentif_gratis"] != insentif_gratis:
        perubahan.append("gratis dihitung komisi mitra" if insentif_gratis else "gratis tidak dihitung komisi")
    if (lama["tgl_pasang"] or "") != (tgl_pasang or ""):
        perubahan.append(f"tanggal pasang {lama['tgl_pasang'] or '-'} → {tgl_pasang or '-'}")
    if (lama["diskon"] or 0) != diskon:
        perubahan.append(f"diskon {_rp(lama['diskon'] or 0)} → {_rp(diskon)}/bulan")
    if (lama["alamat"] or "") != (d.alamat or "").strip() or (lama["no_hp"] or "") != (d.no_hp or "").strip():
        perubahan.append("kontak/alamat diubah")
    if terhapus:
        perubahan.append(f"{terhapus} tagihan belum bayar dihapus ({_rp(sum(r['jumlah'] for r in hapus_rinci))}: "
                         + ", ".join(f"{r['periode']} {_rp(r['jumlah'])}" for r in hapus_rinci[:12]) + ")")
    if perubahan:
        catat_aktivitas(_oleh(request), "pelanggan_ubah", f"{username}: " + "; ".join(perubahan))

    # Status berubah -> nonaktifkan / aktifkan secret PPPoE di MikroTik
    secret = None
    if lama["status"] != status:
        if d.atur_secret:
            secret = _terapkan_secret(username, "nonaktif" if status == "berhenti" else "aktif", _oleh(request))
        elif lama["secret_target"]:
            # Sinkron lama yang masih tertunda tidak berlaku lagi setelah status diubah,
            # supaya pelanggan yang baru diaktifkan tidak ikut dinonaktifkan nanti.
            conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
            try:
                conn.execute("UPDATE pelanggan SET secret_target = NULL WHERE username = ?", (username,))
                conn.commit()
            finally:
                conn.close()
            catat_aktivitas(_oleh(request), "secret_tertunda", f"{username}: sinkron secret tertunda dibatalkan (status diubah)")
    return {"ok": True, "tagihan_dihapus": terhapus, "secret": secret}


def _atur_secret_mikrotik(username: str, nonaktif: bool) -> tuple[bool, str]:
    """disabled=yes/no pada /ppp/secret; saat dinonaktifkan sesi aktifnya diputus.
    Secret TIDAK dihapus, jadi password & profil tetap ada bila pelanggan kembali."""
    pool = _mikrotik_pool()
    try:
        api = pool.get_api()
        secret = api.get_resource("/ppp/secret")
        rows = secret.get(name=username)
        if not rows:
            return False, "secret tidak ditemukan di MikroTik"
        secret.set(id=rows[0]["id"], disabled="yes" if nonaktif else "no")
        putus = 0
        if nonaktif:
            aktif = api.get_resource("/ppp/active")
            for sesi in aktif.get(name=username):
                aktif.remove(id=sesi["id"])
                putus += 1
        return True, ("dinonaktifkan" + (", sesi aktif diputus" if putus else "")) if nonaktif else "diaktifkan"
    finally:
        pool.disconnect()


_KATA_SECRET = {"nonaktif": ("dinonaktifkan", "menonaktifkan"), "aktif": ("diaktifkan", "mengaktifkan")}


def _terapkan_secret(username: str, target: str, oleh: str, ulang: bool = False) -> dict:
    """Terapkan target ('nonaktif'/'aktif'). Bila MikroTik gagal, simpan sebagai
    tertunda (secret_target) supaya dicoba ulang tiap siklus pemantauan."""
    try:
        ok, pesan = _atur_secret_mikrotik(username, target == "nonaktif")
    except Exception as e:
        ok, pesan = False, f"MikroTik tidak terbaca: {e}"
    tidak_ada = not ok and "tidak ditemukan" in pesan
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        if ok or tidak_ada:
            conn.execute("UPDATE pelanggan SET secret_target = NULL, secret_nonaktif = ? WHERE username = ?",
                         (1 if (ok and target == "nonaktif") else 0, username))
        else:
            conn.execute("UPDATE pelanggan SET secret_target = ? WHERE username = ?", (target, username))
        conn.commit()
    finally:
        conn.close()
    if ok:
        catat_aktivitas(oleh, "secret_" + target, f"{username}: secret PPPoE {pesan}")
    elif tidak_ada:
        catat_aktivitas(oleh, "secret_" + target, f"{username}: {pesan} (dilewati)")
    elif not ulang:      # percobaan ulang otomatis tidak dicatat tiap siklus
        catat_aktivitas(oleh, "secret_tertunda",
                        f"{username}: gagal {_KATA_SECRET[target][1]} secret ({pesan[:120]}), dicoba ulang otomatis")
    return {"ok": ok, "pesan": pesan, "tertunda": not ok and not tidak_ada}


_secret_gagal_dikabari: set = set()


def proses_sinkron_secret() -> None:
    """Tiap siklus: coba lagi secret yang tertunda (MikroTik sempat tak terjangkau)."""
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        tertunda = conn.execute(
            "SELECT username, nama, secret_target FROM pelanggan WHERE secret_target IS NOT NULL").fetchall()
    except sqlite3.OperationalError:
        return
    finally:
        conn.close()
    for username, nama, target in tertunda:
        hasil = _terapkan_secret(username, target, "sistem", ulang=True)
        if hasil["ok"]:
            _secret_gagal_dikabari.discard(username)
            send_telegram_message(f"✅ Secret PPPoE {_esc(nama or username)} berhasil {_KATA_SECRET[target][0]} "
                                  "(sempat tertunda karena MikroTik tidak terjangkau).")
        elif hasil["tertunda"] and username not in _secret_gagal_dikabari:
            _secret_gagal_dikabari.add(username)
            send_telegram_message(f"⚠️ Gagal {_KATA_SECRET[target][1]} secret PPPoE {_esc(nama or username)}: "
                                  f"{_esc(hasil['pesan'][:150])}\nDicoba ulang otomatis tiap siklus.")


def _periode_valid(periode: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", periode))


@app.post("/api/tagihan/buat")
def buat_tagihan_api(periode: str, request: Request):
    hasil = buat_tagihan(periode)
    catat_aktivitas(_oleh(request), "tagihan_buat",
                    f"periode {periode}: {hasil['baru']} baru, {hasil['sudah_ada']} sudah ada")
    return hasil


def _hitung_tagihan(harga: int, diskon: int, tgl_pasang: Optional[str], periode: str) -> Optional[tuple]:
    """(jumlah, jatuh_tempo, catatan) untuk satu pelanggan di satu periode, atau None bila belum
    berlangganan di periode itu. Bulan pertama dihitung prorata sejak tanggal pasang:
    (harga − diskon) × sisa hari ÷ jumlah hari bulan itu, dibulatkan ke Rp1.000 terdekat."""
    import calendar
    tahun, bulan = map(int, periode.split("-"))
    hari_bulan = calendar.monthrange(tahun, bulan)[1]
    jatuh_tempo = f"{periode}-{CONFIG['JATUH_TEMPO_TANGGAL']:02d}"
    dasar = max(0, int(harga) - int(diskon or 0))
    catatan = [f"diskon {_rp(diskon)}"] if diskon else []
    if tgl_pasang:
        if tgl_pasang[:7] > periode:
            return None
        if tgl_pasang[:7] == periode and int(tgl_pasang[8:10]) > 1:
            hari = hari_bulan - int(tgl_pasang[8:10]) + 1
            dasar = int(dasar * hari / hari_bulan / 1000 + 0.5) * 1000
            catatan.insert(0, f"prorata {hari}/{hari_bulan} hari sejak {tgl_pasang}")
            # beri waktu bayar: paling cepat 7 hari setelah pasang
            batas = (datetime.strptime(tgl_pasang, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")
            jatuh_tempo = max(jatuh_tempo, batas)
    if dasar <= 0:
        return None
    return dasar, jatuh_tempo, "; ".join(catatan) or None


def _buat_tagihan_satu(conn, username: str, periode: str) -> int:
    r = conn.execute("SELECT k.harga, p.diskon, p.tgl_pasang FROM pelanggan p JOIN paket k ON k.id = p.paket_id "
                     "WHERE p.username = ? AND p.gratis = 0 AND p.status = 'aktif'", (username,)).fetchone()
    if not r:
        return 0
    h = _hitung_tagihan(r["harga"], r["diskon"], r["tgl_pasang"], periode)
    if not h:
        return 0
    return conn.execute("INSERT OR IGNORE INTO tagihan (username, periode, jumlah, jatuh_tempo, catatan) VALUES (?,?,?,?,?)",
                        (username, periode, *h)).rowcount


def buat_tagihan(periode: str):
    if not _periode_valid(periode):
        raise HTTPException(status_code=400, detail="Format periode harus YYYY-MM, contoh 2026-11.")
    jatuh_tempo = f"{periode}-{CONFIG['JATUH_TEMPO_TANGGAL']:02d}"
    conn = _billing_conn()
    try:
        rows = conn.execute(
            "SELECT p.username FROM pelanggan p JOIN paket k ON k.id = p.paket_id "
            "WHERE p.gratis = 0 AND p.status = 'aktif'"
        ).fetchall()
        baru = 0
        for r in rows:
            baru += _buat_tagihan_satu(conn, r["username"], periode)
        tanpa_paket = conn.execute(
            "SELECT COUNT(*) FROM pelanggan WHERE paket_id IS NULL AND gratis = 0 AND status = 'aktif'"
        ).fetchone()[0]
        conn.commit()
        return {"periode": periode, "jatuh_tempo": jatuh_tempo, "baru": baru,
                "sudah_ada": len(rows) - baru, "tanpa_paket": tanpa_paket}
    finally:
        conn.close()


@app.get("/api/tagihan")
def list_tagihan(periode: str):
    if not _periode_valid(periode):
        raise HTTPException(status_code=400, detail="Format periode harus YYYY-MM, contoh 2026-11.")
    conn = _billing_conn()
    try:
        rows = conn.execute(
            "SELECT t.id, t.username, p.nama, p.no_hp, t.jumlah, t.jatuh_tempo, t.status, t.tgl_bayar, "
            "t.metode_bayar, t.lunas_oleh, t.lunas_pada, t.catatan "
            "FROM tagihan t JOIN pelanggan p ON p.username = t.username "
            "WHERE t.periode = ? ORDER BY t.status ASC, p.nama COLLATE NOCASE",
            (periode,),
        ).fetchall()
        total = sum(r["jumlah"] for r in rows)
        lunas = sum(r["jumlah"] for r in rows if r["status"] == "lunas")
        # Pelanggan tanpa tagihan di periode ini (gratis, belum punya paket, atau
        # tagihannya belum dibuat) tetap dikirim supaya bisa diisolir manual.
        tanpa_tagihan = conn.execute(
            "SELECT p.username, p.nama, p.no_hp, p.gratis, p.paket_id, p.status FROM pelanggan p "
            "WHERE p.username NOT IN (SELECT username FROM tagihan WHERE periode = ?) "
            "AND (p.status = 'aktif' OR p.username IN (SELECT username FROM isolir)) "
            "ORDER BY p.gratis DESC, p.nama COLLATE NOCASE",
            (periode,),
        ).fetchall()
        insentif = _hitung_insentif(conn, periode)
        return {
            "periode": periode,
            "insentif_mitra": {k: insentif[k] for k in ("jumlah_lunas", "jumlah_gratis", "jumlah", "per_pelanggan", "total")} if conn.execute(
                "SELECT 1 FROM akun WHERE peran='mitra' LIMIT 1").fetchone() else None,
            "ringkasan": {"jumlah_tagihan": len(rows), "total": total,
                          "terbayar": lunas, "belum_bayar": total - lunas},
            "tagihan": [dict(r) for r in rows],
            "tanpa_tagihan": [dict(r) for r in tanpa_tagihan],
        }
    finally:
        conn.close()


def _kelompok_umur(hari_telat: int) -> str:
    if hari_telat <= 0:
        return "belum_jatuh_tempo"
    return "1_30" if hari_telat <= 30 else ("31_60" if hari_telat <= 60 else "60_lebih")


@app.get("/api/tagihan/tunggakan")
def list_tunggakan():
    """Semua tagihan belum lunas dari semua bulan, dikelompokkan per pelanggan, dengan
    umur piutang dihitung dari jatuh tempo TERTUA (0 / 1–30 / 31–60 / >60 hari)."""
    _siapkan_tabel_isolir()
    hari_ini = datetime.now().date()
    conn = _billing_conn()
    try:
        rows = conn.execute(
            "SELECT t.id, t.username, t.periode, t.jumlah, t.jatuh_tempo, p.nama, p.no_hp, p.status AS status_pelanggan, "
            "k.nama AS paket, (i.username IS NOT NULL) AS isolir "
            "FROM tagihan t JOIN pelanggan p ON p.username = t.username "
            "LEFT JOIN paket k ON k.id = p.paket_id LEFT JOIN isolir i ON i.username = t.username "
            "WHERE t.status != 'lunas' ORDER BY t.username, t.periode"
        ).fetchall()
        diingatkan = dict(conn.execute("SELECT username, MAX(waktu) FROM pengingat GROUP BY username").fetchall())
    finally:
        conn.close()
    per = {}
    for r in rows:
        telat = (hari_ini - datetime.strptime(r["jatuh_tempo"], "%Y-%m-%d").date()).days
        g = per.setdefault(r["username"], {
            "username": r["username"], "nama": r["nama"], "no_hp": r["no_hp"], "paket": r["paket"],
            "status_pelanggan": r["status_pelanggan"], "isolir": bool(r["isolir"]), "tagihan": [],
            "total": 0, "hari_telat": telat, "diingatkan": diingatkan.get(r["username"]),
        })
        g["tagihan"].append({"id": r["id"], "periode": r["periode"], "jumlah": r["jumlah"],
                             "jatuh_tempo": r["jatuh_tempo"], "hari_telat": telat})
        g["total"] += r["jumlah"]
        g["hari_telat"] = max(g["hari_telat"], telat)
    daftar = sorted(per.values(), key=lambda g: (-g["hari_telat"], -g["total"], (g["nama"] or "").lower()))
    ringkasan = {k: {"pelanggan": 0, "total": 0} for k in ("belum_jatuh_tempo", "1_30", "31_60", "60_lebih")}
    for g in daftar:
        g["bulan"] = len(g["tagihan"])
        g["bulan_telat"] = sum(1 for t in g["tagihan"] if t["hari_telat"] > 0)
        g["kelompok"] = _kelompok_umur(g["hari_telat"])
        ringkasan[g["kelompok"]]["pelanggan"] += 1
        ringkasan[g["kelompok"]]["total"] += g["total"]
    return {
        "per": hari_ini.isoformat(),
        "jeda_pengingat_hari": CONFIG["PENGINGAT_JEDA_HARI"],
        "jumlah_pelanggan": len(daftar),
        "total": sum(g["total"] for g in daftar),
        "total_telat": sum(t["jumlah"] for g in daftar for t in g["tagihan"] if t["hari_telat"] > 0),
        "kelompok": ringkasan,
        "pelanggan": daftar,
    }


@app.get("/api/pesan-wa")
def pengaturan_pesan_wa():
    """Teks yang dipakai browser untuk menyusun pesan WA ke pelanggan."""
    return {"usaha": CONFIG["NAMA_USAHA"], "cara_bayar": CONFIG["INFO_BAYAR"], "template": CONFIG["TEMPLATE_PENGINGAT"]}


class PengingatIn(BaseModel):
    username: str
    total: int = 0
    bulan: int = 0


@app.post("/api/tagihan/pengingat")
def catat_pengingat(d: PengingatIn, request: Request):
    """Catat bahwa pelanggan sudah diingatkan lewat WA (pesan dikirim dari HP admin), supaya
    pengingat massal tidak mengirim ulang ke orang yang sama dalam beberapa hari."""
    conn = _billing_conn()
    try:
        if not conn.execute("SELECT 1 FROM pelanggan WHERE username = ?", (d.username,)).fetchone():
            raise HTTPException(status_code=404, detail="Pelanggan tidak ditemukan.")
        conn.execute("INSERT INTO pengingat (username, waktu, oleh, total, bulan) VALUES (?,?,?,?,?)",
                     (d.username, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), _oleh(request),
                      max(0, d.total), max(0, d.bulan)))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def _data_kwitansi(tagihan_id: int) -> dict:
    conn = _billing_conn()
    try:
        r = conn.execute(
            "SELECT t.id, t.username, t.periode, t.jumlah, t.status, t.tgl_bayar, t.metode_bayar, t.lunas_oleh, "
            "t.lunas_pada, p.nama, p.no_hp, p.alamat, k.nama AS paket "
            "FROM tagihan t JOIN pelanggan p ON p.username = t.username LEFT JOIN paket k ON k.id = p.paket_id "
            "WHERE t.id = ?", (tagihan_id,)
        ).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Tagihan tidak ditemukan.")
        if r["status"] != "lunas":
            raise HTTPException(status_code=400, detail="Kwitansi hanya untuk tagihan yang sudah lunas.")
        sisa = conn.execute("SELECT COUNT(*), COALESCE(SUM(jumlah), 0) FROM tagihan WHERE username = ? AND status != 'lunas'",
                            (r["username"],)).fetchone()
    finally:
        conn.close()
    d = dict(r)
    d["nomor"] = f"KW/{r['periode'].replace('-', '')}/{r['id']:05d}"
    d["nama_periode"] = _nama_periode(r["periode"])
    d["sisa_bulan"], d["sisa_total"] = sisa[0], sisa[1]
    return d


@app.get("/api/tagihan/{tagihan_id}/kwitansi")
def kwitansi_tagihan(tagihan_id: int):
    """Data kwitansi untuk dikirim ke pelanggan lewat WA (teks disusun di browser)."""
    return _data_kwitansi(tagihan_id)


_METODE_LABEL = {"tunai": "Tunai", "transfer": "Transfer bank", "qris": "QRIS / e-wallet", "lainnya": "Lainnya"}


def _tgl_indo(tgl: Optional[str]) -> str:
    if not tgl:
        return "-"
    y, m, d = tgl[:10].split("-")
    return f"{int(d)} {_BLN_PANJANG[int(m) - 1]} {y}"


@app.get("/kwitansi/{tagihan_id}")
def kwitansi_cetak(tagihan_id: int):
    """Halaman kwitansi siap cetak / simpan PDF (ukuran setengah A4 / layar HP)."""
    d = _data_kwitansi(tagihan_id)
    e = _esc
    sisa = (f'<p class="sisa">Masih ada tagihan belum lunas: {d["sisa_bulan"]} bulan, {e(_rp(d["sisa_total"]))}.</p>'
            if d["sisa_bulan"] else "")
    html = f"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Kwitansi {e(d["nomor"])}</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#eef3ef;margin:0;padding:16px;color:#16241c}}
.kw{{max-width:560px;margin:0 auto;background:#fff;border:1px solid #d6e3da;border-radius:12px;padding:22px 24px}}
.kop{{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:2px solid #0b7a3b;padding-bottom:10px;margin-bottom:14px}}
.kop h1{{font-size:20px;margin:0;color:#0b7a3b}} .kop small{{color:#5f7669}} .no{{text-align:right;font-size:13px}}
table{{width:100%;border-collapse:collapse;font-size:14px}} td{{padding:6px 0;vertical-align:top}} td:first-child{{color:#5f7669;width:40%}}
.jml{{font-size:24px;font-weight:800;margin:14px 0 4px}} .cap{{display:inline-block;border:2px solid #0b7a3b;color:#0b7a3b;
border-radius:8px;padding:4px 12px;font-weight:800;letter-spacing:1px;transform:rotate(-4deg);margin-top:8px}}
.sisa{{color:#b45309;font-size:13px}} .kaki{{margin-top:18px;font-size:12px;color:#5f7669}}
.aksi{{max-width:560px;margin:12px auto 0;display:flex;gap:8px}} .aksi button{{flex:1;padding:10px;border-radius:9px;border:1px solid #0b7a3b;
background:#0b7a3b;color:#fff;font-weight:700;font-size:14px}} .aksi button.l{{background:#fff;color:#0b7a3b}}
@media print{{body{{background:#fff;padding:0}} .kw{{border:none}} .aksi{{display:none}}}}
</style></head><body>
<div class="kw">
 <div class="kop"><div><h1>KWITANSI</h1><small>{e(CONFIG.get("NAMA_USAHA") or "Internet Desa")}</small></div>
 <div class="no">No. <b>{e(d["nomor"])}</b><br>{e(_tgl_indo(d["tgl_bayar"]))}</div></div>
 <table>
  <tr><td>Telah diterima dari</td><td><b>{e(d["nama"])}</b><br><small>ID pelanggan: {e(d["username"])}</small></td></tr>
  <tr><td>Untuk pembayaran</td><td>Langganan internet {e(d["nama_periode"])}{(" · paket " + e(d["paket"])) if d["paket"] else ""}</td></tr>
  <tr><td>Tanggal bayar</td><td>{e(_tgl_indo(d["tgl_bayar"]))}</td></tr>
  <tr><td>Metode</td><td>{e(_METODE_LABEL.get(d["metode_bayar"] or "", d["metode_bayar"] or "-"))}</td></tr>
  <tr><td>Diterima oleh</td><td>{e(d["lunas_oleh"] or "-")}</td></tr>
 </table>
 <div class="jml">{e(_rp(d["jumlah"]))}</div>
 <div class="cap">LUNAS</div>
 {sisa}
 <div class="kaki">Kwitansi ini dibuat oleh sistem dan sah tanpa tanda tangan. Simpan sebagai bukti pembayaran.</div>
</div>
<div class="aksi"><button onclick="window.print()">Cetak / Simpan PDF</button><button class="l" onclick="window.close()">Tutup</button></div>
</body></html>"""
    return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


def _info_tagihan(conn, tagihan_id: int):
    r = conn.execute(
        "SELECT t.username, t.periode, t.jumlah, t.status FROM tagihan t WHERE t.id=?", (tagihan_id,)
    ).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Tagihan tidak ditemukan.")
    return r


class BatalIn(BaseModel):
    alasan: str


class LunasIn(BaseModel):
    tanggal: Optional[str] = None       # tanggal uang diterima; kosong = hari ini
    metode: str = "tunai"


@app.post("/api/tagihan/{tagihan_id}/lunas")
def tandai_lunas(tagihan_id: int, request: Request, d: Optional[LunasIn] = None):
    d = d or LunasIn()
    tanggal = d.tanggal or datetime.now().strftime("%Y-%m-%d")
    _cek_tanggal_lalu(tanggal, "Tanggal bayar")
    if tanggal > datetime.now().strftime("%Y-%m-%d"):
        raise HTTPException(status_code=400, detail="Tanggal bayar tidak boleh di masa depan.")
    if d.metode not in _METODE:
        raise HTTPException(status_code=400, detail="Metode harus tunai, transfer, qris, atau lainnya.")
    conn = _billing_conn()
    try:
        t = _info_tagihan(conn, tagihan_id)
        if t["status"] == "lunas":
            # Jangan menimpa tanggal bayar lama: pendapatan bulan itu bisa pindah ke hari ini.
            return {"ok": True, "sudah_lunas": True}
        _wajib_buku_terbuka(conn, tanggal)
        # Bersyarat status: dua klik/dua admin bersamaan hanya satu yang tercatat.
        cur = conn.execute(
            "UPDATE tagihan SET status='lunas', tgl_bayar=?, metode_bayar=?, lunas_oleh=?, lunas_pada=? "
            "WHERE id=? AND status != 'lunas'",
            (tanggal, d.metode, _oleh(request), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tagihan_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": True, "sudah_lunas": True}
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "tagihan_lunas",
                    f"{t['username']} {t['periode']} {_rp(t['jumlah'])} · dibayar {tanggal} ({d.metode})")
    return {"ok": True}


@app.post("/api/tagihan/{tagihan_id}/batal")
def batalkan_lunas(tagihan_id: int, d: BatalIn, request: Request):
    alasan = d.alasan.strip()
    if len(alasan) < 3:
        raise HTTPException(status_code=400, detail="Tuliskan alasan pembatalan pelunasan.")
    conn = _billing_conn()
    try:
        t = conn.execute("SELECT * FROM tagihan WHERE id=?", (tagihan_id,)).fetchone()
        if not t:
            raise HTTPException(status_code=404, detail="Tagihan tidak ditemukan.")
        if t["status"] != "lunas":
            return {"ok": True}
        _wajib_buku_terbuka(conn, t["tgl_bayar"])
        cur = conn.execute("UPDATE tagihan SET status='belum', tgl_bayar=NULL, metode_bayar=NULL, lunas_oleh=NULL, "
                           "lunas_pada=NULL WHERE id=? AND status='lunas'", (tagihan_id,))
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": True}
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "tagihan_batal_lunas",
                    f"{t['username']} {t['periode']} {_rp(t['jumlah'])} · pembayaran {t['tgl_bayar']} "
                    f"({t['metode_bayar'] or '-'}, dicatat {t['lunas_oleh'] or '-'}) dibatalkan: {alasan[:150]}")
    return {"ok": True}


# ============================================================
# 6f. KEUANGAN — kas masuk/keluar + pendapatan langganan dari billing
# ============================================================
# Dasar kas (cash basis): pendapatan langganan = tagihan LUNAS menurut tanggal
# bayar, jadi tidak perlu dicatat ulang. Transaksi manual tidak pernah dihapus;
# yang salah "dibatalkan" dengan alasan sehingga jejaknya tetap ada.
# Kategori dengan pengaruh_laba = 0 (setoran modal, ambil pribadi pemilik)
# memengaruhi saldo kas tetapi tidak dihitung sebagai pendapatan/beban.

_KATEGORI_AWAL = [
    # (nama, jenis, pengaruh_laba)
    ("Beban bahan habis pakai (konektor, pigtail, isolasi)", "keluar", 1),
    ("Beban bandwidth (upstream)", "keluar", 1),
    ("Beban gaji & upah", "keluar", 1),
    ("Beban jasa & pemeliharaan", "keluar", 1),
    ("Beban listrik", "keluar", 1),
    ("Beban sewa (tiang, lahan, tempat)", "keluar", 1),
    ("Beban transportasi & BBM", "keluar", 1),
    ("Beban komisi mitra", "keluar", 1),
    ("Beban pajak & retribusi", "keluar", 1),
    ("Beban operasional lainnya", "keluar", 1),
    ("Prive (penarikan pemilik)", "keluar", 0),
    ("Pendapatan jasa pemasangan", "masuk", 1),
    ("Pendapatan penjualan perangkat", "masuk", 1),
    ("Pendapatan lain-lain", "masuk", 1),
    ("Setoran modal", "masuk", 0),
]
_METODE = ("tunai", "transfer", "qris", "lainnya")
# Kategori khusus untuk pembayaran/cicilan utang: mengurangi kas tetapi bukan
# beban (bebannya sudah dihitung saat pembelian kredit dicatat).
_KAT_BAYAR_UTANG = "Pelunasan liabilitas"
_KAT_INSENTIF = "Beban komisi mitra"  # dibayar lewat tab Komisi Mitra, terikat ke periode
_KAT_BUNGA = "Beban bunga & biaya angsuran"  # selisih total cicilan dengan harga barang
_KAT_PENYUSUTAN = "Beban penyusutan aset tetap"
# Dana talangan pemilik: uang pribadi pemilik yang dipinjamkan ke usaha dan nanti dikembalikan.
# Bukan pendapatan dan bukan modal — dicatat sebagai utang usaha kepada pemilik.
_KAT_TALANGAN_MASUK = "Dana talangan pemilik (pinjaman)"
_KAT_TALANGAN_KEMBALI = "Pengembalian dana talangan pemilik"
# Aset tetap (SAK EMKM): dicatat sebagai aset, lalu dibebankan bertahap lewat penyusutan
# garis lurus. Umur manfaat mengikuti kelompok harta pajak (PMK 72/2023): kelompok 1 = 4 tahun,
# kelompok 2 = 8 tahun.
_KAT_ASET_AWAL = [
    ("Aset: perangkat jaringan (OLT, router, switch, ONU, AP)", 48),
    ("Aset: kabel fiber & instalasi (tiang, ODP, ODC, splitter)", 96),
    ("Aset: peralatan kerja & kantor (splicer, OPM, laptop)", 48),
]
_KAT_PERANGKAT_LAMA = "Pembelian perangkat (ONT, router, kabel, dll.)"
# Nama kategori lama -> nama baku akuntansi (data lama ikut berganti nama, id tetap).
_NAMA_KATEGORI_BAKU = [
    ("Bahan habis pakai (konektor, pigtail, isolasi, dll.)", "Beban bahan habis pakai (konektor, pigtail, isolasi)", "keluar"),
    ("Bandwidth / upstream", "Beban bandwidth (upstream)", "keluar"),
    ("Gaji & upah teknisi", "Beban gaji & upah", "keluar"),
    ("Jasa & perbaikan", "Beban jasa & pemeliharaan", "keluar"),
    ("Listrik", "Beban listrik", "keluar"),
    ("Sewa tiang / lahan / tempat", "Beban sewa (tiang, lahan, tempat)", "keluar"),
    ("Transportasi & BBM", "Beban transportasi & BBM", "keluar"),
    ("Insentif mitra", "Beban komisi mitra", "keluar"),
    ("Pajak & retribusi", "Beban pajak & retribusi", "keluar"),
    ("Operasional lain", "Beban operasional lainnya", "keluar"),
    ("Ambil pribadi pemilik (prive)", "Prive (penarikan pemilik)", "keluar"),
    ("Biaya pemasangan baru", "Pendapatan jasa pemasangan", "masuk"),
    ("Penjualan perangkat", "Pendapatan penjualan perangkat", "masuk"),
    ("Pemasukan lain", "Pendapatan lain-lain", "masuk"),
    ("Setoran modal pemilik", "Setoran modal", "masuk"),
    ("Pembayaran utang", "Pelunasan liabilitas", "keluar"),
    ("Bunga & biaya cicilan", "Beban bunga & biaya angsuran", "keluar"),
]
_KAT_PERANGKAT_BARU = "Beban bahan habis pakai (konektor, pigtail, isolasi)"


def _beri_nomor(conn, tid: int) -> str:
    """Nomor bukti kas: BKM-YYMM-0001 (masuk) / BKK-YYMM-0001 (keluar), berurutan per bulan tanggal transaksi."""
    jenis, tanggal = conn.execute("SELECT jenis, tanggal FROM transaksi WHERE id=?", (tid,)).fetchone()
    awalan = f"{'BKM' if jenis == 'masuk' else 'BKK'}-{tanggal[2:4]}{tanggal[5:7]}-"
    terakhir = conn.execute("SELECT MAX(CAST(substr(nomor, 10) AS INTEGER)) FROM transaksi WHERE nomor LIKE ?",
                            (awalan + "%",)).fetchone()[0] or 0
    nomor = f"{awalan}{terakhir + 1:04d}"
    conn.execute("UPDATE transaksi SET nomor=? WHERE id=?", (nomor, tid))
    return nomor


def init_keuangan_db() -> None:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kategori_kas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nama TEXT NOT NULL,
                jenis TEXT NOT NULL CHECK (jenis IN ('masuk','keluar')),
                pengaruh_laba INTEGER NOT NULL DEFAULT 1,
                aktif INTEGER NOT NULL DEFAULT 1,
                UNIQUE (nama, jenis)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transaksi (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tanggal TEXT NOT NULL,
                jenis TEXT NOT NULL CHECK (jenis IN ('masuk','keluar')),
                kategori_id INTEGER NOT NULL REFERENCES kategori_kas(id),
                jumlah INTEGER NOT NULL CHECK (jumlah > 0),
                metode TEXT NOT NULL DEFAULT 'tunai',
                keterangan TEXT,
                dicatat_oleh TEXT NOT NULL,
                dicatat_pada TEXT NOT NULL,
                diubah_oleh TEXT,
                diubah_pada TEXT,
                status TEXT NOT NULL DEFAULT 'aktif',
                batal_oleh TEXT,
                batal_pada TEXT,
                batal_alasan TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_transaksi_tanggal ON transaksi(tanggal)")
        for lama, baru, jenis in _NAMA_KATEGORI_BAKU:
            conn.execute("UPDATE OR IGNORE kategori_kas SET nama=? WHERE nama=? AND jenis=?", (baru, lama, jenis))
        if conn.execute("SELECT COUNT(*) FROM kategori_kas").fetchone()[0] == 0:
            conn.executemany("INSERT INTO kategori_kas (nama, jenis, pengaruh_laba) VALUES (?,?,?)", _KATEGORI_AWAL)
        # --- Utang (pembelian kredit) ---
        if "khusus" not in [r[1] for r in conn.execute("PRAGMA table_info(kategori_kas)")]:
            conn.execute("ALTER TABLE kategori_kas ADD COLUMN khusus INTEGER NOT NULL DEFAULT 0")
        conn.execute("INSERT OR IGNORE INTO kategori_kas (nama, jenis, pengaruh_laba, khusus) VALUES (?, 'keluar', 0, 1)",
                     (_KAT_BAYAR_UTANG,))
        conn.execute("UPDATE kategori_kas SET khusus = 1, pengaruh_laba = 0 WHERE nama = ? AND jenis = 'keluar'",
                     (_KAT_BAYAR_UTANG,))
        if "utang_id" not in [r[1] for r in conn.execute("PRAGMA table_info(transaksi)")]:
            conn.execute("ALTER TABLE transaksi ADD COLUMN utang_id INTEGER REFERENCES utang(id)")
        # Pembayaran insentif mitra terikat ke periode insentifnya (insentif Oktober
        # biasanya dibayar November). Data lama tanpa periode dianggap periode bulan tanggalnya.
        if "insentif_periode" not in [r[1] for r in conn.execute("PRAGMA table_info(transaksi)")]:
            conn.execute("ALTER TABLE transaksi ADD COLUMN insentif_periode TEXT")
        # Banyaknya barang (mis. 10 pcs, 150 meter) — opsional, untuk keterangan & harga satuan.
        for kol, tipe in (("qty", "REAL"), ("satuan", "TEXT"), ("nomor", "TEXT")):
            if kol not in [r[1] for r in conn.execute("PRAGMA table_info(transaksi)")]:
                conn.execute(f"ALTER TABLE transaksi ADD COLUMN {kol} {tipe}")
        # Nomor bukti kas berurutan per bulan: BKM (bukti kas masuk) / BKK (bukti kas keluar).
        for (tid,) in conn.execute("SELECT id FROM transaksi WHERE nomor IS NULL ORDER BY tanggal, id").fetchall():
            _beri_nomor(conn, tid)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_transaksi_nomor ON transaksi(nomor)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS utang (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tanggal TEXT NOT NULL,
                pemasok TEXT NOT NULL,
                kategori_id INTEGER NOT NULL REFERENCES kategori_kas(id),
                jumlah INTEGER NOT NULL CHECK (jumlah > 0),
                jatuh_tempo TEXT,
                keterangan TEXT,
                dicatat_oleh TEXT NOT NULL,
                dicatat_pada TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'aktif',
                batal_oleh TEXT,
                batal_pada TEXT,
                batal_alasan TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_utang_tanggal ON utang(tanggal)")
        # Tutup buku: bulan yang sudah diperiksa owner dikunci, tidak bisa ditambah/diubah/dihapus.
        conn.execute("CREATE TABLE IF NOT EXISTS tutup_buku (periode TEXT PRIMARY KEY, oleh TEXT NOT NULL, "
                     "pada TEXT NOT NULL, catatan TEXT)")
        for kol, tipe in (("qty", "REAL"), ("satuan", "TEXT")):
            if kol not in [r[1] for r in conn.execute("PRAGMA table_info(utang)")]:
                conn.execute(f"ALTER TABLE utang ADD COLUMN {kol} {tipe}")
        # Cicilan bulanan (mis. PayLater): jumlah = total kewajiban (uang muka + semua cicilan),
        # pokok = harga barang; selisihnya bunga/biaya layanan (dibebankan terpisah).
        kol_u = [r[1] for r in conn.execute("PRAGMA table_info(utang)")]
        for kol, tipe in (("pokok", "INTEGER"), ("uang_muka", "INTEGER NOT NULL DEFAULT 0"),
                          ("tenor", "INTEGER"), ("cicilan", "INTEGER")):
            if kol not in kol_u:
                conn.execute(f"ALTER TABLE utang ADD COLUMN {kol} {tipe}")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS utang_cicilan (
                utang_id INTEGER NOT NULL REFERENCES utang(id),
                ke INTEGER NOT NULL,
                jatuh_tempo TEXT NOT NULL,
                jumlah INTEGER NOT NULL,
                PRIMARY KEY (utang_id, ke)
            )
        """)
        conn.execute("INSERT OR IGNORE INTO kategori_kas (nama, jenis, pengaruh_laba) VALUES (?, 'keluar', 1)",
                     (_KAT_BUNGA,))
        conn.execute("INSERT OR IGNORE INTO kategori_kas (nama, jenis, pengaruh_laba) VALUES (?, 'masuk', 0)", (_KAT_TALANGAN_MASUK,))
        conn.execute("INSERT OR IGNORE INTO kategori_kas (nama, jenis, pengaruh_laba) VALUES (?, 'keluar', 0)", (_KAT_TALANGAN_KEMBALI,))
        # Aset tetap: umur_bulan terisi = pembelian dicatat sebagai aset, disusutkan per bulan.
        if "umur_bulan" not in [r[1] for r in conn.execute("PRAGMA table_info(kategori_kas)")]:
            conn.execute("ALTER TABLE kategori_kas ADD COLUMN umur_bulan INTEGER")
        for nama, umur in _KAT_ASET_AWAL:
            conn.execute("INSERT OR IGNORE INTO kategori_kas (nama, jenis, pengaruh_laba, umur_bulan) VALUES (?, 'keluar', 1, ?)",
                         (nama, umur))
        # Kategori lama "Pembelian perangkat" mencampur aset dan bahan habis pakai. Bila belum
        # dipakai, diganti nama menjadi bahan habis pakai (perangkat besar masuk kategori aset).
        lama = conn.execute("SELECT id FROM kategori_kas WHERE nama=? AND jenis='keluar'", (_KAT_PERANGKAT_LAMA,)).fetchone()
        if lama and not conn.execute("SELECT 1 FROM transaksi WHERE kategori_id=? UNION ALL SELECT 1 FROM utang WHERE kategori_id=? LIMIT 1",
                                     (lama[0], lama[0])).fetchone():
            conn.execute("UPDATE OR IGNORE kategori_kas SET nama=? WHERE id=?", (_KAT_PERANGKAT_BARU, lama[0]))
        conn.commit()
    finally:
        conn.close()


def _tanggal_valid(t: str) -> bool:
    try:
        datetime.strptime(t, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def _batas_periode(periode: str) -> tuple[str, str]:
    """('2026-10-01', '2026-11-01') — awal inklusif, akhir eksklusif. 'semua' = seluruh riwayat."""
    if periode == "semua":
        return "0000-01-01", "9999-12-31"
    awal = datetime.strptime(periode + "-01", "%Y-%m-%d")
    akhir = (awal + timedelta(days=32)).replace(day=1)
    return awal.strftime("%Y-%m-%d"), akhir.strftime("%Y-%m-%d")


def _bunga_diakui(conn, awal: str, akhir: str) -> tuple[int, int]:
    """(total bunga, jumlah utang) yang diakui untuk cicilan jatuh tempo di [awal, akhir).
    Bunga dibagi rata per cicilan; sisa pembulatan masuk cicilan terakhir."""
    total, utang = 0, set()
    for r in conn.execute(
            "SELECT u.id, u.jumlah - u.pokok AS bunga, u.tenor, c.ke FROM utang u JOIN utang_cicilan c ON c.utang_id = u.id "
            "WHERE u.status='aktif' AND u.pokok IS NOT NULL AND u.jumlah > u.pokok AND u.tenor > 0 "
            "AND c.jatuh_tempo >= ? AND c.jatuh_tempo < ?", (awal, akhir)):
        bagian = r["bunga"] // r["tenor"] + (r["bunga"] % r["tenor"] if r["ke"] == r["tenor"] else 0)
        total += bagian
        utang.add(r["id"])
    return total, len(utang)


def _perolehan_aset(conn, sebelum: str = "9999-12-31") -> list[dict]:
    """Semua pembelian aset tetap (tunai & kredit) bertanggal < sebelum."""
    rows = conn.execute(
        "SELECT 'transaksi' AS sumber, t.id, t.tanggal, t.jumlah AS harga, t.kategori_id, k.nama AS kategori, k.umur_bulan, "
        "t.keterangan FROM transaksi t JOIN kategori_kas k ON k.id = t.kategori_id "
        "WHERE t.status='aktif' AND t.jenis='keluar' AND k.umur_bulan > 0 AND t.tanggal < ? "
        "UNION ALL "
        "SELECT 'utang', u.id, u.tanggal, COALESCE(u.pokok, u.jumlah), u.kategori_id, k.nama, k.umur_bulan, "
        "u.pemasok || COALESCE(' — ' || u.keterangan, '') FROM utang u JOIN kategori_kas k ON k.id = u.kategori_id "
        "WHERE u.status='aktif' AND k.umur_bulan > 0 AND u.tanggal < ? ORDER BY 3", (sebelum, sebelum)).fetchall()
    return [dict(r) for r in rows]


def _bulan_ke(tanggal: str) -> int:
    return int(tanggal[:4]) * 12 + int(tanggal[5:7]) - 1


def _susut_aset(a: dict, bulan_awal: int, bulan_akhir: int) -> int:
    """Penyusutan garis lurus satu aset untuk bulan [bulan_awal, bulan_akhir) (indeks bulan).
    Disusutkan mulai bulan perolehan; sisa pembulatan masuk bulan terakhir umur manfaat."""
    mulai, umur = _bulan_ke(a["tanggal"]), a["umur_bulan"]
    dari, sampai = max(bulan_awal, mulai), min(bulan_akhir, mulai + umur)
    if sampai <= dari:
        return 0
    per_bulan = a["harga"] // umur
    total = per_bulan * (sampai - dari)
    if dari <= mulai + umur - 1 < sampai:
        total += a["harga"] - per_bulan * umur
    return total


def _penyusutan(conn, awal: str, akhir: str) -> tuple[int, int]:
    """(beban penyusutan, jumlah aset) untuk periode [awal, akhir)."""
    if awal <= "0001":
        b_awal = 0
    else:
        b_awal = _bulan_ke(awal)
    b_akhir = _bulan_ke(akhir) if akhir < "9999" else 10**9
    total, n = 0, 0
    for a in _perolehan_aset(conn, akhir):
        x = _susut_aset(a, b_awal, b_akhir)
        if x:
            total, n = total + x, n + 1
    return total, n


def _ringkasan_aset(conn, akhir: str) -> dict:
    """Posisi aset tetap per tanggal akhir (eksklusif): harga perolehan, akumulasi penyusutan, nilai buku."""
    daftar = []
    for a in _perolehan_aset(conn, akhir):
        akum = _susut_aset(a, 0, _bulan_ke(akhir) if akhir < "9999" else 10**9)
        daftar.append({**a, "akumulasi": akum, "nilai_buku": a["harga"] - akum,
                       "susut_per_bulan": a["harga"] // a["umur_bulan"],
                       "habis": _geser_periode(a["tanggal"][:7], a["umur_bulan"] - 1)})
    harga = sum(a["harga"] for a in daftar)
    akum = sum(a["akumulasi"] for a in daftar)
    return {"harga_perolehan": harga, "akumulasi": akum, "nilai_buku": harga - akum, "daftar": daftar}


def _ringkas_periode(conn, periode: str) -> dict:
    awal, akhir = _batas_periode(periode)
    return _ringkas_antara(conn, awal, akhir, periode)


def _ringkas_antara(conn, awal: str, akhir: str, periode: Optional[str] = None) -> dict:
    """Laba: pendapatan (langganan lunas + pemasukan) - beban (pengeluaran tunai +
    pembelian kredit, dihitung pada tanggal pembelian). Kas: hanya uang yang
    benar-benar keluar/masuk (cicilan utang mengurangi kas, bukan beban lagi)."""
    langganan = conn.execute(
        "SELECT COALESCE(SUM(jumlah),0), COUNT(*) FROM tagihan WHERE status='lunas' AND tgl_bayar >= ? AND tgl_bayar < ?",
        (awal, akhir),
    ).fetchone()
    per_kat = conn.execute(
        "SELECT k.id, k.nama, t.jenis, k.pengaruh_laba, k.khusus, k.umur_bulan, SUM(t.jumlah) AS total, COUNT(*) AS n "
        "FROM transaksi t JOIN kategori_kas k ON k.id = t.kategori_id "
        "WHERE t.status='aktif' AND t.tanggal >= ? AND t.tanggal < ? GROUP BY k.id",
        (awal, akhir),
    ).fetchall()
    kredit = {r["kategori_id"]: dict(r) for r in conn.execute(
        "SELECT u.kategori_id, k.nama, k.pengaruh_laba, k.umur_bulan, SUM(COALESCE(u.pokok, u.jumlah)) AS total, COUNT(*) AS n "
        "FROM utang u JOIN kategori_kas k ON k.id = u.kategori_id "
        "WHERE u.status='aktif' AND u.tanggal >= ? AND u.tanggal < ? GROUP BY u.kategori_id",
        (awal, akhir),
    ).fetchall()}
    # Bunga/biaya cicilan (total cicilan - harga barang) adalah biaya pendanaan: diakui per
    # bulan sesuai jadwal cicilan yang jatuh tempo di periode ini (basis akrual), bukan
    # sekaligus di bulan pembelian.
    bunga = _bunga_diakui(conn, awal, akhir)
    if bunga[0]:
        kb = conn.execute("SELECT id FROM kategori_kas WHERE nama = ? AND jenis='keluar'", (_KAT_BUNGA,)).fetchone()
        if kb:
            g = kredit.setdefault(kb[0], {"kategori_id": kb[0], "nama": _KAT_BUNGA, "pengaruh_laba": 1, "umur_bulan": None,
                                          "total": 0, "n": 0})
            g["total"] += bunga[0]
            g["n"] += bunga[1]
    aset = lambda r: bool(r["umur_bulan"])
    masuk_ops = sum(r["total"] for r in per_kat if r["jenis"] == "masuk" and r["pengaruh_laba"])
    keluar_tunai = sum(r["total"] for r in per_kat if r["jenis"] == "keluar" and r["pengaruh_laba"] and not aset(r))
    beli_aset_tunai = sum(r["total"] for r in per_kat if r["jenis"] == "keluar" and aset(r))
    talangan_masuk = sum(r["total"] for r in per_kat if r["jenis"] == "masuk" and r["nama"] == _KAT_TALANGAN_MASUK)
    talangan_kembali = sum(r["total"] for r in per_kat if r["jenis"] == "keluar" and r["nama"] == _KAT_TALANGAN_KEMBALI)
    masuk_lain = sum(r["total"] for r in per_kat if r["jenis"] == "masuk" and not r["pengaruh_laba"]) - talangan_masuk
    prive = (sum(r["total"] for r in per_kat if r["jenis"] == "keluar" and not r["pengaruh_laba"] and not r["khusus"] and not aset(r))
             - talangan_kembali)
    bayar_utang = sum(r["total"] for r in per_kat if r["khusus"])
    beban_kredit = sum(r["total"] for r in kredit.values() if not aset(r))
    beli_aset_kredit = sum(r["total"] for r in kredit.values() if aset(r))
    susut = _penyusutan(conn, awal, akhir)
    # Rincian per kategori: gabungan tunai + kredit (tanpa kategori khusus pembayaran utang)
    gabung: dict[int, dict] = {}
    for r in per_kat:
        if r["khusus"]:
            continue
        gabung[r["id"]] = {"id": r["id"], "nama": r["nama"], "jenis": r["jenis"], "pengaruh_laba": r["pengaruh_laba"],
                           "aset": aset(r), "total": r["total"], "n": r["n"], "kredit": 0}
    for kid, r in kredit.items():
        g = gabung.setdefault(kid, {"id": kid, "nama": r["nama"], "jenis": "keluar", "pengaruh_laba": r["pengaruh_laba"],
                                    "aset": aset(r), "total": 0, "n": 0, "kredit": 0})
        g["total"] += r["total"]
        g["n"] += r["n"]
        g["kredit"] += r["total"]
    if susut[0]:
        # Baris tiruan (id 0): bukan kategori yang bisa dipilih saat mencatat transaksi.
        gabung[0] = {"id": 0, "nama": _KAT_PENYUSUTAN, "jenis": "keluar", "pengaruh_laba": 1, "aset": False,
                     "penyusutan": True, "total": susut[0], "n": susut[1], "kredit": 0}
    pendapatan = langganan[0] + masuk_ops
    beban = keluar_tunai + beban_kredit + susut[0]
    return {
        "periode": periode,
        "pendapatan_langganan": langganan[0], "jumlah_pembayaran": langganan[1],
        "pendapatan_lain": masuk_ops, "pendapatan": pendapatan,
        "beban": beban, "beban_kredit": beban_kredit, "beban_tunai": keluar_tunai, "penyusutan": susut[0],
        "laba": pendapatan - beban,
        "beli_aset": beli_aset_tunai + beli_aset_kredit, "beli_aset_tunai": beli_aset_tunai,
        "beli_aset_kredit": beli_aset_kredit,
        "modal_masuk": masuk_lain, "prive": prive, "bayar_utang": bayar_utang,
        "talangan_masuk": talangan_masuk, "talangan_kembali": talangan_kembali,
        "arus_kas": (pendapatan + masuk_lain + talangan_masuk - keluar_tunai - beli_aset_tunai - prive - bayar_utang
                     - talangan_kembali),
        "per_kategori": sorted(gabung.values(), key=lambda g: -g["total"]),
    }


def _saldo_kas_sampai(conn, tanggal_eksklusif: str) -> int:
    lunas = conn.execute("SELECT COALESCE(SUM(jumlah),0) FROM tagihan WHERE status='lunas' AND tgl_bayar < ?",
                         (tanggal_eksklusif,)).fetchone()[0]
    masuk = conn.execute("SELECT COALESCE(SUM(jumlah),0) FROM transaksi WHERE status='aktif' AND jenis='masuk' AND tanggal < ?",
                         (tanggal_eksklusif,)).fetchone()[0]
    keluar = conn.execute("SELECT COALESCE(SUM(jumlah),0) FROM transaksi WHERE status='aktif' AND jenis='keluar' AND tanggal < ?",
                          (tanggal_eksklusif,)).fetchone()[0]
    return lunas + masuk - keluar


def _kas_terendah(conn, awal: str, akhir: str, saldo_awal: int) -> dict:
    """Saldo kas terendah di akhir hari selama [awal, akhir): menunjukkan kapan kas sempat minus
    (biasanya karena ada uang pribadi pemilik yang terpakai tapi belum dicatat)."""
    harian: dict = {}
    for tgl, n in conn.execute("SELECT substr(tgl_bayar,1,10), SUM(jumlah) FROM tagihan WHERE status='lunas' "
                               "AND tgl_bayar >= ? AND tgl_bayar < ? GROUP BY 1", (awal, akhir)):
        harian[tgl] = harian.get(tgl, 0) + n
    for tgl, n in conn.execute("SELECT tanggal, SUM(CASE WHEN jenis='masuk' THEN jumlah ELSE -jumlah END) FROM transaksi "
                               "WHERE status='aktif' AND tanggal >= ? AND tanggal < ? GROUP BY 1", (awal, akhir)):
        harian[tgl] = harian.get(tgl, 0) + n
    saldo, terendah = saldo_awal, {"tanggal": None, "saldo": saldo_awal}
    for tgl in sorted(harian):
        saldo += harian[tgl]
        if saldo < terendah["saldo"]:
            terendah = {"tanggal": tgl, "saldo": saldo}
    return terendah


def _geser_periode(periode: str, n: int) -> str:
    th, bl = map(int, periode.split("-"))
    total = th * 12 + (bl - 1) + n
    return f"{total // 12}-{total % 12 + 1:02d}"


_KUNCI_ABAIKAN_KAS = "KAS_MINUS_DIABAIKAN_SAMPAI"


def _kas_minus_diabaikan(conn) -> str:
    """Periode terakhir (YYYY-MM) yang peringatan kas minusnya diabaikan owner, mis. bulan-bulan
    sebelum pencatatan rapi yang transaksinya sudah tidak bisa dilacak. '' = tidak ada."""
    conn.execute("CREATE TABLE IF NOT EXISTS pengaturan (kunci TEXT PRIMARY KEY, nilai TEXT NOT NULL)")
    r = conn.execute("SELECT nilai FROM pengaturan WHERE kunci = ?", (_KUNCI_ABAIKAN_KAS,)).fetchone()
    return r[0] if r else ""


class AbaikanKasIn(BaseModel):
    periode: str = ""      # kosong = tampilkan lagi semua peringatan


@app.post("/api/keuangan/abaikan-kas-minus")
def keuangan_abaikan_kas_minus(d: AbaikanKasIn, request: Request):
    _wajib_owner(request, "Hanya owner yang bisa mengabaikan peringatan kas minus.")
    if d.periode and (not _periode_valid(d.periode) or d.periode > datetime.now().strftime("%Y-%m")):
        raise HTTPException(status_code=400, detail="Periode tidak valid.")
    conn = _billing_conn()
    try:
        _kas_minus_diabaikan(conn)
        if d.periode:
            conn.execute("INSERT OR REPLACE INTO pengaturan (kunci, nilai) VALUES (?,?)", (_KUNCI_ABAIKAN_KAS, d.periode))
        else:
            conn.execute("DELETE FROM pengaturan WHERE kunci = ?", (_KUNCI_ABAIKAN_KAS,))
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "pengaturan_ubah",
                    f"Peringatan kas minus diabaikan s/d {_nama_periode(d.periode)}" if d.periode
                    else "Peringatan kas minus ditampilkan lagi")
    return {"ok": True, "sampai": d.periode}


@app.get("/api/keuangan/ringkasan")
def keuangan_ringkasan(periode: str, request: Request):
    _wajib_admin(request)
    if not _periode_valid(periode):
        raise HTTPException(status_code=400, detail="Format periode harus YYYY-MM.")
    awal, akhir = _batas_periode(periode)
    conn = _billing_conn()
    try:
        ini = _ringkas_periode(conn, periode)
        lalu = _ringkas_periode(conn, _geser_periode(periode, -1))
        tren = []
        for i in range(5, -1, -1):
            p = _geser_periode(periode, -i)
            r = ini if p == periode else _ringkas_periode(conn, p)
            tren.append({k: r[k] for k in ("periode", "pendapatan", "beban", "laba")})
        saldo_awal = _saldo_kas_sampai(conn, awal)
        saldo_akhir = _saldo_kas_sampai(conn, akhir)
        kas_terendah = _kas_terendah(conn, awal, akhir, saldo_awal)
        kum = _ringkas_antara(conn, "0000-01-01", akhir)
        talangan_sisa = kum["talangan_masuk"] - kum["talangan_kembali"]
        piutang_periode = conn.execute(
            "SELECT COALESCE(SUM(jumlah),0), COUNT(*) FROM tagihan WHERE periode = ? AND status='belum'", (periode,)
        ).fetchone()
        piutang_total = conn.execute(
            "SELECT COALESCE(SUM(t.jumlah),0), COUNT(*) FROM tagihan t JOIN pelanggan p ON p.username = t.username "
            "WHERE t.status='belum' AND t.periode <= ?", (periode,)
        ).fetchone()
        tagihan_periode = conn.execute(
            "SELECT COALESCE(SUM(jumlah),0), COUNT(*) FROM tagihan WHERE periode = ?", (periode,)
        ).fetchone()
        insentif = _hitung_insentif(conn, periode)
        insentif_dibayar = _insentif_dibayar(conn, periode)
        insentif_tunggak = _insentif_tertunggak(conn, periode)
        pelanggan_aktif = conn.execute(
            "SELECT COUNT(*) FROM pelanggan WHERE status='aktif' AND gratis=0").fetchone()[0]
        utang_info = _ringkas_utang(conn)
        tanpa_nota = _tanpa_nota(conn, awal, akhir)
        terbaru = conn.execute(
            "SELECT t.id, t.tanggal, t.jenis, k.nama AS kategori, t.jumlah, t.keterangan, t.dicatat_oleh, t.status "
            "FROM transaksi t JOIN kategori_kas k ON k.id = t.kategori_id ORDER BY t.id DESC LIMIT 8"
        ).fetchall()
    finally:
        conn.close()
    conn = _billing_conn()
    try:
        tb = _buku_tertutup(conn, periode)
        abaikan = _kas_minus_diabaikan(conn)
        conn.commit()
    finally:
        conn.close()
    minus = saldo_akhir < 0 or kas_terendah["saldo"] < 0
    return {
        **ini,
        "tutup_buku": dict(tb) if tb else None,
        "kas_negatif": minus and not (abaikan and periode <= abaikan),
        "kas_negatif_diabaikan": minus and bool(abaikan and periode <= abaikan),
        "kas_minus_diabaikan_sampai": abaikan,
        "kas_terendah": kas_terendah, "talangan_sisa": talangan_sisa,
        "bulan_lalu": {k: lalu[k] for k in ("pendapatan", "beban", "laba")},
        "tren": tren,
        "saldo_awal": saldo_awal, "saldo_akhir": saldo_akhir,
        "tagihan_periode": {"total": tagihan_periode[0], "jumlah": tagihan_periode[1]},
        "piutang_periode": {"total": piutang_periode[0], "jumlah": piutang_periode[1]},
        "piutang_total": {"total": piutang_total[0], "jumlah": piutang_total[1]},
        "insentif_mitra": {"terhitung": insentif["total"], "dibayar": insentif_dibayar,
                           "tertunggak_total": insentif_tunggak["total"], "tertunggak_bulan": insentif_tunggak["bulan"]},
        "utang": utang_info,
        "tanpa_nota": tanpa_nota,
        "pelanggan_berbayar": pelanggan_aktif,
        "transaksi_terbaru": [dict(r) for r in terbaru],
    }



def _sisa_utang_per(conn, akhir: str) -> int:
    """Kewajiban kepada pemasok per tanggal akhir (eksklusif): total kredit - pembayaran,
    dikurangi bunga cicilan yang baru jatuh tempo setelah tanggal itu (belum terjadi)."""
    kredit = conn.execute("SELECT COALESCE(SUM(jumlah),0) FROM utang WHERE status='aktif' AND tanggal < ?", (akhir,)).fetchone()[0]
    bayar = conn.execute(
        "SELECT COALESCE(SUM(t.jumlah),0) FROM transaksi t JOIN kategori_kas k ON k.id=t.kategori_id "
        "WHERE t.status='aktif' AND k.khusus=1 AND t.tanggal < ?", (akhir,)).fetchone()[0]
    tangguh = 0
    for r in conn.execute(
            "SELECT u.jumlah - u.pokok AS bunga, u.tenor, c.ke FROM utang u JOIN utang_cicilan c ON c.utang_id = u.id "
            "WHERE u.status='aktif' AND u.tanggal < ? AND u.pokok IS NOT NULL AND u.jumlah > u.pokok AND u.tenor > 0 "
            "AND c.jatuh_tempo >= ?", (akhir, akhir)):
        tangguh += r["bunga"] // r["tenor"] + (r["bunga"] % r["tenor"] if r["ke"] == r["tenor"] else 0)
    return kredit - bayar - tangguh


def _posisi_keuangan(conn, akhir: str) -> dict:
    """Laporan posisi keuangan (neraca) per tanggal akhir (eksklusif), basis SAK EMKM sederhana:
    Aset (kas + aset tetap bersih) = Liabilitas (utang usaha) + Ekuitas (modal - prive + laba ditahan)."""
    kum = _ringkas_antara(conn, "0000-01-01", akhir)
    kas = _saldo_kas_sampai(conn, akhir)
    at = _ringkasan_aset(conn, akhir)
    utang = _sisa_utang_per(conn, akhir)
    aset = {"kas": kas, "aset_tetap": at["harga_perolehan"], "akumulasi_penyusutan": -at["akumulasi"],
            "bunga_dibayar_dimuka": max(0, -utang)}
    talangan = kum["talangan_masuk"] - kum["talangan_kembali"]
    kewajiban = {"utang_usaha": max(0, utang), "utang_pemilik": max(0, talangan)}
    if talangan < 0:                       # dikembalikan melebihi talangan: kelebihannya piutang kepada pemilik
        aset["piutang_pemilik"] = -talangan
    ekuitas = {"modal_disetor": kum["modal_masuk"], "prive": -kum["prive"], "laba_ditahan": kum["laba"]}
    total_aset, total_pasiva = sum(aset.values()), sum(kewajiban.values()) + sum(ekuitas.values())
    piutang = conn.execute("SELECT COALESCE(SUM(t.jumlah),0) FROM tagihan t JOIN pelanggan p ON p.username=t.username "
                           "WHERE t.status='belum' AND t.periode < ?", (akhir[:7],)).fetchone()[0]
    return {"per": (datetime.strptime(akhir, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d") if akhir < "9999" else None,
            "aset": aset, "kewajiban": kewajiban, "ekuitas": ekuitas,
            "total_aset": total_aset, "total_pasiva": total_pasiva, "seimbang": total_aset == total_pasiva,
            "nilai_buku_aset": at["nilai_buku"], "piutang_belum_diakui": piutang}


@app.get("/api/keuangan/laporan")
def keuangan_laporan(periode: str, request: Request):
    """Laporan keuangan standar untuk satu bulan (atau 'YYYY' untuk satu tahun):
    laba rugi, arus kas (metode langsung), posisi keuangan, dan daftar aset tetap."""
    _wajib_admin(request)
    if re.fullmatch(r"\d{4}", periode or ""):
        awal, akhir = f"{periode}-01-01", f"{int(periode) + 1}-01-01"
        nama = f"Tahun {periode}"
    elif _periode_valid(periode):
        awal, akhir = _batas_periode(periode)
        nama = _nama_periode(periode)
    else:
        raise HTTPException(status_code=400, detail="Format periode harus YYYY-MM atau YYYY.")
    conn = _billing_conn()
    try:
        r = _ringkas_antara(conn, awal, akhir, periode)
        kat = r["per_kategori"]
        pendapatan = [{"nama": "Pendapatan langganan internet", "jumlah": r["pendapatan_langganan"],
                       "ket": f"{r['jumlah_pembayaran']} pembayaran"}]
        pendapatan += [{"nama": k["nama"], "jumlah": k["total"]} for k in kat if k["jenis"] == "masuk" and k["pengaruh_laba"]]
        beban_usaha = [{"nama": k["nama"], "jumlah": k["total"], "kredit": k["kredit"]} for k in kat
                       if k["jenis"] == "keluar" and k["pengaruh_laba"] and not k.get("aset") and k["nama"] != _KAT_BUNGA]
        beban_lain = [{"nama": k["nama"], "jumlah": k["total"]} for k in kat if k["nama"] == _KAT_BUNGA and k["jenis"] == "keluar"]
        bayar_aset = conn.execute(
            "SELECT COALESCE(SUM(t.jumlah),0) FROM transaksi t JOIN utang u ON u.id=t.utang_id "
            "JOIN kategori_kas k ON k.id=u.kategori_id WHERE t.status='aktif' AND k.umur_bulan > 0 "
            "AND t.tanggal >= ? AND t.tanggal < ?", (awal, akhir)).fetchone()[0]
        kas_awal, kas_akhir = _saldo_kas_sampai(conn, awal), _saldo_kas_sampai(conn, akhir)
        posisi = _posisi_keuangan(conn, akhir)
        aset_tetap = _ringkasan_aset(conn, akhir)
        tb = _buku_tertutup(conn, periode) if _periode_valid(periode) else None
    finally:
        conn.close()
    t_pend, t_usaha, t_lain = (sum(x["jumlah"] for x in L) for L in (pendapatan, beban_usaha, beban_lain))
    operasi = [{"nama": "Penerimaan dari pelanggan (langganan & penerimaan usaha)", "jumlah": r["pendapatan"]},
               {"nama": "Pembayaran beban usaha tunai", "jumlah": -r["beban_tunai"]},
               {"nama": "Pembayaran liabilitas pemasok (beban)", "jumlah": -(r["bayar_utang"] - bayar_aset)}]
    investasi = [{"nama": "Pembelian aset tetap tunai", "jumlah": -r["beli_aset_tunai"]},
                 {"nama": "Pembayaran liabilitas pembelian aset", "jumlah": -bayar_aset}]
    pendanaan = [{"nama": "Setoran modal pemilik", "jumlah": r["modal_masuk"]},
                 {"nama": "Pengambilan pribadi pemilik (prive)", "jumlah": -r["prive"]}]
    if r["talangan_masuk"] or r["talangan_kembali"]:
        pendanaan += [{"nama": "Dana talangan dari pemilik", "jumlah": r["talangan_masuk"]},
                      {"nama": "Pengembalian dana talangan pemilik", "jumlah": -r["talangan_kembali"]}]
    bersih = {k: sum(x["jumlah"] for x in L) for k, L in (("operasi", operasi), ("investasi", investasi), ("pendanaan", pendanaan))}
    return {
        "periode": periode, "nama": nama, "awal": awal,
        "akhir": (datetime.strptime(akhir, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d"),
        "tutup_buku": dict(tb) if tb else None,
        "laba_rugi": {"pendapatan": pendapatan, "total_pendapatan": t_pend,
                      "beban_usaha": beban_usaha, "total_beban_usaha": t_usaha, "laba_usaha": t_pend - t_usaha,
                      "beban_lain": beban_lain, "total_beban_lain": t_lain, "laba_bersih": t_pend - t_usaha - t_lain,
                      "margin_persen": round((t_pend - t_usaha - t_lain) * 100 / t_pend) if t_pend else None},
        "arus_kas": {"operasi": operasi, "investasi": investasi, "pendanaan": pendanaan, "bersih": bersih,
                     "kenaikan": sum(bersih.values()), "kas_awal": kas_awal, "kas_akhir": kas_akhir,
                     "cocok": kas_awal + sum(bersih.values()) == kas_akhir},
        "posisi": posisi,
        "aset_tetap": aset_tetap,
        "pembelian_aset": r["beli_aset"],
    }


@app.get("/api/keuangan/valuasi")
def keuangan_valuasi(request: Request):
    """Bahan estimasi valuasi usaha (khusus owner). Angka pengali dipilih di layar; server hanya
    menyediakan data: posisi keuangan, pendapatan berulang (MRR) dari pelanggan aktif, dan
    EBITDA rata-rata bulan lengkap terakhir (maks. 3 bulan)."""
    _wajib_owner(request, "Valuasi hanya bisa dilihat owner.")
    hari = datetime.now()
    periode_ini = hari.strftime("%Y-%m")
    conn = _billing_conn()
    try:
        posisi = _posisi_keuangan(conn, (hari + timedelta(days=1)).strftime("%Y-%m-%d"))
        pel = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(k.harga),0) AS mrr FROM pelanggan p JOIN paket k ON k.id = p.paket_id "
            "WHERE p.status='aktif' AND p.gratis=0").fetchone()
        n_gratis = conn.execute("SELECT COUNT(*) FROM pelanggan WHERE status='aktif' AND gratis=1").fetchone()[0]
        mulai = [r[0] for r in (
            conn.execute("SELECT MIN(substr(tgl_bayar,1,7)) FROM tagihan WHERE status='lunas'").fetchone(),
            conn.execute("SELECT MIN(substr(tanggal,1,7)) FROM transaksi WHERE status='aktif'").fetchone(),
            conn.execute("SELECT MIN(substr(tanggal,1,7)) FROM utang WHERE status='aktif'").fetchone()) if r[0]]
        awal_data = min(mulai) if mulai else periode_ini
        # Bulan lengkap terakhir (bulan berjalan belum selesai); bila belum ada, pakai bulan berjalan.
        bulan = []
        p = _geser_periode(periode_ini, -1)
        while p >= awal_data and len(bulan) < 3:
            bulan.append(p)
            p = _geser_periode(p, -1)
        if not bulan:
            bulan = [periode_ini]
        rinci = []
        for per in reversed(bulan):
            r = _ringkas_periode(conn, per)
            bunga = sum(k["total"] for k in r["per_kategori"] if k["nama"] == _KAT_BUNGA and k["jenis"] == "keluar")
            rinci.append({"periode": per, "pendapatan": r["pendapatan"], "beban": r["beban"], "laba": r["laba"],
                          "penyusutan": r["penyusutan"], "bunga": bunga,
                          "ebitda": r["laba"] + r["penyusutan"] + bunga})
    finally:
        conn.close()
    n = len(rinci)
    rata = {k: round(sum(x[k] for x in rinci) / n) for k in ("pendapatan", "laba", "ebitda")}
    utang = posisi["kewajiban"].get("utang_usaha", 0) + posisi["kewajiban"].get("utang_pemilik", 0)
    return {
        "per": hari.strftime("%Y-%m-%d"), "mulai_usaha": awal_data,
        "umur_bulan": max(1, _bulan_ke(periode_ini + "-01") - _bulan_ke(awal_data + "-01") + 1),
        "nilai_buku": posisi["total_aset"] - sum(posisi["kewajiban"].values()),
        "kas": posisi["aset"]["kas"], "aset_tetap_buku": posisi["nilai_buku_aset"], "utang": utang,
        "neraca_seimbang": posisi["seimbang"],
        "pelanggan_berbayar": pel["n"], "pelanggan_gratis": n_gratis, "mrr": pel["mrr"],
        "arpu": round(pel["mrr"] / pel["n"]) if pel["n"] else 0, "arr": pel["mrr"] * 12,
        "bulan_dihitung": rinci, "rata_bulanan": rata,
        "ebitda_tahunan": rata["ebitda"] * 12,
    }


@app.get("/api/keuangan/keseluruhan")
def keuangan_keseluruhan(request: Request):
    """Gambaran keuangan sejak data pertama tercatat sampai bulan ini."""
    _wajib_admin(request)
    periode_ini = datetime.now().strftime("%Y-%m")
    conn = _billing_conn()
    try:
        awal = [r[0] for r in (
            conn.execute("SELECT MIN(substr(tgl_bayar,1,7)) FROM tagihan WHERE status='lunas' AND tgl_bayar IS NOT NULL").fetchone(),
            conn.execute("SELECT MIN(substr(tanggal,1,7)) FROM transaksi WHERE status='aktif'").fetchone(),
            conn.execute("SELECT MIN(substr(tanggal,1,7)) FROM utang WHERE status='aktif'").fetchone(),
        ) if r[0] and _periode_valid(r[0])]
        mulai = min(awal) if awal else periode_ini
        mulai = max(mulai, _geser_periode(periode_ini, -119))     # maks. 10 tahun ditampilkan
        bulanan, kategori = [], {}
        p, laba_kum, saldo = mulai, 0, _saldo_kas_sampai(conn, f"{mulai}-01")
        while p <= periode_ini:
            r = _ringkas_periode(conn, p)
            laba_kum += r["laba"]
            saldo += r["arus_kas"]
            b = {k: r[k] for k in ("periode", "pendapatan", "pendapatan_langganan", "pendapatan_lain", "beban",
                                   "laba", "modal_masuk", "prive", "bayar_utang", "arus_kas", "penyusutan", "beli_aset")}
            b.update(laba_kumulatif=laba_kum, saldo_akhir=saldo)
            bulanan.append(b)
            for k in r["per_kategori"]:
                g = kategori.setdefault(k["id"], {**k, "total": 0, "n": 0, "kredit": 0})
                g["total"] += k["total"]
                g["n"] += k["n"]
                g["kredit"] += k["kredit"]
            p = _geser_periode(p, 1)
        saldo_kas = _saldo_kas_sampai(conn, (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"))
        utang_info = _ringkas_utang(conn)
        # Bunga cicilan yang jatuh tempo setelah bulan ini belum menjadi beban, jadi juga belum
        # menjadi kewajiban yang sudah terjadi (dikurangkan dari sisa utang di neraca).
        bunga_tangguh = _bunga_diakui(conn, _batas_periode(periode_ini)[1], "9999-12-31")[0]
        piutang = conn.execute(
            "SELECT COALESCE(SUM(t.jumlah),0), COUNT(*), COUNT(DISTINCT t.username) FROM tagihan t "
            "JOIN pelanggan p ON p.username = t.username WHERE t.status='belum'").fetchone()
        pelanggan = conn.execute(
            "SELECT SUM(status='aktif' AND gratis=0), SUM(status='aktif' AND gratis=1), SUM(status='berhenti') FROM pelanggan").fetchone()
        jml_trx = conn.execute("SELECT COUNT(*) FROM transaksi WHERE status='aktif'").fetchone()[0]
        jml_bayar = conn.execute("SELECT COUNT(*) FROM tagihan WHERE status='lunas'").fetchone()[0]
        insentif = conn.execute(
            "SELECT COALESCE(SUM(t.jumlah),0) FROM transaksi t JOIN kategori_kas k ON k.id=t.kategori_id "
            "WHERE t.status='aktif' AND k.nama=?", (_KAT_INSENTIF,)).fetchone()[0]
    finally:
        conn.close()
    total = {k: sum(b[k] for b in bulanan) for k in ("pendapatan", "pendapatan_langganan", "pendapatan_lain", "beban",
                                                      "laba", "modal_masuk", "prive", "bayar_utang", "penyusutan", "beli_aset")}
    tahunan: dict[str, dict] = {}
    for b in bulanan:
        t = tahunan.setdefault(b["periode"][:4], {"tahun": b["periode"][:4], "pendapatan": 0, "beban": 0, "laba": 0, "bulan": 0})
        for k in ("pendapatan", "beban", "laba"):
            t[k] += b[k]
        t["bulan"] += 1
    ada_data = [b for b in bulanan if b["pendapatan"] or b["beban"]]
    # Neraca per akhir bulan ini. Aset = Kewajiban + Ekuitas harus selalu seimbang;
    # bila tidak, ada data yang janggal dan ditampilkan sebagai peringatan.
    conn = _billing_conn()
    try:
        neraca = _posisi_keuangan(conn, _batas_periode(periode_ini)[1])
    finally:
        conn.close()
    total_aset, total_pasiva = neraca["total_aset"], neraca["total_pasiva"]
    peringatan = []
    if total_aset != total_pasiva:
        peringatan.append(f"Neraca tidak seimbang (selisih {_rp(total_aset - total_pasiva)}). Periksa transaksi bertanggal di masa depan atau data yang diubah langsung di database.")
    conn = _billing_conn()
    try:
        abaikan = _kas_minus_diabaikan(conn)
        conn.commit()
        try:
            tertutup = {r[0] for r in conn.execute("SELECT periode FROM tutup_buku").fetchall()}
        except sqlite3.OperationalError:
            tertutup = set()
    finally:
        conn.close()
    # Selama buku masih terbuka, kas minus sering hanya karena pemasukan belum dicatat; peringatan
    # baru ditampilkan untuk bulan yang bukunya sudah ditutup (saat tutup buku sudah diingatkan).
    minus = [b["periode"] for b in bulanan if b["saldo_akhir"] < 0 and b["periode"] in tertutup
             and not (abaikan and b["periode"] <= abaikan)]
    if minus:
        peringatan.append(f"Saldo kas tercatat negatif pada {', '.join(_nama_periode(p) for p in minus[:6])}"
                          f"{' dan bulan lain' if len(minus) > 6 else ''}. Uang tidak mungkin minus — kemungkinan ada "
                          "setoran modal/pemasukan yang belum dicatat, atau pengeluaran tercatat dobel.")
    return {
        "mulai": mulai, "sampai": periode_ini, "jumlah_bulan": len(bulanan),
        "total": total, "saldo_kas": saldo_kas,
        "rata_laba": int(total["laba"] / len(ada_data)) if ada_data else 0,
        "terbaik": max(ada_data, key=lambda b: b["laba"])["periode"] if ada_data else None,
        "terburuk": min(ada_data, key=lambda b: b["laba"])["periode"] if ada_data else None,
        "modal_kembali_persen": round(total["laba"] * 100 / total["modal_masuk"]) if total["modal_masuk"] else None,
        "insentif_dibayar": insentif,
        "utang": utang_info,
        "piutang": {"total": piutang[0], "jumlah": piutang[1], "pelanggan": piutang[2]},
        "pelanggan": {"berbayar": pelanggan[0] or 0, "gratis": pelanggan[1] or 0, "berhenti": pelanggan[2] or 0},
        "jumlah_transaksi": jml_trx, "jumlah_pembayaran": jml_bayar,
        "neraca": {**neraca, "piutang_belum_diakui": piutang[0], "bunga_belum_jatuh_tempo": bunga_tangguh},
        "peringatan": peringatan,
        "bulanan": bulanan, "tahunan": list(tahunan.values()),
        "per_kategori": sorted(kategori.values(), key=lambda g: -g["total"]),
    }

@app.get("/api/keuangan/kategori")
def keuangan_kategori(request: Request):
    _wajib_admin(request)
    conn = _billing_conn()
    try:
        rows = conn.execute("SELECT k.id, k.nama, k.jenis, k.pengaruh_laba, k.aktif, k.khusus, k.umur_bulan, "
                            "(SELECT COUNT(*) FROM transaksi t WHERE t.kategori_id=k.id) + "
                            "(SELECT COUNT(*) FROM utang u WHERE u.kategori_id=k.id) AS dipakai FROM kategori_kas k "
                            "ORDER BY k.jenis DESC, k.umur_bulan IS NOT NULL, k.id").fetchall()
    finally:
        conn.close()
    bawaan = _nama_kategori_bawaan()
    return {"kategori": [dict(r, bawaan=r["nama"] in bawaan or bool(r["khusus"])) for r in rows]}


class KategoriIn(BaseModel):
    nama: str
    jenis: str
    pengaruh_laba: bool = True
    umur_bulan: Optional[int] = None     # diisi (48/96) = kategori aset tetap yang disusutkan


def _nama_kategori_bawaan() -> set:
    return ({n for n, _j, _l in _KATEGORI_AWAL} | {n for n, _u in _KAT_ASET_AWAL}
            | {_KAT_BAYAR_UTANG, _KAT_INSENTIF, _KAT_BUNGA, _KAT_TALANGAN_MASUK, _KAT_TALANGAN_KEMBALI})


@app.post("/api/keuangan/kategori")
def keuangan_tambah_kategori(d: KategoriIn, request: Request):
    _wajib_admin(request)
    nama = d.nama.strip()
    if not nama or len(nama) > 80 or d.jenis not in ("masuk", "keluar"):
        raise HTTPException(status_code=400, detail="Nama kategori wajib (maks. 80 karakter) dan jenis masuk/keluar.")
    if d.umur_bulan is not None and (d.jenis != "keluar" or d.umur_bulan not in (48, 96)):
        raise HTTPException(status_code=400, detail="Umur aset tetap: 48 bulan (4 tahun) atau 96 bulan (8 tahun), khusus pengeluaran.")
    if d.umur_bulan and not nama.lower().startswith("aset"):
        nama = f"Aset: {nama}"
    conn = _billing_conn()
    try:
        try:
            cur = conn.execute("INSERT INTO kategori_kas (nama, jenis, pengaruh_laba, umur_bulan) VALUES (?,?,?,?)",
                               (nama, d.jenis, int(d.pengaruh_laba or bool(d.umur_bulan)), d.umur_bulan))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Kategori itu sudah ada.")
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "kas_kategori", f"tambah kategori {d.jenis}: {nama}"
                    + (f" (aset tetap, {d.umur_bulan // 12} th)" if d.umur_bulan else ""))
    return {"id": cur.lastrowid, "nama": nama}


@app.delete("/api/keuangan/kategori/{kid}")
def keuangan_hapus_kategori(kid: int, request: Request, pindah_ke: Optional[int] = None):
    """Owner: hapus kategori buatan sendiri. Bila sudah dipakai, transaksi & liabilitasnya
    dipindahkan dulu ke kategori lain (jenis sama). Kategori bawaan tidak bisa dihapus."""
    _wajib_owner(request, "Menghapus kategori hanya bisa dilakukan owner.")
    conn = _billing_conn()
    try:
        k = conn.execute("SELECT * FROM kategori_kas WHERE id=?", (kid,)).fetchone()
        if not k:
            raise HTTPException(status_code=404, detail="Kategori tidak ditemukan.")
        if k["khusus"] or k["nama"] in _nama_kategori_bawaan():
            raise HTTPException(status_code=400, detail="Kategori bawaan tidak bisa dihapus.")
        tgl = [r[0] for r in conn.execute("SELECT tanggal FROM transaksi WHERE kategori_id=? UNION ALL "
                                          "SELECT tanggal FROM utang WHERE kategori_id=?", (kid, kid))]
        tujuan = None
        if tgl:
            if pindah_ke is None:
                raise HTTPException(status_code=400, detail=f"Kategori ini dipakai {len(tgl)} catatan. Pilih kategori tujuan pemindahan.")
            tujuan = conn.execute("SELECT * FROM kategori_kas WHERE id=?", (pindah_ke,)).fetchone()
            if not tujuan or tujuan["id"] == kid or tujuan["jenis"] != k["jenis"] or tujuan["khusus"]:
                raise HTTPException(status_code=400, detail="Kategori tujuan harus kategori lain dengan jenis yang sama.")
            _wajib_buku_terbuka(conn, *tgl)
            conn.execute("UPDATE transaksi SET kategori_id=? WHERE kategori_id=?", (pindah_ke, kid))
            conn.execute("UPDATE utang SET kategori_id=? WHERE kategori_id=?", (pindah_ke, kid))
        conn.execute("DELETE FROM kategori_kas WHERE id=?", (kid,))
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "kas_kategori", f"hapus kategori {k['jenis']}: {k['nama']}"
                    + (f" — {len(tgl)} catatan dipindah ke {tujuan['nama']}" if tujuan else ""))
    return {"ok": True, "dipindah": len(tgl)}


def _daftar_utang(conn, hanya_belum: bool = False) -> list[dict]:
    rows = conn.execute(
        "SELECT u.*, k.nama AS kategori, "
        "(SELECT COALESCE(SUM(t.jumlah),0) FROM transaksi t WHERE t.utang_id = u.id AND t.status='aktif') AS dibayar "
        "FROM utang u JOIN kategori_kas k ON k.id = u.kategori_id "
        "ORDER BY u.status, COALESCE(u.jatuh_tempo, '9999-12-31'), u.id DESC"
    ).fetchall()
    hasil = []
    hari = datetime.now().strftime("%Y-%m-%d")
    jadwal_semua: dict[int, list] = {}
    for c in conn.execute("SELECT utang_id, ke, jatuh_tempo, jumlah FROM utang_cicilan ORDER BY utang_id, ke"):
        jadwal_semua.setdefault(c[0], []).append({"ke": c[1], "jatuh_tempo": c[2], "jumlah": c[3]})
    for r in rows:
        d = dict(r)
        d["sisa"] = d["jumlah"] - d["dibayar"] if d["status"] == "aktif" else 0
        d["lunas"] = d["status"] == "aktif" and d["sisa"] <= 0
        d["bunga"] = max(0, d["jumlah"] - d["pokok"]) if d.get("pokok") is not None else 0
        jadwal = jadwal_semua.get(d["id"])
        if jadwal:
            # Pembayaran (di luar uang muka) menutup cicilan berurutan dari yang pertama
            tertutup = max(0, d["dibayar"] - (d.get("uang_muka") or 0))
            kum, berikut, tertunggak = 0, None, 0
            for c in jadwal:
                awal_c, kum = kum, kum + c["jumlah"]
                dibayar_c = min(c["jumlah"], max(0, tertutup - awal_c))
                c["dibayar"], c["sisa"] = dibayar_c, c["jumlah"] - dibayar_c
                c["status"] = ("lunas" if c["sisa"] <= 0 else
                               ("terlambat" if c["jatuh_tempo"] < hari else ("sebagian" if dibayar_c else "belum")))
                if c["sisa"] > 0 and c["jatuh_tempo"] < hari:
                    tertunggak += c["sisa"]
                if berikut is None and c["sisa"] > 0:
                    berikut = c
            d["jadwal"] = jadwal
            d["jatuh_tempo_akhir"] = jadwal[-1]["jatuh_tempo"]
            d["cicilan_lunas"] = sum(1 for c in jadwal if c["status"] == "lunas")
            if berikut and d["status"] == "aktif":
                d["jatuh_tempo"] = berikut["jatuh_tempo"]          # yang dipakai pengingat & urutan
                d["cicilan_ke"] = berikut["ke"]
                d["tagihan_berikut"] = min(berikut["sisa"], d["sisa"])
            d["tertunggak"] = tertunggak
            d["lewat_tempo"] = bool(d["status"] == "aktif" and tertunggak > 0)
        else:
            d["tagihan_berikut"] = d["sisa"]
            d["lewat_tempo"] = bool(d["status"] == "aktif" and d["sisa"] > 0 and d["jatuh_tempo"] and d["jatuh_tempo"] < hari)
            d["tertunggak"] = d["sisa"] if d["lewat_tempo"] else 0
        if hanya_belum and (d["status"] != "aktif" or d["sisa"] <= 0):
            continue
        hasil.append(d)
    hasil.sort(key=lambda u: (u["status"], u["jatuh_tempo"] or "9999-12-31", -u["id"]))
    return hasil


def _ringkas_utang(conn) -> dict:
    belum = _daftar_utang(conn, hanya_belum=True)
    lewat = [u for u in belum if u["lewat_tempo"]]
    tempo = sorted((u for u in belum if u["jatuh_tempo"]), key=lambda u: u["jatuh_tempo"])
    return {"sisa": sum(u["sisa"] for u in belum), "jumlah": len(belum),
            "lewat_tempo": len(lewat), "lewat_tempo_total": sum(u["tertunggak"] for u in lewat),
            "terdekat": ({"pemasok": tempo[0]["pemasok"], "sisa": tempo[0]["tagihan_berikut"],
                          "jatuh_tempo": tempo[0]["jatuh_tempo"], "cicilan_ke": tempo[0].get("cicilan_ke"),
                          "tenor": tempo[0].get("tenor")} if tempo else None)}


class UtangIn(BaseModel):
    tanggal: str
    pemasok: str
    kategori_id: int
    jumlah: int                         # harga barang/jasa (pokok)
    jatuh_tempo: Optional[str] = None   # sekali bayar; untuk cicilan = jatuh tempo cicilan pertama
    keterangan: Optional[str] = None
    uang_muka: int = 0
    metode_uang_muka: str = "tunai"
    tenor: Optional[int] = None         # diisi = cicilan bulanan sebanyak ini
    cicilan: Optional[int] = None       # nominal per bulan (termasuk bunga/biaya)
    qty: Optional[float] = None
    satuan: Optional[str] = None


def _tambah_bulan(tanggal: str, n: int) -> str:
    """Tanggal yang sama n bulan kemudian; tanggal 29-31 menyesuaikan akhir bulan."""
    d = datetime.strptime(tanggal, "%Y-%m-%d")
    total = d.year * 12 + (d.month - 1) + n
    th, bl = total // 12, total % 12 + 1
    akhir = ((datetime(th + (bl == 12), bl % 12 + 1, 1)) - timedelta(days=1)).day
    return f"{th}-{bl:02d}-{min(d.day, akhir):02d}"


class BayarUtangIn(BaseModel):
    tanggal: str
    jumlah: int
    metode: str = "tunai"
    keterangan: Optional[str] = None


def _id_kat_bayar_utang(conn) -> int:
    return conn.execute("SELECT id FROM kategori_kas WHERE khusus = 1 AND nama = ?", (_KAT_BAYAR_UTANG,)).fetchone()[0]


def _catat_bayar_utang(conn, utang_id: int, d: BayarUtangIn, oleh: str) -> int:
    cur = conn.execute(
        "INSERT INTO transaksi (tanggal, jenis, kategori_id, jumlah, metode, keterangan, dicatat_oleh, dicatat_pada, utang_id) "
        "VALUES (?, 'keluar', ?, ?, ?, ?, ?, ?, ?)",
        (d.tanggal, _id_kat_bayar_utang(conn), d.jumlah, d.metode, (d.keterangan or "").strip()[:500] or None,
         oleh, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), utang_id),
    )
    _beri_nomor(conn, cur.lastrowid)
    return cur.lastrowid


# ------------------------------------------------------------
# Nota (foto/PDF bukti transaksi). Disimpan sebagai berkas di folder NOTA_FOLDER
# dengan nama acak; database hanya menyimpan metadata. Tidak pernah dihapus
# permanen: "hapus" hanya menandai, berkasnya tetap ada sebagai jejak audit.
# ------------------------------------------------------------

_JENIS_NOTA = {  # tanda tangan berkas (magic bytes) -> (mime, ekstensi)
    b"\xff\xd8\xff": ("image/jpeg", "jpg"),
    b"\x89PNG\r\n\x1a\n": ("image/png", "png"),
    b"%PDF-": ("application/pdf", "pdf"),
}


def _folder_nota() -> str:
    folder = os.path.join(_env("INDESO_DATA_DIR", BASE_DIR), CONFIG["NOTA_FOLDER"])
    os.makedirs(folder, exist_ok=True)
    return folder


def init_nota_db() -> None:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nota (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pemilik TEXT NOT NULL CHECK (pemilik IN ('transaksi','utang')),
                pemilik_id INTEGER NOT NULL,
                berkas TEXT NOT NULL UNIQUE,
                mime TEXT NOT NULL,
                ukuran INTEGER NOT NULL,
                nama_asli TEXT,
                diunggah_oleh TEXT NOT NULL,
                diunggah_pada TEXT NOT NULL,
                dihapus_oleh TEXT,
                dihapus_pada TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nota_pemilik ON nota(pemilik, pemilik_id)")
        conn.execute("CREATE TABLE IF NOT EXISTS status_sistem (kunci TEXT PRIMARY KEY, nilai TEXT NOT NULL)")
        conn.commit()
    finally:
        conn.close()


def _jenis_berkas(isi: bytes) -> Optional[tuple[str, str]]:
    for awal, jenis in _JENIS_NOTA.items():
        if isi.startswith(awal):
            return jenis
    if isi[:4] == b"RIFF" and isi[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def _ambil_pemilik_nota(conn, pemilik: str, pid: int):
    tabel = {"transaksi": "transaksi", "utang": "utang"}.get(pemilik)
    if not tabel:
        raise HTTPException(status_code=404, detail="Jenis tidak dikenal.")
    r = conn.execute(f"SELECT * FROM {tabel} WHERE id = ?", (pid,)).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Transaksi tidak ditemukan.")
    return r


def _boleh_kelola_nota(request: Request, item) -> None:
    """Owner boleh kapan saja. Admin: hanya pada catatannya sendiri, dalam 24 jam."""
    if request.state.peran == "owner":
        return
    if item["dicatat_oleh"] != request.state.user:
        raise HTTPException(status_code=403, detail="Hanya pencatat atau owner yang bisa menambah/menghapus nota.")
    if datetime.now() - datetime.strptime(item["dicatat_pada"], "%Y-%m-%d %H:%M:%S") > timedelta(hours=24):
        raise HTTPException(status_code=403, detail="Lewat 24 jam sejak dicatat — minta owner untuk menambah nota.")


def _daftar_nota(conn, pemilik: str, ids: list[int]) -> dict[int, list[dict]]:
    if not ids:
        return {}
    tanda = ",".join("?" * len(ids))
    hasil: dict[int, list[dict]] = {}
    for r in conn.execute(
        f"SELECT id, pemilik_id, mime, ukuran, nama_asli, diunggah_oleh, diunggah_pada FROM nota "
        f"WHERE pemilik = ? AND pemilik_id IN ({tanda}) AND dihapus_pada IS NULL ORDER BY id", [pemilik, *ids]
    ):
        hasil.setdefault(r["pemilik_id"], []).append(dict(r))
    return hasil


@app.post("/api/keuangan/{pemilik}/{pid}/nota")
async def unggah_nota(pemilik: str, pid: int, request: Request):
    """Isi body = berkas mentah (JPEG/PNG/WEBP/PDF), nama asli di header X-Nama-Berkas.
    Tidak memakai multipart supaya tidak perlu dependensi tambahan."""
    _wajib_admin(request)
    batas = CONFIG["NOTA_MAKS_MB"] * 1024 * 1024
    if int(request.headers.get("content-length") or 0) > batas:
        raise HTTPException(status_code=413, detail=f"Berkas maksimal {CONFIG['NOTA_MAKS_MB']} MB.")
    isi = await request.body()
    if not isi or len(isi) > batas:
        raise HTTPException(status_code=413 if isi else 400,
                            detail=f"Berkas kosong atau lebih dari {CONFIG['NOTA_MAKS_MB']} MB.")
    jenis = _jenis_berkas(isi)
    if not jenis:
        raise HTTPException(status_code=415, detail="Hanya foto (JPG/PNG/WEBP) atau PDF.")
    nama_asli = re.sub(r"[^\w .()-]", "_", request.headers.get("x-nama-berkas", ""))[:120] or None

    def simpan():
        conn = _billing_conn()
        try:
            item = _ambil_pemilik_nota(conn, pemilik, pid)
            if item["status"] != "aktif":
                raise HTTPException(status_code=400, detail="Transaksi sudah dibatalkan.")
            _boleh_kelola_nota(request, item)
            n = conn.execute("SELECT COUNT(*) FROM nota WHERE pemilik=? AND pemilik_id=? AND dihapus_pada IS NULL",
                             (pemilik, pid)).fetchone()[0]
            if n >= CONFIG["NOTA_MAKS_PER_ITEM"]:
                raise HTTPException(status_code=400, detail=f"Maksimal {CONFIG['NOTA_MAKS_PER_ITEM']} nota per transaksi.")
            berkas = f"{datetime.now().strftime('%Y%m')}/{secrets.token_hex(12)}.{jenis[1]}"
            path = os.path.join(_folder_nota(), berkas)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(isi)
            cur = conn.execute(
                "INSERT INTO nota (pemilik, pemilik_id, berkas, mime, ukuran, nama_asli, diunggah_oleh, diunggah_pada) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (pemilik, pid, berkas, jenis[0], len(isi), nama_asli, _oleh(request),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    nid = await asyncio.to_thread(simpan)
    kode = f"#U{pid}" if pemilik == "utang" else f"#{pid}"
    catat_aktivitas(_oleh(request), "nota_unggah", f"{kode}: nota {jenis[1].upper()} {round(len(isi) / 1024)} KB")
    return {"id": nid}


@app.get("/api/keuangan/nota/{nid}")
def lihat_nota(nid: int, request: Request):
    _wajib_admin(request)
    conn = _billing_conn()
    try:
        r = conn.execute("SELECT berkas, mime, nama_asli FROM nota WHERE id = ?", (nid,)).fetchone()
    finally:
        conn.close()
    if not r:
        raise HTTPException(status_code=404, detail="Nota tidak ditemukan.")
    path = os.path.realpath(os.path.join(_folder_nota(), r["berkas"]))
    if not path.startswith(os.path.realpath(_folder_nota()) + os.sep) or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Berkas nota tidak ada di server.")
    return FileResponse(path, media_type=r["mime"], headers={
        "Content-Disposition": f'inline; filename="nota-{nid}.{r["berkas"].rsplit(".", 1)[-1]}"',
        "Cache-Control": "private, max-age=3600",
    })


@app.delete("/api/keuangan/nota/{nid}")
def hapus_nota(nid: int, request: Request):
    _wajib_admin(request)
    conn = _billing_conn()
    try:
        r = conn.execute("SELECT * FROM nota WHERE id = ? AND dihapus_pada IS NULL", (nid,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Nota tidak ditemukan.")
        item = _ambil_pemilik_nota(conn, r["pemilik"], r["pemilik_id"])
        _boleh_kelola_nota(request, item)
        _wajib_buku_terbuka(conn, item["tanggal"])
        conn.execute("UPDATE nota SET dihapus_oleh=?, dihapus_pada=? WHERE id=?",
                     (_oleh(request), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), nid))
        conn.commit()
    finally:
        conn.close()
    kode = f"#U{r['pemilik_id']}" if r["pemilik"] == "utang" else f"#{r['pemilik_id']}"
    catat_aktivitas(_oleh(request), "nota_hapus", f"{kode}: nota #{nid} dihapus (berkas tetap disimpan)")
    return {"ok": True}


def _tanpa_nota(conn, awal: str, akhir: str) -> dict:
    """Pengeluaran >= NOTA_WAJIB_DI_ATAS yang belum punya nota (transaksi tunai + pembelian kredit)."""
    batas = CONFIG["NOTA_WAJIB_DI_ATAS"]
    trx = conn.execute(
        "SELECT t.id, t.tanggal, t.jumlah, k.nama AS kategori, t.keterangan, t.dicatat_oleh FROM transaksi t "
        "JOIN kategori_kas k ON k.id = t.kategori_id "
        "WHERE t.status='aktif' AND t.jenis='keluar' AND t.utang_id IS NULL AND t.jumlah >= ? "
        "AND t.tanggal >= ? AND t.tanggal < ? AND k.pengaruh_laba = 1 "
        "AND NOT EXISTS (SELECT 1 FROM nota n WHERE n.pemilik='transaksi' AND n.pemilik_id=t.id AND n.dihapus_pada IS NULL)",
        (batas, awal, akhir)).fetchall()
    utg = conn.execute(
        "SELECT u.id, u.tanggal, u.jumlah, k.nama AS kategori, u.pemasok AS keterangan, u.dicatat_oleh FROM utang u "
        "JOIN kategori_kas k ON k.id = u.kategori_id WHERE u.status='aktif' AND u.jumlah >= ? "
        "AND u.tanggal >= ? AND u.tanggal < ? "
        "AND NOT EXISTS (SELECT 1 FROM nota n WHERE n.pemilik='utang' AND n.pemilik_id=u.id AND n.dihapus_pada IS NULL)",
        (batas, awal, akhir)).fetchall()
    daftar = [dict(r, kode=f"#{r['id']}") for r in trx] + [dict(r, kode=f"#U{r['id']}") for r in utg]
    return {"batas": batas, "jumlah": len(daftar), "total": sum(d["jumlah"] for d in daftar),
            "daftar": sorted(daftar, key=lambda d: -d["jumlah"])[:20]}


def _cek_tanggal_lalu(t: str, label: str = "Tanggal") -> None:
    if not _tanggal_valid(t):
        raise HTTPException(status_code=400, detail=f"{label} tidak valid.")
    if t > (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"):
        raise HTTPException(status_code=400, detail=f"{label} tidak boleh di masa depan.")


# ------------------------------------------------------------
# Tutup buku. Setelah owner memeriksa satu bulan, bulan itu dikunci: tidak ada transaksi,
# pelunasan, utang, pembayaran, atau penghapusan yang boleh bertanggal di bulan itu, supaya
# laporan yang sudah dikirim/diperiksa tidak berubah diam-diam. Koreksi dicatat di bulan
# berjalan, atau owner membuka kembali bukunya dengan alasan (tercatat di log).
# ------------------------------------------------------------

def _buku_tertutup(conn, tanggal: Optional[str]):
    if not tanggal:
        return None
    try:
        return conn.execute("SELECT * FROM tutup_buku WHERE periode = ?", (str(tanggal)[:7],)).fetchone()
    except sqlite3.OperationalError:
        return None


def _wajib_buku_terbuka(conn, *tanggal: Optional[str]) -> None:
    for t in tanggal:
        r = _buku_tertutup(conn, t)
        if r:
            raise HTTPException(status_code=409, detail=(
                f"Buku {_nama_periode(r['periode'])} sudah ditutup oleh {r['oleh']}. Catat koreksinya di bulan berjalan, "
                f"atau minta owner membuka kembali buku bulan itu."))


class TutupBukuIn(BaseModel):
    catatan: Optional[str] = None


@app.get("/api/keuangan/tutup-buku")
def daftar_tutup_buku(request: Request):
    _wajib_admin(request)
    conn = _billing_conn()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM tutup_buku ORDER BY periode DESC")]
    finally:
        conn.close()
    return {"tutup_buku": rows}


@app.post("/api/keuangan/tutup-buku/{periode}")
def tutup_buku(periode: str, d: TutupBukuIn, request: Request):
    _wajib_owner(request, "Tutup buku hanya bisa dilakukan owner.")
    if not _periode_valid(periode):
        raise HTTPException(status_code=400, detail="Format periode harus YYYY-MM.")
    if periode > datetime.now().strftime("%Y-%m"):
        raise HTTPException(status_code=400, detail="Bulan yang belum berjalan tidak bisa ditutup.")
    conn = _billing_conn()
    try:
        if _buku_tertutup(conn, periode):
            raise HTTPException(status_code=400, detail="Buku bulan ini sudah ditutup.")
        r = _ringkas_periode(conn, periode)
        # Komisi mitra bulan ini ikut final (hasil kerja bulan ini, dibayar awal bulan depan)
        _siapkan_kunci_komisi(conn)
        # Bulan tanpa tagihan di INDESO (sebelum Penagihan dipakai) tidak dikunci otomatis di Rp0;
        # komisinya dikunci manual oleh owner dengan angka yang disepakati.
        if _ada_tagihan(conn, periode) and not conn.execute("SELECT 1 FROM komisi_kunci WHERE periode = ?", (periode,)).fetchone():
            _kunci_komisi(conn, periode, "tutup_buku", _oleh(request))
        conn.execute("INSERT INTO tutup_buku (periode, oleh, pada, catatan) VALUES (?,?,?,?)",
                     (periode, _oleh(request), datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      (d.catatan or "").strip()[:300] or None))
        saldo = _saldo_kas_sampai(conn, _batas_periode(periode)[1])
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "tutup_buku",
                    f"{_nama_periode(periode)} ditutup · pendapatan {_rp(r['pendapatan'])}, beban {_rp(r['beban'])}, "
                    f"laba {_rp(r['laba'])}, saldo kas akhir {_rp(saldo)}" + (f" · {d.catatan.strip()[:120]}" if d.catatan and d.catatan.strip() else ""))
    return {"ok": True}


@app.post("/api/keuangan/tutup-buku/{periode}/buka")
def buka_buku(periode: str, d: BatalIn, request: Request):
    _wajib_owner(request, "Membuka kembali buku hanya bisa dilakukan owner.")
    alasan = d.alasan.strip()
    if len(alasan) < 3:
        raise HTTPException(status_code=400, detail="Tuliskan alasan membuka kembali buku.")
    conn = _billing_conn()
    try:
        if not _buku_tertutup(conn, periode):
            raise HTTPException(status_code=404, detail="Buku bulan ini belum ditutup.")
        conn.execute("DELETE FROM tutup_buku WHERE periode = ?", (periode,))
        _siapkan_kunci_komisi(conn)
        conn.execute("DELETE FROM komisi_kunci WHERE periode = ? AND sumber = 'tutup_buku'", (periode,))
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "buka_buku", f"{_nama_periode(periode)} dibuka kembali: {alasan[:200]}")
    return {"ok": True}


@app.get("/api/keuangan/utang")
def keuangan_list_utang(request: Request, status: str = "belum"):
    _wajib_admin(request)
    conn = _billing_conn()
    try:
        semua = _daftar_utang(conn)
        bayar = {}
        for r in conn.execute(
            "SELECT t.id, t.utang_id, t.tanggal, t.jumlah, t.metode, t.keterangan, t.dicatat_oleh, t.status, t.batal_alasan "
            "FROM transaksi t WHERE t.utang_id IS NOT NULL ORDER BY t.tanggal, t.id"
        ):
            bayar.setdefault(r["utang_id"], []).append(dict(r))
        ringkas = _ringkas_utang(conn)
        nota = _daftar_nota(conn, "utang", [u["id"] for u in semua])
    finally:
        conn.close()
    for u in semua:
        u["nota"] = nota.get(u["id"], [])
    if status == "belum":
        semua = [u for u in semua if u["status"] == "aktif" and u["sisa"] > 0]
    elif status == "lunas":
        semua = [u for u in semua if u["lunas"]]
    elif status == "dihapus":
        semua = [u for u in semua if u["status"] == "batal"]
    else:
        semua = [u for u in semua if u["status"] == "aktif"]
    for u in semua:
        u["pembayaran"] = bayar.get(u["id"], [])
    return {"saya": request.state.user, "peran": request.state.peran, "ringkasan": ringkas, "utang": semua}


@app.post("/api/keuangan/utang")
def keuangan_tambah_utang(d: UtangIn, request: Request):
    """Pembelian kredit: beban dicatat sekarang, kas berkurang saat dibayar."""
    _wajib_admin(request)
    _cek_tanggal_lalu(d.tanggal, "Tanggal pembelian")
    pemasok = d.pemasok.strip()
    if not pemasok or len(pemasok) > 100:
        raise HTTPException(status_code=400, detail="Isi nama pemasok/toko (maks. 100 karakter).")
    if not 0 < d.jumlah <= 10_000_000_000:
        raise HTTPException(status_code=400, detail="Jumlah harus lebih dari 0.")
    if d.jatuh_tempo and (not _tanggal_valid(d.jatuh_tempo) or d.jatuh_tempo < d.tanggal):
        raise HTTPException(status_code=400, detail="Jatuh tempo tidak valid (tidak boleh sebelum tanggal pembelian).")
    if not 0 <= d.uang_muka < d.jumlah:
        raise HTTPException(status_code=400, detail="Uang muka harus kurang dari total pembelian. Kalau lunas, catat sebagai pengeluaran biasa.")
    if d.metode_uang_muka not in _METODE:
        raise HTTPException(status_code=400, detail="Metode uang muka tidak valid.")
    d.qty, d.satuan = _cek_qty(d.qty, d.satuan)
    jadwal, total = [], d.jumlah
    if d.tenor:
        if not 2 <= d.tenor <= 60:
            raise HTTPException(status_code=400, detail="Tenor angsuran 2–60 bulan.")
        if not d.cicilan or d.cicilan <= 0:
            raise HTTPException(status_code=400, detail="Isi nominal angsuran per bulan.")
        if not d.jatuh_tempo:
            raise HTTPException(status_code=400, detail="Isi jatuh tempo angsuran pertama.")
        total = d.uang_muka + d.cicilan * d.tenor
        if total < d.jumlah:
            raise HTTPException(status_code=400, detail=f"Uang muka + {d.tenor} × angsuran ({_rp(total)}) kurang dari harga "
                                f"barang ({_rp(d.jumlah)}). Periksa nominal angsuran.")
        if total > d.jumlah * 3:
            raise HTTPException(status_code=400, detail="Total angsuran lebih dari 3× harga barang — periksa lagi angkanya.")
        jadwal = [(i + 1, _tambah_bulan(d.jatuh_tempo, i), d.cicilan) for i in range(d.tenor)]
    conn = _billing_conn()
    try:
        k = conn.execute("SELECT nama, jenis, pengaruh_laba, khusus FROM kategori_kas WHERE id = ?", (d.kategori_id,)).fetchone()
        if not k or k["jenis"] != "keluar" or k["khusus"] or not k["pengaruh_laba"]:
            raise HTTPException(status_code=400, detail="Pilih kategori pengeluaran (beban) untuk pembelian kredit.")
        _wajib_buku_terbuka(conn, d.tanggal)
        oleh = _oleh(request)
        cur = conn.execute(
            "INSERT INTO utang (tanggal, pemasok, kategori_id, jumlah, jatuh_tempo, keterangan, dicatat_oleh, dicatat_pada, "
            "pokok, uang_muka, tenor, cicilan, qty, satuan) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d.tanggal, pemasok, d.kategori_id, total, (jadwal[-1][1] if jadwal else d.jatuh_tempo) or None,
             (d.keterangan or "").strip()[:500] or None, oleh, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             d.jumlah, d.uang_muka, d.tenor if jadwal else None, d.cicilan if jadwal else None, d.qty, d.satuan),
        )
        uid = cur.lastrowid
        conn.executemany("INSERT INTO utang_cicilan (utang_id, ke, jatuh_tempo, jumlah) VALUES (?,?,?,?)",
                         [(uid, ke, tgl, jml) for ke, tgl, jml in jadwal])
        if d.uang_muka:
            _catat_bayar_utang(conn, uid, BayarUtangIn(tanggal=d.tanggal, jumlah=d.uang_muka, metode=d.metode_uang_muka,
                                                       keterangan="Uang muka"), oleh)
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "utang_catat",
                    f"#U{uid} {pemasok} {_rp(d.jumlah)} · {k['nama']} · {d.tanggal}"
                    + (f" · {_teks_qty(d.qty, d.satuan)}" if d.qty else "")
                    + (f" · angsuran {d.tenor}× {_rp(d.cicilan)} mulai {d.jatuh_tempo} (total {_rp(total)}, "
                       f"bunga/biaya {_rp(total - d.jumlah)})" if jadwal else
                       (f" · jatuh tempo {d.jatuh_tempo}" if d.jatuh_tempo else ""))
                    + (f" · uang muka {_rp(d.uang_muka)}" if d.uang_muka else ""))
    return {"id": uid, "total": total}


@app.post("/api/keuangan/utang/{uid}/bayar")
def keuangan_bayar_utang(uid: int, d: BayarUtangIn, request: Request):
    _wajib_admin(request)
    _cek_tanggal_lalu(d.tanggal)
    if d.metode not in _METODE:
        raise HTTPException(status_code=400, detail="Metode tidak valid.")
    conn = _billing_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")  # kunci dulu: dua bayar bersamaan tidak bisa melebihi sisa
        u = next((x for x in _daftar_utang(conn) if x["id"] == uid), None)
        if not u or u["status"] != "aktif":
            raise HTTPException(status_code=404, detail="Liabilitas tidak ditemukan atau sudah dibatalkan.")
        if u["sisa"] <= 0:
            raise HTTPException(status_code=400, detail="Liabilitas ini sudah lunas.")
        if not 0 < d.jumlah <= u["sisa"]:
            raise HTTPException(status_code=400, detail=f"Jumlah bayar harus 1 sampai {_rp(u['sisa'])} (sisa liabilitas).")
        if d.tanggal < u["tanggal"]:
            raise HTTPException(status_code=400, detail="Tanggal bayar tidak boleh sebelum tanggal pembelian.")
        _wajib_buku_terbuka(conn, d.tanggal)
        tid = _catat_bayar_utang(conn, uid, d, _oleh(request))
        conn.commit()
    finally:
        conn.close()
    sisa = u["sisa"] - d.jumlah
    catat_aktivitas(_oleh(request), "utang_bayar",
                    f"#U{uid} {u['pemasok']}: bayar {_rp(d.jumlah)} ({d.metode}), sisa {_rp(sisa)}" + (" — LUNAS" if not sisa else ""))
    return {"ok": True, "transaksi_id": tid, "sisa": sisa}


@app.post("/api/keuangan/utang/{uid}/batal")
def keuangan_batal_utang(uid: int, d: BatalIn, request: Request):
    _wajib_admin(request)
    alasan = d.alasan.strip()
    if len(alasan) < 3:
        raise HTTPException(status_code=400, detail="Tuliskan alasan pembatalan.")
    conn = _billing_conn()
    try:
        u = conn.execute("SELECT * FROM utang WHERE id = ?", (uid,)).fetchone()
        if not u or u["status"] != "aktif":
            raise HTTPException(status_code=404, detail="Liabilitas tidak ditemukan atau sudah dibatalkan.")
        _boleh_ubah_transaksi(request, u)
        # Pembayarannya (uang muka/cicilan) ikut dihapus dalam satu langkah. Admin hanya
        # boleh bila semua pembayaran itu juga catatannya sendiri yang masih <= 24 jam.
        bayar = conn.execute("SELECT * FROM transaksi WHERE utang_id = ? AND status='aktif'", (uid,)).fetchall()
        for t in bayar:
            _boleh_ubah_transaksi(request, t)
        _wajib_buku_terbuka(conn, u["tanggal"], *[t["tanggal"] for t in bayar])
        waktu = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE utang SET status='batal', batal_oleh=?, batal_pada=?, batal_alasan=? WHERE id=?",
                     (_oleh(request), waktu, alasan[:300], uid))
        conn.execute("UPDATE transaksi SET status='batal', batal_oleh=?, batal_pada=?, batal_alasan=? "
                     "WHERE utang_id = ? AND status='aktif'", (_oleh(request), waktu, f"liabilitas #U{uid} dihapus: {alasan[:250]}", uid))
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "utang_batal",
                    f"#U{uid} {u['pemasok']} {_rp(u['jumlah'])} dihapus: {alasan[:120]}"
                    + (f" (beserta {len(bayar)} pembayaran {_rp(sum(t['jumlah'] for t in bayar))})" if bayar else ""))
    return {"ok": True, "pembayaran_ikut_dihapus": len(bayar)}


def _wajib_owner(request: Request, pesan: str = "Hapus permanen hanya bisa dilakukan owner.") -> None:
    if request.state.peran != "owner":
        raise HTTPException(status_code=403, detail=pesan)


@app.delete("/api/keuangan/transaksi/{tid}")
def keuangan_hapus_permanen(tid: int, request: Request):
    """Owner: hapus permanen transaksi yang SUDAH dihapus (dibatalkan), mis. data uji coba.
    Rinciannya tetap tersimpan di log aktivitas."""
    _wajib_owner(request)
    conn = _billing_conn()
    try:
        t = conn.execute("SELECT t.*, k.nama AS kategori FROM transaksi t JOIN kategori_kas k ON k.id = t.kategori_id "
                         "WHERE t.id = ?", (tid,)).fetchone()
        if not t:
            raise HTTPException(status_code=404, detail="Transaksi tidak ditemukan.")
        if t["status"] != "batal":
            raise HTTPException(status_code=400, detail="Hapus (batalkan) dulu transaksinya, baru bisa dihapus permanen.")
        _wajib_buku_terbuka(conn, t["tanggal"])
        conn.execute("UPDATE nota SET dihapus_oleh=?, dihapus_pada=? WHERE pemilik='transaksi' AND pemilik_id=? "
                     "AND dihapus_pada IS NULL", (_oleh(request), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tid))
        conn.execute("DELETE FROM transaksi WHERE id = ?", (tid,))
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "kas_hapus_permanen",
                    f"#{tid} {t['tanggal']} {t['jenis']} {_rp(t['jumlah'])} · {t['kategori']} · {t['keterangan'] or '-'} "
                    f"(dicatat {t['dicatat_oleh']}, alasan hapus: {t['batal_alasan'] or '-'})"
                    # Salinan lengkap baris yang dihapus tetap tersimpan sebagai jejak audit.
                    + " | salinan data: " + json.dumps(dict(t), ensure_ascii=False, default=str))
    return {"ok": True}


class KosongkanIn(BaseModel):
    password: str
    konfirmasi: str
    utang: bool = True          # ikut hapus utang/kredit beserta jadwal cicilannya
    tutup_buku: bool = True     # buka kembali semua bulan yang sudah ditutup


_KONFIRMASI_KOSONGKAN = "HAPUS DATA KEUANGAN"


@app.post("/api/keuangan/kosongkan")
def keuangan_kosongkan(d: KosongkanIn, request: Request):
    """Owner: kosongkan seluruh catatan kas (dan utang) untuk dicatat ulang dari awal.
    Database disalin utuh lebih dulu ke folder backup sehingga bisa dikembalikan.
    Pembayaran tagihan pelanggan (Billing) dan kategori tidak disentuh."""
    _wajib_owner(request)
    if d.konfirmasi.strip().upper() != _KONFIRMASI_KOSONGKAN:
        raise HTTPException(status_code=400, detail=f'Ketik "{_KONFIRMASI_KOSONGKAN}" untuk konfirmasi.')
    conn = _billing_conn()
    try:
        r = conn.execute("SELECT password_hash FROM akun WHERE username = ?", (request.state.user,)).fetchone()
        if not r or not _cek_password(d.password, r[0]):
            raise HTTPException(status_code=400, detail="Password salah.")
    finally:
        conn.close()
    waktu = datetime.now()
    tujuan = os.path.join(_folder_backup(), f"billing-sebelum-kosongkan-{waktu.strftime('%Y%m%d-%H%M%S')}.db")
    sumber, salinan = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15), sqlite3.connect(tujuan)
    try:
        sumber.backup(salinan)
        salinan.execute("PRAGMA journal_mode=DELETE")
        if salinan.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise HTTPException(status_code=500, detail="Cadangan gagal diperiksa; data tidak dihapus.")
    finally:
        salinan.close()
        sumber.close()
    conn = _billing_conn()
    try:
        n = {"transaksi": conn.execute("SELECT COUNT(*) FROM transaksi").fetchone()[0],
             "utang": conn.execute("SELECT COUNT(*) FROM utang").fetchone()[0] if d.utang else 0,
             "tutup_buku": conn.execute("SELECT COUNT(*) FROM tutup_buku").fetchone()[0] if d.tutup_buku else 0}
        oleh = _oleh(request)
        stempel = waktu.strftime("%Y-%m-%d %H:%M:%S")
        if d.utang:
            conn.execute("UPDATE nota SET dihapus_oleh=?, dihapus_pada=? WHERE dihapus_pada IS NULL", (oleh, stempel))
            conn.execute("DELETE FROM transaksi")
            conn.execute("DELETE FROM utang_cicilan")
            conn.execute("DELETE FROM utang")
        else:
            # Pembayaran utang tetap disimpan agar sisa utang tidak berubah.
            conn.execute("UPDATE nota SET dihapus_oleh=?, dihapus_pada=? WHERE dihapus_pada IS NULL AND pemilik='transaksi' "
                         "AND pemilik_id IN (SELECT id FROM transaksi WHERE utang_id IS NULL)", (oleh, stempel))
            n["transaksi"] = conn.execute("SELECT COUNT(*) FROM transaksi WHERE utang_id IS NULL").fetchone()[0]
            conn.execute("DELETE FROM transaksi WHERE utang_id IS NULL")
        if d.tutup_buku:
            conn.execute("DELETE FROM tutup_buku")
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "kas_kosongkan",
                    f"{n['transaksi']} transaksi, {n['utang']} liabilitas, {n['tutup_buku']} tutup buku dihapus · "
                    f"cadangan: {os.path.basename(tujuan)}")
    return {"ok": True, "dihapus": n, "cadangan": os.path.basename(tujuan)}


@app.delete("/api/keuangan/utang/{uid}")
def keuangan_hapus_utang_permanen(uid: int, request: Request):
    _wajib_owner(request)
    conn = _billing_conn()
    try:
        u = conn.execute("SELECT * FROM utang WHERE id = ?", (uid,)).fetchone()
        if not u:
            raise HTTPException(status_code=404, detail="Liabilitas tidak ditemukan.")
        if u["status"] != "batal":
            raise HTTPException(status_code=400, detail="Hapus (batalkan) dulu utangnya, baru bisa dihapus permanen.")
        waktu = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        bayar = [dict(r) for r in conn.execute("SELECT * FROM transaksi WHERE utang_id = ?", (uid,))]
        ids = [b["id"] for b in bayar]
        _wajib_buku_terbuka(conn, u["tanggal"], *[b["tanggal"] for b in bayar])
        conn.execute("UPDATE nota SET dihapus_oleh=?, dihapus_pada=? WHERE dihapus_pada IS NULL AND "
                     "((pemilik='utang' AND pemilik_id=?) OR (pemilik='transaksi' AND pemilik_id IN "
                     "(SELECT id FROM transaksi WHERE utang_id=?)))", (_oleh(request), waktu, uid, uid))
        conn.execute("DELETE FROM transaksi WHERE utang_id = ?", (uid,))
        conn.execute("DELETE FROM utang_cicilan WHERE utang_id = ?", (uid,))
        conn.execute("DELETE FROM utang WHERE id = ?", (uid,))
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "kas_hapus_permanen",
                    f"liabilitas #U{uid} {u['tanggal']} {u['pemasok']} {_rp(u['jumlah'])} (dicatat {u['dicatat_oleh']}, "
                    f"alasan hapus: {u['batal_alasan'] or '-'})" + (f", {len(ids)} pembayaran ikut terhapus" if ids else "")
                    + " | salinan data: " + json.dumps({"utang": dict(u), "pembayaran": bayar}, ensure_ascii=False, default=str))
    return {"ok": True}


_pengingat_utang_terakhir: Optional[str] = None


def proses_pengingat_utang(sekarang=None) -> None:
    """Sekali sehari (setelah TAGIHAN_JAM): kabari utang yang jatuh tempo dalam 3 hari atau sudah lewat."""
    global _pengingat_utang_terakhir
    sekarang = sekarang or datetime.now()
    hari = sekarang.strftime("%Y-%m-%d")
    if sekarang.hour < CONFIG["TAGIHAN_JAM"] or _pengingat_utang_terakhir == hari:
        return
    conn = _billing_conn()
    try:
        if conn.execute("SELECT 1 FROM otomasi_log WHERE tanggal=? AND jenis='utang'", (hari,)).fetchone():
            _pengingat_utang_terakhir = hari
            return
        batas = (sekarang + timedelta(days=3)).strftime("%Y-%m-%d")
        dekat = [u for u in _daftar_utang(conn, hanya_belum=True) if u["jatuh_tempo"] and u["jatuh_tempo"] <= batas]
        if dekat:
            baris = []
            for u in sorted(dekat, key=lambda x: x["jatuh_tempo"]):
                selisih = (datetime.strptime(u["jatuh_tempo"], "%Y-%m-%d").date() - sekarang.date()).days
                ket = f"lewat {-selisih} hari" if selisih < 0 else ("hari ini" if selisih == 0 else f"{selisih} hari lagi")
                apa = (f"angsuran ke-{u['cicilan_ke']}/{u['tenor']} {_rp(u['tagihan_berikut'])}" if u.get("cicilan_ke")
                       else f"sisa {_rp(u['sisa'])}")
                baris.append(f"• {_esc(u['pemasok'])}: {apa}, jatuh tempo {u['jatuh_tempo'][8:10]}/{u['jatuh_tempo'][5:7]} ({ket})")
            send_telegram_message(f"💳 <b>Liabilitas jatuh tempo</b> ({len(dekat)})\\n" + "\\n".join(baris[:15])
                                  + "\\nBayar lewat dashboard → Keuangan → Liabilitas.")
        conn.execute("INSERT OR IGNORE INTO otomasi_log VALUES (?, 'utang')", (hari,))
        conn.commit()
        _pengingat_utang_terakhir = hari
    finally:
        conn.close()


def _insentif_dibayar(conn, periode: str) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(t.jumlah),0) FROM transaksi t JOIN kategori_kas k ON k.id = t.kategori_id "
        "WHERE t.status='aktif' AND k.nama = ? AND COALESCE(t.insentif_periode, substr(t.tanggal,1,7)) = ?",
        (_KAT_INSENTIF, periode),
    ).fetchone()[0]


def _periode_insentif_awal(conn) -> Optional[str]:
    """Bulan pertama ada data: tagihan pertama atau transaksi keuangan pertama (bulan-bulan sebelum
    Penagihan dipakai tetap tampil supaya komisinya bisa dikunci manual & dibayar)."""
    calon = [conn.execute("SELECT MIN(periode) FROM tagihan").fetchone()[0]]
    try:
        calon.append(conn.execute("SELECT MIN(substr(tanggal,1,7)) FROM transaksi WHERE status='aktif'").fetchone()[0])
    except sqlite3.OperationalError:
        pass
    calon = [c for c in calon if c]
    return min(calon) if calon else None


def _ada_tagihan(conn, periode: str) -> bool:
    return conn.execute("SELECT 1 FROM tagihan WHERE periode = ? LIMIT 1", (periode,)).fetchone() is not None


def _rekap_insentif(conn, sampai: str, maks_bulan: int = 24) -> list[dict]:
    """Rekap per bulan dari bulan pertama ada data (maks. 24 bulan terakhir) s/d `sampai`, ditambah
    periode tagihan yang sudah dibuat lebih dulu (mis. tagihan bulan depan)."""
    awal = _periode_insentif_awal(conn)
    if not awal:
        return []
    akhir = max(sampai, conn.execute("SELECT MAX(periode) FROM tagihan").fetchone()[0] or sampai)
    awal = max(awal, _geser_periode(akhir, -(maks_bulan - 1)), CONFIG.get("KOMISI_MULAI") or "")
    hasil, p = [], awal
    while p <= akhir:
        ins = _hitung_insentif(conn, p)
        dibayar = _insentif_dibayar(conn, p)
        sisa = ins["total"] - dibayar
        hasil.append({"periode": p, "jumlah_pelanggan": ins["jumlah"], "jumlah_lunas": ins["jumlah_lunas"],
                      "jumlah_gratis": ins["jumlah_gratis"], "per_pelanggan": ins["per_pelanggan"],
                      "terhitung": ins["total"], "dibayar": dibayar, "sisa": max(0, sisa),
                      "lebih": max(0, -sisa), "terkunci": ins.get("terkunci"), "ada_tagihan": _ada_tagihan(conn, p),
                      "status": "lunas" if ins["total"] and sisa <= 0 else ("sebagian" if dibayar else
                                                                            ("belum" if ins["total"] else "nihil"))})
        p = _geser_periode(p, 1)
    return hasil


def _insentif_tertunggak(conn, sampai: str) -> dict:
    bulan = [b for b in _rekap_insentif(conn, sampai) if b["sisa"] > 0]
    return {"total": sum(b["sisa"] for b in bulan), "bulan": [{"periode": b["periode"], "sisa": b["sisa"]} for b in bulan]}


def _pembayaran_insentif(conn, periode_list: list[str]) -> dict[str, list[dict]]:
    if not periode_list:
        return {}
    tanda = ",".join("?" * len(periode_list))
    hasil: dict[str, list[dict]] = {}
    for r in conn.execute(
        f"SELECT t.id, t.tanggal, t.jumlah, t.metode, t.keterangan, t.dicatat_oleh, t.dicatat_pada, t.status, "
        f"t.batal_alasan, COALESCE(t.insentif_periode, substr(t.tanggal,1,7)) AS periode "
        f"FROM transaksi t JOIN kategori_kas k ON k.id = t.kategori_id WHERE k.nama = ? "
        f"AND COALESCE(t.insentif_periode, substr(t.tanggal,1,7)) IN ({tanda}) ORDER BY t.tanggal, t.id",
        [_KAT_INSENTIF, *periode_list],
    ):
        hasil.setdefault(r["periode"], []).append(dict(r))
    return hasil


class KunciKomisiIn(BaseModel):
    total: Optional[int] = None        # kosong = pakai angka terhitung sekarang
    catatan: Optional[str] = None


@app.post("/api/keuangan/insentif/{periode}/kunci")
def keuangan_kunci_komisi(periode: str, d: KunciKomisiIn, request: Request):
    _wajib_owner(request, "Hanya owner yang bisa mengunci komisi mitra.")
    if not _periode_valid(periode) or periode > datetime.now().strftime("%Y-%m"):
        raise HTTPException(status_code=400, detail="Periode tidak valid.")
    if d.total is not None and not 0 <= d.total <= 100_000_000:
        raise HTTPException(status_code=400, detail="Jumlah komisi tidak valid.")
    conn = _billing_conn()
    try:
        _siapkan_kunci_komisi(conn)
        ins = _kunci_komisi(conn, periode, "manual", _oleh(request), d.total, (d.catatan or "").strip()[:200] or None)
        conn.commit()
    finally:
        conn.close()
    total = ins["total"] if d.total is None else d.total
    catat_aktivitas(_oleh(request), "komisi_kunci", f"Komisi {_nama_periode(periode)} dikunci {_rp(total)}"
                    + (f" (terhitung {_rp(ins['total'])})" if total != ins["total"] else "")
                    + (f" · {d.catatan.strip()[:100]}" if d.catatan and d.catatan.strip() else ""))
    return {"ok": True, "total": total, "terhitung": ins["total"]}


@app.post("/api/keuangan/insentif/{periode}/mulai")
def keuangan_komisi_mulai(periode: str, request: Request):
    """Owner: komisi mitra dihitung mulai bulan ini (bulan-bulan sebelumnya disembunyikan)."""
    _wajib_owner(request, "Hanya owner yang bisa mengatur awal perhitungan komisi.")
    if not _periode_valid(periode):
        raise HTTPException(status_code=400, detail="Periode tidak valid.")
    conn = _billing_conn()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS pengaturan (kunci TEXT PRIMARY KEY, nilai TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO pengaturan (kunci, nilai) VALUES ('KOMISI_MULAI', ?)", (periode,))
        conn.commit()
    finally:
        conn.close()
    lama = CONFIG.get("KOMISI_MULAI") or "-"
    CONFIG["KOMISI_MULAI"] = periode
    catat_aktivitas(_oleh(request), "pengaturan_ubah", f"Komisi mitra dihitung mulai: {lama} → {periode}")
    return {"ok": True, "mulai": periode}


@app.delete("/api/keuangan/insentif/{periode}/kunci")
def keuangan_buka_kunci_komisi(periode: str, request: Request):
    _wajib_owner(request, "Hanya owner yang bisa membuka kunci komisi mitra.")
    conn = _billing_conn()
    try:
        _siapkan_kunci_komisi(conn)
        n = conn.execute("DELETE FROM komisi_kunci WHERE periode = ?", (periode,)).rowcount
        conn.commit()
    finally:
        conn.close()
    if n:
        catat_aktivitas(_oleh(request), "komisi_kunci", f"Kunci komisi {_nama_periode(periode)} dibuka")
    return {"ok": True}


@app.get("/api/keuangan/insentif")
def keuangan_insentif(request: Request, sampai: str = ""):
    _wajib_admin(request)
    sampai = sampai if _periode_valid(sampai) else datetime.now().strftime("%Y-%m")
    conn = _billing_conn()
    try:
        rekap = _rekap_insentif(conn, sampai)
        bayar = _pembayaran_insentif(conn, [b["periode"] for b in rekap])
        nota = _daftar_nota(conn, "transaksi", [p["id"] for ps in bayar.values() for p in ps])
    finally:
        conn.close()
    for b in rekap:
        b["pembayaran"] = [dict(p, nota=nota.get(p["id"], [])) for p in bayar.get(b["periode"], [])]
    rekap.reverse()      # terbaru di atas
    return {"saya": request.state.user, "peran": request.state.peran,
            "ringkasan": {"tertunggak": sum(b["sisa"] for b in rekap),
                          "bulan_tertunggak": sum(1 for b in rekap if b["sisa"] > 0),
                          "dibayar_total": sum(b["dibayar"] for b in rekap)},
            "rekap": rekap}


class BayarInsentifIn(BaseModel):
    tanggal: str
    jumlah: int
    metode: str = "transfer"
    penerima: Optional[str] = None
    keterangan: Optional[str] = None


@app.post("/api/keuangan/insentif/{periode}/bayar")
def keuangan_bayar_insentif(periode: str, d: BayarInsentifIn, request: Request):
    _wajib_admin(request)
    if not _periode_valid(periode):
        raise HTTPException(status_code=400, detail="Format periode harus YYYY-MM.")
    _cek_tanggal_lalu(d.tanggal)
    if d.metode not in _METODE:
        raise HTTPException(status_code=400, detail="Metode tidak valid.")
    if d.tanggal[:7] < periode:
        raise HTTPException(status_code=400, detail="Tanggal bayar tidak boleh sebelum bulan insentifnya.")
    conn = _billing_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")  # kunci dulu: dua bayar bersamaan tidak bisa melebihi sisa
        _wajib_buku_terbuka(conn, d.tanggal)
        ins = _hitung_insentif(conn, periode)
        sisa = ins["total"] - _insentif_dibayar(conn, periode)
        if sisa <= 0:
            raise HTTPException(status_code=400, detail=f"Komisi {_nama_periode(periode)} sudah lunas.")
        if not 0 < d.jumlah <= sisa:
            raise HTTPException(status_code=400, detail=f"Jumlah harus 1 sampai {_rp(sisa)} (sisa komisi).")
        kid = conn.execute("SELECT id FROM kategori_kas WHERE nama = ? AND jenis = 'keluar'", (_KAT_INSENTIF,)).fetchone()
        if not kid:
            kid = (conn.execute("INSERT INTO kategori_kas (nama, jenis, pengaruh_laba) VALUES (?, 'keluar', 1)",
                                (_KAT_INSENTIF,)).lastrowid,)
        penerima = (d.penerima or "").strip()[:60]
        ket = f"Komisi {_nama_periode(periode)}" + (f" untuk {penerima}" if penerima else "") \
            + (f" · {d.keterangan.strip()[:300]}" if d.keterangan and d.keterangan.strip() else "")
        cur = conn.execute(
            "INSERT INTO transaksi (tanggal, jenis, kategori_id, jumlah, metode, keterangan, dicatat_oleh, dicatat_pada, "
            "insentif_periode) VALUES (?, 'keluar', ?, ?, ?, ?, ?, ?, ?)",
            (d.tanggal, kid[0], d.jumlah, d.metode, ket, _oleh(request), datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             periode),
        )
        nomor = _beri_nomor(conn, cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "insentif_bayar",
                    f"{nomor} {_nama_periode(periode)}: bayar {_rp(d.jumlah)} ({d.metode})"
                    + (f" ke {penerima}" if penerima else "") + f", sisa {_rp(sisa - d.jumlah)}")
    return {"ok": True, "transaksi_id": cur.lastrowid, "sisa": sisa - d.jumlah}


class TransaksiIn(BaseModel):
    tanggal: str
    jenis: str
    kategori_id: int
    jumlah: int
    metode: str = "tunai"
    keterangan: Optional[str] = None
    qty: Optional[float] = None         # banyaknya barang, mis. 10 (pcs)
    satuan: Optional[str] = None


def _cek_qty(qty: Optional[float], satuan: Optional[str]) -> tuple[Optional[float], Optional[str]]:
    """Banyaknya barang opsional. Kalau diisi, satuan bawaan 'pcs'."""
    if qty is None or qty == 0:
        return None, None
    if not 0 < qty <= 10_000_000:
        raise HTTPException(status_code=400, detail="Jumlah barang (qty) harus lebih dari 0.")
    satuan = (satuan or "").strip()[:20] or "pcs"
    return (int(qty) if float(qty).is_integer() else round(qty, 3)), satuan


def _angka_qty(qty):
    """SQLite REAL mengembalikan 3.0 — tampilkan 3 bila bulat."""
    if qty is None:
        return None
    return int(qty) if float(qty).is_integer() else qty


def _teks_qty(qty, satuan) -> str:
    if not qty:
        return ""
    return f"{int(qty) if float(qty).is_integer() else qty:g} {satuan or 'pcs'}"


def _cek_transaksi(conn, d: TransaksiIn) -> str:
    if not _tanggal_valid(d.tanggal):
        raise HTTPException(status_code=400, detail="Tanggal tidak valid.")
    if d.tanggal > (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"):
        raise HTTPException(status_code=400, detail="Tanggal tidak boleh di masa depan.")
    if d.jenis not in ("masuk", "keluar"):
        raise HTTPException(status_code=400, detail="Jenis harus masuk atau keluar.")
    if not 0 < d.jumlah <= 10_000_000_000:
        raise HTTPException(status_code=400, detail="Jumlah harus lebih dari 0.")
    if d.metode not in _METODE:
        raise HTTPException(status_code=400, detail="Metode harus tunai, transfer, qris, atau lainnya.")
    d.qty, d.satuan = _cek_qty(d.qty, d.satuan)
    k = conn.execute("SELECT nama, jenis, aktif, khusus FROM kategori_kas WHERE id = ?", (d.kategori_id,)).fetchone()
    if not k or k["jenis"] != d.jenis:
        raise HTTPException(status_code=400, detail="Kategori tidak cocok dengan jenis transaksi.")
    if k["khusus"]:
        raise HTTPException(status_code=400, detail="Pembayaran liabilitas dicatat dari tab Liabilitas.")
    if k["nama"] == _KAT_INSENTIF:
        raise HTTPException(status_code=400, detail="Pembayaran komisi mitra dicatat dari tab Komisi (agar terikat ke bulannya).")
    return k["nama"]


@app.post("/api/keuangan/transaksi")
def keuangan_tambah(d: TransaksiIn, request: Request):
    _wajib_admin(request)
    conn = _billing_conn()
    try:
        kat = _cek_transaksi(conn, d)
        _wajib_buku_terbuka(conn, d.tanggal)
        # Kunci tulis dulu, lalu tolak kiriman kembar (tombol Simpan tertekan dua kali): transaksi
        # yang persis sama dari orang yang sama dalam 20 detik terakhir dianggap dobel.
        conn.execute("BEGIN IMMEDIATE")
        kembar = conn.execute(
            "SELECT id, nomor FROM transaksi WHERE status='aktif' AND dicatat_oleh=? AND tanggal=? AND jenis=? "
            "AND kategori_id=? AND jumlah=? AND COALESCE(keterangan,'')=? AND metode=? AND COALESCE(qty,-1)=? "
            "AND dicatat_pada >= ? ORDER BY id DESC LIMIT 1",
            (_oleh(request), d.tanggal, d.jenis, d.kategori_id, d.jumlah, (d.keterangan or "").strip()[:500], d.metode,
             d.qty if d.qty is not None else -1,
             (datetime.now() - timedelta(seconds=20)).strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
        if kembar:
            conn.rollback()
            raise HTTPException(status_code=409, detail=f"Transaksi yang sama baru saja tercatat ({kembar['nomor']}). "
                                "Bila memang ada dua transaksi kembar, tunggu 20 detik lalu simpan lagi.")
        cur = conn.execute(
            "INSERT INTO transaksi (tanggal, jenis, kategori_id, jumlah, metode, keterangan, dicatat_oleh, dicatat_pada, "
            "qty, satuan) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (d.tanggal, d.jenis, d.kategori_id, d.jumlah, d.metode, (d.keterangan or "").strip()[:500] or None,
             _oleh(request), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), d.qty, d.satuan),
        )
        nomor = _beri_nomor(conn, cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "kas_catat",
                    f"{nomor} {'masuk' if d.jenis == 'masuk' else 'keluar'} {_rp(d.jumlah)} · {kat} · {d.tanggal}"
                    + (f" · {_teks_qty(d.qty, d.satuan)}" if d.qty else "")
                    + (f" · {d.keterangan.strip()[:80]}" if d.keterangan and d.keterangan.strip() else ""))
    return {"id": cur.lastrowid, "nomor": nomor}


def _boleh_ubah_transaksi(request: Request, t) -> None:
    """Owner boleh semua. Admin hanya catatannya sendiri, maksimal 24 jam sejak dicatat."""
    if request.state.peran == "owner":
        return
    if t["dicatat_oleh"] != request.state.user:
        raise HTTPException(status_code=403, detail="Hanya pencatat atau owner yang bisa mengubah transaksi ini.")
    if datetime.now() - datetime.strptime(t["dicatat_pada"], "%Y-%m-%d %H:%M:%S") > timedelta(hours=24):
        raise HTTPException(status_code=403, detail="Lewat 24 jam sejak dicatat — minta owner untuk mengubahnya.")


@app.put("/api/keuangan/transaksi/{tid}")
def keuangan_ubah(tid: int, d: TransaksiIn, request: Request):
    _wajib_admin(request)
    conn = _billing_conn()
    try:
        t = conn.execute("SELECT t.*, k.nama AS kategori FROM transaksi t JOIN kategori_kas k ON k.id = t.kategori_id "
                         "WHERE t.id = ?", (tid,)).fetchone()
        if not t:
            raise HTTPException(status_code=404, detail="Transaksi tidak ditemukan.")
        if t["status"] != "aktif":
            raise HTTPException(status_code=400, detail="Transaksi yang sudah dibatalkan tidak bisa diubah.")
        if t["utang_id"]:
            raise HTTPException(status_code=400, detail="Pembayaran liabilitas tidak bisa diubah. Batalkan, lalu bayar ulang dari tab Liabilitas.")
        if t["insentif_periode"]:
            raise HTTPException(status_code=400, detail="Pembayaran komisi tidak bisa diubah. Batalkan, lalu bayar ulang dari tab Komisi.")
        _boleh_ubah_transaksi(request, t)
        kat = _cek_transaksi(conn, d)
        _wajib_buku_terbuka(conn, t["tanggal"], d.tanggal)
        conn.execute(
            "UPDATE transaksi SET tanggal=?, jenis=?, kategori_id=?, jumlah=?, metode=?, keterangan=?, "
            "qty=?, satuan=?, diubah_oleh=?, diubah_pada=? WHERE id=?",
            (d.tanggal, d.jenis, d.kategori_id, d.jumlah, d.metode, (d.keterangan or "").strip()[:500] or None,
             d.qty, d.satuan, _oleh(request), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tid),
        )
        nomor_baru = None
        if d.jenis != t["jenis"] or d.tanggal[:7] != t["tanggal"][:7]:
            # Bukti kas lama tidak berlaku lagi (jenis/bulan berubah): terbitkan nomor baru, nomor lama tercatat di log.
            conn.execute("UPDATE transaksi SET nomor=NULL WHERE id=?", (tid,))
            nomor_baru = _beri_nomor(conn, tid)
        conn.commit()
    finally:
        conn.close()
    ubah = [f"{n}: {a} → {b}" for n, a, b in (
        ("tanggal", t["tanggal"], d.tanggal), ("jumlah", _rp(t["jumlah"]), _rp(d.jumlah)),
        ("kategori", t["kategori"], kat), ("metode", t["metode"], d.metode),
        ("qty", _teks_qty(t["qty"], t["satuan"]) or "-", _teks_qty(d.qty, d.satuan) or "-"),
        ("keterangan", t["keterangan"] or "-", (d.keterangan or "").strip() or "-")) if a != b]
    if nomor_baru:
        ubah.append(f"nomor bukti: {t['nomor']} → {nomor_baru}")
    if ubah:
        catat_aktivitas(_oleh(request), "kas_ubah", f"{nomor_baru or t['nomor'] or '#' + str(tid)} " + "; ".join(ubah))
    return {"ok": True}


@app.post("/api/keuangan/transaksi/{tid}/batal")
def keuangan_batal(tid: int, d: BatalIn, request: Request):
    _wajib_admin(request)
    alasan = d.alasan.strip()
    if len(alasan) < 3:
        raise HTTPException(status_code=400, detail="Tuliskan alasan pembatalan.")
    conn = _billing_conn()
    try:
        t = conn.execute("SELECT * FROM transaksi WHERE id = ?", (tid,)).fetchone()
        if not t:
            raise HTTPException(status_code=404, detail="Transaksi tidak ditemukan.")
        if t["status"] != "aktif":
            raise HTTPException(status_code=400, detail="Transaksi sudah dibatalkan.")
        _boleh_ubah_transaksi(request, t)
        _wajib_buku_terbuka(conn, t["tanggal"])
        conn.execute("UPDATE transaksi SET status='batal', batal_oleh=?, batal_pada=?, batal_alasan=? WHERE id=?",
                     (_oleh(request), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), alasan[:300], tid))
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "kas_batal", f"#{tid} {_rp(t['jumlah'])} dibatalkan: {alasan[:120]}")
    return {"ok": True}


def _query_transaksi(conn, periode: str, jenis: str, kategori_id: Optional[int], cari: str, batal: bool):
    awal, akhir = _batas_periode(periode)
    sql = ("SELECT t.id, t.tanggal, t.jenis, t.kategori_id, k.nama AS kategori, k.pengaruh_laba, t.jumlah, t.metode, "
           "t.keterangan, t.dicatat_oleh, t.dicatat_pada, t.diubah_oleh, t.diubah_pada, t.status, "
           "t.batal_oleh, t.batal_pada, t.batal_alasan, t.utang_id, t.insentif_periode, t.qty, t.satuan, t.nomor, "
           "(SELECT u.pemasok FROM utang u WHERE u.id = t.utang_id) AS pemasok "
           "FROM transaksi t JOIN kategori_kas k ON k.id = t.kategori_id WHERE t.tanggal >= ? AND t.tanggal < ?")
    arg: list = [awal, akhir]
    if jenis in ("masuk", "keluar"):
        sql += " AND t.jenis = ?"
        arg.append(jenis)
    if kategori_id:
        sql += " AND t.kategori_id = ?"
        arg.append(kategori_id)
    if cari.strip():
        sql += " AND (t.keterangan LIKE ? OR k.nama LIKE ? OR t.dicatat_oleh LIKE ?)"
        arg += [f"%{cari.strip()}%"] * 3
    if not batal:
        sql += " AND t.status = 'aktif'"
    return conn.execute(sql + " ORDER BY t.tanggal DESC, t.id DESC", arg).fetchall()


@app.get("/api/keuangan/transaksi")
def keuangan_list(request: Request, periode: str = "semua", jenis: str = "", kategori_id: Optional[int] = None,
                  cari: str = "", batal: bool = False, urut: str = "tanggal", dari: str = "", sampai: str = "",
                  metode: str = ""):
    """Riwayat transaksi. dari/sampai (YYYY-MM-DD, inklusif) mempersempit periode; metode = tunai/
    transfer/qris/lainnya/kredit/langganan; urut = tanggal (terbaru dulu) | lama | dicatat."""
    _wajib_admin(request)
    if periode != "semua" and not _periode_valid(periode):
        raise HTTPException(status_code=400, detail="Format periode harus YYYY-MM atau 'semua'.")
    for t in (dari, sampai):
        if t and not _tanggal_valid(t):
            raise HTTPException(status_code=400, detail="Format tanggal harus YYYY-MM-DD.")
    if dari and sampai and dari > sampai:
        dari, sampai = sampai, dari
    awal, akhir = _batas_periode(periode)
    conn = _billing_conn()
    try:
        manual = jenis in ("", "masuk", "keluar")
        rows = _query_transaksi(conn, periode, jenis, kategori_id, cari, batal) if manual else []
        conn_batal = conn.execute("SELECT COUNT(*) FROM transaksi WHERE status='batal' AND tanggal >= ? AND tanggal < ?",
                                  (awal, akhir)).fetchone()[0]
        nota = _daftar_nota(conn, "transaksi", [r["id"] for r in rows])
        daftar = [dict(r, sumber="manual", nota=nota.get(r["id"], [])) for r in rows]
        # Supaya riwayat satu bulan lengkap: pembayaran langganan (dari Billing) dan
        # pembelian kredit (dari tab Utang) ikut ditampilkan, hanya-baca di sini.
        # batal=True dipakai tampilan "Yang dihapus", jadi baris otomatis tidak disertakan.
        q = f"%{cari.strip()}%"
        if jenis in ("", "masuk", "langganan") and not kategori_id and not batal:
            for r in conn.execute(
                    "SELECT t.id, t.tgl_bayar, t.periode, t.jumlah, t.username, t.metode_bayar, p.nama FROM tagihan t "
                    "LEFT JOIN pelanggan p ON p.username = t.username "
                    "WHERE t.status='lunas' AND t.tgl_bayar >= ? AND t.tgl_bayar < ? "
                    "AND (? = '%%' OR t.username LIKE ? OR p.nama LIKE ? OR 'pembayaran langganan' LIKE ?)",
                    (awal, akhir, q, q, q, q.lower())):
                daftar.append({"id": r["id"], "sumber": "langganan", "tanggal": r["tgl_bayar"][:10], "jenis": "masuk",
                               "kategori": "Pembayaran langganan", "pengaruh_laba": 1, "jumlah": r["jumlah"],
                               "metode": r["metode_bayar"] or "tunai", "status": "aktif", "username": r["username"],
                               "keterangan": f"{r['nama'] or r['username']} ({r['username']}) · tagihan {_nama_periode(r['periode'])}",
                               "dicatat_oleh": "Billing", "dicatat_pada": r["tgl_bayar"], "nota": []})
        if jenis in ("", "keluar", "kredit") and not batal:
            sql = ("SELECT u.id, u.tanggal, u.pemasok, u.jumlah, u.pokok, u.uang_muka, u.tenor, u.keterangan, u.qty, u.satuan, "
                   "u.dicatat_oleh, u.dicatat_pada, u.kategori_id, k.nama AS kategori, k.pengaruh_laba "
                   "FROM utang u JOIN kategori_kas k ON k.id = u.kategori_id "
                   "WHERE u.status='aktif' AND u.tanggal >= ? AND u.tanggal < ?")
            arg: list = [awal, akhir]
            if kategori_id:
                sql += " AND u.kategori_id = ?"
                arg.append(kategori_id)
            if cari.strip():
                sql += " AND (u.pemasok LIKE ? OR u.keterangan LIKE ? OR k.nama LIKE ? OR u.dicatat_oleh LIKE ?)"
                arg += [q] * 4
            kredit = conn.execute(sql, arg).fetchall()
            nota_u = _daftar_nota(conn, "utang", [r["id"] for r in kredit])
            for r in kredit:
                daftar.append({"id": r["id"], "sumber": "kredit", "tanggal": r["tanggal"], "jenis": "keluar",
                               "kategori": r["kategori"], "pengaruh_laba": r["pengaruh_laba"], "jumlah": r["jumlah"],
                               "pokok": r["pokok"], "tenor": r["tenor"], "uang_muka": r["uang_muka"],
                               "qty": r["qty"], "satuan": r["satuan"],
                               "metode": "kredit", "status": "aktif", "pemasok": r["pemasok"],
                               "keterangan": r["keterangan"], "dicatat_oleh": r["dicatat_oleh"],
                               "dicatat_pada": r["dicatat_pada"], "nota": nota_u.get(r["id"], [])})
    finally:
        conn.close()
    if dari:
        daftar = [t for t in daftar if t["tanggal"] >= dari]
    if sampai:
        daftar = [t for t in daftar if t["tanggal"] <= sampai]
    if metode:
        daftar = [t for t in daftar if (t.get("metode") or "") == metode]
    if urut == "dicatat":
        # Urut menurut waktu dicatat: yang baru diinput (berapa pun tanggal transaksinya) di atas.
        daftar.sort(key=lambda t: (t.get("dicatat_pada") or "", t["id"]), reverse=True)
    else:
        daftar.sort(key=lambda t: (t["tanggal"], t.get("dicatat_pada") or ""), reverse=(urut != "lama"))
    aktif = [t for t in daftar if t["status"] == "aktif"]
    tunai = [t for t in aktif if t["sumber"] != "kredit"]
    return {
        "periode": periode, "saya": request.state.user, "peran": request.state.peran,
        "ringkasan": {"masuk": sum(t["jumlah"] for t in tunai if t["jenis"] == "masuk"),
                      "keluar": sum(t["jumlah"] for t in tunai if t["jenis"] == "keluar"),
                      "langganan": sum(t["jumlah"] for t in aktif if t["sumber"] == "langganan"),
                      "n_langganan": sum(1 for t in aktif if t["sumber"] == "langganan"),
                      "kredit": sum(t["jumlah"] for t in aktif if t["sumber"] == "kredit"),
                      "n_kredit": sum(1 for t in aktif if t["sumber"] == "kredit"),
                      "jumlah": len(aktif), "dibatalkan": conn_batal},
        "transaksi": daftar,
    }


def _buat_csv_keuangan(periode: str) -> str:
    """CSV satu bulan: transaksi manual + pembayaran tagihan + pembelian kredit."""
    import csv
    import io
    awal, akhir = _batas_periode(periode)
    conn = _billing_conn()
    try:
        trx = _query_transaksi(conn, periode, "", None, "", True)
        bayar = conn.execute(
            "SELECT t.tgl_bayar, t.username, p.nama, t.periode, t.jumlah FROM tagihan t "
            "JOIN pelanggan p ON p.username = t.username WHERE t.status='lunas' AND t.tgl_bayar >= ? AND t.tgl_bayar < ? "
            "ORDER BY t.tgl_bayar", (awal, akhir)).fetchall()
        utang = conn.execute(
            "SELECT u.*, k.nama AS kategori FROM utang u JOIN kategori_kas k ON k.id = u.kategori_id "
            "WHERE u.tanggal >= ? AND u.tanggal < ? ORDER BY u.tanggal", (awal, akhir)).fetchall()
    finally:
        conn.close()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["No. bukti", "Tanggal", "Jenis", "Kategori", "Jumlah", "Qty", "Satuan", "Metode", "Keterangan", "Dicatat oleh", "Status", "Alasan batal"])
    def aman(v):
        # Cegah formula injection saat CSV dibuka di Excel (=, +, -, @ di awal sel)
        v = "" if v is None else str(v)
        return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
    for b in bayar:
        w.writerow(["", b["tgl_bayar"], "masuk", "Pembayaran langganan", b["jumlah"], "", "", "", aman(f"{b['nama']} ({b['username']}) periode {b['periode']}"), "billing", "aktif", ""])
    for t in trx:
        ket = t["keterangan"] or ""
        if t["utang_id"]:
            ket = f"Bayar liabilitas #U{t['utang_id']} {t['pemasok'] or ''}" + (f" · {ket}" if ket else "")
        w.writerow([t["nomor"] or "", t["tanggal"], t["jenis"], aman(t["kategori"]), t["jumlah"], _angka_qty(t["qty"]) or "", aman(t["satuan"] or ""),
                    t["metode"], aman(ket),
                    aman(t["dicatat_oleh"]), t["status"], aman(t["batal_alasan"])])
    for u in utang:
        w.writerow([f"#U{u['id']}", u["tanggal"], "kredit (liabilitas)", aman(u["kategori"]), u["jumlah"], _angka_qty(u["qty"]) or "", aman(u["satuan"] or ""), "kredit",
                    aman(f"#U{u['id']} {u['pemasok']}"
                         + (f" · angsuran {u['tenor']}x {u['cicilan']}, harga barang {u['pokok']}" if u["tenor"] else "")
                         + (f" · jatuh tempo {u['jatuh_tempo']}" if u["jatuh_tempo"] else "")
                         + (f" · {u['keterangan']}" if u["keterangan"] else "")),
                    aman(u["dicatat_oleh"]), u["status"], aman(u["batal_alasan"])])
    # BOM supaya Excel membaca UTF-8 dengan benar
    return "\ufeff" + buf.getvalue()


@app.get("/api/keuangan/ekspor")
def keuangan_ekspor(periode: str, request: Request):
    _wajib_admin(request)
    if periode != "semua" and not _periode_valid(periode):
        raise HTTPException(status_code=400, detail="Format periode harus YYYY-MM atau 'semua'.")
    isi = _buat_csv_keuangan(periode)
    catat_aktivitas(_oleh(request), "kas_ekspor", f"periode {periode}")
    return Response(isi, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="keuangan-{periode}.csv"'})


# --- Ekspor CSV pelanggan, tagihan, tunggakan (dibuka di Excel / Google Sheets) ---
def _csv_respon(nama_file: str, header: list, baris: list) -> Response:
    def aman(v):
        # Cegah formula injection saat dibuka di Excel (=, +, -, @ di awal sel); angka dibiarkan apa adanya
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
        v = "" if v is None else str(v)
        return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(header)
    for b in baris:
        w.writerow([aman(x) for x in b])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{nama_file}"'})


@app.get("/api/ekspor/pelanggan")
def ekspor_pelanggan(request: Request):
    _wajib_admin(request)
    _siapkan_tabel_isolir()
    conn = _billing_conn()
    try:
        rows = conn.execute(
            "SELECT p.username, p.nama, p.alamat, p.no_hp, k.nama AS paket, k.harga, p.status, p.gratis, p.berhenti_sejak, "
            "(i.username IS NOT NULL) AS isolir, "
            "(SELECT COUNT(*) FROM tagihan t WHERE t.username = p.username AND t.status != 'lunas') AS bulan_tunggak, "
            "(SELECT COALESCE(SUM(jumlah),0) FROM tagihan t WHERE t.username = p.username AND t.status != 'lunas') AS total_tunggak "
            "FROM pelanggan p LEFT JOIN paket k ON k.id = p.paket_id LEFT JOIN isolir i ON i.username = p.username "
            "ORDER BY p.nama COLLATE NOCASE").fetchall()
    finally:
        conn.close()
    onu = {o.pppoe_username: o for o in (_latest_data.onu_list if _latest_data else []) if o.pppoe_username}
    online = {u.username for u in (_latest_data.pppoe_users if _latest_data else [])}
    baris = []
    for r in rows:
        o = onu.get(r["username"])
        baris.append([r["username"], r["nama"], r["alamat"], r["no_hp"], r["paket"], r["harga"], r["status"],
                      "ya" if r["gratis"] else "tidak", r["berhenti_sejak"], "ya" if r["isolir"] else "tidak",
                      "online" if r["username"] in online else "offline",
                      f"{o.pon} onu {o.onu_id}" if o else "", (o.serial_number if o else "") or "", (o.model or "").strip('"') if o else "",
                      o.onu_rx_dbm if o else "", r["bulan_tunggak"], r["total_tunggak"]])
    catat_aktivitas(_oleh(request), "ekspor", f"data pelanggan ({len(baris)} baris)")
    return _csv_respon(f"pelanggan-{datetime.now().strftime('%Y-%m-%d')}.csv",
                       ["Username", "Nama", "Alamat", "No. HP", "Paket", "Harga paket", "Status", "Gratis", "Berhenti sejak",
                        "Isolir", "PPPoE", "ONU (PON)", "SN ONU", "Model ONU", "Redaman ONU (dBm)",
                        "Bulan belum lunas", "Total belum lunas"], baris)


@app.get("/api/ekspor/tagihan")
def ekspor_tagihan(periode: str, request: Request):
    _wajib_admin(request)
    if not _periode_valid(periode):
        raise HTTPException(status_code=400, detail="Format periode harus YYYY-MM.")
    conn = _billing_conn()
    try:
        rows = conn.execute(
            "SELECT t.username, p.nama, p.no_hp, t.periode, t.jumlah, t.jatuh_tempo, t.status, t.tgl_bayar, t.metode_bayar, "
            "t.lunas_oleh FROM tagihan t JOIN pelanggan p ON p.username = t.username WHERE t.periode = ? "
            "ORDER BY t.status DESC, p.nama COLLATE NOCASE", (periode,)).fetchall()
    finally:
        conn.close()
    baris = [[r["username"], r["nama"], r["no_hp"], r["periode"], r["jumlah"], r["jatuh_tempo"],
              "lunas" if r["status"] == "lunas" else "belum", r["tgl_bayar"], r["metode_bayar"], r["lunas_oleh"]] for r in rows]
    catat_aktivitas(_oleh(request), "ekspor", f"tagihan {periode} ({len(baris)} baris)")
    return _csv_respon(f"tagihan-{periode}.csv", ["Username", "Nama", "No. HP", "Periode", "Jumlah", "Jatuh tempo", "Status",
                                                  "Tanggal bayar", "Metode", "Dicatat oleh"], baris)


@app.get("/api/ekspor/tunggakan")
def ekspor_tunggakan(request: Request):
    _wajib_admin(request)
    d = list_tunggakan()
    label = {"belum_jatuh_tempo": "Belum jatuh tempo", "1_30": "Telat 1-30 hari", "31_60": "Telat 31-60 hari",
             "60_lebih": "Telat > 60 hari"}
    baris = [[g["username"], g["nama"], g["no_hp"], g["paket"], g["bulan"], ", ".join(t["periode"] for t in g["tagihan"]),
              g["total"], max(g["hari_telat"], 0), label[g["kelompok"]], "ya" if g["isolir"] else "tidak",
              (g["diingatkan"] or "")[:10]] for g in d["pelanggan"]]
    catat_aktivitas(_oleh(request), "ekspor", f"tunggakan ({len(baris)} pelanggan)")
    return _csv_respon(f"tunggakan-{d['per']}.csv", ["Username", "Nama", "No. HP", "Paket", "Jumlah bulan", "Bulan",
                                                     "Total", "Hari telat (tertua)", "Kelompok umur", "Isolir",
                                                     "Terakhir diingatkan"], baris)


# ============================================================
# 6g. LAPORAN KEUANGAN BULANAN KE TELEGRAM OWNER
# ============================================================

def _chat_owner() -> list[str]:
    return [x.strip() for x in str(CONFIG["TELEGRAM_OWNER_CHAT_ID"] or "").split(",") if x.strip()]


def _persen_ubah(ini: int, lalu: int) -> str:
    if not lalu:
        return ""
    p = round((ini - lalu) * 100 / abs(lalu))
    return " (sama)" if p == 0 else f" ({'▲' if p > 0 else '▼'}{abs(p)}%)"


def susun_laporan_bulanan(periode: str) -> str:
    """Teks laporan (HTML Telegram, < 4096 karakter) untuk satu periode YYYY-MM."""
    awal, akhir = _batas_periode(periode)
    conn = _billing_conn()
    try:
        r = _ringkas_periode(conn, periode)
        lalu = _ringkas_periode(conn, _geser_periode(periode, -1))
        saldo_awal, saldo_akhir = _saldo_kas_sampai(conn, awal), _saldo_kas_sampai(conn, akhir)
        tag = conn.execute("SELECT COALESCE(SUM(jumlah),0), COUNT(*), "
                           "COALESCE(SUM(CASE WHEN status='lunas' THEN jumlah END),0), "
                           "COALESCE(SUM(CASE WHEN status='lunas' THEN 1 END),0) FROM tagihan WHERE periode=?",
                           (periode,)).fetchone()
        piutang = conn.execute(
            "SELECT COALESCE(SUM(jumlah),0), COUNT(*) FROM tagihan WHERE status='belum' AND periode <= ?", (periode,)
        ).fetchone()
        penunggak = conn.execute(
            "SELECT p.nama, p.username, SUM(t.jumlah) AS total, COUNT(*) AS n FROM tagihan t "
            "JOIN pelanggan p ON p.username = t.username WHERE t.status='belum' AND t.periode <= ? "
            "GROUP BY t.username ORDER BY total DESC, n DESC LIMIT 5", (periode,)
        ).fetchall()
        utang = _ringkas_utang(conn)
        insentif = _hitung_insentif(conn, periode)
        ins_bayar = _insentif_dibayar(conn, periode)
        ins_tunggak = _insentif_tertunggak(conn, periode)
        pel = conn.execute(
            "SELECT SUM(status='aktif' AND gratis=0), SUM(status='aktif' AND gratis=1), "
            "SUM(status='berhenti' AND berhenti_sejak >= ? AND berhenti_sejak < ?) FROM pelanggan", (awal, akhir)
        ).fetchone()
        n_isolir = conn.execute("SELECT COUNT(*) FROM isolir").fetchone()[0]
        n_trx = conn.execute("SELECT COUNT(*), COUNT(DISTINCT dicatat_oleh) FROM transaksi "
                             "WHERE status='aktif' AND tanggal >= ? AND tanggal < ?", (awal, akhir)).fetchone()
        n_batal = conn.execute("SELECT COUNT(*) FROM transaksi WHERE status='batal' AND tanggal >= ? AND tanggal < ?",
                               (awal, akhir)).fetchone()[0]
        tn = _tanpa_nota(conn, awal, akhir)
    finally:
        conn.close()
    try:
        hc = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
        try:
            ev = dict(hc.execute("SELECT tingkat, COUNT(*) FROM event WHERE waktu >= ? AND waktu < ? GROUP BY tingkat",
                                 (awal, akhir)).fetchall())
            ev_top = hc.execute("SELECT judul, COUNT(*) AS n FROM event WHERE tingkat='kritis' AND waktu >= ? AND waktu < ? "
                                "GROUP BY judul ORDER BY n DESC LIMIT 3", (awal, akhir)).fetchall()
        finally:
            hc.close()
    except Exception:
        ev, ev_top = {}, []

    nama_bln = _nama_periode(periode)
    L = [f"📊 <b>Laporan Keuangan {nama_bln}</b>", ""]
    L.append("<b>Laba rugi</b>")
    L.append(f"Pendapatan: <b>{_rp(r['pendapatan'])}</b>{_persen_ubah(r['pendapatan'], lalu['pendapatan'])}")
    L.append(f"  • Langganan ({r['jumlah_pembayaran']}x bayar): {_rp(r['pendapatan_langganan'])}")
    if r["pendapatan_lain"]:
        L.append(f"  • Penerimaan lain: {_rp(r['pendapatan_lain'])}")
    L.append(f"Pengeluaran: <b>{_rp(r['beban'])}</b>{_persen_ubah(r['beban'], lalu['beban'])}"
             + (f" (termasuk kredit {_rp(r['beban_kredit'])})" if r["beban_kredit"] else ""))
    top = [k for k in r["per_kategori"] if k["jenis"] == "keluar" and k["pengaruh_laba"] and not k.get("aset")][:3]
    for k in top:
        L.append(f"  • {_esc(k['nama'])}: {_rp(k['total'])}")
    ikon = "🟢" if r["laba"] >= 0 else "🔴"
    L.append(f"{ikon} Laba bersih: <b>{_rp(r['laba'])}</b>{_persen_ubah(r['laba'], lalu['laba'])}")
    if r["pendapatan"]:
        L.append(f"Margin: {round(r['laba'] * 100 / r['pendapatan'])}%")
    L += ["", "<b>Kas</b>", f"Saldo awal {_rp(saldo_awal)} → akhir <b>{_rp(saldo_akhir)}</b>"]
    lain = []
    if r["modal_masuk"]:
        lain.append(f"modal masuk {_rp(r['modal_masuk'])}")
    if r["prive"]:
        lain.append(f"prive {_rp(r['prive'])}")
    if r["bayar_utang"]:
        lain.append(f"bayar liabilitas {_rp(r['bayar_utang'])}")
    if lain:
        L.append("Termasuk " + ", ".join(lain))
    L += ["", "<b>Tagihan pelanggan</b>"]
    if tag[1]:
        L.append(f"Tagihan {nama_bln}: {tag[3]}/{tag[1]} lunas ({round(tag[2] * 100 / tag[0]) if tag[0] else 0}%), "
                 f"{_rp(tag[2])} dari {_rp(tag[0])}")
    else:
        L.append(f"Belum ada tagihan untuk {nama_bln}.")
    L.append(f"Piutang s/d bulan ini: <b>{_rp(piutang[0])}</b> ({piutang[1]} tagihan)")
    for p in penunggak:
        L.append(f"  • {_esc(p['nama'] or p['username'])}: {_rp(p['total'])}" + (f" ({p['n']} bulan)" if p["n"] > 1 else ""))
    if utang["jumlah"]:
        L += ["", "<b>Liabilitas ke pemasok</b>", f"Sisa: <b>{_rp(utang['sisa'])}</b> ({utang['jumlah']} liabilitas)"]
        if utang["lewat_tempo"]:
            L.append(f"⚠️ Lewat jatuh tempo: {_rp(utang['lewat_tempo_total'])} ({utang['lewat_tempo']} liabilitas)")
        if utang["terdekat"]:
            t = utang["terdekat"]
            L.append(f"Terdekat: {_esc(t['pemasok'])} {_rp(t['sisa'])}"
                     + (f" (angsuran ke-{t['cicilan_ke']}/{t['tenor']})" if t.get("cicilan_ke") else "")
                     + f", {t['jatuh_tempo'][8:10]}/{t['jatuh_tempo'][5:7]}")
    if insentif["total"] or ins_bayar or ins_tunggak["total"]:
        sisa = max(0, insentif["total"] - ins_bayar)
        L += ["", "<b>Komisi mitra</b>",
              f"{nama_bln}: terhitung {_rp(insentif['total'])} ({insentif['jumlah']} pelanggan), dibayar {_rp(ins_bayar)}"
              + (f" — <b>belum dibayar {_rp(sisa)}</b>" if sisa else " ✅")]
        lain = [b for b in ins_tunggak["bulan"] if b["periode"] != periode]
        if lain:
            L.append("Tunggakan bulan lain: " + ", ".join(f"{_nama_periode(b['periode'])} {_rp(b['sisa'])}" for b in lain[:4]))
    L += ["", "<b>Pelanggan</b>",
          f"Aktif berbayar {pel[0] or 0}, gratis {pel[1] or 0}, berhenti bulan ini {pel[2] or 0}, terisolir saat ini {n_isolir}"]
    if ev:
        L += ["", "<b>Jaringan</b>",
              f"Event: {ev.get('kritis', 0)} kritis, {ev.get('peringatan', 0)} peringatan"]
        if ev_top:
            L.append("Kritis terbanyak: " + ", ".join(f"{_esc(e[0])} ({e[1]}x)" for e in ev_top))
    if tn["jumlah"]:
        L += ["", f"📎 <b>{tn['jumlah']} pengeluaran ≥ {_rp(tn['batas'])} belum ada nota</b> (total {_rp(tn['total'])})"]
        for d in tn["daftar"][:5]:
            L.append(f"  • {d['kode']} {d['tanggal'][8:10]}/{d['tanggal'][5:7]} {_esc(d['kategori'])}: {_rp(d['jumlah'])}"
                     f" ({_esc(d['dicatat_oleh'])})")
    conn = _billing_conn()
    try:
        tb = _buku_tertutup(conn, periode)
    finally:
        conn.close()
    L += ["", (f"🔒 Buku {nama_bln} sudah ditutup oleh {_esc(tb['oleh'])}." if tb else
               f"⚠️ Buku {nama_bln} belum ditutup — periksa transaksinya lalu tutup buku di menu Keuangan → Ringkasan.")]
    L += ["", f"<i>{n_trx[0]} transaksi dicatat oleh {n_trx[1]} petugas"
          + (f", {n_batal} dibatalkan" if n_batal else "") + ". Rincian di berkas CSV terlampir.</i>"]
    teks = "\n".join(L)
    return teks if len(teks) < 4000 else teks[:3990] + "…"


def kirim_laporan_bulanan(periode: str, chat_ids: Optional[list[str]] = None) -> tuple[int, int]:
    """Kirim laporan + CSV ke chat owner. Kembalikan (berhasil, total chat)."""
    tujuan = chat_ids or _chat_owner()
    if not tujuan:
        return 0, 0
    teks = susun_laporan_bulanan(periode)
    csv_isi = _buat_csv_keuangan(periode).encode("utf-8")
    ok = 0
    for cid in tujuan:
        if kirim_telegram_ke(cid, teks):
            ok += 1
            kirim_berkas_telegram(cid, f"keuangan-{periode}.csv", csv_isi, f"Rincian transaksi {_nama_periode(periode)}")
    return ok, len(tujuan)


def _handle_perintah_owner(chat_id: str, text: str) -> bool:
    """Perintah khusus chat owner. True bila sudah ditangani."""
    perintah, _, arg = (text or "").strip().partition(" ")
    if perintah.lower().split("@")[0] != "/laporan":
        return False
    arg = arg.strip()
    periode = arg if _periode_valid(arg) else _geser_periode(datetime.now().strftime("%Y-%m"), -1)
    if arg and not _periode_valid(arg):
        kirim_telegram_ke(chat_id, "Format: <code>/laporan</code> (bulan lalu) atau <code>/laporan 2026-09</code>")
        return True
    kirim_laporan_bulanan(periode, [chat_id])
    catat_aktivitas("telegram-owner", "laporan_bulanan", f"periode {periode} diminta lewat /laporan")
    return True


def proses_laporan_bulanan(sekarang=None) -> None:
    """Tanggal 1-7 (menyusul bila server mati di tanggal 1), setelah LAPORAN_BULANAN_JAM:
    kirim laporan bulan lalu sekali saja (dicatat di otomasi_log)."""
    sekarang = sekarang or datetime.now()
    if CONFIG["LAPORAN_BULANAN_AKTIF"] != "ya" or sekarang.day > 7:
        return
    if sekarang.day == 1 and sekarang.hour < CONFIG["LAPORAN_BULANAN_JAM"]:
        return
    if not _chat_owner():
        return
    periode = _geser_periode(sekarang.strftime("%Y-%m"), -1)
    kunci = f"laporan:{periode}"
    conn = _billing_conn()
    try:
        if conn.execute("SELECT 1 FROM otomasi_log WHERE tanggal=? AND jenis='laporan_bulanan'", (kunci,)).fetchone():
            return
    finally:
        conn.close()
    ok, total = kirim_laporan_bulanan(periode)
    if ok:
        conn = _billing_conn()
        try:
            conn.execute("INSERT OR IGNORE INTO otomasi_log VALUES (?, 'laporan_bulanan')", (kunci,))
            conn.commit()
        finally:
            conn.close()
        catat_aktivitas("sistem", "laporan_bulanan", f"periode {periode} terkirim ke {ok}/{total} chat owner")
    else:
        logger.warning(f"Laporan bulanan {periode} gagal dikirim; dicoba lagi siklus berikutnya.")


@app.get("/api/keuangan/laporan-telegram")
def pratinjau_laporan(periode: str, request: Request):
    if request.state.peran != "owner":
        raise HTTPException(status_code=403, detail="Khusus owner.")
    if not _periode_valid(periode):
        raise HTTPException(status_code=400, detail="Format periode harus YYYY-MM.")
    return {"periode": periode, "teks": susun_laporan_bulanan(periode), "chat_owner": len(_chat_owner()),
            "aktif": CONFIG["LAPORAN_BULANAN_AKTIF"] == "ya", "jam": CONFIG["LAPORAN_BULANAN_JAM"]}


@app.post("/api/keuangan/laporan-telegram")
def kirim_laporan_sekarang(periode: str, request: Request):
    if request.state.peran != "owner":
        raise HTTPException(status_code=403, detail="Khusus owner.")
    if not _periode_valid(periode):
        raise HTTPException(status_code=400, detail="Format periode harus YYYY-MM.")
    if not _chat_owner():
        raise HTTPException(status_code=400, detail="Chat ID Telegram owner belum diisi (Pengaturan → Sistem).")
    ok, total = kirim_laporan_bulanan(periode)
    catat_aktivitas(request.state.user, "laporan_bulanan", f"periode {periode} dikirim manual ke {ok}/{total} chat owner")
    if not ok:
        raise HTTPException(status_code=502, detail="Telegram menolak pesan. Pastikan owner sudah menekan Start di bot dan chat ID benar.")
    return {"ok": True, "terkirim": ok, "total": total}


# ============================================================
# 6e. HALAMAN MITRA — hanya pelanggan tampil_mitra=1, kontak saja yang bisa diubah
# ============================================================

def _siapkan_kunci_komisi(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS komisi_kunci (periode TEXT PRIMARY KEY, jumlah INTEGER, jumlah_lunas INTEGER, "
                 "jumlah_gratis INTEGER, per_pelanggan INTEGER, total INTEGER NOT NULL, pelanggan TEXT, sumber TEXT NOT NULL, "
                 "oleh TEXT, pada TEXT, catatan TEXT)")


def _hitung_insentif(conn, periode: str) -> dict:
    """Komisi mitra = hasil kerja bulan `periode` (dibayar awal bulan berikutnya). Bila sudah dikunci
    (otomatis saat tutup buku bulan itu, atau manual oleh owner), angka kuncian yang dipakai, sehingga
    pembayaran susulan atau perubahan tarif tidak mengubah komisi yang sudah final."""
    _siapkan_kunci_komisi(conn)
    k = conn.execute("SELECT * FROM komisi_kunci WHERE periode = ?", (periode,)).fetchone()
    if k:
        return {"periode": periode, "per_pelanggan": k["per_pelanggan"], "jumlah_lunas": k["jumlah_lunas"],
                "jumlah_gratis": k["jumlah_gratis"], "jumlah": k["jumlah"], "total": k["total"],
                "pelanggan": json.loads(k["pelanggan"] or "[]"),
                "terkunci": {"sumber": k["sumber"], "oleh": k["oleh"], "pada": k["pada"], "catatan": k["catatan"]}}
    return {**_hitung_insentif_berjalan(conn, periode), "terkunci": None}


def _kunci_komisi(conn, periode: str, sumber: str, oleh: str, total: Optional[int] = None, catatan: Optional[str] = None) -> dict:
    ins = _hitung_insentif_berjalan(conn, periode)
    conn.execute("INSERT OR REPLACE INTO komisi_kunci (periode, jumlah, jumlah_lunas, jumlah_gratis, per_pelanggan, total, "
                 "pelanggan, sumber, oleh, pada, catatan) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (periode, ins["jumlah"], ins["jumlah_lunas"], ins["jumlah_gratis"], ins["per_pelanggan"],
                  ins["total"] if total is None else total, json.dumps(ins["pelanggan"]), sumber, oleh,
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S"), catatan))
    return ins


def _hitung_insentif_berjalan(conn, periode: str) -> dict:
    """Insentif mitra = (pelanggan dengan tagihan periode itu LUNAS + pelanggan
    gratis yang ditandai insentif_gratis dan masih aktif di periode itu) x tarif.
    Hanya pelanggan yang ditampilkan ke mitra. Menunggak/berhenti tanpa
    pembayaran tidak terhitung."""
    lunas = conn.execute(
        "SELECT p.username, p.nama, t.tgl_bayar FROM tagihan t JOIN pelanggan p ON p.username = t.username "
        "WHERE t.periode = ? AND t.status = 'lunas' AND p.tampil_mitra = 1 ORDER BY p.nama COLLATE NOCASE",
        (periode,),
    ).fetchall()
    # Pelanggan gratis dihitung untuk periode selama ia belum berhenti sebelum
    # awal periode tersebut.
    gratis = conn.execute(
        "SELECT p.username, p.nama FROM pelanggan p "
        "WHERE p.gratis = 1 AND p.insentif_gratis = 1 AND p.tampil_mitra = 1 "
        "AND (p.status = 'aktif' OR p.berhenti_sejak >= ?) "
        "AND p.username NOT IN (SELECT username FROM tagihan WHERE periode = ? AND status = 'lunas') "
        "ORDER BY p.nama COLLATE NOCASE",
        (f"{periode}-01", periode),
    ).fetchall()
    tarif = CONFIG["MITRA_INSENTIF_PER_PELANGGAN"]
    jumlah = len(lunas) + len(gratis)
    return {"periode": periode, "per_pelanggan": tarif, "jumlah_lunas": len(lunas),
            "jumlah_gratis": len(gratis), "jumlah": jumlah, "total": jumlah * tarif,
            "pelanggan": [dict(r) for r in lunas] + [{**dict(r), "gratis": True} for r in gratis]}


@app.get("/api/mitra/pelanggan")
def mitra_list_pelanggan(periode: str, request: Request):
    if not _periode_valid(periode):
        raise HTTPException(status_code=400, detail="Format periode harus YYYY-MM.")
    conn = _billing_conn()
    try:
        rows = conn.execute(
            "SELECT p.username, p.nama, p.alamat, p.no_hp, p.status, p.gratis, p.insentif_gratis, k.nama AS paket, "
            "t.status AS status_bayar, t.tgl_bayar, t.jatuh_tempo, "
            "(SELECT 1 FROM isolir i WHERE i.username = p.username) AS terisolir "
            "FROM pelanggan p LEFT JOIN paket k ON k.id = p.paket_id "
            "LEFT JOIN tagihan t ON t.username = p.username AND t.periode = ? "
            # Pelanggan yang berhenti tetap tampil bila masih punya tagihan bulan itu,
            # supaya jumlahnya cocok dengan perhitungan insentif.
            "WHERE p.tampil_mitra = 1 AND (p.status = 'aktif' OR t.id IS NOT NULL) "
            "ORDER BY p.nama COLLATE NOCASE",
            (periode,),
        ).fetchall()
        insentif = _hitung_insentif(conn, periode)
        insentif["dibayar"] = _insentif_dibayar(conn, periode)
        insentif["sisa"] = max(0, insentif["total"] - insentif["dibayar"])
        # Mitra hanya melihat tanggal, jumlah, dan metode pembayaran (bukan catatan internal)
        insentif["pembayaran"] = [{"tanggal": p["tanggal"], "jumlah": p["jumlah"], "metode": p["metode"]}
                                  for p in _pembayaran_insentif(conn, [periode]).get(periode, [])
                                  if p["status"] == "aktif"]
        insentif["tertunggak_lain"] = [b for b in _insentif_tertunggak(conn, periode)["bulan"] if b["periode"] != periode]
    finally:
        conn.close()
    # Pelanggan gratis yang ikut dihitung komisi (mis. pengurus) ditampilkan ke mitra seperti pelanggan
    # biasa: otomatis "lunas" sejak tanggal 1 bulan itu. Status gratisnya tidak dikirim ke HP mitra sama
    # sekali, supaya tidak menimbulkan iri; jumlah komisinya tetap sama.
    bulan_ini = datetime.now().strftime("%Y-%m")
    daftar = []
    for r in rows:
        p = dict(r)
        if p["gratis"] and p["insentif_gratis"]:
            p["status_bayar"], p["tgl_bayar"], p["jatuh_tempo"] = (
                ("lunas", f"{periode}-01", f"{periode}-{CONFIG['JATUH_TEMPO_TANGGAL']:02d}") if periode <= bulan_ini
                else (None, None, None))
        p.pop("insentif_gratis", None)
        p["gratis"] = 0 if r["insentif_gratis"] else p["gratis"]
        daftar.append(p)
    insentif["jumlah_lunas"] += insentif.get("jumlah_gratis") or 0
    insentif["jumlah_gratis"] = 0
    insentif.pop("pelanggan", None)        # daftar internal (berisi tanda gratis) tidak dikirim ke mitra
    t = insentif.get("terkunci")
    insentif["final"] = bool(t)
    insentif["terkunci"] = {"pada": t["pada"]} if t else None
    insentif["dibayar_bulan"] = _geser_periode(periode, 1)
    return {"akun": _oleh(request), "periode": periode, "insentif": insentif, "pelanggan": daftar}


class KontakIn(BaseModel):
    nama: str
    alamat: Optional[str] = None
    no_hp: Optional[str] = None


@app.put("/api/mitra/pelanggan/{username}")
def mitra_ubah_kontak(username: str, d: KontakIn, request: Request):
    nama = d.nama.strip()
    if not nama:
        raise HTTPException(status_code=400, detail="Nama tidak boleh kosong.")
    alamat = (d.alamat or "").strip() or None
    no_hp = (d.no_hp or "").strip() or None
    conn = _billing_conn()
    try:
        lama = conn.execute(
            "SELECT nama, alamat, no_hp FROM pelanggan WHERE username=? AND tampil_mitra=1 AND status='aktif'",
            (username,),
        ).fetchone()
        if not lama:
            raise HTTPException(status_code=404, detail="Pelanggan tidak ditemukan.")
        conn.execute("UPDATE pelanggan SET nama=?, alamat=?, no_hp=? WHERE username=?", (nama, alamat, no_hp, username))
        conn.commit()
    finally:
        conn.close()
    ubah = [f for f, a, b in (("nama", lama["nama"], nama), ("alamat", lama["alamat"], alamat), ("no HP", lama["no_hp"], no_hp)) if a != b]
    if ubah:
        catat_aktivitas(_oleh(request), "mitra_ubah_kontak", f"{username}: {', '.join(ubah)} diubah")
    return {"ok": True}


# ============================================================
# TIKET GANGGUAN — keluhan pelanggan dicatat, ditugaskan, dipantau sampai selesai
# ============================================================

_KATEGORI_TIKET = ("Internet mati", "Internet lambat", "Putus-putus", "Kabel / ONU rusak",
                   "Pindah / pasang baru", "Lainnya")
_STATUS_TIKET_TERBUKA = ("baru", "dikerjakan")


def init_tiket_db() -> None:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tiket (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                nama TEXT,
                kategori TEXT NOT NULL,
                keterangan TEXT,
                prioritas TEXT NOT NULL DEFAULT 'normal',
                status TEXT NOT NULL DEFAULT 'baru',
                teknisi TEXT,
                dibuat_oleh TEXT NOT NULL,
                dibuat_pada TEXT NOT NULL,
                diambil_pada TEXT,
                selesai_pada TEXT,
                solusi TEXT,
                diingatkan_pada TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tiket_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tiket_id INTEGER NOT NULL REFERENCES tiket(id),
                waktu TEXT NOT NULL,
                oleh TEXT NOT NULL,
                aksi TEXT NOT NULL,
                catatan TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tiket_status ON tiket(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tiket_log ON tiket_log(tiket_id)")
        conn.commit()
    finally:
        conn.close()


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


def _log_tiket(conn, tid: int, oleh: str, aksi: str, catatan: Optional[str] = None) -> None:
    conn.execute("INSERT INTO tiket_log (tiket_id, waktu, oleh, aksi, catatan) VALUES (?,?,?,?,?)",
                 (tid, _sekarang_str(), oleh, aksi, catatan))


def _diagnosa_pelanggan(conn, username: Optional[str]) -> Optional[dict]:
    """Kondisi pelanggan saat ini dari data monitoring terakhir, supaya teknisi
    langsung tahu apakah PPPoE online, redaman ONU, isolir, atau tagihan menunggak."""
    if not username:
        return None
    hasil = {"pppoe_online": None, "ip": None, "uptime": None, "onu": None,
             "terisolir": False, "tagihan_belum": [], "data_monitoring": _latest_data is not None}
    if _latest_data is not None:
        u = next((x for x in _latest_data.pppoe_users if x.username == username), None)
        hasil["pppoe_online"] = u is not None
        if u:
            hasil["ip"], hasil["uptime"] = u.ip_address, u.uptime
        onu = next((o for o in _latest_data.onu_list if o.pppoe_username == username), None)
        if onu:
            hasil["onu"] = {"pon": onu.pon, "onu_id": onu.onu_id, "status": onu.status, "online": onu.online,
                            "olt_rx_dbm": onu.olt_rx_dbm, "onu_rx_dbm": onu.onu_rx_dbm,
                            "serial": onu.serial_number, "model": onu.model}
    try:
        hasil["terisolir"] = conn.execute("SELECT 1 FROM isolir WHERE username=?", (username,)).fetchone() is not None
    except sqlite3.OperationalError:
        pass
    hasil["tagihan_belum"] = [dict(r) for r in conn.execute(
        "SELECT periode, jumlah, jatuh_tempo FROM tagihan WHERE username=? AND status='belum' ORDER BY periode",
        (username,))]
    return hasil


def _baris_tiket(r) -> dict:
    t = dict(r)
    t["umur"] = _durasi_teks(t["dibuat_pada"], t["selesai_pada"])
    t["terbuka"] = t["status"] in _STATUS_TIKET_TERBUKA
    return t


def _teks_tiket(t, judul: str) -> str:
    siapa = f"{_esc(t['nama'] or t['username'])} ({_esc(t['username'])})" if t["username"] else "Umum / jaringan"
    baris = [f"{judul} <b>#T{t['id']}</b>{' 🔺 PRIORITAS TINGGI' if t['prioritas'] == 'tinggi' else ''}",
             f"Pelanggan: {siapa}", f"Masalah: {_esc(t['kategori'])}"]
    if t["keterangan"]:
        baris.append(f"Keterangan: {_esc(t['keterangan'])}")
    return "\n".join(baris)


def _kabar_tiket(teks: str) -> None:
    try:
        kirim_telegram_ke(str(CONFIG["TELEGRAM_CHAT_ID"]), teks)
    except Exception as e:
        logger.error(f"Gagal kirim kabar tiket: {e}")


def _akun_teknisi(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT username FROM akun WHERE aktif=1 AND peran IN ('owner','admin') ORDER BY username COLLATE NOCASE")]


class TiketIn(BaseModel):
    username: Optional[str] = None
    kategori: str
    keterangan: Optional[str] = None
    prioritas: str = "normal"
    teknisi: Optional[str] = None


def _buat_tiket(d: TiketIn, oleh: str, hanya_mitra: bool = False) -> dict:
    kategori = d.kategori.strip()
    if kategori not in _KATEGORI_TIKET:
        raise HTTPException(status_code=400, detail="Jenis masalah tidak dikenal.")
    if d.prioritas not in ("normal", "tinggi"):
        raise HTTPException(status_code=400, detail="Prioritas harus normal atau tinggi.")
    keterangan = (d.keterangan or "").strip()[:1000] or None
    username = (d.username or "").strip() or None
    if hanya_mitra and not username:
        raise HTTPException(status_code=400, detail="Pilih pelanggan yang mengalami gangguan.")
    if not username and not keterangan:
        raise HTTPException(status_code=400, detail="Tuliskan keterangan untuk tiket tanpa pelanggan.")
    conn = _billing_conn()
    try:
        nama = None
        if username:
            sql = "SELECT nama FROM pelanggan WHERE username=?" + (" AND tampil_mitra=1 AND status='aktif'" if hanya_mitra else "")
            p = conn.execute(sql, (username,)).fetchone()
            if not p:
                raise HTTPException(status_code=404, detail="Pelanggan tidak ditemukan.")
            nama = p["nama"]
            ada = conn.execute(
                f"SELECT id FROM tiket WHERE username=? AND status IN {_STATUS_TIKET_TERBUKA}", (username,)).fetchone()
            if ada:
                raise HTTPException(status_code=409,
                                    detail=f"Pelanggan ini sudah punya tiket terbuka #T{ada['id']}. Tambahkan catatan di tiket itu.")
        teknisi = (d.teknisi or "").strip() or None
        if teknisi and teknisi not in _akun_teknisi(conn):
            raise HTTPException(status_code=400, detail="Teknisi harus akun admin/owner yang aktif.")
        sekarang = _sekarang_str()
        cur = conn.execute(
            "INSERT INTO tiket (username, nama, kategori, keterangan, prioritas, status, teknisi, dibuat_oleh, "
            "dibuat_pada, diambil_pada) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (username, nama, kategori, keterangan, d.prioritas, "dikerjakan" if teknisi else "baru",
             teknisi, oleh, sekarang, sekarang if teknisi else None))
        tid = cur.lastrowid
        _log_tiket(conn, tid, oleh, "buat", keterangan)
        if teknisi:
            _log_tiket(conn, tid, oleh, "tugaskan", teknisi)
        conn.commit()
        t = conn.execute("SELECT * FROM tiket WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()
    catat_aktivitas(oleh, "tiket_buat", f"#T{tid} {username or 'umum'}: {kategori}")
    teks = _teks_tiket(t, "🛠 Tiket baru")
    teks += f"\nDilaporkan: {_esc(oleh)}" + (f"\nTeknisi: {_esc(teknisi)}" if teknisi else "\nBelum ada teknisi — ambil di menu Tiket.")
    _kabar_tiket(teks)
    return {"ok": True, "id": tid}


@app.get("/api/tiket")
def list_tiket(request: Request, status: str = "terbuka", cari: str = "", limit: int = 300):
    _wajib_admin(request)
    where, arg = [], []
    if status == "terbuka":
        where.append(f"status IN {_STATUS_TIKET_TERBUKA}")
    elif status in ("baru", "dikerjakan", "selesai", "batal"):
        where.append("status = ?")
        arg.append(status)
    elif status == "saya":
        where.append(f"status IN {_STATUS_TIKET_TERBUKA} AND teknisi = ?")
        arg.append(_oleh(request))
    if cari.strip():
        q = f"%{cari.strip()}%"
        where.append("(username LIKE ? OR nama LIKE ? OR keterangan LIKE ? OR kategori LIKE ? OR teknisi LIKE ? OR CAST(id AS TEXT) = ?)")
        arg += [q, q, q, q, q, cari.strip().lstrip("#Tt")]
    sql = "SELECT * FROM tiket" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY CASE WHEN status IN ('baru','dikerjakan') THEN 0 ELSE 1 END, " \
           "CASE prioritas WHEN 'tinggi' THEN 0 ELSE 1 END, " \
           "CASE WHEN status IN ('baru','dikerjakan') THEN dibuat_pada END ASC, id DESC LIMIT ?"
    arg.append(max(1, min(limit, 1000)))
    batas30 = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _billing_conn()
    try:
        rows = [_baris_tiket(r) for r in conn.execute(sql, arg)]
        hitung = dict(conn.execute("SELECT status, COUNT(*) FROM tiket GROUP BY status").fetchall())
        selesai30 = conn.execute(
            "SELECT dibuat_pada, selesai_pada FROM tiket WHERE status='selesai' AND selesai_pada >= ?", (batas30,)).fetchall()
        teknisi = _akun_teknisi(conn)
    finally:
        conn.close()
    menit = [(datetime.strptime(b, "%Y-%m-%d %H:%M:%S") - datetime.strptime(a, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
             for a, b in selesai30]
    rata = int(sum(menit) / len(menit)) if menit else None
    return {"saya": _oleh(request), "peran": request.state.peran, "kategori": list(_KATEGORI_TIKET), "teknisi": teknisi,
            "ringkasan": {"baru": hitung.get("baru", 0), "dikerjakan": hitung.get("dikerjakan", 0),
                          "terbuka": hitung.get("baru", 0) + hitung.get("dikerjakan", 0),
                          "selesai_30hari": len(menit),
                          "rata_selesai": _teks_menit(rata) if rata is not None else None},
            "tiket": rows}


@app.post("/api/tiket")
def tambah_tiket(d: TiketIn, request: Request):
    _wajib_admin(request)
    return _buat_tiket(d, _oleh(request))


@app.get("/api/tiket/{tid}")
def detail_tiket(tid: int, request: Request):
    _wajib_admin(request)
    conn = _billing_conn()
    try:
        t = conn.execute("SELECT * FROM tiket WHERE id=?", (tid,)).fetchone()
        if not t:
            raise HTTPException(status_code=404, detail="Tiket tidak ditemukan.")
        log = [dict(r) for r in conn.execute("SELECT waktu, oleh, aksi, catatan FROM tiket_log WHERE tiket_id=? ORDER BY id", (tid,))]
        kontak = None
        if t["username"]:
            p = conn.execute("SELECT nama, alamat, no_hp FROM pelanggan WHERE username=?", (t["username"],)).fetchone()
            kontak = dict(p) if p else None
        riwayat = [dict(r) for r in conn.execute(
            "SELECT id, kategori, status, dibuat_pada, solusi FROM tiket WHERE username=? AND id<>? ORDER BY id DESC LIMIT 5",
            (t["username"], tid))] if t["username"] else []
        diagnosa = _diagnosa_pelanggan(conn, t["username"]) if t["status"] in _STATUS_TIKET_TERBUKA else None
    finally:
        conn.close()
    return {"tiket": _baris_tiket(t), "log": log, "kontak": kontak, "riwayat": riwayat, "diagnosa": diagnosa}


class AksiTiketIn(BaseModel):
    aksi: str                   # ambil | tugaskan | selesai | batal | buka | catatan | prioritas
    catatan: Optional[str] = None
    teknisi: Optional[str] = None
    prioritas: Optional[str] = None


@app.post("/api/tiket/{tid}/aksi")
def aksi_tiket(tid: int, d: AksiTiketIn, request: Request):
    _wajib_admin(request)
    oleh = _oleh(request)
    catatan = (d.catatan or "").strip()[:1000] or None
    conn = _billing_conn()
    try:
        t = conn.execute("SELECT * FROM tiket WHERE id=?", (tid,)).fetchone()
        if not t:
            raise HTTPException(status_code=404, detail="Tiket tidak ditemukan.")
        terbuka = t["status"] in _STATUS_TIKET_TERBUKA
        sekarang = _sekarang_str()
        kabar = None
        if d.aksi in ("ambil", "tugaskan"):
            if not terbuka:
                raise HTTPException(status_code=400, detail="Tiket sudah ditutup. Buka lagi dulu bila perlu.")
            teknisi = oleh if d.aksi == "ambil" else (d.teknisi or "").strip()
            if teknisi not in _akun_teknisi(conn):
                raise HTTPException(status_code=400, detail="Teknisi harus akun admin/owner yang aktif.")
            conn.execute("UPDATE tiket SET teknisi=?, status='dikerjakan', diambil_pada=COALESCE(diambil_pada, ?) WHERE id=?",
                         (teknisi, sekarang, tid))
            _log_tiket(conn, tid, oleh, "tugaskan", teknisi)
            if teknisi != oleh:
                kabar = f"👷 Tiket <b>#T{tid}</b> ({_esc(t['nama'] or t['username'] or 'umum')}) ditugaskan ke <b>{_esc(teknisi)}</b> oleh {_esc(oleh)}."
        elif d.aksi == "selesai":
            if not terbuka:
                raise HTTPException(status_code=400, detail="Tiket sudah ditutup.")
            if not catatan or len(catatan) < 3:
                raise HTTPException(status_code=400, detail="Tuliskan apa yang dikerjakan / penyebabnya.")
            conn.execute("UPDATE tiket SET status='selesai', selesai_pada=?, solusi=?, "
                         "teknisi=COALESCE(teknisi, ?), diambil_pada=COALESCE(diambil_pada, ?) WHERE id=?",
                         (sekarang, catatan, oleh, sekarang, tid))
            _log_tiket(conn, tid, oleh, "selesai", catatan)
            kabar = (f"✅ Tiket <b>#T{tid}</b> selesai — {_esc(t['nama'] or t['username'] or 'umum')} ({_esc(t['kategori'])})\n"
                     f"Solusi: {_esc(catatan)}\nOleh {_esc(oleh)} · waktu penanganan {_durasi_teks(t['dibuat_pada'], sekarang)}")
        elif d.aksi == "batal":
            if not terbuka:
                raise HTTPException(status_code=400, detail="Tiket sudah ditutup.")
            if not catatan or len(catatan) < 3:
                raise HTTPException(status_code=400, detail="Tuliskan alasan pembatalan.")
            conn.execute("UPDATE tiket SET status='batal', selesai_pada=?, solusi=? WHERE id=?", (sekarang, catatan, tid))
            _log_tiket(conn, tid, oleh, "batal", catatan)
        elif d.aksi == "buka":
            if terbuka:
                raise HTTPException(status_code=400, detail="Tiket masih terbuka.")
            if t["username"]:
                ada = conn.execute(f"SELECT id FROM tiket WHERE username=? AND id<>? AND status IN {_STATUS_TIKET_TERBUKA}",
                                   (t["username"], tid)).fetchone()
                if ada:
                    raise HTTPException(status_code=409, detail=f"Pelanggan ini sudah punya tiket terbuka #T{ada['id']}.")
            conn.execute("UPDATE tiket SET status=?, selesai_pada=NULL, solusi=NULL, diingatkan_pada=NULL WHERE id=?",
                         ("dikerjakan" if t["teknisi"] else "baru", tid))
            _log_tiket(conn, tid, oleh, "buka", catatan)
        elif d.aksi == "catatan":
            if not catatan:
                raise HTTPException(status_code=400, detail="Catatan kosong.")
            _log_tiket(conn, tid, oleh, "catatan", catatan)
        elif d.aksi == "prioritas":
            if d.prioritas not in ("normal", "tinggi"):
                raise HTTPException(status_code=400, detail="Prioritas harus normal atau tinggi.")
            conn.execute("UPDATE tiket SET prioritas=? WHERE id=?", (d.prioritas, tid))
            _log_tiket(conn, tid, oleh, "prioritas", d.prioritas)
        else:
            raise HTTPException(status_code=400, detail="Aksi tidak dikenal.")
        conn.commit()
    finally:
        conn.close()
    if d.aksi != "catatan":
        catat_aktivitas(oleh, f"tiket_{d.aksi}", f"#T{tid}" + (f": {catatan[:120]}" if catatan else "")
                        + (f" → {d.teknisi}" if d.aksi == "tugaskan" else ""))
    if kabar:
        _kabar_tiket(kabar)
    return {"ok": True}


# --- Mitra: lapor gangguan untuk pelanggannya dan pantau statusnya ---

@app.get("/api/mitra/tiket")
def mitra_list_tiket(request: Request):
    conn = _billing_conn()
    try:
        rows = conn.execute(
            "SELECT t.id, t.username, t.nama, t.kategori, t.keterangan, t.status, t.teknisi, t.dibuat_pada, "
            "t.selesai_pada, t.solusi, t.dibuat_oleh FROM tiket t JOIN pelanggan p ON p.username = t.username "
            "WHERE p.tampil_mitra = 1 AND (t.status IN ('baru','dikerjakan') OR t.dibuat_pada >= ?) "
            "ORDER BY CASE WHEN t.status IN ('baru','dikerjakan') THEN 0 ELSE 1 END, t.id DESC LIMIT 100",
            ((datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),)).fetchall()
    finally:
        conn.close()
    return {"kategori": list(_KATEGORI_TIKET), "tiket": [_baris_tiket(r) for r in rows]}


class TiketMitraIn(BaseModel):
    username: str
    kategori: str
    keterangan: Optional[str] = None


@app.post("/api/mitra/tiket")
def mitra_tambah_tiket(d: TiketMitraIn, request: Request):
    return _buat_tiket(TiketIn(username=d.username, kategori=d.kategori, keterangan=d.keterangan),
                       _oleh(request), hanya_mitra=True)


# --- Pengingat: tiket terbuka terlalu lama ---

def _tiket_terbuka(conn) -> list:
    return conn.execute(
        f"SELECT * FROM tiket WHERE status IN {_STATUS_TIKET_TERBUKA} "
        "ORDER BY CASE prioritas WHEN 'tinggi' THEN 0 ELSE 1 END, dibuat_pada").fetchall()


def proses_pengingat_tiket(sekarang=None) -> None:
    """Satu pengingat per tiket setiap TIKET_PENGINGAT_JAM selama tiket belum selesai."""
    sekarang = sekarang or datetime.now()
    jam = int(CONFIG.get("TIKET_PENGINGAT_JAM", 24) or 0)
    if jam <= 0:
        return
    batas = (sekarang - timedelta(hours=jam)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _billing_conn()
    try:
        lama = conn.execute(
            f"SELECT * FROM tiket WHERE status IN {_STATUS_TIKET_TERBUKA} AND dibuat_pada <= ? "
            "AND (diingatkan_pada IS NULL OR diingatkan_pada <= ?) ORDER BY dibuat_pada", (batas, batas)).fetchall()
        if not lama:
            return
        baris = [f"⏰ <b>{len(lama)} tiket belum selesai lebih dari {jam} jam</b>"]
        for t in lama[:15]:
            baris.append(f"- #T{t['id']} {_esc(t['nama'] or t['username'] or 'umum')} · {_esc(t['kategori'])} · "
                         f"{_durasi_teks(t['dibuat_pada'], sekarang.strftime('%Y-%m-%d %H:%M:%S'))} · "
                         f"{'teknisi ' + _esc(t['teknisi']) if t['teknisi'] else 'BELUM ADA TEKNISI'}")
        if len(lama) > 15:
            baris.append(f"… dan {len(lama) - 15} lainnya")
        if kirim_telegram_ke(str(CONFIG["TELEGRAM_CHAT_ID"]), "\n".join(baris)):
            conn.executemany("UPDATE tiket SET diingatkan_pada=? WHERE id=?",
                             [(sekarang.strftime("%Y-%m-%d %H:%M:%S"), t["id"]) for t in lama])
            conn.commit()
    finally:
        conn.close()


def _format_tiket_list() -> str:
    conn = _billing_conn()
    try:
        rows = _tiket_terbuka(conn)
    finally:
        conn.close()
    if not rows:
        return "✅ Tidak ada tiket gangguan yang terbuka."
    baris = [f"🛠 <b>Tiket terbuka ({len(rows)})</b>"]
    for t in rows[:25]:
        baris.append(f"- #T{t['id']}{' 🔺' if t['prioritas'] == 'tinggi' else ''} {_esc(t['nama'] or t['username'] or 'umum')} · "
                     f"{_esc(t['kategori'])} · {_durasi_teks(t['dibuat_pada'])} · {_esc(t['teknisi'] or 'belum ada teknisi')}")
    return "\n".join(baris)


def _nilai_kesehatan(o, username, sinyal_rows, pakai_rows, total_snap):
    """Beri nilai kesehatan satu pelanggan dari kondisi sekarang + riwayat."""
    skor = 0
    alasan = []
    saran = []
    redaman_buruk = False

    # --- 1. Kondisi sekarang ---
    if not o.online:
        skor += 3
        alasan.append("ONU offline")
        ev = (o.last_event or "").lower()
        if "dying" in ev or "power" in ev:
            saran.append("Kemungkinan listrik atau adaptor pelanggan mati. Hubungi pelanggan, cek listrik dan adaptor ONT.")
        elif "los" in ev:
            saran.append("Sinyal hilang (LOS): cek kabel drop putus, konektor lepas, atau ONT tercabut.")
        else:
            saran.append("ONU tidak terbaca di OLT: cek listrik ONT, lalu kabel fiber ke pelanggan.")
    else:
        for nama, nilai in (("OLT RX", o.olt_rx_dbm), ("ONU RX", o.onu_rx_dbm)):
            if nilai is None:
                continue
            st = classify_rx_power(nilai)
            if st == "CRITICAL":
                skor += 3
                redaman_buruk = True
                alasan.append(f"Redaman {nama} {nilai:.1f} dBm (kritis)")
            elif st == "WARNING":
                skor += 1
                redaman_buruk = True
                alasan.append(f"Redaman {nama} {nilai:.1f} dBm (mendekati batas)")
        if redaman_buruk:
            saran.append("Redaman tinggi: bersihkan dan cek konektor, periksa kabel drop yang tertekuk atau terjepit.")

    # --- 2. Kestabilan sinyal ---
    nilai_olt = [r["olt_rx_dbm"] for r in sinyal_rows if r["olt_rx_dbm"] is not None]
    sinyal_min = sinyal_max = sinyal_rata = None
    if nilai_olt:
        sinyal_min, sinyal_max = min(nilai_olt), max(nilai_olt)
        sinyal_rata = sum(nilai_olt) / len(nilai_olt)
        selisih = sinyal_max - sinyal_min
        if len(nilai_olt) >= 6 and selisih >= 3.0:
            skor += 2 if selisih >= 5.0 else 1
            alasan.append(f"Sinyal naik-turun {selisih:.1f} dB")
            saran.append("Sinyal tidak stabil: curigai konektor longgar atau kabel yang bergoyang.")

    # --- 3. Reconnect PPPoE (uptime yang tiba-tiba turun) ---
    uptimes = [r["uptime_seconds"] for r in pakai_rows if r["uptime_seconds"] is not None]
    reconnect = sum(1 for a, b in zip(uptimes, uptimes[1:]) if b < a)
    if reconnect >= 5:
        skor += 3
    elif reconnect >= 2:
        skor += 1
    if reconnect >= 2:
        alasan.append(f"Reconnect {reconnect}x")
        if not redaman_buruk:
            saran.append("Sinyal fiber bagus tapi sering reconnect: cek adaptor ONT, kabel LAN, atau router pelanggan.")

    # --- 4. Ketersediaan ---
    tersedia = None
    if username and pakai_rows and total_snap:
        tersedia = min(100.0, len(pakai_rows) * 100.0 / total_snap)
        if tersedia < 90:
            skor += 2
            alasan.append(f"Tersedia hanya {tersedia:.0f}%")
        elif tersedia < 97:
            skor += 1
            alasan.append(f"Tersedia {tersedia:.0f}%")

    # --- 5. Hardware ONT ---
    if o.hardware_status == "PERLU_CEK":
        skor += 2
        alasan.extend(o.hardware_notes or ["Hardware ONT perlu dicek"])
        saran.append("Perangkat ONT atau adaptor mulai bermasalah: jadwalkan pengecekan atau penggantian.")

    # --- 6. ONU hidup tapi PPPoE tidak aktif ---
    if o.online and not o.pppoe_username:
        skor += 2
        alasan.append("ONU online tapi tidak ada PPPoE aktif")
        saran.append("Cek pengaturan PPPoE di router pelanggan (username dan password), atau apakah pelanggan sedang diisolir.")

    label = "BERMASALAH" if skor >= 3 else ("PANTAU" if skor >= 1 else "SEHAT")
    return {
        "label": label, "skor": skor, "alasan": alasan, "saran": saran,
        "reconnect": reconnect,
        "tersedia_persen": None if tersedia is None else round(tersedia, 1),
        "sinyal_min": sinyal_min, "sinyal_max": sinyal_max,
        "sinyal_rata": None if sinyal_rata is None else round(sinyal_rata, 2),
        "grafik": nilai_olt[::max(1, len(nilai_olt) // 48)][:48],
    }


@app.get("/api/kesehatan")
def get_kesehatan(jam: int = 24):
    data = _latest_data
    if data is None:
        raise HTTPException(status_code=503, detail="Data belum siap, coba lagi sebentar.")
    jam = max(1, min(jam, 168))
    batas = (datetime.now() - timedelta(hours=jam)).isoformat(timespec="seconds")

    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        sinyal = {}
        for r in conn.execute(
            "SELECT ts, pon, onu_id, olt_rx_dbm FROM onu_history WHERE ts >= ? ORDER BY ts", (batas,)
        ):
            sinyal.setdefault((r["pon"], r["onu_id"]), []).append(r)
        pakai = {}
        for r in conn.execute(
            "SELECT ts, username, uptime_seconds FROM usage_history WHERE ts >= ? ORDER BY ts", (batas,)
        ):
            pakai.setdefault(r["username"], []).append(r)
        total_snap = conn.execute(
            "SELECT COUNT(DISTINCT ts) FROM usage_history WHERE ts >= ?", (batas,)
        ).fetchone()[0]
        dikenal = {
            (r["pon"], r["onu_id"]): r["pppoe_username"]
            for r in conn.execute(
                "SELECT pon, onu_id, pppoe_username, MAX(ts) FROM onu_history "
                "WHERE pppoe_username IS NOT NULL AND pppoe_username != '' "
                "GROUP BY pon, onu_id"
            )
        }
    finally:
        conn.close()

    kontak = {}
    bconn = _billing_conn()
    try:
        for r in bconn.execute("SELECT username, nama, alamat, no_hp FROM pelanggan"):
            kontak[r["username"]] = dict(r)
    finally:
        bconn.close()

    hasil = []
    anonim = 0
    for o in data.onu_list:
        username = o.pppoe_username or dikenal.get((o.pon, o.onu_id))
        if not username:
            anonim += 1
            continue
        n = _nilai_kesehatan(
            o, username, sinyal.get((o.pon, o.onu_id), []), pakai.get(username, []), total_snap
        )
        k = kontak.get(username, {})
        n.update({
            "username": username, "nama": k.get("nama") or username,
            "alamat": k.get("alamat"), "no_hp": k.get("no_hp"),
            "pon": o.pon, "onu_id": o.onu_id, "online": o.online,
            "olt_rx_dbm": o.olt_rx_dbm, "onu_rx_dbm": o.onu_rx_dbm, "onu_tx_dbm": o.onu_tx_dbm,
            "suhu_c": o.temperature_c, "voltage_v": o.voltage_v,
            "last_event": o.last_event, "last_event_time": o.last_event_time,
        })
        hasil.append(n)

    hasil.sort(key=lambda x: (-x["skor"], x["nama"].lower()))
    hitung = lambda lb: sum(1 for x in hasil if x["label"] == lb)
    return {
        "jam": jam, "snapshot": total_snap,
        "ringkasan": {"total": len(hasil), "bermasalah": hitung("BERMASALAH"),
                      "pantau": hitung("PANTAU"), "sehat": hitung("SEHAT"),
                      "anonim_diabaikan": anonim},
        "pelanggan": hasil,
    }
def _bersih_event(teks):
    t = (teks or "").strip().strip('"').strip()
    return None if t in ("", "--") else t


@app.get("/api/kesehatan-jaringan")
def get_kesehatan_jaringan(jam: int = 24):
    data = _latest_data
    if data is None:
        raise HTTPException(status_code=503, detail="Data belum siap, coba lagi sebentar.")
    jam = max(1, min(jam, 168))
    batas = (datetime.now() - timedelta(hours=jam)).isoformat(timespec="seconds")

    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        sinyal = {}
        for r in conn.execute(
            "SELECT ts, pon, onu_id, olt_rx_dbm FROM onu_history WHERE ts >= ? ORDER BY ts", (batas,)
        ):
            sinyal.setdefault((r["pon"], r["onu_id"]), []).append(r)
        pakai = {}
        for r in conn.execute(
            "SELECT ts, username, uptime_seconds FROM usage_history WHERE ts >= ? ORDER BY ts", (batas,)
        ):
            pakai.setdefault(r["username"], []).append(r)
        total_snap = conn.execute(
            "SELECT COUNT(DISTINCT ts) FROM usage_history WHERE ts >= ?", (batas,)
        ).fetchone()[0]
        dikenal = {
            (r["pon"], r["onu_id"]): r["pppoe_username"]
            for r in conn.execute(
                "SELECT pon, onu_id, pppoe_username, MAX(ts) FROM onu_history "
                "WHERE pppoe_username IS NOT NULL AND pppoe_username != '' "
                "GROUP BY pon, onu_id"
            )
        }
    finally:
        conn.close()

    kontak = {}
    bconn = _billing_conn()
    try:
        for r in bconn.execute("SELECT username, nama, alamat, no_hp FROM pelanggan"):
            kontak[r["username"]] = dict(r)
    finally:
        bconn.close()

    identitas = [(o, o.pppoe_username or dikenal.get((o.pon, o.onu_id))) for o in data.onu_list]
    punya_onu_aktif = {u for o, u in identitas if u and o.online}

    hasil = []
    anonim = 0
    lama = 0
    for o, username in identitas:
        if not username:
            anonim += 1
            continue
        if not o.online and username in punya_onu_aktif:
            lama += 1   # ONU lama yang sudah diganti: pelanggannya aktif lewat ONU lain
            continue
        n = _nilai_kesehatan_v2(
            o, username, sinyal.get((o.pon, o.onu_id), []), pakai.get(username, []), total_snap
        )
        k = kontak.get(username, {})
        n.update({
            "username": username, "nama": k.get("nama") or username,
            "alamat": k.get("alamat"), "no_hp": k.get("no_hp"),
            "pon": o.pon, "onu_id": o.onu_id, "online": o.online,
            "olt_rx_dbm": o.olt_rx_dbm, "onu_rx_dbm": o.onu_rx_dbm, "onu_tx_dbm": o.onu_tx_dbm,
            "suhu_c": o.temperature_c if o.online else None,
            "voltage_v": o.voltage_v if o.online else None,
            "last_event": _bersih_event(o.last_event),
            "last_event_time": _bersih_event(o.last_event_time),
        })
        hasil.append(n)

    hasil.sort(key=lambda x: (-x["skor"], x["nama"].lower()))
    hitung = lambda lb: sum(1 for x in hasil if x["label"] == lb)
    return {
        "jam": jam, "snapshot": total_snap,
        "ringkasan": {"total": len(hasil), "bermasalah": hitung("BERMASALAH"),
                      "pantau": hitung("PANTAU"), "sehat": hitung("SEHAT"),
                      "anonim_diabaikan": anonim, "onu_lama_diabaikan": lama},
        "pelanggan": hasil,
    }

def _nilai_kesehatan_v2(o, username, sinyal_rows, pakai_rows, total_snap):
    """Beri nilai kesehatan satu pelanggan dari kondisi sekarang + riwayat
    (dipakai /api/kesehatan-jaringan)."""
    skor = 0
    alasan = []
    saran = []
    redaman_kritis = False
    redaman_agak = False
    saran_putus = False

    # Event terakhir dari OLT, dianggap "baru" kalau terjadi dalam 24 jam terakhir
    ev = (_bersih_event(o.last_event) or "").lower()
    ev_waktu = _bersih_event(o.last_event_time)
    ev_baru = False
    if ev and ev_waktu:
        try:
            ev_baru = datetime.strptime(ev_waktu, "%Y-%m-%d %H:%M:%S") >= datetime.now() - timedelta(hours=24)
        except ValueError:
            ev_baru = False

    # --- 1. Kondisi sekarang ---
    if not o.online:
        skor += 3
        alasan.append("ONU offline")
        if "dying" in ev or "power" in ev:
            saran.append("Kemungkinan listrik atau adaptor pelanggan mati. Hubungi pelanggan, cek listrik dan adaptor ONT.")
        elif "los" in ev:
            saran.append("Sinyal hilang (LOS): cek kabel drop putus, konektor lepas, atau ONT tercabut.")
        else:
            saran.append("ONU tidak terbaca di OLT: cek listrik ONT, lalu kabel fiber ke pelanggan.")
    else:
        for nama, nilai in (("OLT RX", o.olt_rx_dbm), ("ONU RX", o.onu_rx_dbm)):
            if nilai is None:
                continue
            st = classify_rx_power(nilai)
            if st == "CRITICAL":
                skor += 3
                redaman_kritis = True
                alasan.append(f"Redaman {nama} {nilai:.1f} dBm (kritis)")
            elif st == "WARNING":
                skor += 1
                redaman_agak = True
                alasan.append(f"Redaman {nama} {nilai:.1f} dBm (mendekati batas)")
        if redaman_kritis:
            saran.append("Redaman kritis, perlu dikunjungi: bersihkan dan cek konektor, periksa kabel drop yang tertekuk atau terjepit.")
        elif redaman_agak:
            saran.append("Redaman agak tinggi tapi masih bekerja: tidak mendesak, bersihkan konektor saat berkunjung berikutnya.")

    # --- 2. Kestabilan sinyal (selisih persentil 5-95, kebal terhadap satu titik nyasar) ---
    nilai_olt = [
        r["olt_rx_dbm"] for r in sinyal_rows
        if r["olt_rx_dbm"] is not None and -45.0 < r["olt_rx_dbm"] < -5.0
    ]
    sinyal_min = sinyal_max = sinyal_rata = None
    if nilai_olt:
        sinyal_min, sinyal_max = min(nilai_olt), max(nilai_olt)
        sinyal_rata = sum(nilai_olt) / len(nilai_olt)
        if len(nilai_olt) >= 12:
            urut = sorted(nilai_olt)
            n = len(urut)
            sebaran = urut[int(n * 0.95) - 1] - urut[int(n * 0.05)]
            if sebaran >= 2.0:
                skor += 2 if sebaran >= 4.0 else 1
                alasan.append(f"Sinyal naik-turun {sebaran:.1f} dB")
                saran.append("Sinyal tidak stabil: curigai konektor longgar atau kabel yang bergoyang.")

    # --- 3. Reconnect PPPoE (uptime yang tiba-tiba turun) ---
    uptimes = [r["uptime_seconds"] for r in pakai_rows if r["uptime_seconds"] is not None]
    reconnect = sum(1 for a, b in zip(uptimes, uptimes[1:]) if b < a)
    if reconnect >= 5:
        skor += 3
    elif reconnect >= 2:
        skor += 1
    if reconnect >= 2:
        alasan.append(f"Reconnect {reconnect}x")
        if not redaman_kritis:
            if ev_baru and "dying" in ev:
                saran.append(f"ONT kehilangan listrik pada {ev_waktu} (dying-gasp): cek adaptor, stop kontak, dan kabel adaptor yang longgar.")
            else:
                saran.append("Sinyal fiber bagus tapi sering reconnect: cek adaptor ONT, kabel LAN, atau router pelanggan.")
            saran_putus = True

    # --- 4. Ketersediaan ---
    tersedia = None
    if username and pakai_rows and total_snap:
        tersedia = min(100.0, len(pakai_rows) * 100.0 / total_snap)
        if tersedia < 90:
            skor += 3
            alasan.append(f"Tersedia hanya {tersedia:.0f}%")
        elif tersedia < 97:
            skor += 1
            alasan.append(f"Tersedia {tersedia:.0f}%")
        if tersedia < 97 and not saran_putus and not redaman_kritis:
            if ev_baru and "dying" in ev:
                saran.append(f"Sering mati: ONT kehilangan listrik pada {ev_waktu} (dying-gasp). Cek adaptor, stop kontak, dan kabel adaptor yang longgar.")
            else:
                saran.append("Sering terputus: cek listrik pelanggan (adaptor dan stop kontak), kabel fiber, dan konektor.")
            saran_putus = True

    # --- 5. Hardware ONT ---
    if o.online and o.hardware_status == "PERLU_CEK":
        skor += 2
        alasan.extend(o.hardware_notes or ["Hardware ONT perlu dicek"])
        saran.append("Perangkat ONT atau adaptor mulai bermasalah: jadwalkan pengecekan atau penggantian.")

    # --- 6. ONU hidup tapi PPPoE tidak aktif ---
    if o.online and not o.pppoe_username:
        skor += 2
        alasan.append("ONU online tapi tidak ada PPPoE aktif")
        saran.append("Cek pengaturan PPPoE di router pelanggan (username dan password), atau apakah pelanggan sedang diisolir.")

    label = "BERMASALAH" if skor >= 3 else ("PANTAU" if skor >= 1 else "SEHAT")
    return {
        "label": label, "skor": skor, "alasan": alasan, "saran": saran,
        "reconnect": reconnect, "event_24j": ev_baru,
        "tersedia_persen": None if tersedia is None else round(tersedia, 1),
        "sinyal_min": sinyal_min, "sinyal_max": sinyal_max,
        "sinyal_rata": None if sinyal_rata is None else round(sinyal_rata, 2),
        "grafik": nilai_olt[::max(1, len(nilai_olt) // 48)][:48],
    }
@app.get("/api/riwayat-putus/{username}")
def get_riwayat_putus(username: str, jam: int = 24):
    """Riwayat putus-sambung satu pelanggan dari catatan 5 menit.
    'kembali' akurat (dihitung dari uptime sesi). 'mulai' diperkirakan:
    paling lambat selisih satu interval catatan (5 menit)."""
    jam = max(1, min(jam, 168))
    sekarang = datetime.now()
    batas = (sekarang - timedelta(hours=jam)).isoformat(timespec="seconds")

    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
    try:
        semua_ts = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT ts FROM usage_history WHERE ts >= ? ORDER BY ts", (batas,)
            )
        ]
        hadir = {
            r[0]: r[1] for r in conn.execute(
                "SELECT ts, uptime_seconds FROM usage_history WHERE username = ? AND ts >= ?",
                (username, batas),
            )
        }
    finally:
        conn.close()

    def _waktu(teks):
        return datetime.fromisoformat(teks)

    kejadian = []
    prev_ts = None
    prev_uptime = None
    hilang = False

    for ts in semua_ts:
        if ts in hadir:
            up = hadir[ts]
            kembali = _waktu(ts)
            if up is not None:
                mulai_sesi = kembali - timedelta(seconds=up)
                if prev_ts is None or mulai_sesi > _waktu(prev_ts):
                    kembali = mulai_sesi
            if hilang:
                mulai = _waktu(prev_ts) if prev_ts else None
                durasi = round((kembali - mulai).total_seconds() / 60) if mulai else None
                kejadian.append({
                    "jenis": "putus",
                    "mulai": mulai.strftime("%Y-%m-%d %H:%M:%S") if mulai else None,
                    "kembali": kembali.strftime("%Y-%m-%d %H:%M:%S"),
                    "durasi_menit": durasi, "berlangsung": False,
                })
                hilang = False
            elif prev_uptime is not None and up is not None and up < prev_uptime:
                kejadian.append({
                    "jenis": "putus_singkat", "mulai": None,
                    "kembali": kembali.strftime("%Y-%m-%d %H:%M:%S"),
                    "durasi_menit": None, "berlangsung": False,
                })
            prev_ts = ts
            prev_uptime = up
        else:
            hilang = True

    if hilang:
        mulai = _waktu(prev_ts) if prev_ts else None
        kejadian.append({
            "jenis": "putus",
            "mulai": mulai.strftime("%Y-%m-%d %H:%M:%S") if mulai else None,
            "kembali": None,
            "durasi_menit": round((sekarang - mulai).total_seconds() / 60) if mulai else None,
            "berlangsung": True,
        })

    kejadian.sort(key=lambda k: k["kembali"] or "9999", reverse=True)
    total = sum(k["durasi_menit"] or 0 for k in kejadian if k["jenis"] == "putus")
    return {"username": username, "jam": jam, "total_putus_menit": total, "kejadian": kejadian}
_putus_aktif: dict = {}      # username -> {"mulai": datetime, "alert": bool, "awal": bool}
_putus_siap = False          # False sampai siklus pertama selesai
_pernah_aktif: set = set()   # pelanggan yang pernah terlihat aktif
_peta_onu: dict = {}         # username -> (pon, onu_id) terakhir diketahui


def _siapkan_putus() -> None:
    """Muat data awal dari riwayat supaya pemantauan tidak mulai dari nol."""
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
    try:
        for (u,) in conn.execute("SELECT DISTINCT username FROM usage_history"):
            _pernah_aktif.add(u)
        for pon, onu_id, u, _t in conn.execute(
            "SELECT pon, onu_id, pppoe_username, MAX(ts) AS t FROM onu_history "
            "WHERE pppoe_username IS NOT NULL AND pppoe_username != '' "
            "GROUP BY pon, onu_id ORDER BY t"
        ):
            _peta_onu[u] = (pon, onu_id)
    finally:
        conn.close()


def _data_pelanggan() -> dict:
    conn = _billing_conn()
    try:
        return {
            r["username"]: dict(r)
            for r in conn.execute("SELECT username, nama, alamat, no_hp FROM pelanggan WHERE status = 'aktif'")
        }
    finally:
        conn.close()


def _dugaan_putus(username, onu_by_key, mulai):
    """Kembalikan (teks dugaan, kategori, 'PON x / ONU y')."""
    key = _peta_onu.get(username)
    o = onu_by_key.get(key) if key else None
    if o is None:
        return "ONU belum teridentifikasi, cek manual", "tak_tahu", "-"
    lokasi = f"PON {o.pon} / ONU {o.onu_id}"
    if o.online:
        return ("ONU masih hidup tapi PPPoE tidak aktif: cek router, kabel LAN pelanggan, "
                "atau status isolir"), "pppoe", lokasi
    ev = (o.last_event or "").strip().strip('"').lower()
    ev_waktu = (o.last_event_time or "").strip().strip('"')
    relevan = False
    try:
        relevan = datetime.strptime(ev_waktu, "%Y-%m-%d %H:%M:%S") >= mulai - timedelta(minutes=15)
    except ValueError:
        pass
    if relevan and "dying" in ev:
        return (f"ONU offline, dying-gasp pukul {ev_waktu[11:16]}: dugaan listrik atau "
                "adaptor pelanggan mati"), "dying", lokasi
    if relevan and "los" in ev:
        return (f"ONU offline, LOS pukul {ev_waktu[11:16]}: dugaan kabel fiber putus atau "
                "konektor lepas"), "los", lokasi
    return "ONU offline: cek listrik ONT, lalu kabel fiber", "offline", lokasi


_ALASAN_PPP = [
    ("peer is not responding", "ONU tidak menjawab MikroTik (ONU mati/hang, listrik padam, atau jalur fiber terganggu)"),
    ("hungup", "ONU sendiri yang memutus (restart, dial ulang, atau kabel LAN/WAN dicabut)"),
    ("hung up", "ONU sendiri yang memutus (restart, dial ulang, atau kabel LAN/WAN dicabut)"),
    ("user request", "ONU sendiri yang memutus (restart atau dial ulang)"),
    ("session timeout", "batas waktu sesi habis (pengaturan session-timeout MikroTik)"),
    ("admin", "diputus dari MikroTik (admin, isolir, atau ubah profil)"),
    ("no response", "ONU tidak menjawab MikroTik"),
]


def _alasan_putus_mikrotik(username: str, mulai: datetime, sekarang: datetime) -> Optional[str]:
    """Alasan putus menurut log PPPoE MikroTik (sudah tersimpan di tabel event) di sekitar waktu putus."""
    try:
        conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
        try:
            r = conn.execute(
                "SELECT detail FROM event WHERE kategori='pppoe' AND pelanggan=? AND judul='PPPoE terputus' "
                "AND waktu BETWEEN ? AND ? ORDER BY waktu DESC LIMIT 1",
                (username, (mulai - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"),
                 sekarang.strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not r:
        return None
    pesan = re.sub(r"\s*\[[^\]]*\]$", "", r[0] or "")
    rendah = pesan.lower()
    for kunci, arti in _ALASAN_PPP:
        if kunci in rendah:
            return arti
    m = re.search(r"terminating\.*\s*-\s*(.+)$", pesan)
    return m.group(1).strip() if m else None


def _jam_senyap(waktu: datetime) -> bool:
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", str(CONFIG.get("PUTUS_SENYAP_MALAM") or ""))
    if not m:
        return False
    a, b, h = int(m.group(1)) % 24, int(m.group(2)) % 24, waktu.hour
    return (a <= h < b) if a < b else (h >= a or h < b)


_listrik_malam: list = []    # (username, mulai) listrik ONU mati di jam senyap, untuk laporan harian


def _ringkasan_listrik_malam(data: Optional[dict] = None) -> list[str]:
    if not _listrik_malam:
        return []
    data = data if data is not None else _data_pelanggan()
    baris = [f"🔌 <b>Listrik ONU mati di jam malam ({len(_listrik_malam)})</b> — tidak dikabarkan satu per satu:"]
    for u, mulai in _listrik_malam[:10]:
        baris.append(f"- {_esc(data.get(u, {}).get('nama') or u)} ±{mulai.strftime('%H:%M')}")
    if len(_listrik_malam) > 10:
        baris.append(f"… dan {len(_listrik_malam) - 10} lainnya")
    return baris


def _daftar_nama(users, data, batas=15):
    nama = [_esc(data.get(u, {}).get("nama") or u) for u in users[:batas]]
    sisa = len(users) - len(nama)
    return "\n".join(f"• {n}" for n in nama) + (f"\n… dan {sisa} lainnya" if sisa > 0 else "")


def _pesan_massal(users, data, onu_by_key, sekarang, tunggu):
    kategori = {}
    pon = {}
    for u in users:
        mulai_u = _putus_aktif.get(u, {}).get("mulai", sekarang)
        _teks, kat, lokasi = _dugaan_putus(u, onu_by_key, mulai_u)
        kategori[kat] = kategori.get(kat, 0) + 1
        if lokasi != "-":
            p = lokasi.split(" / ")[0]
            pon[p] = pon.get(p, 0) + 1
    utama = max(kategori, key=kategori.get)
    dugaan = {
        "dying": "banyak ONT kehilangan listrik: kemungkinan listrik area padam",
        "los": "banyak sinyal fiber hilang (LOS): kemungkinan kabel feeder atau splitter putus",
        "pppoe": "ONU masih hidup tapi PPPoE tidak aktif serentak: cek MikroTik atau PPPoE server",
        "offline": "banyak ONU offline: cek listrik area dan jalur fiber",
    }.get(utama, "penyebab belum bisa dipastikan, cek manual")
    pon_txt = ", ".join(f"{p} ({n})" for p, n in sorted(pon.items())) or "-"
    return (
        f"🔴 <b>GANGGUAN MASSAL</b>\n"
        f"{len(users)} pelanggan putus lebih dari {tunggu} menit:\n"
        f"{_daftar_nama(users, data)}\n\n"
        f"Dugaan: {dugaan}\nPON terdampak: {pon_txt}\n"
        f"Waktu: {sekarang.strftime('%H:%M')}"
    )


def proses_putus_pelanggan(pppoe_users, mikrotik_error, onu_list, sekarang=None) -> None:
    """Peringatan putus dengan masa tunggu. Putus yang pulih sebelum masa tunggu
    habis tidak dikirim (tetap tercatat di riwayat). Kembali konek dikabarkan
    hanya kalau peringatan putusnya sudah terkirim."""
    global _putus_siap
    if mikrotik_error is not None:
        return  # data MikroTik tidak bisa dipercaya: jangan anggap semua pelanggan putus
    sekarang = sekarang or datetime.now()
    tunggu = CONFIG["PUTUS_TUNGGU_MENIT"]
    massal_min = CONFIG["PUTUS_MASSAL_MIN"]

    if not _putus_siap:
        _siapkan_putus()
    aktif = {u.username: u for u in pppoe_users}
    _pernah_aktif.update(aktif)
    for o in onu_list:
        if o.pppoe_username:
            _peta_onu[o.pppoe_username] = (o.pon, o.onu_id)
    onu_by_key = {(o.pon, o.onu_id): o for o in onu_list}

    data = _data_pelanggan()
    dipantau = {u for u in data if u in _pernah_aktif}
    putus_sekarang = [u for u in sorted(dipantau) if u not in aktif]

    # Siklus pertama setelah program dinyalakan: catat saja, satu pesan ringkasan
    if not _putus_siap:
        for u in putus_sekarang:
            _putus_aktif[u] = {"mulai": sekarang, "alert": True, "awal": True}
        if putus_sekarang:
            send_telegram_message(
                f"ℹ️ <b>Pemantauan dimulai</b>\n{len(putus_sekarang)} pelanggan sedang tidak terhubung:\n"
                f"{_daftar_nama(putus_sekarang, data)}"
            )
        _putus_siap = True
        return

    # Putus baru
    for u in putus_sekarang:
        if u not in _putus_aktif:
            _putus_aktif[u] = {"mulai": sekarang, "alert": False, "awal": False}

    # Kembali konek
    kembali = []
    for u in list(_putus_aktif):
        if u in aktif or u not in dipantau:
            st = _putus_aktif.pop(u)
            if st["alert"] is True and u in aktif:
                up = aktif[u].uptime_seconds
                waktu = sekarang - timedelta(seconds=up) if up is not None else sekarang
                kembali.append((u, st, max(waktu, st["mulai"])))
    if len(kembali) >= massal_min:
        send_telegram_message(
            f"✅ <b>KEMBALI MASSAL</b>\n{len(kembali)} pelanggan terhubung lagi:\n"
            f"{_daftar_nama([k[0] for k in kembali], data)}\nWaktu: {sekarang.strftime('%H:%M')}"
        )
    else:
        for u, st, waktu in kembali:
            d = data.get(u, {})
            lama = round((waktu - st["mulai"]).total_seconds() / 60)
            awal = "sebelum pemantauan dimulai ulang" if st["awal"] else f"±{st['mulai'].strftime('%H:%M')}"
            send_telegram_message(
                f"✅ <b>KEMBALI</b>\n{_esc(d.get('nama') or u)} ({_esc(u)})\n"
                f"Putus: {awal} → kembali {waktu.strftime('%H:%M')}"
                + ("" if st["awal"] else f" (±{lama} menit)"),
                senyap=st.get("jenis") == "dying",
            )

    # Peringatan putus yang masa tunggunya habis
    belum = [u for u, st in _putus_aktif.items() if not st["alert"]]
    jatuh = [
        u for u in belum
        if sekarang - _putus_aktif[u]["mulai"] >= timedelta(minutes=tunggu)
    ]
    if jatuh:
        if len(belum) >= massal_min:
            send_telegram_message(_pesan_massal(belum, data, onu_by_key, sekarang, tunggu))
            for u in belum:
                _putus_aktif[u]["alert"] = True
            logger.warning(f"Alert GANGGUAN MASSAL terkirim ({len(belum)} pelanggan)")
        else:
            for u in jatuh:
                d = data.get(u, {})
                st = _putus_aktif[u]
                mulai = st["mulai"]
                dugaan, kat, lokasi = _dugaan_putus(u, onu_by_key, mulai)
                st["jenis"] = kat
                if kat == "dying" and _jam_senyap(mulai):
                    # ONU dimatikan/listrik rumah padam di malam hari: masuk laporan pagi saja.
                    st["alert"] = "senyap"
                    _listrik_malam.append((u, mulai))
                    continue
                alasan = _alasan_putus_mikrotik(u, mulai, sekarang)
                durasi = round((sekarang - mulai).total_seconds() / 60)
                if kat == "dying":
                    judul = (f"🔌 <b>Listrik ONU mati</b> (sudah {durasi} menit)\n"
                             "<i>Biasanya ONU dicabut/dimatikan pelanggan atau listrik rumah padam, "
                             "bukan gangguan jaringan.</i>")
                else:
                    judul = f"🔴 <b>PUTUS lebih dari {tunggu} menit</b>"
                send_telegram_message(
                    f"{judul}\n"
                    f"{_esc(d.get('nama') or u)} ({_esc(u)})\n"
                    f"Mulai ±{mulai.strftime('%H:%M')} (sudah {durasi} menit)\n"
                    f"Dugaan: {dugaan}\n"
                    + (f"Log MikroTik: {_esc(alasan)}\n" if alasan else "")
                    + f"Alamat: {_esc(d.get('alamat') or '-')}\nHP: {_esc(d.get('no_hp') or '-')}\n"
                    f"{lokasi}",
                    senyap=kat == "dying",
                )
                st["alert"] = True
                logger.warning(f"Alert PUTUS terkirim untuk {u}")
_BLN_PANJANG = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
                "Agustus", "September", "Oktober", "November", "Desember"]


def _rp(n) -> str:
    n = int(n)
    return ("-" if n < 0 else "") + "Rp " + f"{abs(n):,}".replace(",", ".")


def _nama_periode(p: str) -> str:
    tahun, bulan = p.split("-")
    return f"{_BLN_PANJANG[int(bulan) - 1]} {tahun}"


def proses_tagihan_harian(sekarang=None) -> None:
    """Dipanggil tiap siklus pemantauan, tapi hanya bekerja di hari yang perlu
    dan sekali per hari (dicatat di tabel otomasi_log, jadi aman kalau program
    dinyalakan ulang):
    1. Hari terakhir bulan: buat tagihan bulan depan lalu kabari lewat Telegram.
       (Kalau terlewat, tanggal 1-3 dibuatkan tagihan bulan berjalan.)
    2. Pada tanggal di TAGIHAN_PENGINGAT_HARI: kirim daftar yang belum bayar."""
    sekarang = sekarang or datetime.now()
    if sekarang.hour < CONFIG["TAGIHAN_JAM"]:
        return
    akhir_bulan = (sekarang + timedelta(days=1)).day == 1
    if not (akhir_bulan or sekarang.day <= 3 or sekarang.day in CONFIG["TAGIHAN_PENGINGAT_HARI"]):
        return

    hari = sekarang.strftime("%Y-%m-%d")
    periode_ini = sekarang.strftime("%Y-%m")
    periode_depan = (sekarang.replace(day=1) + timedelta(days=32)).strftime("%Y-%m")
    jatuh_tempo = int(CONFIG["JATUH_TEMPO_TANGGAL"])

    conn = _billing_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS otomasi_log ("
            "tanggal TEXT NOT NULL, jenis TEXT NOT NULL, PRIMARY KEY (tanggal, jenis))"
        )
        conn.commit()

        def sudah(jenis):
            return conn.execute(
                "SELECT 1 FROM otomasi_log WHERE tanggal=? AND jenis=?", (hari, jenis)
            ).fetchone() is not None

        def tandai(jenis):
            conn.execute("INSERT OR IGNORE INTO otomasi_log VALUES (?,?)", (hari, jenis))
            conn.commit()

        # --- 1. Buat tagihan ---
        periode_buat, terlambat = None, False
        if akhir_bulan:
            periode_buat = periode_depan
        elif sekarang.day <= 3:
            ada = conn.execute(
                "SELECT COUNT(*) FROM tagihan WHERE periode=?", (periode_ini,)
            ).fetchone()[0]
            if ada == 0:
                periode_buat, terlambat = periode_ini, True

        if periode_buat and not sudah("buat"):
            hasil = buat_tagihan(periode_buat)
            catat_aktivitas("sistem", "tagihan_buat",
                            f"otomatis periode {periode_buat}: {hasil['baru']} baru, {hasil['sudah_ada']} sudah ada")
            jumlah, total = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(jumlah),0) FROM tagihan WHERE periode=?",
                (periode_buat,),
            ).fetchone()
            tanpa_hp = conn.execute(
                "SELECT COUNT(*) FROM tagihan t JOIN pelanggan p ON p.username=t.username "
                "WHERE t.periode=? AND (p.no_hp IS NULL OR p.no_hp='')",
                (periode_buat,),
            ).fetchone()[0]
            catatan = []
            if hasil.get("tanpa_paket"):
                catatan.append(f"{hasil['tanpa_paket']} pelanggan belum punya paket (tidak ditagih)")
            if tanpa_hp:
                catatan.append(f"{tanpa_hp} pelanggan belum punya nomor HP")
            if hasil["sudah_ada"] == 0:
                status = "dibuat (terlambat)" if terlambat else "dibuat"
            elif hasil["baru"] > 0:
                status = f"diperbarui (+{hasil['baru']} tagihan baru)"
            else:
                status = "sudah siap"
            send_telegram_message(
                f"🧾 <b>Tagihan {_nama_periode(periode_buat)} {status}</b>\n"
                f"{jumlah} tagihan, total {_rp(total)}\n"
                f"Jatuh tempo tanggal {jatuh_tempo}\n"
                + ("\n".join(f"• {c}" for c in catatan) + "\n" if catatan else "")
                + "Saatnya kirim pemberitahuan: dashboard → Billing → Ingatkan."
            )
            tandai("buat")
            logger.warning(f"Tagihan {periode_buat} dibuat otomatis: {hasil}")

        # --- 2. Pengingat belum bayar untuk pengurus ---
        if sekarang.day in CONFIG["TAGIHAN_PENGINGAT_HARI"] and not sudah("ingatkan"):
            rows = conn.execute(
                "SELECT p.nama, p.username, t.jumlah FROM tagihan t "
                "JOIN pelanggan p ON p.username = t.username "
                "WHERE t.periode=? AND t.status='belum' ORDER BY p.nama COLLATE NOCASE",
                (periode_ini,),
            ).fetchall()
            selisih = jatuh_tempo - sekarang.day
            if rows:
                if selisih > 0:
                    judul = f"⏰ <b>Jatuh tempo {selisih} hari lagi</b>"
                elif selisih == 0:
                    judul = "⏰ <b>Hari ini jatuh tempo</b>"
                else:
                    judul = f"⚠️ <b>Lewat jatuh tempo {-selisih} hari</b>"
                total = sum(r["jumlah"] for r in rows)
                nama = [f"• {_esc(r['nama'] or r['username'])} ({_rp(r['jumlah'])})" for r in rows[:15]]
                sisa = len(rows) - len(nama)
                send_telegram_message(
                    f"{judul}\nTagihan {_nama_periode(periode_ini)}: {len(rows)} belum bayar, "
                    f"total {_rp(total)}\n" + "\n".join(nama)
                    + (f"\n… dan {sisa} lainnya" if sisa > 0 else "")
                    + "\nBuka dashboard → Billing untuk kirim pengingat WhatsApp."
                )
            elif sekarang.day >= jatuh_tempo:
                send_telegram_message(f"✅ Semua tagihan {_nama_periode(periode_ini)} sudah lunas.")
            tandai("ingatkan")
    finally:
        conn.close()
def _siapkan_tabel_isolir() -> None:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS isolir ("
            "username TEXT PRIMARY KEY, profile_asal TEXT, "
            "sejak TEXT NOT NULL, otomatis INTEGER NOT NULL DEFAULT 1)"
        )
        # buka_saat_lunas = 0: isolir manual tanpa tunggakan (mis. pelanggan gratis),
        # hanya bisa dibuka manual — otomasi tidak boleh membukanya.
        kolom = [r[1] for r in conn.execute("PRAGMA table_info(isolir)").fetchall()]
        if "buka_saat_lunas" not in kolom:
            conn.execute("ALTER TABLE isolir ADD COLUMN buka_saat_lunas INTEGER NOT NULL DEFAULT 1")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS otomasi_log ("
            "tanggal TEXT NOT NULL, jenis TEXT NOT NULL, PRIMARY KEY (tanggal, jenis))"
        )
        conn.commit()
    finally:
        conn.close()


_siapkan_tabel_isolir()
_isolir_gagal_terakhir = None   # supaya pesan gagal tidak dikirim tiap 2 menit


def _mikrotik_pool():
    return routeros_api.RouterOsApiPool(
        CONFIG["MIKROTIK_HOST"],
        username=CONFIG["MIKROTIK_USER"],
        password=CONFIG["MIKROTIK_PASSWORD"],
        port=CONFIG["MIKROTIK_PORT"],
        use_ssl=CONFIG["MIKROTIK_USE_SSL"],
        ssl_verify=False,
        plaintext_login=True,
    )


def _ganti_profil_mikrotik(perubahan: dict, hanya_dari=None) -> dict:
    """perubahan: {username: profil_baru}. Mengubah profil PPP secret lalu memutus
    sesi aktifnya supaya langsung berlaku. hanya_dari: kalau diisi, profil baru
    hanya dipasang bila profil sekarang sama dengan nilai ini.
    Hasil: {username: (berhasil, profil_lama atau pesan_error)}."""
    pool = _mikrotik_pool()
    hasil = {}
    try:
        api = pool.get_api()
        secret = api.get_resource("/ppp/secret")
        aktif = api.get_resource("/ppp/active")
        for u, baru in perubahan.items():
            try:
                rows = secret.get(name=u)
                if not rows:
                    hasil[u] = (False, "secret tidak ditemukan di MikroTik")
                    continue
                lama = rows[0].get("profile")
                if hanya_dari is not None and lama != hanya_dari:
                    hasil[u] = (True, lama)      # sudah bukan profil isolir, biarkan
                    continue
                if lama != baru:
                    secret.set(id=rows[0]["id"], profile=baru)
                for sesi in aktif.get(name=u):
                    aktif.remove(id=sesi["id"])
                hasil[u] = (True, lama)
            except Exception as e:
                hasil[u] = (False, str(e))
    finally:
        pool.disconnect()
    return hasil


def _profil_dari_paket(conn, username):
    r = conn.execute(
        "SELECT k.kecepatan_mbps FROM pelanggan p JOIN paket k ON k.id = p.paket_id "
        "WHERE p.username = ?", (username,)
    ).fetchone()
    return f"paket-{int(r['kecepatan_mbps'])}mbps" if r else None


def isolir_pelanggan(daftar, otomatis=True) -> dict:
    """Isolir sekumpulan pelanggan. Hasil: {username: (berhasil, keterangan)}."""
    if not daftar:
        return {}
    profil_isolir = CONFIG["ISOLIR_PROFILE"]
    try:
        hasil = _ganti_profil_mikrotik({u: profil_isolir for u in daftar})
    except Exception as e:
        return {u: (False, f"MikroTik tidak terbaca: {e}") for u in daftar}
    conn = _billing_conn()
    try:
        punya_tunggakan = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT t.username FROM tagihan t JOIN pelanggan p ON p.username = t.username "
                "WHERE t.status = 'belum' AND p.gratis = 0"
            )
        }
        for u, (ok, ket) in hasil.items():
            if ok:
                asal = ket if ket and ket != profil_isolir else None
                # Isolir otomatis selalu karena tunggakan. Isolir manual hanya dibuka
                # otomatis kalau saat itu pelanggan memang punya tagihan belum bayar.
                buka_saat_lunas = 1 if (otomatis or u in punya_tunggakan) else 0
                conn.execute(
                    "INSERT OR IGNORE INTO isolir (username, profile_asal, sejak, otomatis, buka_saat_lunas) "
                    "VALUES (?,?,?,?,?)",
                    (u, asal, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 1 if otomatis else 0,
                     buka_saat_lunas),
                )
        conn.commit()
    finally:
        conn.close()
    return hasil


def buka_isolir_pelanggan(daftar) -> dict:
    """Kembalikan profil asli pelanggan yang terisolir."""
    conn = _billing_conn()
    try:
        asal = {r["username"]: r["profile_asal"] for r in conn.execute("SELECT username, profile_asal FROM isolir")}
        rencana, hasil = {}, {}
        for u in daftar:
            if u not in asal:
                continue
            profil = asal[u] or _profil_dari_paket(conn, u)
            if profil:
                rencana[u] = profil
            else:
                hasil[u] = (False, "profil asal tidak diketahui dan pelanggan belum punya paket")
    finally:
        conn.close()
    if rencana:
        try:
            hasil.update(_ganti_profil_mikrotik(rencana, hanya_dari=CONFIG["ISOLIR_PROFILE"]))
        except Exception as e:
            hasil.update({u: (False, f"MikroTik tidak terbaca: {e}") for u in rencana})
        conn = _billing_conn()
        try:
            for u, (ok, _ket) in hasil.items():
                if ok:
                    conn.execute("DELETE FROM isolir WHERE username = ?", (u,))
            conn.commit()
        finally:
            conn.close()
    return hasil


def _batas_menunggak(sekarang) -> str:
    return (sekarang - timedelta(days=CONFIG["ISOLIR_TOLERANSI_HARI"])).strftime("%Y-%m-%d")


def _kabar_gagal(teks: str, sekarang) -> None:
    """Pesan gagal dibatasi satu kali per jam."""
    global _isolir_gagal_terakhir
    if _isolir_gagal_terakhir is None or sekarang - _isolir_gagal_terakhir >= timedelta(hours=1):
        send_telegram_message(teks)
        _isolir_gagal_terakhir = sekarang


def proses_isolir(sekarang=None) -> None:
    """Dipanggil tiap siklus pemantauan.
    A. Tiap siklus: pelanggan terisolir yang tagihannya sudah lunas dibuka isolirnya.
    B. Sekali per hari (setelah ISOLIR_JAM): pelanggan yang tagihannya lewat jatuh
       tempo diisolir. Mode "simulasi" hanya mengirim daftarnya ke Telegram."""
    sekarang = sekarang or datetime.now()
    batas = _batas_menunggak(sekarang)

    # --- A. Buka isolir yang sudah lunas ---
    # Isolir otomatis dibuka begitu tidak ada lagi tagihan lewat jatuh tempo.
    # Isolir manual dibuka begitu semua tagihan lunas (termasuk yang belum jatuh
    # tempo). Isolir manual tanpa tunggakan (buka_saat_lunas=0) tidak disentuh.
    conn = _billing_conn()
    try:
        terisolir = conn.execute(
            "SELECT username, otomatis FROM isolir WHERE buka_saat_lunas = 1"
        ).fetchall()
        nama = {r["username"]: r["nama"] for r in conn.execute("SELECT username, nama FROM pelanggan")}
        layak = []
        if terisolir:
            masih_telat, masih_belum = set(), set()
            for r in conn.execute(
                "SELECT DISTINCT t.username, t.jatuh_tempo < ? AS telat FROM tagihan t "
                "JOIN pelanggan p ON p.username = t.username "
                "WHERE t.status = 'belum' AND p.gratis = 0", (batas,)
            ):
                masih_belum.add(r[0])
                if r[1]:
                    masih_telat.add(r[0])
            layak = [
                r["username"] for r in terisolir
                if r["username"] not in (masih_telat if r["otomatis"] else masih_belum)
            ]
    finally:
        conn.close()
    if layak:
        hasil = buka_isolir_pelanggan(layak)
        dibuka = [u for u, (ok, _k) in hasil.items() if ok]
        gagal = [u for u, (ok, _k) in hasil.items() if not ok]
        if dibuka:
            send_telegram_message(
                "🔓 <b>Isolir dibuka</b> (tagihan lunas atau pelanggan dibebaskan)\n"
                + "\n".join(f"• {_esc(nama.get(u) or u)}" for u in dibuka)
            )
            logger.warning(f"Isolir dibuka: {dibuka}")
            catat_aktivitas("sistem", "isolir_buka", "otomatis (lunas): " + ", ".join(dibuka))
        if gagal:
            _kabar_gagal(
                "⚠️ <b>Gagal membuka isolir</b>\n"
                + "\n".join(f"• {_esc(nama.get(u) or u)}: {_esc(str(hasil[u][1]))}" for u in gagal)
                + "\nAkan dicoba lagi otomatis.", sekarang
            )

    # --- B. Isolir yang menunggak, sekali per hari ---
    if sekarang.hour < CONFIG["ISOLIR_JAM"]:
        return
    hari = sekarang.strftime("%Y-%m-%d")
    conn = _billing_conn()
    try:
        if conn.execute("SELECT 1 FROM otomasi_log WHERE tanggal=? AND jenis='isolir'", (hari,)).fetchone():
            return
        kandidat = conn.execute(
            "SELECT p.username, p.nama, COUNT(*) AS n, SUM(t.jumlah) AS total, MIN(t.jatuh_tempo) AS tertua "
            "FROM tagihan t JOIN pelanggan p ON p.username = t.username "
            "WHERE t.status = 'belum' AND t.jatuh_tempo < ? AND p.gratis = 0 AND p.status = 'aktif' "
            "AND p.username NOT IN (SELECT username FROM isolir) "
            "GROUP BY p.username ORDER BY p.nama COLLATE NOCASE", (batas,)
        ).fetchall()

        def tandai():
            conn.execute("INSERT OR IGNORE INTO otomasi_log VALUES (?,?)", (hari, "isolir"))
            conn.commit()

        if not kandidat:
            tandai()
            return
        daftar_teks = "\n".join(
            f"• {_esc(r['nama'] or r['username'])}: {_rp(r['total'])}, jatuh tempo {r['tertua'][8:10]}/{r['tertua'][5:7]}"
            for r in kandidat[:15]
        ) + (f"\n… dan {len(kandidat) - 15} lainnya" if len(kandidat) > 15 else "")

        if CONFIG["ISOLIR_MODE"] != "aktif":
            send_telegram_message(
                f"🧪 <b>SIMULASI isolir</b>: {len(kandidat)} pelanggan akan diisolir\n{daftar_teks}\n"
                "Mode simulasi aktif, belum ada yang diisolir."
            )
            tandai()
            return
        if len(kandidat) > CONFIG["ISOLIR_MAKS_SEKALI"]:
            send_telegram_message(
                f"⛔ <b>Isolir ditahan</b>: {len(kandidat)} pelanggan menunggak, melebihi batas "
                f"{CONFIG['ISOLIR_MAKS_SEKALI']} sekali jalan.\n{daftar_teks}\n"
                "Cek dulu apakah ada yang sudah bayar tapi belum ditandai lunas. "
                "Isolir manual per pelanggan lewat dashboard, atau naikkan ISOLIR_MAKS_SEKALI."
            )
            tandai()
            return

        hasil = isolir_pelanggan([r["username"] for r in kandidat], otomatis=True)
        ok = [r for r in kandidat if hasil.get(r["username"], (False,))[0]]
        gagal = [r for r in kandidat if not hasil.get(r["username"], (False,))[0]]
        if ok:
            send_telegram_message(
                f"🔒 <b>ISOLIR otomatis: {len(ok)} pelanggan</b>\nTagihan lewat jatuh tempo:\n"
                + "\n".join(
                    f"• {_esc(r['nama'] or r['username'])}: {_rp(r['total'])}, jatuh tempo {r['tertua'][8:10]}/{r['tertua'][5:7]}"
                    for r in ok
                )
                + "\nIsolir dibuka otomatis begitu tagihannya ditandai lunas di dashboard."
            )
            logger.warning(f"Isolir otomatis: {[r['username'] for r in ok]}")
            catat_aktivitas("sistem", "isolir", "otomatis (menunggak): " + ", ".join(r["username"] for r in ok))
        if gagal:
            _kabar_gagal(
                "⚠️ <b>Gagal mengisolir</b>\n"
                + "\n".join(f"• {_esc(r['nama'] or r['username'])}: {_esc(str(hasil.get(r['username'], (False, '?'))[1]))}" for r in gagal),
                sekarang,
            )
        if ok or not gagal:
            tandai()      # kalau semuanya gagal, dicoba lagi di siklus berikutnya
    finally:
        conn.close()


@app.get("/api/isolir")
def get_isolir():
    conn = _billing_conn()
    try:
        rows = conn.execute(
            "SELECT i.username, p.nama, i.sejak, i.otomatis, i.buka_saat_lunas FROM isolir i "
            "LEFT JOIN pelanggan p ON p.username = i.username ORDER BY i.sejak DESC"
        ).fetchall()
        return {"mode": CONFIG["ISOLIR_MODE"], "terisolir": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/isolir/{username}")
def isolir_manual(username: str, request: Request):
    conn = _billing_conn()
    try:
        ada = conn.execute("SELECT 1 FROM pelanggan WHERE username=?", (username,)).fetchone()
    finally:
        conn.close()
    if not ada:
        raise HTTPException(status_code=404, detail="Pelanggan tidak ditemukan.")
    ok, ket = isolir_pelanggan([username], otomatis=False)[username]
    if not ok:
        raise HTTPException(status_code=502, detail=f"Gagal mengisolir: {ket}")
    catat_aktivitas(_oleh(request), "isolir", f"manual: {username} (profil asal {ket or '-'})")
    return {"ok": True, "profil_asal": ket}


@app.post("/api/isolir/{username}/buka")
def buka_isolir_manual(username: str, request: Request):
    hasil = buka_isolir_pelanggan([username])
    if username not in hasil:
        raise HTTPException(status_code=404, detail="Pelanggan tidak sedang terisolir.")
    ok, ket = hasil[username]
    if not ok:
        raise HTTPException(status_code=502, detail=f"Gagal membuka isolir: {ket}")
    catat_aktivitas(_oleh(request), "isolir_buka", f"manual: {username}")
    return {"ok": True}
@app.get("/api/isolir-cek")
def cek_isolir():
    """Cek apakah profil isolir ada di MikroTik dan siapa yang sudah memakainya."""
    pool = _mikrotik_pool()
    try:
        api = pool.get_api()
        profil = [p.get("name") for p in api.get_resource("/ppp/profile").get()]
        secrets = api.get_resource("/ppp/secret").get()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"MikroTik tidak terbaca: {e}")
    finally:
        pool.disconnect()
    nama = CONFIG["ISOLIR_PROFILE"]
    return {
        "profil_isolir": nama,
        "ada_di_mikrotik": nama in profil,
        "profil_tersedia": sorted(profil),
        "pelanggan_berprofil_isolir": sorted(s.get("name") for s in secrets if s.get("profile") == nama),
    }
def _folder_backup() -> str:
    folder = os.path.join(_env("INDESO_DATA_DIR", BASE_DIR), CONFIG["BACKUP_FOLDER"])
    os.makedirs(folder, exist_ok=True)
    return folder


def _daftar_backup(folder: str) -> list:
    return sorted(f for f in os.listdir(folder) if re.fullmatch(r"billing-\d{4}-\d{2}-\d{2}\.db", f))


def buat_backup(sekarang=None) -> dict:
    """Salinan konsisten billing.db lewat API backup SQLite (aman walau database
    sedang dipakai), lalu diperiksa keutuhannya dan backup lama dibuang."""
    sekarang = sekarang or datetime.now()
    folder = _folder_backup()
    tujuan = os.path.join(folder, f"billing-{sekarang.strftime('%Y-%m-%d')}.db")
    sumber = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    salinan = sqlite3.connect(tujuan)
    try:
        sumber.backup(salinan)
        salinan.execute("PRAGMA journal_mode=DELETE")
    finally:
        salinan.close()
        sumber.close()

    cek = sqlite3.connect(tujuan)
    try:
        integritas = cek.execute("PRAGMA integrity_check").fetchone()[0]
        jumlah = {
            t: cek.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("pelanggan", "paket", "tagihan", "transaksi")
            if cek.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
        }
    finally:
        cek.close()

    ukuran_kb = round(os.path.getsize(tujuan) / 1024, 1)
    for lama in _daftar_backup(folder)[:-CONFIG["BACKUP_SIMPAN"]]:
        if os.path.join(folder, lama) != tujuan:    # berkas yang baru dibuat tidak pernah dihapus
            os.remove(os.path.join(folder, lama))
    return {"berkas": tujuan, "ukuran_kb": ukuran_kb, "integritas": integritas, "jumlah": jumlah}


def kirim_dokumen_telegram(path: str, caption: str, chat_id: Optional[str] = None, senyap: bool = False) -> bool:
    url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_BOT_TOKEN']}/sendDocument"
    data = {"chat_id": chat_id or CONFIG["TELEGRAM_CHAT_ID"], "caption": caption}
    if senyap:
        data["disable_notification"] = "true"
    try:
        with open(path, "rb") as f:
            r = requests.post(url, data=data, files={"document": f}, timeout=60)
    except requests.RequestException as e:
        # Pesan error requests memuat URL lengkap (berisi token bot): samarkan sebelum naik.
        raise RuntimeError(_samarkan(str(e))) from None
    return r.ok


def kirim_berkas_telegram(chat_id: str, nama: str, isi: bytes, caption: str = "") -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{CONFIG['TELEGRAM_BOT_TOKEN']}/sendDocument",
            data={"chat_id": chat_id, "caption": caption}, files={"document": (nama, isi)}, timeout=60,
        )
        return r.ok
    except Exception as e:
        logger.error(f"Gagal kirim berkas Telegram ke {chat_id}: {e}")
        return False


_backup_gagal_terakhir = None


def _hitung_nota_belum_backup() -> int:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        return conn.execute("SELECT COUNT(*) FROM nota WHERE id > ?",
                            (int(_status_get("backup_nota_terakhir_id", "0") or 0),)).fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _tujuan_backup() -> list[str]:
    """Backup berisi data keuangan & hash password: kirim ke tujuan laporan owner
    (grup internal owner / chat pribadi). Grup operasional hanya cadangan bila belum diisi."""
    return _chat_owner() or [str(CONFIG["TELEGRAM_CHAT_ID"])]


def _status_get(kunci: str, bawaan: str = "") -> str:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS status_sistem (kunci TEXT PRIMARY KEY, nilai TEXT NOT NULL)")
        r = conn.execute("SELECT nilai FROM status_sistem WHERE kunci = ?", (kunci,)).fetchone()
        return r[0] if r else bawaan
    finally:
        conn.close()


def _status_set(kunci: str, nilai: str) -> None:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"], timeout=15)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS status_sistem (kunci TEXT PRIMARY KEY, nilai TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO status_sistem (kunci, nilai) VALUES (?,?)", (kunci, nilai))
        conn.commit()
    finally:
        conn.close()


def _zip_nota(rows: list, tujuan_zip: str, bagian: int, total_bagian: int) -> None:
    """Satu ZIP: berkas nota (path sama dengan folder nota/, jadi tinggal diekstrak ke
    sana untuk memulihkan) + daftar-nota.csv yang mencocokkan berkas dengan transaksinya."""
    import csv
    import io
    import zipfile
    folder = _folder_nota()
    daftar = io.StringIO()
    w = csv.writer(daftar, delimiter=";")
    w.writerow(["nota_id", "berkas", "jenis", "kode", "tanggal", "kategori", "jumlah", "keterangan",
                "diunggah_oleh", "diunggah_pada", "status_nota"])
    with zipfile.ZipFile(tujuan_zip, "w") as z:
        for r in rows:
            path = os.path.join(folder, r["berkas"])
            if os.path.isfile(path):
                # Foto sudah terkompresi (JPEG/WEBP), simpan apa adanya; PDF/PNG dikompres
                metode = zipfile.ZIP_STORED if r["mime"] in ("image/jpeg", "image/webp") else zipfile.ZIP_DEFLATED
                z.write(path, arcname=r["berkas"], compress_type=metode)
            w.writerow([r["id"], r["berkas"], r["mime"], r["kode"], r["tanggal"] or "", r["kategori"] or "",
                        r["jumlah"] or "", r["keterangan"] or "", r["diunggah_oleh"], r["diunggah_pada"],
                        "dihapus" if r["dihapus_pada"] else ("ada" if os.path.isfile(path) else "berkas hilang")])
        z.writestr("daftar-nota.csv", "﻿" + daftar.getvalue(), compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("CARA-PULIHKAN.txt",
                   f"Backup nota bagian {bagian}/{total_bagian}.\n"
                   "Ekstrak isi ZIP ini ke folder 'nota' di samping main.py (struktur folder per bulan dipertahankan).\n"
                   "daftar-nota.csv berisi pasangan berkas <-> transaksi.\n")


def kirim_backup_nota(semua: bool = False, oleh: str = "sistem") -> dict:
    """Kirim nota ke Telegram sebagai ZIP (dipecah per BACKUP_NOTA_MAKS_MB).
    semua=False: hanya nota baru sejak backup nota terakhir yang berhasil."""
    dari_id = 0 if semua else int(_status_get("backup_nota_terakhir_id", "0") or 0)
    conn = _billing_conn()
    try:
        rows = conn.execute(
            "SELECT n.*, CASE n.pemilik WHEN 'utang' THEN '#U' || n.pemilik_id ELSE '#' || n.pemilik_id END AS kode, "
            "COALESCE(t.tanggal, u.tanggal) AS tanggal, COALESCE(kt.nama, ku.nama) AS kategori, "
            "COALESCE(t.jumlah, u.jumlah) AS jumlah, COALESCE(t.keterangan, u.pemasok) AS keterangan "
            "FROM nota n "
            "LEFT JOIN transaksi t ON n.pemilik='transaksi' AND t.id = n.pemilik_id "
            "LEFT JOIN kategori_kas kt ON kt.id = t.kategori_id "
            "LEFT JOIN utang u ON n.pemilik='utang' AND u.id = n.pemilik_id "
            "LEFT JOIN kategori_kas ku ON ku.id = u.kategori_id "
            "WHERE n.id > ? ORDER BY n.id", (dari_id,)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return {"berkas": 0, "bagian": 0, "terkirim": True, "pesan": "Tidak ada nota baru."}

    # Bagi menjadi beberapa ZIP agar tiap berkas di bawah batas Telegram
    batas = CONFIG["BACKUP_NOTA_MAKS_MB"] * 1024 * 1024
    kelompok, isi, ukuran = [], [], 0
    for r in rows:
        if isi and ukuran + r["ukuran"] > batas:
            kelompok.append(isi)
            isi, ukuran = [], 0
        isi.append(r)
        ukuran += r["ukuran"]
    kelompok.append(isi)

    tujuan = _tujuan_backup()
    hari = datetime.now().strftime("%Y-%m-%d")
    sukses_sampai = dari_id
    terkirim_bagian = 0
    for i, grup in enumerate(kelompok, 1):
        nama = f"nota-{hari}-bagian{i}dari{len(kelompok)}.zip"
        path = os.path.join(_folder_backup(), nama)
        try:
            _zip_nota(grup, path, i, len(kelompok))
            mb = os.path.getsize(path) / 1048576
            caption = (f"🧾 Backup nota {hari} — bagian {i}/{len(kelompok)}\n"
                       f"{len(grup)} berkas ({mb:.1f} MB), nota #{grup[0]['id']}–#{grup[-1]['id']}"
                       + (" (arsip lengkap)" if semua else ""))
            ok = all(kirim_dokumen_telegram(path, caption, cid) for cid in tujuan)
        except Exception as e:
            logger.error(f"Backup nota bagian {i} gagal: {e}")
            ok = False
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        if not ok:
            break       # bagian berikutnya dicoba lagi di backup berikutnya, mulai dari yang gagal
        terkirim_bagian += 1
        sukses_sampai = grup[-1]["id"]
    if sukses_sampai > int(_status_get("backup_nota_terakhir_id", "0") or 0):
        _status_set("backup_nota_terakhir_id", str(sukses_sampai))
        _status_set("backup_nota_terakhir_pada", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    hasil = {"berkas": sum(len(g) for g in kelompok[:terkirim_bagian]), "bagian": terkirim_bagian,
             "total_bagian": len(kelompok), "terkirim": terkirim_bagian == len(kelompok)}
    catat_aktivitas(oleh, "backup_nota",
                    f"{'semua' if semua else 'baru'}: {hasil['berkas']} berkas, {terkirim_bagian}/{len(kelompok)} bagian terkirim")
    return hasil


@app.post("/api/backup/nota")
def backup_nota_sekarang(request: Request, semua: bool = False):
    _wajib_admin(request)
    if semua and request.state.peran != "owner":
        raise HTTPException(status_code=403, detail="Arsip lengkap nota hanya bisa diminta owner.")
    hasil = kirim_backup_nota(semua=semua, oleh=_oleh(request))
    if not hasil["terkirim"]:
        raise HTTPException(status_code=502, detail=f"Hanya {hasil['bagian']}/{hasil['total_bagian']} bagian terkirim. "
                            "Cek koneksi/bot Telegram; sisanya dikirim ulang pada backup berikutnya.")
    return hasil




def proses_backup_harian(sekarang=None) -> None:
    """Sekali per hari setelah BACKUP_JAM. Berkas dikirim ke Telegram pada hari
    BACKUP_HARI_KIRIM tiap pekan, dan juga pada backup pertama kali (untuk
    memastikan jalurnya berfungsi)."""
    global _backup_gagal_terakhir
    sekarang = sekarang or datetime.now()
    if sekarang.hour < CONFIG["BACKUP_JAM"]:
        return
    hari = sekarang.strftime("%Y-%m-%d")
    conn = _billing_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS otomasi_log ("
            "tanggal TEXT NOT NULL, jenis TEXT NOT NULL, PRIMARY KEY (tanggal, jenis))"
        )
        conn.commit()
        if conn.execute("SELECT 1 FROM otomasi_log WHERE tanggal=? AND jenis='backup'", (hari,)).fetchone():
            return
    finally:
        conn.close()

    try:
        pertama = not _daftar_backup(_folder_backup())
        info = buat_backup(sekarang)
    except Exception as e:
        if _backup_gagal_terakhir is None or sekarang - _backup_gagal_terakhir >= timedelta(hours=1):
            send_telegram_message(f"⚠️ <b>Backup database GAGAL</b>\n{_esc(str(e))}\nAkan dicoba lagi otomatis.")
            _backup_gagal_terakhir = sekarang
        return

    conn = _billing_conn()
    try:
        conn.execute("INSERT OR IGNORE INTO otomasi_log VALUES (?,?)", (hari, "backup"))
        conn.commit()
    finally:
        conn.close()

    j = info["jumlah"]
    if info["integritas"] != "ok":
        send_telegram_message(f"⚠️ <b>Backup selesai tapi pemeriksaan keutuhan gagal</b>: {_esc(info['integritas'])}")
    mingguan = pertama or sekarang.weekday() == CONFIG["BACKUP_HARI_KIRIM"]
    if CONFIG["BACKUP_KIRIM_TELEGRAM"] and (mingguan or CONFIG["BACKUP_KIRIM_HARIAN"]):
        caption = (f"💾 Backup database {hari}\n{j['pelanggan']} pelanggan, {j['paket']} paket, "
                   f"{j['tagihan']} tagihan, {j.get('transaksi', 0)} transaksi ({info['ukuran_kb']} KB)")
        try:
            if not all(kirim_dokumen_telegram(info["berkas"], caption, cid, senyap=not mingguan)
                       for cid in _tujuan_backup()):
                send_telegram_message("⚠️ Backup tersimpan di HP, tapi gagal dikirim ke Telegram.")
        except Exception as e:
            send_telegram_message(f"⚠️ Backup tersimpan di HP, tapi gagal dikirim ke Telegram: {_esc(str(e))}")
        if CONFIG["BACKUP_NOTA_TELEGRAM"] and mingguan:
            try:
                hn = kirim_backup_nota()
                if not hn["terkirim"]:
                    send_telegram_message(f"⚠️ Backup nota baru terkirim {hn['bagian']}/{hn['total_bagian']} bagian; "
                                          "sisanya dicoba lagi minggu depan (atau kirim manual dari Pengaturan).")
            except Exception as e:
                logger.error(f"Backup nota gagal: {e}")
                send_telegram_message(f"⚠️ Backup nota gagal: {_esc(str(e))}")
    logger.warning(f"Backup database selesai: {info}")


@app.get("/healthz")
def healthz():
    """Untuk pemantau dari luar (mis. UptimeRobot): 200 bila loop pemantauan masih berputar,
    503 bila macet. Tanpa login dan tanpa data rahasia."""
    umur = time.monotonic() - _putaran["terakhir"]
    batas = max(3 * CONFIG["POLL_INTERVAL_SECONDS"], 600)
    if umur > batas:
        return JSONResponse({"ok": False, "masalah": "loop pemantauan macet", "detik_sejak_putaran": int(umur)},
                            status_code=503)
    return {"ok": True, "detik_sejak_putaran": int(umur), "putaran": _putaran["jumlah"]}


@app.post("/api/backup")
def backup_sekarang(request: Request, kirim: bool = False):
    """Buat backup sekarang juga. Tambahkan ?kirim=true untuk ikut mengirim ke Telegram."""
    _wajib_admin(request)
    try:
        info = buat_backup()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup gagal: {e}")
    catat_aktivitas(_oleh(request), "backup", os.path.basename(info["berkas"]))
    if kirim:
        j = info["jumlah"]
        try:
            info["terkirim_telegram"] = all(kirim_dokumen_telegram(
                info["berkas"],
                f"💾 Backup manual oleh {_oleh(request)}\n{j['pelanggan']} pelanggan, {j['paket']} paket, "
                f"{j['tagihan']} tagihan, {j.get('transaksi', 0)} transaksi", cid,
            ) for cid in _tujuan_backup())
        except RuntimeError as e:
            info["terkirim_telegram"] = False
            info["galat_telegram"] = str(e)
    return info
  
@app.post("/api/test-telegram")
def test_telegram(request: Request):
    _wajib_admin(request)
    url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_BOT_TOKEN']}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": CONFIG["TELEGRAM_CHAT_ID"],
                                     "text": f"✅ Tes notifikasi dari dashboard oleh {request.state.user}."}, timeout=10)
        ok = r.ok and r.json().get("ok")
    except Exception as e:
        raise HTTPException(status_code=502, detail=_samarkan(f"Telegram tidak terjangkau: {e}"))
    if not ok:
        raise HTTPException(status_code=502, detail="Telegram menolak pesan. Cek TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID.")
    return {"ok": True}



# ============================================================
# 9. PERANGKAT PELANGGAN (TR-069 lewat GenieACS) & DIAGNOSA
# ============================================================
# GenieACS berjalan di VPS. INDESO hanya memakai NBI (REST API, port 7557) yang
# dilindungi password lewat nginx — lihat docs/genieacs.md. ONU dicocokkan ke
# pelanggan lewat nomor seri GPON (sama dengan yang dibaca dari OLT) atau username
# PPPoE yang tersimpan di ONU. Password WiFi tidak pernah disimpan/dicatat di INDESO.

CONFIG.update({
    "GENIEACS_URL": _env("GENIEACS_URL", "").rstrip("/"),     # mis. https://acs.contoh.id/nbi
    "GENIEACS_USER": _env("GENIEACS_USER", ""),
    "GENIEACS_PASS": _env("GENIEACS_PASS", ""),
    "GENIEACS_TIMEOUT": int(_env("GENIEACS_TIMEOUT", "15")),
})

_IGD = "InternetGatewayDevice"
_KUNCI_WIFI = ("KeyPassphrase", "PreSharedKey.1.KeyPassphrase", "PreSharedKey.1.PreSharedKey")


def _acs_aktif() -> bool:
    return bool(CONFIG["GENIEACS_URL"])


def _acs(method: str, path: str, **kw):
    if not _acs_aktif():
        raise HTTPException(status_code=503, detail="Server ACS (GenieACS) belum diatur. Isi GENIEACS_URL di .env — lihat docs/genieacs.md.")
    auth = (CONFIG["GENIEACS_USER"], CONFIG["GENIEACS_PASS"]) if CONFIG["GENIEACS_USER"] else None
    try:
        r = requests.request(method, CONFIG["GENIEACS_URL"] + path, auth=auth,
                             timeout=kw.pop("timeout", CONFIG["GENIEACS_TIMEOUT"]), **kw)
    except requests.RequestException as e:
        logger.error(f"GenieACS tidak terjangkau: {e}")
        raise HTTPException(status_code=502, detail="Server ACS tidak terjangkau. Cek VPS dan koneksi internet.")
    if r.status_code in (401, 403):
        raise HTTPException(status_code=502, detail="Server ACS menolak login. Cek GENIEACS_USER/GENIEACS_PASS di .env.")
    return r


def _nilai(doc: dict, path: str):
    """Ambil _value parameter dari dokumen perangkat GenieACS (path bertitik)."""
    cur = doc
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur.get("_value") if isinstance(cur, dict) else None


def _ada(doc: dict, path: str) -> bool:
    cur = doc
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return False
        cur = cur[p]
    return isinstance(cur, dict)


def _anak(doc: dict, path: str) -> list[str]:
    cur = doc
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return []
        cur = cur[p]
    return sorted((k for k in cur if k.isdigit()), key=int) if isinstance(cur, dict) else []


def _sn_onu(username: str) -> Optional[str]:
    if _latest_data is None:
        return None
    onu = next((o for o in _latest_data.onu_list if o.pppoe_username == username), None)
    return onu.serial_number if onu else None


def _query_pppoe(username: str) -> dict:
    jalur = [f"{_IGD}.WANDevice.1.WANConnectionDevice.{w}.WANPPPConnection.{p}.Username._value"
             for w in range(1, 6) for p in range(1, 3)]
    return {"$or": [{"VirtualParameters.pppoeUsername._value": username}] + [{j: username} for j in jalur]}


_POLA_SN_GPON = re.compile(r"^[A-Z]{4}[0-9A-F]{8}$")
_MEREK_SN = {"ZTEG": "zte", "FHTT": "fiberhome", "HWTC": "huawei", "ALCL": "nokia", "GPON": None}


def _merek(teks: str) -> Optional[str]:
    t = (teks or "").lower()
    for kata, m in (("zte", "zte"), ("fiberhome", "fiberhome"), ("fhtt", "fiberhome"), ("huawei", "huawei"),
                    ("hwtc", "huawei"), ("nokia", "nokia"), ("alcl", "nokia")):
        if kata in t:
            return m
    return None


def _onu_lama(doc: dict, sn_olt: Optional[str]) -> bool:
    """True bila dokumen ACS jelas milik ONU lain dari yang sekarang terpasang menurut OLT
    (ONU pelanggan sudah diganti, ONU baru belum lapor ke ACS)."""
    if not sn_olt:
        return False
    di = doc.get("_deviceId") or {}
    sn_acs = str(di.get("_SerialNumber") or "").upper()
    sn_olt = sn_olt.upper()
    if sn_acs == sn_olt:
        return False
    # Nomor seri ACS berformat SN GPON tapi berbeda → ONU lain. Format lain (sebagian ZTE melapor
    # dengan nomor seri versi TR-069) hanya dianggap lain bila mereknya jelas berbeda.
    if _POLA_SN_GPON.match(sn_acs) and _POLA_SN_GPON.match(sn_olt):
        return True
    m_olt = _MEREK_SN.get(sn_olt[:4]) or _merek(sn_olt[:4])
    m_acs = _merek(f"{di.get('_Manufacturer') or ''} {sn_acs[:4]}")
    return bool(m_olt and m_acs and m_olt != m_acs)


_acs_onu_lama: dict = {}     # username -> ringkasan ONU lama yang tertolak (untuk pesan ke admin)


def _cari_perangkat(username: str) -> Optional[dict]:
    """Cari dokumen ONU di GenieACS: nomor seri GPON dari OLT → id tersimpan → username PPPoE.
    Nomor seri didahulukan: bila ONU pelanggan diganti, dokumen ONU lama masih ada di GenieACS
    (dengan id tersimpan & username PPPoE yang sama), jadi id tersimpan saja bisa salah ONU.
    Dokumen yang ternyata milik ONU lama (SN/merek beda dengan yang terbaca OLT) tidak dipakai."""
    conn = _billing_conn()
    try:
        r = conn.execute("SELECT acs_id FROM pelanggan WHERE username=?", (username,)).fetchone()
    finally:
        conn.close()
    if r is None:
        raise HTTPException(status_code=404, detail="Pelanggan tidak ditemukan.")
    queries = []
    sn = _sn_onu(username)
    if sn:
        queries.append({"_deviceId._SerialNumber": sn})
        if sn.upper() != sn:
            queries.append({"_deviceId._SerialNumber": sn.upper()})
    if r["acs_id"]:
        queries.append({"_id": r["acs_id"]})
    queries.append(_query_pppoe(username))
    _acs_onu_lama.pop(username, None)
    for q in queries:
        res = _acs("GET", "/devices/", params={"query": json.dumps(q)})
        if res.ok and res.json():
            # Beberapa dokumen cocok (mis. ONU lama & baru sama-sama menyimpan username PPPoE):
            # pakai yang paling baru melapor.
            doc = max(res.json(), key=lambda d: str(d.get("_lastInform") or ""))
            if _onu_lama(doc, sn):
                di = doc.get("_deviceId") or {}
                _acs_onu_lama[username] = {"sn_olt": sn, "lama": f"{di.get('_Manufacturer') or ''} "
                                           f"{di.get('_ProductClass') or ''} ({di.get('_SerialNumber') or '-'})".strip()}
                continue
            if doc["_id"] != r["acs_id"]:
                conn = _billing_conn()
                try:
                    conn.execute("UPDATE pelanggan SET acs_id=? WHERE username=?", (doc["_id"], username))
                    conn.commit()
                finally:
                    conn.close()
            return doc
    return None


def _tugas_acs(acs_id: str, tugas: dict, tunggu: int = 20) -> dict:
    """Kirim tugas + connection request. 200 = langsung dijalankan ONU, 202 = antre
    (ONU belum bisa dihubungi; dijalankan saat ONU lapor berikutnya)."""
    from urllib.parse import quote
    r = _acs("POST", f"/devices/{quote(acs_id, safe='')}/tasks",
             params={"connection_request": "", "timeout": str(tunggu * 1000)}, json=tugas, timeout=tunggu + 10)
    try:
        tid = (r.json() or {}).get("_id")
    except ValueError:
        tid = None
    if r.status_code == 200:
        return {"status": "selesai", "tugas_id": tid}
    if r.status_code == 202:
        return {"status": "antre", "tugas_id": tid,
                "pesan": "ONU belum merespons langsung. Perintah tersimpan dan dijalankan saat ONU lapor ke server "
                         "berikutnya (paling lama ±5 menit)."}
    raise HTTPException(status_code=502, detail=f"Server ACS menolak perintah (HTTP {r.status_code}): {r.text[:150]}")


def _param_ditolak(detail) -> list[str]:
    """Nama parameter yang ditolak ONU dari detail fault GenieACS (SetParameterValuesFault)."""
    hasil = []
    if isinstance(detail, dict):
        for k, v in detail.items():
            if k.lower() == "parametername" and isinstance(v, str):
                hasil.append(v)
            else:
                hasil += _param_ditolak(v)
    elif isinstance(detail, list):
        for v in detail:
            hasil += _param_ditolak(v)
    return hasil


def _rincian_fault(detail) -> list[str]:
    """'IPPingDiagnostics: Internal error (9002)' per kolom yang ditolak (SetParameterValuesFault)."""
    hasil = []
    if isinstance(detail, dict):
        nama = detail.get("parameterName") or detail.get("ParameterName")
        if isinstance(nama, str) and nama.strip():
            bagian = nama.rstrip(".").rsplit(".", 1)[-1]
            alasan = str(detail.get("faultString") or detail.get("FaultString") or "").strip()
            kode = str(detail.get("faultCode") or detail.get("FaultCode") or "").strip()
            hasil.append(bagian + (f": {alasan}" if alasan else "") + (f" ({kode})" if kode else ""))
        else:
            for v in detail.values():
                hasil += _rincian_fault(v)
    elif isinstance(detail, list):
        for v in detail:
            hasil += _rincian_fault(v)
    return hasil


def _status_tugas(acs_id: str, tid: str) -> dict:
    """antre = masih menunggu ONU; gagal = ONU menolak (fault); selesai = sudah dijalankan."""
    q = json.dumps({"device": acs_id})
    r = _acs("GET", "/faults/", params={"query": q})
    for f in (r.json() if r.ok else []):
        if f.get("channel") == f"task_{tid}":
            ditolak = [p.rstrip(".").rsplit(".", 1)[-1] for p in _param_ditolak(f.get("detail")) if p.strip()]
            rinci = _rincian_fault(f.get("detail"))
            sibuk = any("internal error" in r.lower() or "(9002)" in r for r in rinci)
            return {"status": "gagal", "fault_id": f.get("_id"), "kode": f.get("code"), "parameter": ditolak,
                    "sibuk": sibuk,
                    "pesan": f"ONU menolak perintah ({f.get('code')}): {str(f.get('message') or '')[:200]}"
                             + (f" — {'; '.join(rinci[:4])}" if rinci else "")
                             + (". ONU sedang sibuk atau fitur diagnosanya macet: coba lagi 5–10 menit lagi, "
                                "atau Reboot ONU bila tetap gagal." if sibuk else "")}
    r = _acs("GET", "/tasks/", params={"query": q})
    if r.ok and any(str(t.get("_id")) == tid for t in r.json()):
        return {"status": "antre"}
    return {"status": "selesai"}


@app.get("/api/acs/{username}/tugas/{tid}")
def acs_status_tugas(username: str, tid: str, request: Request):
    _wajib_admin(request)
    if not re.fullmatch(r"[0-9a-f]{24}", tid):
        raise HTTPException(status_code=400, detail="ID tugas tidak valid.")
    doc = _cari_perangkat(username)
    if not doc:
        raise HTTPException(status_code=404, detail="ONU pelanggan ini belum terdaftar di server ACS.")
    return _status_tugas(doc["_id"], tid)


@app.post("/api/acs/{username}/tugas/{tid}/batal")
def acs_batal_tugas(username: str, tid: str, request: Request):
    """Hapus perintah yang tertahan (mis. ditolak ONU) beserta catatan fault-nya."""
    _wajib_admin(request)
    if not re.fullmatch(r"[0-9a-f]{24}", tid):
        raise HTTPException(status_code=400, detail="ID tugas tidak valid.")
    doc = _cari_perangkat(username)
    if not doc:
        raise HTTPException(status_code=404, detail="ONU pelanggan ini belum terdaftar di server ACS.")
    from urllib.parse import quote
    _acs("DELETE", f"/tasks/{tid}")
    _acs("DELETE", f"/faults/{quote(doc['_id'] + ':task_' + tid, safe='')}")
    catat_aktivitas(_oleh(request), "onu_batal_tugas", f"{username} ({doc['_id']}) tugas {tid} dibatalkan")
    return {"ok": True}


def _band_wifi(doc: dict, b: str, ssid: str) -> str:
    """2,4 / 5 GHz: dari channel, parameter band (nama berbeda per vendor), standar, atau nama SSID."""
    ch = str(_nilai(doc, f"{b}.Channel") or "")
    if ch.isdigit() and int(ch) > 0:
        return "5 GHz" if int(ch) > 14 else "2,4 GHz"
    cur = doc
    for p in b.split("."):
        cur = cur.get(p, {}) if isinstance(cur, dict) else {}
    for k, v in (cur.items() if isinstance(cur, dict) else []):
        # ZTE punya X_ZTE-COM_BandWidth (20/40 MHz) — itu lebar kanal, bukan frekuensi
        if "Width" in k or "Steering" in k:
            continue
        if ("Band" in k or "Frequency" in k) and isinstance(v, dict) and v.get("_value") not in (None, ""):
            return "5 GHz" if "5" in str(v["_value"]) else "2,4 GHz"
    std = str(_nilai(doc, f"{b}.Standard") or "").lower()
    if std and ("ac" in std or "ax" in std or std in ("a", "a,n", "an")):
        return "5 GHz"
    return "5 GHz" if re.search(r"5\s*g", ssid or "", re.I) else "2,4 GHz"


def _ringkas_perangkat(doc: dict) -> dict:
    di = doc.get("_deviceId", {})
    wlan = []
    mac_wifi = {}      # MAC perangkat yang sedang tersambung ke pemancar WiFi -> nama WiFi
    for i in _anak(doc, f"{_IGD}.LANDevice.1.WLANConfiguration"):
        b = f"{_IGD}.LANDevice.1.WLANConfiguration.{i}"
        ssid = _nilai(doc, f"{b}.SSID")
        if ssid is None:
            continue
        kunci = next((k for k in _KUNCI_WIFI if _ada(doc, f"{b}.{k}")), None)
        # Sebagian ONU (mis. Fiberhome) tidak mengisi TotalAssociations; pakai yang terbesar.
        total = _nilai(doc, f"{b}.TotalAssociations")
        klien = max(int(total) if str(total or "").isdigit() else 0, len(_anak(doc, f"{b}.AssociatedDevice")))
        for j in _anak(doc, f"{b}.AssociatedDevice"):
            mac = str(_nilai(doc, f"{b}.AssociatedDevice.{j}.AssociatedDeviceMACAddress") or "").upper()
            if mac:
                mac_wifi[mac] = ssid
        wlan.append({"index": int(i), "ssid": ssid, "aktif": _nilai(doc, f"{b}.Enable") in (True, "true", "1", 1),
                     "channel": _nilai(doc, f"{b}.Channel"), "standar": _nilai(doc, f"{b}.Standard"),
                     "band": _band_wifi(doc, b, ssid), "klien": klien, "bisa_ganti_password": kunci is not None})
    host = []
    for i in _anak(doc, f"{_IGD}.LANDevice.1.Hosts.Host"):
        b = f"{_IGD}.LANDevice.1.Hosts.Host.{i}"
        aktif = _nilai(doc, f"{b}.Active")
        host.append({"nama": _nilai(doc, f"{b}.HostName") or "-", "ip": _nilai(doc, f"{b}.IPAddress"),
                     "mac": _nilai(doc, f"{b}.MACAddress"), "aktif": aktif in (True, "true", "1", 1),
                     "antarmuka": _nilai(doc, f"{b}.InterfaceType")})
    # Tabel host (DHCP) sering tetap "aktif" sampai sewa IP habis walau HP sudah pergi. Bila ONU memberi
    # daftar perangkat yang benar-benar tersambung ke WiFi, itu yang dipakai untuk perangkat WiFi.
    for h in host:
        mac = str(h["mac"] or "").upper()
        if mac in mac_wifi:
            h["wifi"] = mac_wifi[mac]
            h["aktif"] = True
        elif mac_wifi and "802.11" in str(h["antarmuka"] or "") and h["aktif"]:
            h["aktif"] = False
            h["catatan"] = "tercatat di ONU, tidak sedang tersambung ke WiFi"
    host_wifi = sum(1 for h in host if h["aktif"] and "802.11" in str(h["antarmuka"] or ""))
    if host_wifi and not any(w["klien"] for w in wlan):
        for w in wlan:
            w["klien"] = None          # ONU tidak melaporkan jumlah klien per WiFi
    wan_ip = None
    for w in _anak(doc, f"{_IGD}.WANDevice.1.WANConnectionDevice"):
        for p in _anak(doc, f"{_IGD}.WANDevice.1.WANConnectionDevice.{w}.WANPPPConnection"):
            wan_ip = wan_ip or _nilai(doc, f"{_IGD}.WANDevice.1.WANConnectionDevice.{w}.WANPPPConnection.{p}.ExternalIPAddress")
    uptime = _nilai(doc, f"{_IGD}.DeviceInfo.UpTime")
    return {
        "id": doc.get("_id"), "pabrikan": di.get("_Manufacturer"), "model": di.get("_ProductClass"),
        "serial": di.get("_SerialNumber"), "firmware": _nilai(doc, f"{_IGD}.DeviceInfo.SoftwareVersion"),
        "uptime_detik": int(uptime) if str(uptime or "").isdigit() else None,
        "lapor_terakhir": doc.get("_lastInform"), "wan_ip": wan_ip,
        "wifi": wlan, "perangkat": host, "perangkat_aktif": sum(1 for h in host if h["aktif"]),
        "perangkat_wifi": host_wifi,
    }


def _lapor_terakhir_menit(teks: Optional[str]) -> Optional[int]:
    if not teks:
        return None
    try:
        t = datetime.fromisoformat(teks.replace("Z", "+00:00"))
        return int((datetime.now(t.tzinfo) - t).total_seconds() // 60)
    except ValueError:
        return None


def _info_acs(username: str, segarkan: bool = False) -> dict:
    doc = _cari_perangkat(username)
    if not doc:
        lama = _acs_onu_lama.get(username)
        if lama:
            return {"terhubung": False, "diganti": True, "pesan": (
                f"ONU pelanggan ini sudah diganti: OLT membaca ONU baru {lama['sn_olt']}, sedangkan data di ACS masih "
                f"milik ONU lama {lama['lama']}. ONU baru belum lapor ke ACS. Kirim pengaturan TR-069 dari OLT di bawah; "
                "untuk ZTE pastikan juga WAN PPPoE-nya berjenis INTERNET_TR069, lalu reboot ONU.")}
        return {"terhubung": False, "pesan": "ONU pelanggan ini belum terdaftar di server ACS. Pastikan TR-069 di ONU sudah "
                                              "diarahkan ke server ACS (lihat docs/genieacs.md)."}
    hasil_segar = None
    if segarkan:
        hasil_segar = _tugas_acs(doc["_id"], {"name": "refreshObject", "objectName": f"{_IGD}.LANDevice.1"}, tunggu=25)
        doc = _cari_perangkat(username) or doc
    info = _ringkas_perangkat(doc)
    info["lapor_menit_lalu"] = _lapor_terakhir_menit(info["lapor_terakhir"])
    return {"terhubung": True, "perangkat": info, "segarkan": hasil_segar}


@app.get("/api/acs/status")
def acs_status(request: Request):
    _wajib_admin(request)
    if not _acs_aktif():
        return {"aktif": False}
    r = _acs("GET", "/devices/", params={"query": "{}", "projection": "_id"}, timeout=10)
    return {"aktif": True, "terjangkau": r.ok, "jumlah_perangkat": len(r.json()) if r.ok else None}


@app.get("/api/acs/{username}")
def acs_info(username: str, request: Request, segarkan: bool = False):
    _wajib_admin(request)
    return _info_acs(username, segarkan)


class WifiIn(BaseModel):
    index: int
    ssid: Optional[str] = None
    password: Optional[str] = None


@app.post("/api/acs/{username}/wifi")
def acs_ganti_wifi(username: str, d: WifiIn, request: Request):
    _wajib_admin(request)
    ssid = (d.ssid or "").strip()
    pw = d.password or ""
    if not ssid and not pw:
        raise HTTPException(status_code=400, detail="Isi nama WiFi baru dan/atau password baru.")
    if ssid and not (1 <= len(ssid) <= 32 and ssid.isprintable()):
        raise HTTPException(status_code=400, detail="Nama WiFi 1–32 karakter.")
    if pw and not (8 <= len(pw) <= 63 and all(32 <= ord(c) < 127 for c in pw)):
        raise HTTPException(status_code=400, detail="Password WiFi 8–63 karakter, huruf/angka/simbol biasa (tanpa emoji).")
    doc = _cari_perangkat(username)
    if not doc:
        raise HTTPException(status_code=404, detail="ONU pelanggan ini belum terdaftar di server ACS.")
    b = f"{_IGD}.LANDevice.1.WLANConfiguration.{d.index}"
    if _nilai(doc, f"{b}.SSID") is None:
        raise HTTPException(status_code=404, detail="Jaringan WiFi itu tidak ditemukan di ONU.")
    nilai = []
    if ssid:
        nilai.append([f"{b}.SSID", ssid, "xsd:string"])
    if pw:
        kunci = next((k for k in _KUNCI_WIFI if _ada(doc, f"{b}.{k}")), None)
        if not kunci:
            raise HTTPException(status_code=400, detail="Model ONU ini tidak membuka pengaturan password WiFi lewat TR-069.")
        nilai.append([f"{b}.{kunci}", pw, "xsd:string"])
    hasil = _tugas_acs(doc["_id"], {"name": "setParameterValues", "parameterValues": nilai})
    ubah = (["nama WiFi → " + ssid] if ssid else []) + (["password WiFi diganti"] if pw else [])
    catat_aktivitas(_oleh(request), "onu_wifi",
                    f"{username} ({doc['_id']}) WiFi #{d.index}: {', '.join(ubah)}"
                    + (" — menunggu ONU online" if hasil["status"] == "antre" else ""))
    return hasil


_PING_ONU = f"{_IGD}.IPPingDiagnostics"


class PingOnuIn(BaseModel):
    host: str = "google.com"
    jumlah: int = 10
    lewat_wan: bool = True      # arahkan ping lewat koneksi PPPoE (bukan jalur manajemen/LAN)
    minimal: bool = False       # hanya Host + jumlah + mulai (untuk ONU yang menolak Timeout/DataBlockSize)


def _wan_ppp_aktif(doc: dict) -> Optional[str]:
    """Path WANPPPConnection yang sedang tersambung (punya IP), untuk IPPingDiagnostics.Interface."""
    for w in _anak(doc, f"{_IGD}.WANDevice.1.WANConnectionDevice"):
        for p in _anak(doc, f"{_IGD}.WANDevice.1.WANConnectionDevice.{w}.WANPPPConnection"):
            b = f"{_IGD}.WANDevice.1.WANConnectionDevice.{w}.WANPPPConnection.{p}"
            ip = str(_nilai(doc, f"{b}.ExternalIPAddress") or "")
            if ip and ip != "0.0.0.0":
                return b
    return None


def _param(doc: dict, path: str) -> dict:
    cur = doc
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return {}
        cur = cur[p]
    return cur if isinstance(cur, dict) else {}


def _bersihkan_tugas_ping(acs_id: str) -> int:
    """Hapus perintah ping lama yang masih tertahan (mis. pernah ditolak ONU) beserta fault-nya.
    GenieACS mengirim ulang tugas yang tertahan setiap ONU lapor dan menggabungkannya dengan
    perintah baru dalam satu SetParameterValues, sehingga satu tugas rusak membuat semua ping
    berikutnya ikut ditolak (terjadi pada ZTE F609: 9003 terus-menerus)."""
    from urllib.parse import quote
    q = json.dumps({"device": acs_id})
    r = _acs("GET", "/tasks/", params={"query": q})
    n = 0
    for t in (r.json() if r.ok else []):
        pv = t.get("parameterValues") or []
        if t.get("name") == "setParameterValues" and any(str(p[0]).startswith(_PING_ONU) for p in pv if p):
            tid = str(t.get("_id"))
            _acs("DELETE", f"/tasks/{tid}")
            _acs("DELETE", f"/faults/{quote(acs_id + ':task_' + tid, safe='')}")
            n += 1
    if n:
        logger.info(f"{n} perintah ping lama yang tertahan dihapus untuk {acs_id}")
    return n


# Ping ONU tanpa coba-ulang otomatis: perintah yang ditolak langsung dihapus. ONU yang menolak cara
# "lewat WAN" diingat, sehingga permintaan ping berikutnya langsung memakai cara tanpa memilih WAN.
_ping_tugas: dict = {}          # id tugas -> (acs_id, lewat_wan)
_ping_tanpa_wan: set = set()    # acs_id yang menolak kolom Interface


def _hapus_tugas_acs(acs_id: str, tid: str) -> None:
    from urllib.parse import quote
    try:
        _acs("DELETE", f"/tasks/{tid}")
        _acs("DELETE", f"/faults/{quote(acs_id + ':task_' + tid, safe='')}")
    except HTTPException:
        pass


@app.post("/api/acs/{username}/ping")
def acs_ping_mulai(username: str, d: PingOnuIn, request: Request):
    """Suruh ONU pelanggan ping ke host (default google.com) lewat TR-069 IPPingDiagnostics.
    Hasilnya dibaca lewat GET /api/acs/{username}/ping setelah ONU selesai."""
    _wajib_admin(request)
    host = d.host.strip()
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
        raise HTTPException(status_code=400, detail="Alamat tujuan tidak valid (mis. google.com atau 8.8.8.8).")
    jumlah = max(3, min(d.jumlah, 30))
    doc = _cari_perangkat(username)
    if not doc:
        raise HTTPException(status_code=404, detail="ONU pelanggan ini belum terdaftar di server ACS.")
    if not _ada(doc, _PING_ONU):
        raise HTTPException(status_code=400, detail="ONU ini tidak menyediakan tes ping lewat TR-069.")
    _bersihkan_tugas_ping(doc["_id"])
    mulai = datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
    # Tanpa Interface, sebagian ONU ping lewat jalur lain (mis. WAN manajemen/TR-069) yang
    # DNS-nya tidak jalan. Arahkan ke PPPoE internet pelanggan bila ONU menyediakan kolomnya.
    wan = _wan_ppp_aktif(doc) if d.lewat_wan and _ada(doc, f"{_PING_ONU}.Interface") else None
    if d.minimal or doc["_id"] in _ping_tanpa_wan:
        wan = None
    # Timeout/DataBlockSize sengaja tidak dikirim: bawaan ONU sudah wajar, dan makin sedikit
    # kolom makin kecil kemungkinan ditolak (teruji di ZTE F609 V7 & Fiberhome HG6145D2).
    nilai = ([[f"{_PING_ONU}.Interface", wan, "xsd:string"]] if wan else []) + [
             [f"{_PING_ONU}.Host", host, "xsd:string"],
             [f"{_PING_ONU}.NumberOfRepetitions", jumlah, "xsd:unsignedInt"],
             [f"{_PING_ONU}.DiagnosticsState", "Requested", "xsd:string"]]
    # Pakai tipe data yang dilaporkan ONU sendiri (mis. ZTE melaporkan NumberOfRepetitions
    # sebagai xsd:int): tipe yang tidak cocok dijawab sebagian ONU dengan 9003 Invalid arguments.
    for v in nilai:
        tipe = _param(doc, v[0]).get("_type")
        if isinstance(tipe, str) and tipe.startswith("xsd:"):
            v[2] = tipe
    hasil = _tugas_acs(doc["_id"], {"name": "setParameterValues", "parameterValues": nilai})
    if hasil.get("tugas_id"):
        _ping_tugas[str(hasil["tugas_id"])] = (doc["_id"], bool(wan))
        while len(_ping_tugas) > 500:
            _ping_tugas.pop(next(iter(_ping_tugas)))
    catat_aktivitas(_oleh(request), "onu_ping", f"{username} ({doc['_id']}) ping {host} {jumlah}x")
    return dict(hasil, mulai=mulai, host=host, jumlah=jumlah, lewat_wan=bool(wan), minimal=d.minimal)


@app.get("/api/acs/{username}/ping")
def acs_ping_hasil(username: str, request: Request, mulai: str, segarkan: bool = False, tugas: Optional[str] = None):
    """menunggu → ONU belum selesai / hasil belum terbaca; gagal → ONU menolak perintah ping;
    selesai → angka hasil ping."""
    _wajib_admin(request)
    doc = _cari_perangkat(username)
    if not doc:
        raise HTTPException(status_code=404, detail="ONU pelanggan ini belum terdaftar di server ACS.")
    if tugas and re.fullmatch(r"[0-9a-f]{24}", tugas):
        st = _status_tugas(doc["_id"], tugas)
        if st["status"] == "gagal":
            # Hapus langsung supaya tidak diulang GenieACS tiap ONU lapor (tidak menumpuk). Tidak dicoba
            # ulang otomatis; bila tadi lewat WAN, permintaan berikutnya memakai cara tanpa WAN.
            _hapus_tugas_acs(doc["_id"], tugas)
            asal = _ping_tugas.pop(tugas, None)
            ganti_cara = bool(asal and asal[1])
            if ganti_cara:
                _ping_tanpa_wan.add(doc["_id"])
            return {"status": "gagal", "pesan": st["pesan"], "tugas_id": tugas, "dihapus": True,
                    "cara_berikutnya": "tanpa_wan" if ganti_cara else None}
    tertunda = False
    if segarkan:
        r = _acs("GET", "/tasks/", params={"query": json.dumps({"device": doc["_id"], "name": "getParameterValues"})})
        tertunda = r.ok and any(any(str(n).startswith(_PING_ONU) for n in (t.get("parameterNames") or [])) for t in r.json())
    if segarkan and not tertunda:
        # Hasil belum masuk: minta ONU mengirim nilai IPPingDiagnostics (diulang berkala dari browser;
        # tidak ditumpuk bila permintaan sebelumnya masih menunggu ONU).
        _tugas_acs(doc["_id"], {"name": "getParameterValues", "parameterNames": [
            f"{_PING_ONU}.{k}" for k in ("DiagnosticsState", "SuccessCount", "FailureCount",
                                         "AverageResponseTime", "MinimumResponseTime", "MaximumResponseTime")]}, tunggu=15)
        doc = _cari_perangkat(username) or doc
    st = _param(doc, f"{_PING_ONU}.DiagnosticsState")
    keadaan, waktu = st.get("_value"), str(st.get("_timestamp") or "")
    if not keadaan or keadaan in ("Requested", "None") or waktu < mulai:
        return {"status": "menunggu", "keadaan": keadaan}
    angka = lambda k: (lambda v: int(v) if str(v).lstrip("-").isdigit() else None)(_nilai(doc, f"{_PING_ONU}.{k}"))
    sukses, gagal = angka("SuccessCount"), angka("FailureCount")
    total = (sukses or 0) + (gagal or 0)
    return {"status": "selesai", "keadaan": keadaan, "host": _nilai(doc, f"{_PING_ONU}.Host"),
            "sukses": sukses, "gagal": gagal,
            "hilang_persen": round((gagal or 0) * 100 / total) if total else None,
            "rata_ms": angka("AverageResponseTime"), "min_ms": angka("MinimumResponseTime"),
            "maks_ms": angka("MaximumResponseTime")}


@app.post("/api/acs/{username}/reboot")
def acs_reboot(username: str, request: Request):
    _wajib_admin(request)
    doc = _cari_perangkat(username)
    if not doc:
        raise HTTPException(status_code=404, detail="ONU pelanggan ini belum terdaftar di server ACS.")
    hasil = _tugas_acs(doc["_id"], {"name": "reboot"})
    catat_aktivitas(_oleh(request), "onu_reboot", f"{username} ({doc['_id']})"
                    + (" — menunggu ONU online" if hasil["status"] == "antre" else ""))
    return hasil


# --- Diagnosa pelanggan: "internetnya lemot" ---

def _mikrotik_lalu_lintas(username: str) -> Optional[dict]:
    """rx/tx sesaat dari interface PPPoE pelanggan (<pppoe-username>). rx di router =
    upload pelanggan, tx = download pelanggan."""
    pool = _mikrotik_pool()
    try:
        api = pool.get_api()
        r = api.get_resource("/interface").call("monitor-traffic", {"interface": f"<pppoe-{username}>", "once": ""})
    except Exception as e:
        logger.warning(f"monitor-traffic {username} gagal: {e}")
        return None
    finally:
        try:
            pool.disconnect()
        except Exception:
            pass
    if not r:
        return None
    x = r[0]
    def mbps(k):
        try:
            return round(int(x.get(k, 0)) / 1_000_000, 2)
        except (TypeError, ValueError):
            return None
    return {"download_mbps": mbps("tx-bits-per-second"), "upload_mbps": mbps("rx-bits-per-second")}


def _mikrotik_ping(ip: str, jumlah: int = 10) -> dict:
    pool = _mikrotik_pool()
    try:
        api = pool.get_api()
        hasil = api.get_resource("/").call("ping", {"address": ip, "count": str(jumlah), "interval": "0.2"})
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ping lewat MikroTik gagal: {e}")
    finally:
        try:
            pool.disconnect()
        except Exception:
            pass
    waktu = [w for w in (_ms(h.get("time")) for h in hasil or [] if h.get("status") != "timeout") if w is not None]
    akhir = (hasil or [{}])[-1]
    if akhir.get("sent"):
        # RouterOS menyertakan statistik kumulatif di setiap baris; baris terakhir = ringkasan.
        terkirim, diterima = int(akhir["sent"]), int(akhir.get("received", 0))
        rata, mn, mx = _ms(akhir.get("avg-rtt")), _ms(akhir.get("min-rtt")), _ms(akhir.get("max-rtt"))
    else:
        terkirim, diterima = max(jumlah, len(hasil or [])), len(waktu)
        rata = round(sum(waktu) / len(waktu), 1) if waktu else None
        mn, mx = (min(waktu), max(waktu)) if waktu else (None, None)
    hilang = round((terkirim - diterima) * 100 / terkirim) if terkirim else 100
    return {"ip": ip, "terkirim": terkirim, "diterima": diterima, "hilang_persen": hilang,
            "rata_ms": rata, "min_ms": mn, "maks_ms": mx}


def _ms(t) -> Optional[float]:
    """'1ms234us' / '12ms' / '850us' → milidetik."""
    if not t:
        return None
    m = re.fullmatch(r"(?:(\d+)s)?(?:(\d+)ms)?(?:(\d+)us)?", str(t).replace(" ", ""))
    if not m or not any(m.groups()):
        return None
    return round(int(m.group(1) or 0) * 1000 + int(m.group(2) or 0) + int(m.group(3) or 0) / 1000, 1)


# ============================================================
# Pantau internet pusat: ping dari MikroTik ke gateway ISP & internet, beban WAN
# ============================================================

_upstream: dict = {"terakhir": None, "wan": None, "gateway": None, "buruk": 0, "lambat": 0, "penuh": 0,
                   "alert_buruk": False, "alert_penuh": False, "mulai_buruk": None}


def _deteksi_wan(api) -> tuple[Optional[str], Optional[str]]:
    """(interface WAN, IP gateway ISP) dari default route yang aktif."""
    try:
        rute = api.get_resource("/ip/route").get()
    except Exception:
        return None, None
    for r in rute:
        if r.get("dst-address") != "0.0.0.0/0" or str(r.get("active", "true")).lower() not in ("true", "yes"):
            continue
        gw = str(r.get("gateway") or "")
        iface, ip = None, None
        m = re.match(r"^(\d+\.\d+\.\d+\.\d+)(?:%(.+))?$", gw)
        if m:
            ip, iface = m.group(1), m.group(2)
        elif gw:
            iface = gw                                     # mis. pppoe-out1
        ig = str(r.get("immediate-gw") or "")
        if "%" in ig:
            iface = iface or ig.split("%", 1)[1]
            ip = ip or ig.split("%", 1)[0]
        return iface, ip
    return None, None


def _tujuan_upstream(gateway: Optional[str]) -> list[tuple[str, str]]:
    tujuan = [("gateway", gateway)] if gateway else []
    for t in str(CONFIG["UPSTREAM_TUJUAN"] or "").split(","):
        t = t.strip()
        if t and re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", t):
            tujuan.append(("internet", t))
    return tujuan


def ukur_upstream(sekarang: Optional[datetime] = None) -> Optional[dict]:
    """Satu kali pengukuran. Kembalikan ringkasan, atau None bila MikroTik tidak terhubung."""
    sekarang = sekarang or datetime.now()
    pool = _mikrotik_pool()
    try:
        api = pool.get_api()
        wan, gateway = _deteksi_wan(api)
        wan = str(CONFIG["UPSTREAM_WAN"] or "").strip() or wan
        rx = tx = None
        if wan:
            try:
                t = (api.get_resource("/interface").call("monitor-traffic", {"interface": wan, "once": ""}) or [{}])[0]
                rx = round(int(t.get("rx-bits-per-second", 0)) / 1e6, 2)
                tx = round(int(t.get("tx-bits-per-second", 0)) / 1e6, 2)
            except Exception as e:
                logger.warning(f"monitor-traffic WAN {wan} gagal: {e}")
        ping = []
        for jenis, alamat in _tujuan_upstream(gateway):
            try:
                h = api.get_resource("/").call("ping", {"address": alamat, "count": "5", "interval": "0.3"})
            except Exception as e:
                logger.warning(f"Ping upstream {alamat} gagal: {e}")
                h = []
            waktu = [w for w in (_ms(x.get("time")) for x in h or [] if x.get("status") != "timeout") if w is not None]
            akhir = (h or [{}])[-1]
            terkirim = int(akhir.get("sent") or 0) or max(5, len(h or []))
            diterima = int(akhir.get("received") or 0) if akhir.get("sent") else len(waktu)
            ping.append({"jenis": jenis, "tujuan": alamat,
                         "hilang_persen": round((terkirim - diterima) * 100 / terkirim) if terkirim else 100,
                         "rata_ms": _ms(akhir.get("avg-rtt")) if akhir.get("sent") else
                         (round(sum(waktu) / len(waktu), 1) if waktu else None),
                         "maks_ms": max(waktu) if waktu else None})
    except Exception as e:
        logger.warning(f"Pantau internet pusat: MikroTik tidak terhubung ({e})")
        return None
    finally:
        try:
            pool.disconnect()
        except Exception:
            pass
    ts = sekarang.isoformat(timespec="seconds")
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
    try:
        conn.executemany("INSERT INTO upstream_history VALUES (?,?,?,?,?,?)",
                         [(ts, p["jenis"], p["tujuan"], p["rata_ms"], p["maks_ms"], p["hilang_persen"]) for p in ping])
        if rx is not None:
            conn.execute("INSERT INTO upstream_trafik VALUES (?,?,?)", (ts, rx, tx))
        conn.commit()
    finally:
        conn.close()
    hasil = {"waktu": ts, "wan": wan, "gateway": gateway, "rx_mbps": rx, "tx_mbps": tx, "ping": ping}
    _upstream.update(terakhir=hasil, wan=wan, gateway=gateway)
    return hasil


def _nilai_upstream(h: dict) -> dict:
    """Kondisi dari satu pengukuran: internet putus/bermasalah, lambat, WAN penuh."""
    net = [p for p in h["ping"] if p["jenis"] == "internet"]
    gw = next((p for p in h["ping"] if p["jenis"] == "gateway"), None)
    loss = min((p["hilang_persen"] for p in net), default=0)
    rata = [p["rata_ms"] for p in net if p["rata_ms"] is not None]
    kap = float(CONFIG["UPSTREAM_KAPASITAS_MBPS"] or 0)
    pakai = round(h["rx_mbps"] * 100 / kap) if kap and h.get("rx_mbps") is not None else None
    return {"loss": loss, "buruk": bool(net) and loss >= CONFIG["UPSTREAM_LOSS_PERSEN"],
            "gateway_buruk": bool(gw) and gw["hilang_persen"] >= CONFIG["UPSTREAM_LOSS_PERSEN"],
            "rata_ms": round(min(rata), 1) if rata else None,
            "lambat": bool(rata) and min(rata) >= CONFIG["UPSTREAM_LATENSI_MS"],
            "pakai_persen": pakai, "penuh": pakai is not None and pakai >= CONFIG["UPSTREAM_PENUH_PERSEN"]}


def proses_upstream(h: Optional[dict], sekarang: Optional[datetime] = None) -> None:
    """Alert Telegram dengan konfirmasi 2-3 pengukuran berturut-turut, dan kabar saat pulih."""
    if not h:
        return
    sekarang = sekarang or datetime.now()
    n = _nilai_upstream(h)
    st = _upstream
    st["buruk"] = st["buruk"] + 1 if (n["buruk"] or n["lambat"]) else 0
    st["penuh"] = st["penuh"] + 1 if n["penuh"] else 0
    if st["buruk"] == 1:
        st["mulai_buruk"] = sekarang
    if st["buruk"] >= 2 and not st["alert_buruk"]:
        st["alert_buruk"] = True
        if n["buruk"] and n["gateway_buruk"]:
            dugaan = "Ping ke gateway ISP juga gagal: masalah di jalur/modem ISP atau kabel WAN MikroTik."
        elif n["buruk"]:
            dugaan = "Gateway ISP menjawab normal: gangguan di jaringan ISP ke internet."
        elif n["penuh"]:
            dugaan = "Bandwidth WAN sedang penuh: antrean panjang membuat latensi naik."
        else:
            dugaan = "Latensi tinggi tanpa bandwidth penuh: kemungkinan jaringan ISP padat."
        rinci = "\n".join(f"• {p['tujuan']}{' (gateway)' if p['jenis'] == 'gateway' else ''}: "
                          f"loss {p['hilang_persen']}%, {p['rata_ms'] if p['rata_ms'] is not None else '-'} ms"
                          for p in h["ping"])
        send_telegram_message(
            f"🌐 <b>Internet pusat {'bermasalah' if n['buruk'] else 'lambat'}</b>\n{rinci}\n"
            + (f"Bandwidth WAN: ⬇{h['rx_mbps']} / ⬆{h['tx_mbps']} Mbps"
               + (f" ({n['pakai_persen']}% kapasitas)" if n["pakai_persen"] is not None else "") + "\n"
               if h.get("rx_mbps") is not None else "")
            + f"Dugaan: {dugaan}\nSemua pelanggan ikut terdampak. Sejak ±{st['mulai_buruk'].strftime('%H:%M')}")
    elif st["buruk"] == 0 and st["alert_buruk"]:
        st["alert_buruk"] = False
        lama = round((sekarang - st["mulai_buruk"]).total_seconds() / 60) if st["mulai_buruk"] else None
        send_telegram_message(f"✅ <b>Internet pusat normal kembali</b>"
                              + (f" (±{lama} menit)" if lama is not None else "")
                              + (f"\nLatensi {n['rata_ms']} ms" if n["rata_ms"] is not None else ""))
    if st["penuh"] >= 3 and not st["alert_penuh"]:
        st["alert_penuh"] = True
        send_telegram_message(
            f"📶 <b>Bandwidth WAN hampir penuh</b>\n⬇{h['rx_mbps']} Mbps dari {CONFIG['UPSTREAM_KAPASITAS_MBPS']} Mbps "
            f"({n['pakai_persen']}%) selama ±{st['penuh'] * CONFIG['UPSTREAM_INTERVAL_DETIK'] // 60} menit.\n"
            "Pelanggan akan merasa lemot di jam ini. Pertimbangkan tambah kapasitas atau atur antrean (queue).")
    elif st["penuh"] == 0 and st["alert_penuh"] and (n["pakai_persen"] or 0) < CONFIG["UPSTREAM_PENUH_PERSEN"] - 10:
        st["alert_penuh"] = False


async def upstream_loop():
    await asyncio.sleep(30)
    while True:
        try:
            if CONFIG["UPSTREAM_AKTIF"]:
                h = await asyncio.to_thread(ukur_upstream)
                await asyncio.to_thread(proses_upstream, h)
        except Exception as e:
            logger.error(f"Error pantau internet pusat: {e}")
        await asyncio.sleep(max(30, int(CONFIG["UPSTREAM_INTERVAL_DETIK"])))


@app.get("/api/upstream")
def get_upstream(request: Request, jam: int = 24):
    """Kondisi & riwayat internet pusat untuk grafik."""
    _wajib_admin(request)
    jam = max(1, min(jam, 24 * 7))
    batas = (datetime.now() - timedelta(hours=jam)).isoformat(timespec="seconds")
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
    try:
        ping = conn.execute("SELECT ts, jenis, rata_ms, maks_ms, hilang_persen FROM upstream_history "
                            "WHERE ts >= ? ORDER BY ts", (batas,)).fetchall()
        trafik = conn.execute("SELECT ts, rx_mbps, tx_mbps FROM upstream_trafik WHERE ts >= ? ORDER BY ts",
                              (batas,)).fetchall()
    finally:
        conn.close()
    # Satu titik per pengukuran: latensi = tujuan internet tercepat, loss = tujuan internet terbaik.
    gw = _upstream.get("gateway")
    titik: dict = {}
    for ts, jenis, rata, maks, hilang in ping:
        t = titik.setdefault(ts, {"ts": ts, "rata_ms": None, "hilang_persen": None, "gateway_ms": None})
        if jenis == "gateway":
            t["gateway_ms"] = rata
            continue
        if rata is not None and (t["rata_ms"] is None or rata < t["rata_ms"]):
            t["rata_ms"] = rata
        if t["hilang_persen"] is None or hilang < t["hilang_persen"]:
            t["hilang_persen"] = hilang
    kini = _upstream.get("terakhir")
    return {"aktif": bool(CONFIG["UPSTREAM_AKTIF"]), "wan": _upstream.get("wan"), "gateway": gw,
            "kapasitas_mbps": CONFIG["UPSTREAM_KAPASITAS_MBPS"] or None,
            "batas": {"loss": CONFIG["UPSTREAM_LOSS_PERSEN"], "latensi": CONFIG["UPSTREAM_LATENSI_MS"],
                      "penuh": CONFIG["UPSTREAM_PENUH_PERSEN"]},
            "sekarang": dict(kini, **_nilai_upstream(kini)) if kini else None,
            "ping": list(titik.values()),
            "trafik": [{"ts": ts, "rx_mbps": rx, "tx_mbps": tx} for ts, rx, tx in trafik]}


@app.post("/api/upstream/ukur")
def ukur_upstream_sekarang(request: Request):
    _wajib_admin(request)
    h = ukur_upstream()
    if h is None:
        raise HTTPException(status_code=502, detail="MikroTik tidak bisa dihubungi.")
    return dict(h, **_nilai_upstream(h))


def _tren_optik(username: str, jam: int = 72) -> dict:
    batas = (datetime.now() - timedelta(hours=jam)).isoformat(timespec="seconds")
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"], timeout=15)
    try:
        r = conn.execute("SELECT MIN(onu_rx_dbm), AVG(onu_rx_dbm), MAX(onu_rx_dbm), COUNT(*) FROM onu_history "
                         "WHERE pppoe_username=? AND ts>=? AND onu_rx_dbm IS NOT NULL", (username, batas)).fetchone()
        u = conn.execute("SELECT AVG(MAX(rx_mbps, tx_mbps)), MAX(MAX(rx_mbps, tx_mbps)), AVG(rate_limit_mbps), COUNT(*) "
                         "FROM usage_history WHERE username=? AND ts>=?",
                         (username, (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds"))).fetchone()
    finally:
        conn.close()
    return {"onu_rx_min": r[0], "onu_rx_rata": round(r[1], 1) if r[1] is not None else None, "onu_rx_maks": r[2],
            "sampel_optik": r[3], "pakai_rata_mbps": round(u[0], 2) if u[0] is not None else None,
            "pakai_puncak_mbps": round(u[1], 2) if u[1] is not None else None,
            "limit_mbps": u[2], "sampel_pakai": u[3]}


def _kesimpulan(d: dict) -> list[dict]:
    """Aturan sederhana untuk menunjuk penyebab paling mungkin. tingkat: bad/warn/ok/info."""
    k = []
    if d["terisolir"]:
        k.append({"tingkat": "bad", "teks": "Pelanggan sedang DIISOLIR (tunggakan). Internet dibatasi karena itu, bukan gangguan."})
    if not d["pppoe"]["online"]:
        k.append({"tingkat": "bad", "teks": "PPPoE pelanggan tidak online. Cek ONU menyala/LOS, kabel, atau login PPPoE."})
    onu = d.get("onu")
    rx = onu.get("onu_rx_dbm") if onu else None
    if rx is not None:
        if rx < -27:
            k.append({"tingkat": "bad", "teks": f"Redaman ONU {rx} dBm (buruk). Cek konektor/kabel drop, sambungan di ODP, atau tekukan kabel."})
        elif rx < -25:
            k.append({"tingkat": "warn", "teks": f"Redaman ONU {rx} dBm (mendekati batas). Bisa menyebabkan lemot/putus saat cuaca buruk."})
    t = d.get("tren") or {}
    if t.get("onu_rx_min") is not None and rx is not None and t["onu_rx_min"] < rx - 2:
        k.append({"tingkat": "warn", "teks": f"Redaman sempat turun ke {t['onu_rx_min']} dBm dalam 3 hari terakhir (naik-turun) — kabel/konektor mungkin longgar."})
    limit = d["pppoe"].get("limit_mbps")
    live = d.get("lalu_lintas") or {}
    pakai = max(live.get("download_mbps") or 0, live.get("upload_mbps") or 0)
    if limit and pakai >= 0.9 * limit:
        k.append({"tingkat": "warn", "teks": f"Pemakaian sekarang {pakai} Mbps dari paket {limit} Mbps (penuh). Lemot karena kuota dipakai banyak perangkat/unduhan — sarankan upgrade paket."})
    elif limit and t.get("pakai_rata_mbps") and t["pakai_rata_mbps"] >= 0.7 * limit:
        k.append({"tingkat": "warn", "teks": f"Rata-rata pemakaian 24 jam {t['pakai_rata_mbps']} Mbps dari {limit} Mbps — sering mendekati batas paket."})
    if d.get("putus_24jam", 0) >= 3:
        k.append({"tingkat": "warn", "teks": f"Putus-sambung {d['putus_24jam']} kali dalam 24 jam terakhir."})
    acs = (d.get("acs") or {}).get("perangkat")
    if acs:
        if acs["perangkat_aktif"] >= 10:
            k.append({"tingkat": "warn", "teks": f"{acs['perangkat_aktif']} perangkat terhubung ke ONU. Banyak perangkat berbagi bandwidth paket."})
        if acs.get("uptime_detik") is not None and acs["uptime_detik"] < 1800:
            k.append({"tingkat": "info", "teks": "ONU baru menyala/restart kurang dari 30 menit lalu."})
    if not [x for x in k if x["tingkat"] in ("bad", "warn")]:
        k.append({"tingkat": "ok", "teks": "Jaringan sampai ONU terlihat normal. Kemungkinan dari sisi WiFi di rumah: jarak/tembok, "
                                           "sinyal lemah, channel bertabrakan, atau perangkat pelanggan. Coba tes ping, cek perangkat terhubung, atau reboot ONU."})
    return k


@app.get("/api/diagnosa/{username}")
def diagnosa(username: str, request: Request):
    _wajib_admin(request)
    conn = _billing_conn()
    try:
        p = conn.execute("SELECT p.username, p.nama, p.alamat, p.no_hp, p.status, k.nama AS paket, k.kecepatan_mbps "
                         "FROM pelanggan p LEFT JOIN paket k ON k.id = p.paket_id WHERE p.username=?", (username,)).fetchone()
        if not p:
            raise HTTPException(status_code=404, detail="Pelanggan tidak ditemukan.")
        terisolir = conn.execute("SELECT 1 FROM isolir WHERE username=?", (username,)).fetchone() is not None
        tunggak = conn.execute("SELECT COUNT(*), COALESCE(SUM(jumlah),0) FROM tagihan WHERE username=? AND status='belum'",
                               (username,)).fetchone()
        tiket = conn.execute(f"SELECT id, kategori, status FROM tiket WHERE username=? AND status IN {_STATUS_TIKET_TERBUKA}",
                             (username,)).fetchone()
    finally:
        conn.close()
    u = next((x for x in _latest_data.pppoe_users if x.username == username), None) if _latest_data else None
    onu = next((o for o in _latest_data.onu_list if o.pppoe_username == username), None) if _latest_data else None
    hasil = {
        "pelanggan": dict(p), "terisolir": terisolir, "tunggakan": {"jumlah": tunggak[0], "total": tunggak[1]},
        "tiket_terbuka": dict(tiket) if tiket else None, "data_monitoring": _latest_data is not None,
        "pppoe": {"online": u is not None, "ip": u.ip_address if u else None, "uptime": u.uptime if u else None,
                  "limit_mbps": (u.rate_limit_mbps if u else None) or p["kecepatan_mbps"], "mac": u.caller_id if u else None},
        "onu": (onu.model_dump() if hasattr(onu, "model_dump") else onu.dict()) if onu else None,
        "lalu_lintas": _mikrotik_lalu_lintas(username) if u else None,
        "tren": _tren_optik(username),
    }
    try:
        hasil["putus_24jam"] = sum(1 for k in get_riwayat_putus(username, 24)["kejadian"] if k["jenis"] == "putus")
    except Exception as e:
        logger.warning(f"Riwayat putus {username} gagal: {e}")
        hasil["putus_24jam"] = 0
    if _acs_aktif():
        try:
            hasil["acs"] = _info_acs(username)
        except HTTPException as e:
            hasil["acs"] = {"terhubung": False, "pesan": e.detail}
    else:
        hasil["acs"] = None
    hasil["kesimpulan"] = _kesimpulan(hasil)
    return hasil


# --- Cek DNS & MTU di MikroTik (hanya membaca) ---
def _ya(v) -> Optional[bool]:
    v = str(v or "").strip().lower()
    return True if v in ("yes", "true") else (False if v in ("no", "false") else None)


@app.get("/api/mikrotik/cek-dns-mtu")
def mikrotik_cek_dns_mtu(request: Request):
    """Baca pengaturan DNS, profil PPPoE, aturan MSS, dan server PPPoE dari MikroTik, lalu beri
    kesimpulan & perintah perbaikan untuk disalin. Tidak mengubah apa pun di MikroTik."""
    _wajib_admin(request)
    pool = _mikrotik_pool()
    try:
        api = pool.get_api()
        baca = lambda path: api.get_resource(path).get()
        dns = (baca("/ip/dns") or [{}])[0]
        profil = baca("/ppp/profile")
        secret = baca("/ppp/secret")
        try:
            mangle = baca("/ip/firewall/mangle")
        except Exception:
            mangle = []
        try:
            server = baca("/interface/pppoe-server/server")
        except Exception:
            server = []
        resolve = None
        try:
            r = api.get_resource("/").call("resolve", {"domain-name": "google.com"})
            # Hasil `resolve` dikirim RouterOS di pesan !done (bukan baris !re), jadi baris balasannya kosong.
            resolve = (getattr(r, "done_message", None) or {}).get("ret") or (r[0].get("ret") if r else None)
        except Exception as e:
            resolve = None
            resolve_galat = str(e)[:120]
        else:
            resolve_galat = None
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gagal membaca MikroTik: {e}")
    finally:
        try:
            pool.disconnect()
        except Exception:
            pass

    cek = []
    tambah = lambda tingkat, judul, ket, perintah=None: cek.append(
        {"tingkat": tingkat, "judul": judul, "keterangan": ket, "perintah": perintah})

    # 1. DNS MikroTik sendiri
    servers = ",".join(x for x in (dns.get("servers"), dns.get("dynamic-servers")) if x)
    remote = _ya(dns.get("allow-remote-requests"))
    if not servers:
        tambah("buruk", "MikroTik tidak punya server DNS", "MikroTik sendiri tidak bisa mencari alamat situs.",
               "/ip dns set servers=8.8.8.8,1.1.1.1")
    else:
        tambah("baik", "Server DNS MikroTik", f"{servers} · allow-remote-requests={'yes' if remote else 'no'}")
    if resolve:
        tambah("baik", "Uji DNS dari MikroTik", f"google.com → {resolve}")
    elif not resolve_galat or re.search(r"(?i)no such command|unknown|not supported", resolve_galat):
        tambah("info", "Uji DNS dari MikroTik tidak terbaca lewat API",
               "Uji manual di Winbox → New Terminal dengan perintah di bawah.", ":put [:resolve google.com]")
    else:
        tambah("buruk", "Uji DNS dari MikroTik gagal", "MikroTik tidak bisa menerjemahkan google.com"
               + (f" ({resolve_galat})" if resolve_galat else "") + ". Periksa server DNS / koneksi upstream.",
               "/ip dns set servers=8.8.8.8,1.1.1.1")

    # 2. Profil PPPoE yang dipakai pelanggan
    pakai = {}
    for x in secret:
        pakai[x.get("profile") or "default"] = pakai.get(x.get("profile") or "default", 0) + 1
    by_nama = {p.get("name"): p for p in profil}
    mss_bawaan = _ya((by_nama.get("default") or {}).get("change-tcp-mss"))
    mangle_mss = [m for m in mangle if m.get("action") == "change-mss" and _ya(m.get("disabled")) is not True]
    hasil_profil = []
    for nama, n in sorted(pakai.items(), key=lambda x: -x[1]):
        p = by_nama.get(nama, {})
        dns_p = p.get("dns-server") or ""
        mss_v = str(p.get("change-tcp-mss") or "default").lower()
        mss = mss_bawaan if mss_v == "default" else _ya(mss_v)
        hasil_profil.append({"profil": nama, "pelanggan": n, "dns_server": dns_p, "change_tcp_mss": mss_v,
                             "mss_efektif": mss, "local_address": p.get("local-address") or ""})
        if not dns_p:
            tujuan = (p.get("local-address") if remote and p.get("local-address") else "8.8.8.8,1.1.1.1")
            tambah("buruk" if nama != CONFIG["ISOLIR_PROFILE"] else "info",
                   f"Profil '{nama}' tidak membagikan DNS ({n} pelanggan)",
                   "Pelanggan di profil ini tidak menerima alamat DNS dari PPPoE. Gejala: WA chat jalan, tapi "
                   "status WA, Facebook, dan situs lain tidak terbuka. Berlaku setelah pelanggan tersambung ulang.",
                   f'/ppp profile set [find name="{nama}"] dns-server={tujuan}')
        if not mss and not mangle_mss:
            tambah("buruk" if nama != CONFIG["ISOLIR_PROFILE"] else "info",
                   f"Profil '{nama}': penyesuaian MSS mati ({n} pelanggan)",
                   "Paket data besar (gambar, video, halaman web) bisa tersangkut di PPPoE, sedangkan chat kecil tetap "
                   "jalan. Aktifkan change-tcp-mss.",
                   f'/ppp profile set [find name="{nama}"] change-tcp-mss=yes')
    if hasil_profil and not any(c["judul"].startswith("Profil") for c in cek):
        tambah("baik", "Profil PPPoE", "Semua profil yang dipakai pelanggan membagikan DNS dan menyesuaikan MSS.")
    if mangle_mss:
        tambah("baik", "Aturan MSS di firewall mangle", f"{len(mangle_mss)} aturan change-mss aktif.")

    # 3. MTU server PPPoE
    for sv in server:
        try:
            mtu = int(str(sv.get("max-mtu") or "0").split()[0] or 0)
        except ValueError:
            mtu = 0
        nama = sv.get("service-name") or sv.get("interface") or "?"
        if mtu > 1492:
            tambah("buruk", f"MTU server PPPoE '{nama}' {mtu}",
                   "PPPoE memakan 8 byte; MTU di atas 1492 membuat paket besar terpotong.",
                   f'/interface pppoe-server server set [find interface="{sv.get("interface")}"] max-mtu=1480 max-mru=1480')
        elif mtu:
            tambah("baik", f"MTU server PPPoE '{nama}'", f"max-mtu {mtu}")

    buruk = sum(1 for c in cek if c["tingkat"] == "buruk")
    catat_aktivitas(_oleh(request), "mikrotik_cek", f"Cek DNS & MTU: {buruk} masalah")
    return {"masalah": buruk, "cek": cek, "profil": hasil_profil}


@app.post("/api/diagnosa/{username}/ping")
def diagnosa_ping(username: str, request: Request):
    _wajib_admin(request)
    u = next((x for x in _latest_data.pppoe_users if x.username == username), None) if _latest_data else None
    if not u:
        raise HTTPException(status_code=400, detail="Pelanggan sedang tidak online, tidak bisa di-ping.")
    return _mikrotik_ping(u.ip_address)


# ============================================================
# 13. KIRIM PENGATURAN TR-069 KE ONU LEWAT OLT (telnet, C-Data FD16xx)
# ============================================================
# OLT meneruskan ACS URL/login ke ONU lewat OMCI, sama seperti menu "TR-069 Service
# Configuration" di web OLT. Perintah CLI-nya (dibaca dari show current-config):
#   interface gpon 0/0
#    ont tr069 <port> <onu> tr069-management enable
#    ont tr069 <port> <onu> acs-url <url>  acs-username <user> acs-password <sandi>
#    ont tr069 <port> <onu> inform enable interval 300
# ONU ZTE juga perlu jenis layanan WAN PPPoE berisi TR069 (diatur di web ONU).

CONFIG.update({
    "OLT_TELNET_PORT": int(_env("OLT_TELNET_PORT", "23")),
    "OLT_TELNET_USER": _env("OLT_TELNET_USER", ""),
    "OLT_TELNET_PASS": _env("OLT_TELNET_PASS", ""),
    "OLT_SIMPAN_PERINTAH": _env("OLT_SIMPAN_PERINTAH", "save"),   # simpan permanen setelah berubah
    "ACS_ONU_URL": _env("ACS_ONU_URL", "http://10.99.0.1:7547"),
    "ACS_ONU_USER": _env("ACS_ONU_USER", ""),
    "ACS_ONU_PASS": _env("ACS_ONU_PASS", ""),
    "ACS_ONU_INTERVAL": int(_env("ACS_ONU_INTERVAL", "300")),
})

_POLA_AMAN = re.compile(r"^[A-Za-z0-9._:/@+=~-]{1,128}$")   # tanpa spasi/baris baru: tidak bisa menyisipkan perintah


def _olt_telnet_aktif() -> bool:
    return bool(CONFIG["OLT_TELNET_USER"] and CONFIG["OLT_TELNET_PASS"] and CONFIG["ACS_ONU_USER"] and CONFIG["ACS_ONU_PASS"])


# Prompt CLI utuh di awal baris, mis. "FD1602S-B1#" atau "FD1602S-B1(config-gpon-0/0)#". Lebih ketat
# dari sekadar "#" di akhir: isi show current-config punya baris diawali "#" yang bisa terpotong.
_POLA_PROMPT = r"(?:^|\n)[^\s#>]+(?:\([^)]*\))?[>#] ?$"


class _TelnetOLT:
    """Klien telnet kecil (telnetlib sudah dihapus di Python 3.13). Menolak semua opsi telnet
    kecuali echo/suppress-go-ahead, cukup untuk CLI OLT."""
    IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240

    def __init__(self, host: str, port: int, timeout: float = 45.0):   # OLT bisa lambat saat menunggu jawaban ONU
        import socket
        self.timeout = timeout
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""
        self.terakhir = "(login)"

    def _terima(self) -> None:
        try:
            data = self.sock.recv(4096)
        except TimeoutError:
            raise
        except OSError as e:
            # mis. koneksi direset OLT (sesi telnet penuh / OLT sibuk menyimpan)
            raise ConnectionError(f"koneksi telnet diputus OLT ({e.__class__.__name__}: {e})") from None
        if not data:
            raise ConnectionError("koneksi ditutup OLT")
        out, i = bytearray(), 0
        while i < len(data):
            b = data[i]
            if b == self.IAC and i + 1 < len(data):
                cmd = data[i + 1]
                if cmd in (self.DO, self.DONT, self.WILL, self.WONT) and i + 2 < len(data):
                    opt = data[i + 2]
                    if cmd == self.DO:
                        self.sock.sendall(bytes([self.IAC, self.WONT, opt]))
                    elif cmd == self.WILL:
                        self.sock.sendall(bytes([self.IAC, self.DO if opt in (1, 3) else self.DONT, opt]))
                    i += 3
                    continue
                if cmd == self.SB:
                    akhir = data.find(bytes([self.IAC, self.SE]), i)
                    i = len(data) if akhir < 0 else akhir + 2
                    continue
                if cmd == self.IAC:
                    out.append(255)
                i += 2
                continue
            out.append(b)
            i += 1
        self.buf += bytes(out)

    def tunggu(self, pola: str) -> str:
        """Baca sampai regex `pola` cocok di akhir keluaran; '--More--' dilewati otomatis."""
        batas = time.monotonic() + self.timeout
        rx = re.compile(pola)
        while True:
            teks = self.buf.decode("utf-8", "replace")
            if "--More" in teks[-60:]:
                self.buf = self.buf.replace(b"--More", b"")
                self.sock.sendall(b" ")
            elif rx.search(teks):
                self.buf = b""
                return teks
            if time.monotonic() > batas:
                raise TimeoutError(f"OLT tidak membalas {int(self.timeout)} detik setelah perintah "
                                   f"'{_samarkan(self.terakhir)}'. Tulisan terakhir dari OLT: {_samarkan(teks[-200:])!r}")
            self.sock.settimeout(max(0.1, batas - time.monotonic()))
            try:
                self._terima()
            except TimeoutError:
                batas = 0      # putaran berikutnya melapor dengan konteks lengkap

    def kirim(self, baris: str, pola: str = _POLA_PROMPT) -> str:
        self.terakhir = baris
        self.sock.sendall(baris.encode() + b"\r\n")
        return self.tunggu(pola)

    def tutup(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


_POLA_KONFIRMASI = r"(?i)(y/n|yes/no)[^\n]*$"   # pertanyaan konfirmasi di baris terakhir, mis. "(y/n) [n]:"


def _simpan_olt(t: "_TelnetOLT") -> bool:
    """Simpan konfigurasi OLT secara permanen. Dicoba di mode config dulu, lalu di mode enable,
    dengan beberapa nama perintah yang umum dipakai firmware C-Data. Pertanyaan (y/n) dijawab y."""
    lama, t.timeout = t.timeout, 90.0          # menulis flash bisa butuh beberapa detik
    try:
        def coba(perintah: str) -> bool:
            out = t.kirim(perintah, r"(?i)[>#]\s*$|" + _POLA_KONFIRMASI[4:])
            if re.search(_POLA_KONFIRMASI, out):
                out = t.kirim("y")
            return _galat_cli(out) is None
        utama = CONFIG["OLT_SIMPAN_PERINTAH"] or "save"
        if coba(utama):
            return True
        t.kirim("end")
        for perintah in dict.fromkeys([utama, "save", "write", "write memory", "copy running-config startup-config"]):
            if coba(perintah):
                return True
        logger.error("OLT: konfigurasi TIDAK tersimpan permanen — tidak ada perintah simpan yang diterima.")
        return False
    finally:
        t.timeout = lama


def _galat_cli(keluaran: str) -> Optional[str]:
    for baris in keluaran.splitlines():
        b = baris.strip()
        if b.startswith("%") or re.match(r"(?i)^(error|failure|failed)\b", b):
            return b
    return None


def olt_kirim_tr069(daftar: list, reboot: bool = False) -> dict:
    """daftar = [("0/0/1", 3), ...]. Satu sesi telnet untuk semua ONU. Kembalikan hasil per ONU.
    reboot=True: TR-069 diikat ke `ip-index 0` lalu dilepas lagi (`unbind`), konfigurasi disimpan, lalu ONU
    yang berhasil dikirimi di-restart satu per satu (`ont reboot P O`). ZTE F609 dkk. baru memakai
    pengaturan TR-069 dari OLT setelah langkah ini."""
    if not _olt_telnet_aktif():
        raise HTTPException(status_code=503, detail="Login telnet OLT / login ACS ONU belum diisi di .env "
                                                    "(OLT_TELNET_USER, OLT_TELNET_PASS, ACS_ONU_USER, ACS_ONU_PASS).")
    url, user, sandi = CONFIG["ACS_ONU_URL"], CONFIG["ACS_ONU_USER"], CONFIG["ACS_ONU_PASS"]
    if not all(_POLA_AMAN.match(x) for x in (url, user, sandi)):
        raise HTTPException(status_code=500, detail="ACS_ONU_URL/USER/PASS di .env berisi spasi atau karakter yang tidak didukung.")
    interval = max(60, min(int(CONFIG["ACS_ONU_INTERVAL"]), 86400))
    per_if: dict = {}
    for pon, onu_id in daftar:
        m = re.fullmatch(r"(\d{1,2}/\d{1,2})/(\d{1,2})", str(pon))
        if not m or not 1 <= int(onu_id) <= 256:
            raise HTTPException(status_code=400, detail=f"Alamat ONU tidak valid: {pon} {onu_id}")
        per_if.setdefault(m.group(1), []).append((int(m.group(2)), int(onu_id), pon))
    hasil = []
    try:
        t = _TelnetOLT(CONFIG["OLT_HOST"], CONFIG["OLT_TELNET_PORT"])
    except OSError as e:
        raise HTTPException(status_code=502, detail=f"Tidak bisa telnet ke OLT {CONFIG['OLT_HOST']}: {e}")
    try:
        t.tunggu(r"(?i)(user ?name|login)\s*:\s*$")
        t.kirim(CONFIG["OLT_TELNET_USER"], r"(?i)password\s*:\s*$")
        masuk = t.kirim(CONFIG["OLT_TELNET_PASS"], r"(?i)([>#]\s*$|user ?name\s*:\s*$|fail|incorrect|invalid)")
        if not re.search(r"[>#]\s*$", masuk):
            raise HTTPException(status_code=502, detail="Login telnet OLT ditolak. Periksa OLT_TELNET_USER/OLT_TELNET_PASS.")
        if masuk.rstrip().endswith(">"):
            out = t.kirim("enable", r"(?i)(#\s*$|password\s*:\s*$)")
            if not out.rstrip().endswith("#"):
                t.kirim(CONFIG["OLT_TELNET_PASS"], r"#\s*$")
        t.kirim("config")
        for antarmuka, onus in per_if.items():
            out = t.kirim(f"interface gpon {antarmuka}")
            if _galat_cli(out):
                for port, onu_id, pon in onus:
                    hasil.append({"pon": pon, "onu_id": onu_id, "ok": False, "pesan": _galat_cli(out)})
                continue
            for port, onu_id, pon in onus:
                galat = None
                for perintah in (f"ont tr069 {port} {onu_id} tr069-management enable",
                                 f"ont tr069 {port} {onu_id} acs-url {url} acs-username {user} acs-password {sandi}",
                                 f"ont tr069 {port} {onu_id} inform enable interval {interval}"):
                    galat = _galat_cli(t.kirim(perintah))
                    if galat:
                        break
                if galat is None and reboot:
                    # Urutan yang terbukti di ZTE F609 (Gunawan 1/23, Endah 1/9): ikat ke ip-index 0 lalu
                    # lepas lagi, simpan, restart. Unbind saja tidak cukup (tidak ada ikatan yang dilepas);
                    # pasang-lalu-lepas membuat ONU menyusun ulang jalur TR-069 ke WAN yang punya internet.
                    # Konfigurasi akhir di OLT sama seperti sebelumnya (tanpa ip-index).
                    t.kirim(f"ont tr069 {port} {onu_id} ip-index 0")
                    t.kirim(f"ont tr069 {port} {onu_id} ip-index unbind")
                hasil.append({"pon": pon, "onu_id": onu_id, "ok": galat is None,
                              "pesan": galat or "Pengaturan TR-069 terkirim ke ONU."})
            t.kirim("exit")
        tersimpan = None
        if any(h["ok"] for h in hasil):
            # Wajib: tanpa save, perubahan hilang saat OLT restart.
            tersimpan = _simpan_olt(t)
        if reboot and any(h["ok"] for h in hasil):
            t.kirim("end")
            t.kirim("config")
            for antarmuka, onus in per_if.items():
                ok = [(port, onu_id) for port, onu_id, pon in onus
                      if any(h["ok"] and h["pon"] == pon and h["onu_id"] == onu_id for h in hasil)]
                if not ok:
                    continue
                t.kirim(f"interface gpon {antarmuka}")
                for port, onu_id in ok:
                    # Selalu satu ONU (port & nomor dari angka tervalidasi), tidak pernah "all".
                    out = t.kirim(f"ont reboot {port} {onu_id}", r"(?i)[>#]\s*$|" + _POLA_KONFIRMASI[4:])
                    if re.search(_POLA_KONFIRMASI, out):
                        out = t.kirim("y")
                    galat = _galat_cli(out)
                    for h in hasil:
                        if h["pon"] == f"{antarmuka}/{port}" and h["onu_id"] == onu_id:
                            h["reboot"] = galat is None
                            h["pesan"] += " ONU di-restart, internet pelanggan kembali ±2 menit." if galat is None \
                                else f" Restart ONU gagal: {galat}"
                t.kirim("exit")
        t.kirim("end")
    except (OSError, TimeoutError, ConnectionError) as e:
        raise HTTPException(status_code=502, detail=f"Sesi telnet OLT terputus: {e}")
    finally:
        t.tutup()
    return {"hasil": hasil, "tersimpan": tersimpan}


def _sn_terdaftar_acs() -> Optional[set]:
    if not _acs_aktif():
        return None
    try:
        r = _acs("GET", "/devices/", params={"query": "{}", "projection": "_deviceId._SerialNumber"}, timeout=10)
    except HTTPException:
        return None
    if not r.ok:
        return None
    return {str((d.get("_deviceId") or {}).get("_SerialNumber") or "").upper() for d in r.json()}


def _onu_di_acs() -> Optional[dict]:
    """Penanda ONU yang ada di GenieACS: nomor seri, username PPPoE, dan id dokumen.
    Nomor seri saja tidak cukup: sebagian ONU (ZTE/Fiberhome) melapor ke ACS dengan nomor seri
    yang berbeda dari SN GPON di OLT."""
    if not _acs_aktif():
        return None
    jalur = [f"{_IGD}.WANDevice.1.WANConnectionDevice.{w}.WANPPPConnection.{p}.Username"
             for w in range(1, 6) for p in range(1, 3)]
    try:
        r = _acs("GET", "/devices/", params={"query": "{}", "projection": ",".join(
            ["_id", "_deviceId._SerialNumber", "VirtualParameters.pppoeUsername"] + jalur)}, timeout=15)
    except HTTPException:
        return None
    if not r.ok:
        return None
    sn, user, ids = set(), {}, {}
    for d in r.json():
        ids[d.get("_id")] = d
        sn.add(str((d.get("_deviceId") or {}).get("_SerialNumber") or "").upper())
        for j in ["VirtualParameters.pppoeUsername"] + jalur:
            u = _nilai(d, j)
            if u:
                user.setdefault(str(u), []).append(d)
    return {"sn": sn, "user": user, "id": ids}


@app.get("/api/olt/tr069")
def olt_tr069_daftar(request: Request):
    """ONU yang terbaca dari OLT beserta status terdaftar di ACS, untuk dipilih."""
    _wajib_admin(request)
    acs = _onu_di_acs()
    nama, acs_id = {}, {}
    conn = _billing_conn()
    try:
        for p in conn.execute("SELECT username, nama, acs_id FROM pelanggan").fetchall():
            nama[p["username"]], acs_id[p["username"]] = p["nama"], p["acs_id"]
    finally:
        conn.close()
    onu = []
    for o in (_latest_data.onu_list if _latest_data else []):
        sn, u = (o.serial_number or "").upper(), o.pppoe_username
        ada = None
        if acs is not None and (sn or u):
            # Lewat PPPoE / id tersimpan hanya dihitung bila dokumennya bukan milik ONU lama yang sudah diganti.
            calon = (acs["user"].get(u, []) + ([acs["id"][acs_id[u]]] if acs_id.get(u) in acs["id"] else [])) if u else []
            ada = bool((sn and sn in acs["sn"]) or any(not _onu_lama(d, sn) for d in calon))
        onu.append({"pon": o.pon, "onu_id": o.onu_id, "sn": o.serial_number, "vendor": o.vendor, "model": o.model,
                    "online": o.online, "pppoe_username": u, "nama": nama.get(u), "terdaftar_acs": ada})
    onu.sort(key=lambda x: (x["terdaftar_acs"] is True, x["pon"], x["onu_id"]))
    return {"aktif": _olt_telnet_aktif(), "acs_url": CONFIG["ACS_ONU_URL"], "onu": onu}


class Tr069OnuIn(BaseModel):
    onu: list
    reboot: bool = False


@app.post("/api/olt/tr069")
def olt_tr069_kirim(d: Tr069OnuIn, request: Request):
    _wajib_admin(request)
    if not d.onu or len(d.onu) > 128:
        raise HTTPException(status_code=400, detail="Pilih 1–128 ONU.")
    try:
        daftar = [(str(x["pon"]), int(x["onu_id"])) for x in d.onu]
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Format ONU tidak valid.")
    hasil = olt_kirim_tr069(daftar, reboot=d.reboot)
    ok = sum(1 for h in hasil["hasil"] if h["ok"])
    catat_aktivitas(_oleh(request), "olt_tr069",
                    f"Kirim TR-069 lewat OLT ke {len(daftar)} ONU: {ok} berhasil"
                    + (f", {sum(1 for h in hasil['hasil'] if h.get('reboot'))} di-restart" if d.reboot else "")
                    + ("" if hasil["tersimpan"] is not False else " (belum tersimpan permanen di OLT)"))
    return hasil


@app.post("/api/olt/tr069/pelanggan/{username}")
def olt_tr069_pelanggan(username: str, request: Request, reboot: bool = False):
    _wajib_admin(request)
    onu = next((o for o in (_latest_data.onu_list if _latest_data else []) if o.pppoe_username == username), None)
    if not onu:
        raise HTTPException(status_code=404, detail="ONU pelanggan ini belum terbaca dari OLT (offline atau MAC belum cocok).")
    hasil = olt_kirim_tr069([(onu.pon, onu.onu_id)], reboot=reboot)
    h = hasil["hasil"][0]
    catat_aktivitas(_oleh(request), "olt_tr069", f"Kirim TR-069 lewat OLT ke ONU {username} ({onu.pon} onu {onu.onu_id}): "
                    + (("berhasil" + (", ONU di-restart" if h.get("reboot") else "")) if h["ok"] else h["pesan"]))
    return hasil


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
