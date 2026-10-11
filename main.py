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
import base64
import logging
import os
import secrets
import sys
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from html import escape as _esc

import requests
from dotenv import load_dotenv

load_dotenv()
import routeros_api
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

# ============================================================
# 1. KONFIGURASI — SESUAIKAN DENGAN JARINGAN ANDA
# ============================================================
# Rahasia (password, token) dan alamat perangkat dibaca dari berkas .env
# (lihat .env.example). Nilai lain cukup diubah langsung di bawah.

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

    # --- Login dashboard (HTTP Basic). Kosongkan DASHBOARD_PASSWORD dan
    # DASHBOARD_USERS = tanpa login. DASHBOARD_USERS untuk beberapa akun
    # sekaligus, format "nama:password,nama2:password2". ---
    "DASHBOARD_USER": _env("DASHBOARD_USER", "admin"),
    "DASHBOARD_PASSWORD": os.environ.get("DASHBOARD_PASSWORD", ""),
    "DASHBOARD_USERS": os.environ.get("DASHBOARD_USERS", ""),
    # Akun mitra: hanya bisa membuka halaman mitra (daftar pelanggan, ubah kontak,
    # rekap insentif). Format sama: "nama:password,nama2:password2".
    "MITRA_USERS": os.environ.get("MITRA_USERS", ""),
    "MITRA_INSENTIF_PER_PELANGGAN": int(_env("MITRA_INSENTIF_PER_PELANGGAN", "10000")),

    # --- History Database (SQLite) ---
    "HISTORY_DB_PATH": "isp_history.db",
    "BILLING_DB_PATH": "billing.db",
    "JATUH_TEMPO_TANGGAL": 5,
    "TAGIHAN_JAM": 8,                       # otomasi tagihan baru jalan setelah jam ini
    "TAGIHAN_PENGINGAT_HARI": [3, 5, 10],   # tanggal kirim daftar belum bayar ke Telegram
    "ISOLIR_MODE": "aktif",            # "aktif" = isolir sungguhan, "simulasi" = hanya kirim daftar ke Telegram
    "ISOLIR_PROFILE": "ISOLIR",
    "ISOLIR_JAM": 8,                   # isolir dijalankan setelah jam ini
    "ISOLIR_TOLERANSI_HARI": 0,        # 0 = isolir mulai sehari setelah jatuh tempo, 2 = beri keringanan 2 hari
    "ISOLIR_MAKS_SEKALI": 8,           # lebih dari ini, isolir ditahan dan minta konfirmasi
    "BACKUP_FOLDER": "backup",          # folder backup, di samping main.py
    "BACKUP_JAM": 2,                    # backup dibuat setelah jam ini tiap hari
    "BACKUP_SIMPAN": 14,                # jumlah backup harian yang disimpan
    "BACKUP_KIRIM_TELEGRAM": True,      # kirim berkas backup ke Telegram
    "BACKUP_HARI_KIRIM": 6,             # hari pengiriman mingguan: 0=Senin ... 6=Minggu
    "HISTORY_INTERVAL_SECONDS": 300,   # simpan snapshot tiap 5 menit, bukan tiap 30 detik
    "HISTORY_RETENTION_DAYS": 30,      # data lebih lama dari ini dibuang otomatis

    # --- Deteksi "boncos" / kandidat upgrade paket ---
    # Kalau MikroTik melaporkan rate-limit pelanggan, itu dipakai sebagai acuan
    # (rasio pemakaian terhadap limit). Kalau tidak ada info rate-limit,
    # fallback ke angka absolut BONCOS_DEFAULT_MBPS.
    "BONCOS_RATIO_THRESHOLD": 0.85,
    "BONCOS_DEFAULT_MBPS": 8.0,
    "BONCOS_MIN_SAMPLES": 10,           # minimal jumlah data sebelum dianggap valid, bukan kebetulan sesaat
    "BONCOS_LOOKBACK_DAYS": 7,

    # --- Deteksi pelanggan sering putus-nyambung ---
    # Ini PERKIRAAN, bukan angka pasti — dihitung dari berapa kali pelanggan
    # "kepergok" uptime-nya masih pendek saat snapshot diambil tiap 5 menit.
    # Kalau pelanggan connect-disconnect di antara dua snapshot, itu tidak akan
    # kehitung. Cukup buat prioritas cek, bukan bukti mutlak.
    "SHORT_UPTIME_THRESHOLD_SECONDS": 600,
    "SHORT_UPTIME_MIN_COUNT": 3,
    "PUTUS_LOOKBACK_DAYS": 1,

    # --- Laporan Harian ---
    "DAILY_REPORT_ENABLED": True,
    "DAILY_REPORT_HOUR": 7,     # jam kirim (waktu HP/server, 24 jam)
    "DAILY_REPORT_MINUTE": 0,

    # --- Interval ---
    "POLL_INTERVAL_SECONDS": 120,
    "ALERT_COOLDOWN_MINUTES": 720,   # 12 jam: satu alert per ONU, lalu diam sampai 12 jam kemudian
    "PUTUS_TUNGGU_MENIT": 10,         # tunggu sekian menit sebelum kirim peringatan putus
    "PUTUS_MASSAL_MIN": 5,            # sekian pelanggan putus bersamaan = satu pesan gangguan massal
    "ALERT_OFFLINE_LANGSUNG": False,  # False = matikan peringatan 'ONU OFFLINE' langsung yang lama
    "SNMP_TIMEOUT_SECONDS": 15,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("isp-dashboard")


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
        connection = routeros_api.RouterOsApiPool(
            CONFIG["MIKROTIK_HOST"],
            username=CONFIG["MIKROTIK_USER"],
            password=CONFIG["MIKROTIK_PASSWORD"],
            port=CONFIG["MIKROTIK_PORT"],
            use_ssl=CONFIG["MIKROTIK_USE_SSL"],
            ssl_verify=False,
            plaintext_login=True,
        )
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
    return results


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


def send_telegram_message(text: str) -> None:
    if not CONFIG["TELEGRAM_ENABLED"]:
        return
    url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_BOT_TOKEN']}/sendMessage"
    try:
        requests.post(
            url,
            data={"chat_id": CONFIG["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
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
            await asyncio.to_thread(proses_putus_pelanggan, pppoe_users, mikrotik_error, onu_list)
        except Exception as e:
            logger.error(f"Error saat proses putus pelanggan: {e}")
        try:
            await asyncio.to_thread(proses_tagihan_harian)
        except Exception as e:
            logger.error(f"Error saat proses tagihan harian: {e}")
        try:
            await asyncio.to_thread(proses_isolir)
        except Exception as e:
            logger.error(f"Error saat proses isolir: {e}")
        try:
            await asyncio.to_thread(proses_backup_harian)
        except Exception as e:
            logger.error(f"Error saat proses backup harian: {e}")

        _latest_data = _build_dashboard(pppoe_users, mikrotik_error, onu_list)

        await asyncio.sleep(CONFIG["POLL_INTERVAL_SECONDS"])


# ============================================================
# 6b. HISTORY DATABASE (SQLite) — bandwidth & redaman
# ============================================================
# Kenapa SQLite: satu file, tidak perlu server database terpisah, cukup
# ringan buat 24/7 di Termux. Ditulis tiap HISTORY_INTERVAL_SECONDS (bukan
# tiap siklus 30 detik) supaya ukuran file tidak membengkak cepat.

def init_history_db() -> None:
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"])
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_onu_hist_ts ON onu_history(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_hist_ts_user ON usage_history(ts, username)")
        conn.commit()
    finally:
        conn.close()

def init_billing_db() -> None:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
    try:
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
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"])
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
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"])
    try:
        conn.execute("DELETE FROM onu_history WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM usage_history WHERE ts < ?", (cutoff,))
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
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"])
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


def get_frequent_disconnect_candidates() -> list[dict]:
    """PERKIRAAN, bukan angka pasti — dihitung dari berapa kali pelanggan
    'kepergok' uptime-nya masih pendek saat snapshot 5-menitan diambil.
    Reconnect yang terjadi di antara dua snapshot tidak akan kehitung,
    jadi angka ini adalah batas bawah, bukan jumlah pasti."""
    since = (datetime.now() - timedelta(days=CONFIG["PUTUS_LOOKBACK_DAYS"])).isoformat(timespec="seconds")
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"])
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT username, COUNT(*) AS short_uptime_count
            FROM usage_history
            WHERE ts >= ? AND uptime_seconds IS NOT NULL AND uptime_seconds < ?
            GROUP BY username
            HAVING short_uptime_count >= ?
            ORDER BY short_uptime_count DESC
            """,
            (since, CONFIG["SHORT_UPTIME_THRESHOLD_SECONDS"], CONFIG["SHORT_UPTIME_MIN_COUNT"]),
        ).fetchall()
    finally:
        conn.close()
    return [{"username": r["username"], "short_uptime_count": r["short_uptime_count"]} for r in rows]


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
        putus = get_frequent_disconnect_candidates()
        if putus:
            lines.append("")
            lines.append(f"🔁 <b>Sering putus ({len(putus)})</b> — perlu dicek:")
            for p in putus[:5]:
                lines.append(f"- {_esc(p['username'])}: {p['short_uptime_count']}x kepergok uptime pendek")
    except Exception as e:
        logger.error(f"Gagal hitung kandidat putus untuk laporan: {e}")

    return "\n".join(lines)


_last_report_date: Optional[str] = None


def _laporan_sudah_terkirim(hari: str) -> bool:
    """Dicatat di otomasi_log supaya laporan tidak terkirim ulang saat program dinyalakan ulang."""
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
    try:
        return conn.execute(
            "SELECT 1 FROM otomasi_log WHERE tanggal=? AND jenis='laporan'", (hari,)
        ).fetchone() is not None
    finally:
        conn.close()


def _tandai_laporan_terkirim(hari: str) -> None:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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
                await asyncio.to_thread(send_telegram_message, text)
                await asyncio.to_thread(cleanup_old_history)
                await asyncio.to_thread(_tandai_laporan_terkirim, today_str)
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
        return f"Tidak ada pelanggan dengan pola sering putus dalam {CONFIG['PUTUS_LOOKBACK_DAYS']} hari terakhir."
    lines = [
        f"🔁 <b>{len(candidates)} pelanggan sering putus</b> ({CONFIG['PUTUS_LOOKBACK_DAYS']} hari terakhir, perkiraan):",
    ]
    for p in candidates[:15]:
        lines.append(f"- {_esc(p['username'])}: {p['short_uptime_count']}x kepergok uptime pendek")
    lines.append("\n<i>Catatan: ini perkiraan dari snapshot tiap 5 menit, bisa lebih sering dari yang tercatat.</i>")
    return "\n".join(lines)


_HELP_TEXT = (
    "Perintah yang tersedia:\n"
    "<code>/cek nama</code> — detail satu pelanggan\n"
    "<code>/masalah</code> — daftar ONU WARNING/CRITICAL saat ini\n"
    "<code>/boncos</code> — kandidat upgrade paket\n"
    "<code>/putus</code> — pelanggan yang sering putus-nyambung\n"
    "<code>/help</code> — tampilkan pesan ini"
)


def _handle_telegram_command(text: str) -> None:
    text = (text or "").strip()
    if not text.startswith("/"):
        return

    command, _, rest = text.partition(" ")
    command = command.lower()

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

app = FastAPI(title="ISP Monitoring Dashboard")

# Dashboard dan API dilayani dari origin yang sama, jadi CORS tidak dibuka:
# dengan CORS "*" situs lain yang dibuka admin di browser bisa ikut memanggil
# API (mis. mengisolir pelanggan).


def _parse_akun(teks: str) -> dict[str, str]:
    akun = {}
    for item in teks.split(","):
        nama, _, pw = item.strip().partition(":")
        if nama.strip() and pw:
            akun[nama.strip()] = pw
    return akun


def _daftar_akun() -> dict[str, tuple[str, str]]:
    """{nama: (password, peran)}. Peran "admin" = akses penuh, "mitra" = halaman mitra saja."""
    akun = {nama: (pw, "mitra") for nama, pw in _parse_akun(CONFIG["MITRA_USERS"]).items()}
    admin = _parse_akun(CONFIG["DASHBOARD_USERS"])
    if CONFIG["DASHBOARD_PASSWORD"]:
        admin[CONFIG["DASHBOARD_USER"]] = CONFIG["DASHBOARD_PASSWORD"]
    for nama in set(akun) & set(admin):
        logger.warning(f"Akun '{nama}' ada di DASHBOARD_USERS dan MITRA_USERS — dipakai sebagai admin.")
    akun.update({nama: (pw, "admin") for nama, pw in admin.items()})
    if akun and not admin:
        # Tanpa akun admin, siapa pun yang tidak login akan diperlakukan admin.
        sys.exit("MITRA_USERS diisi tapi belum ada akun admin. Isi DASHBOARD_PASSWORD atau DASHBOARD_USERS dulu.")
    return akun


_AKUN = _daftar_akun()

# Satu-satunya alamat yang boleh dibuka akun mitra. Selain ini ditolak 403,
# jadi mitra tidak bisa membuka data tagihan, isolir, jaringan, dsb. walau tahu URL-nya.
_JALUR_MITRA = re.compile(r"^/(|favicon\.ico|api/mitra/.*)$")


@app.middleware("http")
async def wajib_login(request: Request, call_next):
    """HTTP Basic auth untuk semua halaman dan API, aktif bila ada akun di .env.
    Nama akun yang login disimpan di request.state.user untuk log aktivitas."""
    request.state.user = "tanpa-login"
    request.state.peran = "admin"
    if _AKUN:
        user_ok = None
        skema, _, kredensial = request.headers.get("authorization", "").partition(" ")
        if skema.lower() == "basic":
            try:
                user, _, pw = base64.b64decode(kredensial).decode("utf-8").partition(":")
                benar = _AKUN.get(user)
                # compare_digest tetap dijalankan walau user tidak ada, supaya waktunya sama
                if secrets.compare_digest(pw.encode(), (benar[0] if benar else "\0").encode()) and benar is not None:
                    user_ok = user
            except Exception:
                user_ok = None
        if user_ok is None:
            return Response(
                "Login diperlukan.", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Internet Desa", charset="UTF-8"'},
            )
        request.state.user = user_ok
        request.state.peran = _AKUN[user_ok][1]
        if request.state.peran == "mitra" and not _JALUR_MITRA.match(request.url.path):
            return Response("Akun mitra tidak punya akses ke halaman ini.", status_code=403)
    elif request.url.path.startswith("/api/mitra/"):
        return Response("Fitur mitra butuh login. Isi MITRA_USERS dan akun admin di .env.", status_code=403)
    return await call_next(request)


_background_tasks: list[asyncio.Task] = []


@app.on_event("startup")
async def on_startup():
    await asyncio.to_thread(init_history_db)
    await asyncio.to_thread(init_billing_db)
    logger.info(
        f"Target: MikroTik {CONFIG['MIKROTIK_HOST']}:{CONFIG['MIKROTIK_PORT']}, "
        f"OLT SNMP v{CONFIG['OLT_SNMP_VERSION']} {CONFIG['OLT_HOST']}:{CONFIG['OLT_SNMP_PORT']}"
    )
    if not _AKUN:
        logger.warning("Belum ada akun login (DASHBOARD_PASSWORD / DASHBOARD_USERS): dashboard dan API bisa dibuka siapa saja di jaringan ini.")
    else:
        logger.info("Login dashboard aktif: " + ", ".join(f"{n} ({p})" for n, (_pw, p) in sorted(_AKUN.items())))
    # Simpan referensinya: task tanpa referensi bisa dibersihkan garbage collector.
    _background_tasks.extend([
        asyncio.create_task(background_monitor_loop()),
        asyncio.create_task(telegram_command_listener()),
        asyncio.create_task(history_logger_loop()),
        asyncio.create_task(daily_report_loop()),
    ])
    logger.info("Semua background task dimulai: monitor, telegram listener, history logger, daily report.")


@app.get("/")
async def serve_dashboard(request: Request):
    # no-cache: browser selalu cek ulang ke server, jadi update index.html langsung terpakai
    halaman = "mitra.html" if request.state.peran == "mitra" else "index.html"
    return FileResponse(halaman, headers={"Cache-Control": "no-cache"})

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


@app.get("/api/pelanggan/{nama}")
async def get_pelanggan(nama: str):
    """Cari pelanggan by nama (substring, case-insensitive) — versi HTTP dari
    perintah /cek di Telegram, buat dipakai lewat browser/curl."""
    matches = find_onu_by_name(nama)
    if not matches:
        return {"found": 0, "results": []}
    return {"found": len(matches), "results": [(m.model_dump() if hasattr(m, "model_dump") else m.dict()) for m in matches]}

@app.get("/api/boncos")
async def get_boncos():
    return {"candidates": await asyncio.to_thread(get_boncos_candidates)}


@app.get("/api/putus")
async def get_putus():
    return {"candidates": await asyncio.to_thread(get_frequent_disconnect_candidates)}

class PaketIn(BaseModel):
    nama: str
    kecepatan_mbps: float
    harga: int


def _billing_conn():
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
    conn.row_factory = sqlite3.Row
    return conn


def catat_aktivitas(oleh: str, aksi: str, detail: str = "") -> None:
    """Catat aksi penting (siapa, kapan, apa) untuk audit internal. Tidak boleh
    menggagalkan aksi utamanya, jadi error cukup di-log."""
    try:
        conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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
    return getattr(request.state, "user", None) or "tanpa-login"


@app.get("/api/aktivitas")
def list_aktivitas(limit: int = 200, cari: str = ""):
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
    connection = routeros_api.RouterOsApiPool(
        CONFIG["MIKROTIK_HOST"],
        username=CONFIG["MIKROTIK_USER"],
        password=CONFIG["MIKROTIK_PASSWORD"],
        port=CONFIG["MIKROTIK_PORT"],
        use_ssl=CONFIG["MIKROTIK_USE_SSL"],
        ssl_verify=False,
        plaintext_login=True,
    )
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

class PelangganIn(BaseModel):
    nama: str
    alamat: Optional[str] = None
    no_hp: Optional[str] = None
    paket_id: Optional[int] = None
    gratis: bool = False
    status: Optional[str] = None    # "aktif" / "berhenti"; None = tidak diubah
    tampil_mitra: Optional[bool] = None   # None = tidak diubah


@app.get("/api/data-pelanggan")
def list_data_pelanggan():
    conn = _billing_conn()
    try:
        rows = conn.execute(
            "SELECT p.username, p.nama, p.alamat, p.no_hp, p.paket_id, p.gratis, "
            "p.status, p.berhenti_sejak, p.tampil_mitra, k.nama AS paket, k.harga "
            "FROM pelanggan p LEFT JOIN paket k ON k.id = p.paket_id "
            "ORDER BY p.nama COLLATE NOCASE"
        ).fetchall()
        return {"pelanggan": [dict(r) for r in rows]}
    finally:
        conn.close()


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
        tampil_mitra = lama["tampil_mitra"] if d.tampil_mitra is None else int(d.tampil_mitra)
        conn.execute(
            "UPDATE pelanggan SET nama=?, alamat=?, no_hp=?, paket_id=?, gratis=?, status=?, berhenti_sejak=?, "
            "tampil_mitra=? WHERE username=?",
            (nama, (d.alamat or "").strip() or None, (d.no_hp or "").strip() or None,
             d.paket_id, int(d.gratis), status, berhenti_sejak, tampil_mitra, username),
        )
        terhapus = 0
        if d.gratis:
            terhapus = conn.execute("DELETE FROM tagihan WHERE username=? AND status='belum'", (username,)).rowcount
        elif status == "berhenti" and lama["status"] != "berhenti":
            # Tagihan bulan-bulan setelah bulan berhenti dibatalkan; tunggakan
            # sampai bulan ini tetap ada karena layanannya sudah dipakai.
            terhapus = conn.execute(
                "DELETE FROM tagihan WHERE username=? AND status='belum' AND periode > ?",
                (username, sekarang.strftime("%Y-%m")),
            ).rowcount
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
    if (lama["alamat"] or "") != (d.alamat or "").strip() or (lama["no_hp"] or "") != (d.no_hp or "").strip():
        perubahan.append("kontak/alamat diubah")
    if terhapus:
        perubahan.append(f"{terhapus} tagihan belum bayar dihapus")
    if perubahan:
        catat_aktivitas(_oleh(request), "pelanggan_ubah", f"{username}: " + "; ".join(perubahan))
    return {"ok": True, "tagihan_dihapus": terhapus}


def _periode_valid(periode: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", periode))


@app.post("/api/tagihan/buat")
def buat_tagihan_api(periode: str, request: Request):
    hasil = buat_tagihan(periode)
    catat_aktivitas(_oleh(request), "tagihan_buat",
                    f"periode {periode}: {hasil['baru']} baru, {hasil['sudah_ada']} sudah ada")
    return hasil


def buat_tagihan(periode: str):
    if not _periode_valid(periode):
        raise HTTPException(status_code=400, detail="Format periode harus YYYY-MM, contoh 2026-11.")
    jatuh_tempo = f"{periode}-{CONFIG['JATUH_TEMPO_TANGGAL']:02d}"
    conn = _billing_conn()
    try:
        rows = conn.execute(
            "SELECT p.username, k.harga FROM pelanggan p JOIN paket k ON k.id = p.paket_id "
            "WHERE p.gratis = 0 AND p.status = 'aktif'"
        ).fetchall()
        baru = 0
        for r in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO tagihan (username, periode, jumlah, jatuh_tempo) VALUES (?,?,?,?)",
                (r["username"], periode, r["harga"], jatuh_tempo),
            )
            baru += cur.rowcount
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
            "SELECT t.id, t.username, p.nama, p.no_hp, t.jumlah, t.jatuh_tempo, t.status, t.tgl_bayar "
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
            "insentif_mitra": {"jumlah_lunas": insentif["jumlah_lunas"], "per_pelanggan": insentif["per_pelanggan"],
                               "total": insentif["total"]} if CONFIG["MITRA_USERS"] else None,
            "ringkasan": {"jumlah_tagihan": len(rows), "total": total,
                          "terbayar": lunas, "belum_bayar": total - lunas},
            "tagihan": [dict(r) for r in rows],
            "tanpa_tagihan": [dict(r) for r in tanpa_tagihan],
        }
    finally:
        conn.close()


def _info_tagihan(conn, tagihan_id: int):
    r = conn.execute(
        "SELECT t.username, t.periode, t.jumlah, t.status FROM tagihan t WHERE t.id=?", (tagihan_id,)
    ).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Tagihan tidak ditemukan.")
    return r


@app.post("/api/tagihan/{tagihan_id}/lunas")
def tandai_lunas(tagihan_id: int, request: Request):
    conn = _billing_conn()
    try:
        t = _info_tagihan(conn, tagihan_id)
        conn.execute(
            "UPDATE tagihan SET status='lunas', tgl_bayar=? WHERE id=?",
            (datetime.now().strftime("%Y-%m-%d"), tagihan_id),
        )
        conn.commit()
    finally:
        conn.close()
    if t["status"] != "lunas":
        catat_aktivitas(_oleh(request), "tagihan_lunas", f"{t['username']} {t['periode']} {_rp(t['jumlah'])}")
    return {"ok": True}


@app.post("/api/tagihan/{tagihan_id}/batal")
def batalkan_lunas(tagihan_id: int, request: Request):
    conn = _billing_conn()
    try:
        t = _info_tagihan(conn, tagihan_id)
        conn.execute("UPDATE tagihan SET status='belum', tgl_bayar=NULL WHERE id=?", (tagihan_id,))
        conn.commit()
    finally:
        conn.close()
    if t["status"] == "lunas":
        catat_aktivitas(_oleh(request), "tagihan_batal_lunas", f"{t['username']} {t['periode']} {_rp(t['jumlah'])}")
    return {"ok": True}


# ============================================================
# 6e. HALAMAN MITRA — hanya pelanggan tampil_mitra=1, kontak saja yang bisa diubah
# ============================================================

def _hitung_insentif(conn, periode: str) -> dict:
    """Insentif mitra = jumlah pelanggan (yang ditampilkan ke mitra) dengan tagihan
    periode itu LUNAS x tarif per pelanggan. Gratis/menunggak/berhenti tanpa
    pembayaran otomatis tidak terhitung karena tidak punya tagihan lunas."""
    rows = conn.execute(
        "SELECT p.username, p.nama, t.tgl_bayar FROM tagihan t JOIN pelanggan p ON p.username = t.username "
        "WHERE t.periode = ? AND t.status = 'lunas' AND p.tampil_mitra = 1 ORDER BY p.nama COLLATE NOCASE",
        (periode,),
    ).fetchall()
    tarif = CONFIG["MITRA_INSENTIF_PER_PELANGGAN"]
    return {"periode": periode, "per_pelanggan": tarif, "jumlah_lunas": len(rows),
            "total": len(rows) * tarif, "pelanggan": [dict(r) for r in rows]}


@app.get("/api/mitra/pelanggan")
def mitra_list_pelanggan(periode: str, request: Request):
    if not _periode_valid(periode):
        raise HTTPException(status_code=400, detail="Format periode harus YYYY-MM.")
    conn = _billing_conn()
    try:
        rows = conn.execute(
            "SELECT p.username, p.nama, p.alamat, p.no_hp, p.status, k.nama AS paket, "
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
    finally:
        conn.close()
    return {"akun": _oleh(request), "periode": periode, "insentif": insentif,
            "pelanggan": [dict(r) for r in rows]}


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

    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"])
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

    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"])
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

    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"])
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
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"])
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
            if st["alert"] and u in aktif:
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
                + ("" if st["awal"] else f" (±{lama} menit)")
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
                mulai = _putus_aktif[u]["mulai"]
                dugaan, _kat, lokasi = _dugaan_putus(u, onu_by_key, mulai)
                send_telegram_message(
                    f"🔴 <b>PUTUS lebih dari {tunggu} menit</b>\n"
                    f"{_esc(d.get('nama') or u)} ({_esc(u)})\n"
                    f"Mulai ±{mulai.strftime('%H:%M')} (sudah {round((sekarang - mulai).total_seconds() / 60)} menit)\n"
                    f"Dugaan: {dugaan}\n"
                    f"Alamat: {_esc(d.get('alamat') or '-')}\nHP: {_esc(d.get('no_hp') or '-')}\n"
                    f"{lokasi}"
                )
                _putus_aktif[u]["alert"] = True
                logger.warning(f"Alert PUTUS terkirim untuk {u}")
_BLN_PANJANG = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
                "Agustus", "September", "Oktober", "November", "Desember"]


def _rp(n) -> str:
    return "Rp " + f"{int(n):,}".replace(",", ".")


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
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG["BACKUP_FOLDER"])
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
    sumber = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
    salinan = sqlite3.connect(tujuan)
    try:
        sumber.backup(salinan)
    finally:
        salinan.close()
        sumber.close()

    cek = sqlite3.connect(tujuan)
    try:
        integritas = cek.execute("PRAGMA integrity_check").fetchone()[0]
        jumlah = {
            t: cek.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("pelanggan", "paket", "tagihan")
        }
    finally:
        cek.close()

    ukuran_kb = round(os.path.getsize(tujuan) / 1024, 1)
    for lama in _daftar_backup(folder)[:-CONFIG["BACKUP_SIMPAN"]]:
        if os.path.join(folder, lama) != tujuan:    # berkas yang baru dibuat tidak pernah dihapus
            os.remove(os.path.join(folder, lama))
    return {"berkas": tujuan, "ukuran_kb": ukuran_kb, "integritas": integritas, "jumlah": jumlah}


def kirim_dokumen_telegram(path: str, caption: str) -> bool:
    url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_BOT_TOKEN']}/sendDocument"
    with open(path, "rb") as f:
        r = requests.post(
            url, data={"chat_id": CONFIG["TELEGRAM_CHAT_ID"], "caption": caption},
            files={"document": f}, timeout=60,
        )
    return r.ok


_backup_gagal_terakhir = None


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
    if CONFIG["BACKUP_KIRIM_TELEGRAM"] and (pertama or sekarang.weekday() == CONFIG["BACKUP_HARI_KIRIM"]):
        caption = (f"💾 Backup database {hari}\n{j['pelanggan']} pelanggan, {j['paket']} paket, "
                   f"{j['tagihan']} tagihan ({info['ukuran_kb']} KB)")
        try:
            if not kirim_dokumen_telegram(info["berkas"], caption):
                send_telegram_message("⚠️ Backup tersimpan di HP, tapi gagal dikirim ke Telegram.")
        except Exception as e:
            send_telegram_message(f"⚠️ Backup tersimpan di HP, tapi gagal dikirim ke Telegram: {_esc(str(e))}")
    logger.warning(f"Backup database selesai: {info}")


@app.post("/api/backup")
def backup_sekarang(request: Request, kirim: bool = False):
    """Buat backup sekarang juga. Tambahkan ?kirim=true untuk ikut mengirim ke Telegram."""
    try:
        info = buat_backup()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup gagal: {e}")
    catat_aktivitas(_oleh(request), "backup", os.path.basename(info["berkas"]))
    if kirim:
        j = info["jumlah"]
        info["terkirim_telegram"] = kirim_dokumen_telegram(
            info["berkas"],
            f"💾 Backup manual\n{j['pelanggan']} pelanggan, {j['paket']} paket, {j['tagihan']} tagihan",
        )
    return info
  
@app.get("/api/test-telegram")
async def test_telegram():
    send_telegram_message("✅ Test notifikasi dari ISP Monitoring Dashboard berhasil.")
    return {"status": "sent"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
