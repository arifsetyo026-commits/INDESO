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
import hashlib
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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
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
    "NOTA_FOLDER": "nota",              # foto/PDF nota transaksi, di samping main.py
    "NOTA_MAKS_MB": 5,
    "NOTA_MAKS_PER_ITEM": 6,
    "NOTA_WAJIB_DI_ATAS": 100_000,      # pengeluaran >= ini tanpa nota ditandai di laporan
    "BACKUP_JAM": 2,                    # backup dibuat setelah jam ini tiap hari
    "BACKUP_SIMPAN": 14,                # jumlah backup harian yang disimpan
    "BACKUP_KIRIM_TELEGRAM": True,      # kirim berkas backup ke Telegram (ke tujuan laporan owner bila diisi)
    "BACKUP_NOTA_TELEGRAM": True,       # backup mingguan ikut mengirim nota baru (ZIP)
    "BACKUP_NOTA_MAKS_MB": 45,          # batas per ZIP (batas Telegram 50 MB per berkas)
    "BACKUP_HARI_KIRIM": 6,             # hari pengiriman mingguan: 0=Senin ... 6=Minggu
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
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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


# ============================================================
# 6a. ALERT & EVENT — log MikroTik, event OLT/ONU, kondisi perangkat
# ============================================================
# Event disimpan di isp_history.db tabel "event". Tingkat: kritis / peringatan / info.
# kunci_dedup UNIQUE supaya log yang sama tidak tersimpan dua kali walau
# dibaca ulang tiap siklus.

_BULAN_ROS = {b: i for i, b in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def init_event_db() -> None:
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"])
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
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"])
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
            logger.error(f"Error saat pengingat utang: {e}")
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
        # insentif_gratis = 1: pelanggan gratis yang tetap dihitung insentif mitra
        # tiap bulan walau tidak punya tagihan.
        if "insentif_gratis" not in kolom:
            conn.execute("ALTER TABLE pelanggan ADD COLUMN insentif_gratis INTEGER NOT NULL DEFAULT 0")
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

app = FastAPI(title="ISP Monitoring Dashboard")

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
_PBKDF2_ITERASI = 200_000
_POLA_USERNAME = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
_PERAN = ("owner", "admin", "mitra")
_PERAN_PENUH = ("owner", "admin")     # akses seluruh dashboard

# Alamat yang boleh dibuka tanpa login, dan yang boleh dibuka akun mitra.
# Selain itu: tanpa login -> 401/ke halaman login, mitra -> 403.
_JALUR_PUBLIK = re.compile(r"^/(login|favicon\.ico|api/login|api/setup|api/setup-status)$")
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
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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
    return FileResponse("login.html", headers={"Cache-Control": "no-cache"})


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
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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
    token = _buat_sesi(r[0], request)
    catat_aktivitas(r[0], "login", f"dari {ip}")
    resp = JSONResponse({"ok": True, "peran": r[2]})
    _pasang_cookie(resp, request, token)
    return resp


@app.post("/api/logout")
def logout(request: Request):
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"])
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
    conn = sqlite3.connect(CONFIG["HISTORY_DB_PATH"])
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
    "MITRA_INSENTIF_PER_PELANGGAN": {"label": "Insentif mitra per pelanggan lunas (Rp)", "tipe": "int", "min": 0, "max": 1_000_000},
    "PUTUS_TUNGGU_MENIT": {"label": "Tunggu sebelum alert putus (menit)", "tipe": "int", "min": 1, "max": 240},
    "DAILY_REPORT_HOUR": {"label": "Jam laporan harian Telegram (0-23)", "tipe": "int", "min": 0, "max": 23},
    "LAPORAN_BULANAN_AKTIF": {"label": "Laporan keuangan bulanan ke Telegram owner", "tipe": "pilihan",
                              "pilihan": ["ya", "tidak"], "owner": True},
    "LAPORAN_BULANAN_JAM": {"label": "Jam kirim laporan bulanan, tanggal 1 (0-23)", "tipe": "int", "min": 0, "max": 23,
                            "owner": True},
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
    if meta["tipe"] == "teks":
        nilai = str(nilai or "").strip()
        if not re.fullmatch(meta["pola"], nilai):
            raise HTTPException(status_code=400, detail=f"{meta['label']}: format tidak valid.")
        return ",".join(x.strip() for x in nilai.split(",") if x.strip())
    if nilai not in meta["pilihan"]:
        raise HTTPException(status_code=400, detail=f"{meta['label']} harus salah satu: {', '.join(meta['pilihan'])}.")
    return nilai


def _muat_pengaturan() -> None:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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
    insentif_gratis: Optional[bool] = None   # None = tidak diubah


@app.get("/api/data-pelanggan")
def list_data_pelanggan():
    conn = _billing_conn()
    try:
        rows = conn.execute(
            "SELECT p.username, p.nama, p.alamat, p.no_hp, p.paket_id, p.gratis, "
            "p.status, p.berhenti_sejak, p.tampil_mitra, p.insentif_gratis, k.nama AS paket, k.harga "
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
        insentif_gratis = lama["insentif_gratis"] if d.insentif_gratis is None else int(d.insentif_gratis)
        if not d.gratis:
            insentif_gratis = 0          # pelanggan berbayar dihitung dari tagihan lunas
        elif insentif_gratis:
            tampil_mitra = 1             # dihitung insentif = pasti terlihat oleh mitra
        conn.execute(
            "UPDATE pelanggan SET nama=?, alamat=?, no_hp=?, paket_id=?, gratis=?, status=?, berhenti_sejak=?, "
            "tampil_mitra=?, insentif_gratis=? WHERE username=?",
            (nama, (d.alamat or "").strip() or None, (d.no_hp or "").strip() or None,
             d.paket_id, int(d.gratis), status, berhenti_sejak, tampil_mitra, insentif_gratis, username),
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
    if lama["insentif_gratis"] != insentif_gratis:
        perubahan.append("gratis dihitung insentif mitra" if insentif_gratis else "gratis tidak dihitung insentif")
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
            "insentif_mitra": {k: insentif[k] for k in ("jumlah_lunas", "jumlah_gratis", "jumlah", "per_pelanggan", "total")} if conn.execute(
                "SELECT 1 FROM akun WHERE peran='mitra' LIMIT 1").fetchone() else None,
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
# 6f. KEUANGAN — kas masuk/keluar + pendapatan langganan dari billing
# ============================================================
# Dasar kas (cash basis): pendapatan langganan = tagihan LUNAS menurut tanggal
# bayar, jadi tidak perlu dicatat ulang. Transaksi manual tidak pernah dihapus;
# yang salah "dibatalkan" dengan alasan sehingga jejaknya tetap ada.
# Kategori dengan pengaruh_laba = 0 (setoran modal, ambil pribadi pemilik)
# memengaruhi saldo kas tetapi tidak dihitung sebagai pendapatan/beban.

_KATEGORI_AWAL = [
    # (nama, jenis, pengaruh_laba)
    ("Pembelian perangkat (ONT, router, kabel, dll.)", "keluar", 1),
    ("Bandwidth / upstream", "keluar", 1),
    ("Gaji & upah teknisi", "keluar", 1),
    ("Jasa & perbaikan", "keluar", 1),
    ("Listrik", "keluar", 1),
    ("Sewa tiang / lahan / tempat", "keluar", 1),
    ("Transportasi & BBM", "keluar", 1),
    ("Insentif mitra", "keluar", 1),
    ("Pajak & retribusi", "keluar", 1),
    ("Operasional lain", "keluar", 1),
    ("Ambil pribadi pemilik (prive)", "keluar", 0),
    ("Biaya pemasangan baru", "masuk", 1),
    ("Penjualan perangkat", "masuk", 1),
    ("Pemasukan lain", "masuk", 1),
    ("Setoran modal pemilik", "masuk", 0),
]
_METODE = ("tunai", "transfer", "qris", "lainnya")
# Kategori khusus untuk pembayaran/cicilan utang: mengurangi kas tetapi bukan
# beban (bebannya sudah dihitung saat pembelian kredit dicatat).
_KAT_BAYAR_UTANG = "Pembayaran utang"


def init_keuangan_db() -> None:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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
    """('2026-10-01', '2026-11-01') — awal inklusif, akhir eksklusif."""
    awal = datetime.strptime(periode + "-01", "%Y-%m-%d")
    akhir = (awal + timedelta(days=32)).replace(day=1)
    return awal.strftime("%Y-%m-%d"), akhir.strftime("%Y-%m-%d")


def _ringkas_periode(conn, periode: str) -> dict:
    """Laba: pendapatan (langganan lunas + pemasukan) - beban (pengeluaran tunai +
    pembelian kredit, dihitung pada tanggal pembelian). Kas: hanya uang yang
    benar-benar keluar/masuk (cicilan utang mengurangi kas, bukan beban lagi)."""
    awal, akhir = _batas_periode(periode)
    langganan = conn.execute(
        "SELECT COALESCE(SUM(jumlah),0), COUNT(*) FROM tagihan WHERE status='lunas' AND tgl_bayar >= ? AND tgl_bayar < ?",
        (awal, akhir),
    ).fetchone()
    per_kat = conn.execute(
        "SELECT k.id, k.nama, t.jenis, k.pengaruh_laba, k.khusus, SUM(t.jumlah) AS total, COUNT(*) AS n "
        "FROM transaksi t JOIN kategori_kas k ON k.id = t.kategori_id "
        "WHERE t.status='aktif' AND t.tanggal >= ? AND t.tanggal < ? GROUP BY k.id",
        (awal, akhir),
    ).fetchall()
    kredit = {r["kategori_id"]: r for r in conn.execute(
        "SELECT u.kategori_id, k.nama, k.pengaruh_laba, SUM(u.jumlah) AS total, COUNT(*) AS n "
        "FROM utang u JOIN kategori_kas k ON k.id = u.kategori_id "
        "WHERE u.status='aktif' AND u.tanggal >= ? AND u.tanggal < ? GROUP BY u.kategori_id",
        (awal, akhir),
    ).fetchall()}
    masuk_ops = sum(r["total"] for r in per_kat if r["jenis"] == "masuk" and r["pengaruh_laba"])
    keluar_tunai = sum(r["total"] for r in per_kat if r["jenis"] == "keluar" and r["pengaruh_laba"])
    masuk_lain = sum(r["total"] for r in per_kat if r["jenis"] == "masuk" and not r["pengaruh_laba"])
    prive = sum(r["total"] for r in per_kat if r["jenis"] == "keluar" and not r["pengaruh_laba"] and not r["khusus"])
    bayar_utang = sum(r["total"] for r in per_kat if r["khusus"])
    beban_kredit = sum(r["total"] for r in kredit.values())
    # Rincian per kategori: gabungan tunai + kredit (tanpa kategori khusus pembayaran utang)
    gabung: dict[int, dict] = {}
    for r in per_kat:
        if r["khusus"]:
            continue
        gabung[r["id"]] = {"id": r["id"], "nama": r["nama"], "jenis": r["jenis"], "pengaruh_laba": r["pengaruh_laba"],
                           "total": r["total"], "n": r["n"], "kredit": 0}
    for kid, r in kredit.items():
        g = gabung.setdefault(kid, {"id": kid, "nama": r["nama"], "jenis": "keluar", "pengaruh_laba": r["pengaruh_laba"],
                                    "total": 0, "n": 0, "kredit": 0})
        g["total"] += r["total"]
        g["n"] += r["n"]
        g["kredit"] += r["total"]
    pendapatan = langganan[0] + masuk_ops
    beban = keluar_tunai + beban_kredit
    return {
        "periode": periode,
        "pendapatan_langganan": langganan[0], "jumlah_pembayaran": langganan[1],
        "pendapatan_lain": masuk_ops, "pendapatan": pendapatan,
        "beban": beban, "beban_kredit": beban_kredit, "laba": pendapatan - beban,
        "modal_masuk": masuk_lain, "prive": prive, "bayar_utang": bayar_utang,
        "arus_kas": pendapatan + masuk_lain - keluar_tunai - prive - bayar_utang,
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


def _geser_periode(periode: str, n: int) -> str:
    th, bl = map(int, periode.split("-"))
    total = th * 12 + (bl - 1) + n
    return f"{total // 12}-{total % 12 + 1:02d}"


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
        insentif_dibayar = conn.execute(
            "SELECT COALESCE(SUM(t.jumlah),0) FROM transaksi t JOIN kategori_kas k ON k.id = t.kategori_id "
            "WHERE t.status='aktif' AND k.nama = 'Insentif mitra' AND t.tanggal >= ? AND t.tanggal < ?", (awal, akhir)
        ).fetchone()[0]
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
    return {
        **ini,
        "bulan_lalu": {k: lalu[k] for k in ("pendapatan", "beban", "laba")},
        "tren": tren,
        "saldo_awal": saldo_awal, "saldo_akhir": saldo_akhir,
        "tagihan_periode": {"total": tagihan_periode[0], "jumlah": tagihan_periode[1]},
        "piutang_periode": {"total": piutang_periode[0], "jumlah": piutang_periode[1]},
        "piutang_total": {"total": piutang_total[0], "jumlah": piutang_total[1]},
        "insentif_mitra": {"terhitung": insentif["total"], "dibayar": insentif_dibayar},
        "utang": utang_info,
        "tanpa_nota": tanpa_nota,
        "pelanggan_berbayar": pelanggan_aktif,
        "transaksi_terbaru": [dict(r) for r in terbaru],
    }


@app.get("/api/keuangan/kategori")
def keuangan_kategori(request: Request):
    _wajib_admin(request)
    conn = _billing_conn()
    try:
        rows = conn.execute("SELECT id, nama, jenis, pengaruh_laba, aktif, khusus FROM kategori_kas ORDER BY jenis DESC, id").fetchall()
    finally:
        conn.close()
    return {"kategori": [dict(r) for r in rows]}


class KategoriIn(BaseModel):
    nama: str
    jenis: str
    pengaruh_laba: bool = True


@app.post("/api/keuangan/kategori")
def keuangan_tambah_kategori(d: KategoriIn, request: Request):
    _wajib_admin(request)
    nama = d.nama.strip()
    if not nama or len(nama) > 80 or d.jenis not in ("masuk", "keluar"):
        raise HTTPException(status_code=400, detail="Nama kategori wajib (maks. 80 karakter) dan jenis masuk/keluar.")
    conn = _billing_conn()
    try:
        try:
            cur = conn.execute("INSERT INTO kategori_kas (nama, jenis, pengaruh_laba) VALUES (?,?,?)",
                               (nama, d.jenis, int(d.pengaruh_laba)))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Kategori itu sudah ada.")
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "kas_kategori", f"tambah kategori {d.jenis}: {nama}")
    return {"id": cur.lastrowid}


class BatalIn(BaseModel):
    alasan: str


def _daftar_utang(conn, hanya_belum: bool = False) -> list[dict]:
    rows = conn.execute(
        "SELECT u.*, k.nama AS kategori, "
        "(SELECT COALESCE(SUM(t.jumlah),0) FROM transaksi t WHERE t.utang_id = u.id AND t.status='aktif') AS dibayar "
        "FROM utang u JOIN kategori_kas k ON k.id = u.kategori_id "
        "ORDER BY u.status, COALESCE(u.jatuh_tempo, '9999-12-31'), u.id DESC"
    ).fetchall()
    hasil = []
    hari = datetime.now().strftime("%Y-%m-%d")
    for r in rows:
        d = dict(r)
        d["sisa"] = d["jumlah"] - d["dibayar"] if d["status"] == "aktif" else 0
        d["lunas"] = d["status"] == "aktif" and d["sisa"] <= 0
        d["lewat_tempo"] = bool(d["status"] == "aktif" and d["sisa"] > 0 and d["jatuh_tempo"] and d["jatuh_tempo"] < hari)
        if hanya_belum and (d["status"] != "aktif" or d["sisa"] <= 0):
            continue
        hasil.append(d)
    return hasil


def _ringkas_utang(conn) -> dict:
    belum = _daftar_utang(conn, hanya_belum=True)
    lewat = [u for u in belum if u["lewat_tempo"]]
    tempo = sorted((u for u in belum if u["jatuh_tempo"]), key=lambda u: u["jatuh_tempo"])
    return {"sisa": sum(u["sisa"] for u in belum), "jumlah": len(belum),
            "lewat_tempo": len(lewat), "lewat_tempo_total": sum(u["sisa"] for u in lewat),
            "terdekat": {k: tempo[0][k] for k in ("pemasok", "sisa", "jatuh_tempo")} if tempo else None}


class UtangIn(BaseModel):
    tanggal: str
    pemasok: str
    kategori_id: int
    jumlah: int
    jatuh_tempo: Optional[str] = None
    keterangan: Optional[str] = None
    uang_muka: int = 0
    metode_uang_muka: str = "tunai"


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
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG["NOTA_FOLDER"])
    os.makedirs(folder, exist_ok=True)
    return folder


def init_nota_db() -> None:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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
        _boleh_kelola_nota(request, _ambil_pemilik_nota(conn, r["pemilik"], r["pemilik_id"]))
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
    conn = _billing_conn()
    try:
        k = conn.execute("SELECT nama, jenis, pengaruh_laba, khusus FROM kategori_kas WHERE id = ?", (d.kategori_id,)).fetchone()
        if not k or k["jenis"] != "keluar" or k["khusus"] or not k["pengaruh_laba"]:
            raise HTTPException(status_code=400, detail="Pilih kategori pengeluaran (beban) untuk pembelian kredit.")
        oleh = _oleh(request)
        cur = conn.execute(
            "INSERT INTO utang (tanggal, pemasok, kategori_id, jumlah, jatuh_tempo, keterangan, dicatat_oleh, dicatat_pada) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (d.tanggal, pemasok, d.kategori_id, d.jumlah, d.jatuh_tempo or None, (d.keterangan or "").strip()[:500] or None,
             oleh, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        uid = cur.lastrowid
        if d.uang_muka:
            _catat_bayar_utang(conn, uid, BayarUtangIn(tanggal=d.tanggal, jumlah=d.uang_muka, metode=d.metode_uang_muka,
                                                       keterangan="Uang muka"), oleh)
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "utang_catat",
                    f"#U{uid} {pemasok} {_rp(d.jumlah)} · {k['nama']} · {d.tanggal}"
                    + (f" · jatuh tempo {d.jatuh_tempo}" if d.jatuh_tempo else "")
                    + (f" · uang muka {_rp(d.uang_muka)}" if d.uang_muka else ""))
    return {"id": uid}


@app.post("/api/keuangan/utang/{uid}/bayar")
def keuangan_bayar_utang(uid: int, d: BayarUtangIn, request: Request):
    _wajib_admin(request)
    _cek_tanggal_lalu(d.tanggal)
    if d.metode not in _METODE:
        raise HTTPException(status_code=400, detail="Metode tidak valid.")
    conn = _billing_conn()
    try:
        u = next((x for x in _daftar_utang(conn) if x["id"] == uid), None)
        if not u or u["status"] != "aktif":
            raise HTTPException(status_code=404, detail="Utang tidak ditemukan atau sudah dibatalkan.")
        if u["sisa"] <= 0:
            raise HTTPException(status_code=400, detail="Utang ini sudah lunas.")
        if not 0 < d.jumlah <= u["sisa"]:
            raise HTTPException(status_code=400, detail=f"Jumlah bayar harus 1 sampai {_rp(u['sisa'])} (sisa utang).")
        if d.tanggal < u["tanggal"]:
            raise HTTPException(status_code=400, detail="Tanggal bayar tidak boleh sebelum tanggal pembelian.")
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
            raise HTTPException(status_code=404, detail="Utang tidak ditemukan atau sudah dibatalkan.")
        _boleh_ubah_transaksi(request, u)
        if conn.execute("SELECT 1 FROM transaksi WHERE utang_id = ? AND status='aktif'", (uid,)).fetchone():
            raise HTTPException(status_code=400, detail="Sudah ada pembayaran. Batalkan pembayarannya dulu di tab Transaksi.")
        conn.execute("UPDATE utang SET status='batal', batal_oleh=?, batal_pada=?, batal_alasan=? WHERE id=?",
                     (_oleh(request), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), alasan[:300], uid))
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "utang_batal", f"#U{uid} {u['pemasok']} {_rp(u['jumlah'])} dibatalkan: {alasan[:120]}")
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
                baris.append(f"• {_esc(u['pemasok'])}: sisa {_rp(u['sisa'])}, jatuh tempo {u['jatuh_tempo'][8:10]}/{u['jatuh_tempo'][5:7]} ({ket})")
            send_telegram_message(f"💳 <b>Utang jatuh tempo</b> ({len(dekat)})\\n" + "\\n".join(baris[:15])
                                  + "\\nBayar lewat dashboard → Keuangan → Utang.")
        conn.execute("INSERT OR IGNORE INTO otomasi_log VALUES (?, 'utang')", (hari,))
        conn.commit()
        _pengingat_utang_terakhir = hari
    finally:
        conn.close()


class TransaksiIn(BaseModel):
    tanggal: str
    jenis: str
    kategori_id: int
    jumlah: int
    metode: str = "tunai"
    keterangan: Optional[str] = None


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
    k = conn.execute("SELECT nama, jenis, aktif, khusus FROM kategori_kas WHERE id = ?", (d.kategori_id,)).fetchone()
    if not k or k["jenis"] != d.jenis:
        raise HTTPException(status_code=400, detail="Kategori tidak cocok dengan jenis transaksi.")
    if k["khusus"]:
        raise HTTPException(status_code=400, detail="Pembayaran utang dicatat dari tab Utang.")
    return k["nama"]


@app.post("/api/keuangan/transaksi")
def keuangan_tambah(d: TransaksiIn, request: Request):
    _wajib_admin(request)
    conn = _billing_conn()
    try:
        kat = _cek_transaksi(conn, d)
        cur = conn.execute(
            "INSERT INTO transaksi (tanggal, jenis, kategori_id, jumlah, metode, keterangan, dicatat_oleh, dicatat_pada) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (d.tanggal, d.jenis, d.kategori_id, d.jumlah, d.metode, (d.keterangan or "").strip()[:500] or None,
             _oleh(request), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    finally:
        conn.close()
    catat_aktivitas(_oleh(request), "kas_catat",
                    f"#{cur.lastrowid} {'masuk' if d.jenis == 'masuk' else 'keluar'} {_rp(d.jumlah)} · {kat} · {d.tanggal}"
                    + (f" · {d.keterangan.strip()[:80]}" if d.keterangan and d.keterangan.strip() else ""))
    return {"id": cur.lastrowid}


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
            raise HTTPException(status_code=400, detail="Pembayaran utang tidak bisa diubah. Batalkan, lalu bayar ulang dari tab Utang.")
        _boleh_ubah_transaksi(request, t)
        kat = _cek_transaksi(conn, d)
        conn.execute(
            "UPDATE transaksi SET tanggal=?, jenis=?, kategori_id=?, jumlah=?, metode=?, keterangan=?, "
            "diubah_oleh=?, diubah_pada=? WHERE id=?",
            (d.tanggal, d.jenis, d.kategori_id, d.jumlah, d.metode, (d.keterangan or "").strip()[:500] or None,
             _oleh(request), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tid),
        )
        conn.commit()
    finally:
        conn.close()
    ubah = [f"{n}: {a} → {b}" for n, a, b in (
        ("tanggal", t["tanggal"], d.tanggal), ("jumlah", _rp(t["jumlah"]), _rp(d.jumlah)),
        ("kategori", t["kategori"], kat), ("metode", t["metode"], d.metode),
        ("keterangan", t["keterangan"] or "-", (d.keterangan or "").strip() or "-")) if a != b]
    if ubah:
        catat_aktivitas(_oleh(request), "kas_ubah", f"#{tid} " + "; ".join(ubah))
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
           "t.batal_oleh, t.batal_pada, t.batal_alasan, t.utang_id, "
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
def keuangan_list(periode: str, request: Request, jenis: str = "", kategori_id: Optional[int] = None,
                  cari: str = "", batal: bool = True):
    _wajib_admin(request)
    if not _periode_valid(periode):
        raise HTTPException(status_code=400, detail="Format periode harus YYYY-MM.")
    conn = _billing_conn()
    try:
        rows = _query_transaksi(conn, periode, jenis, kategori_id, cari, batal)
    finally:
        conn.close()
    aktif = [r for r in rows if r["status"] == "aktif"]
    conn = _billing_conn()
    try:
        nota = _daftar_nota(conn, "transaksi", [r["id"] for r in rows])
    finally:
        conn.close()
    return {
        "periode": periode, "saya": request.state.user, "peran": request.state.peran,
        "ringkasan": {"masuk": sum(r["jumlah"] for r in aktif if r["jenis"] == "masuk"),
                      "keluar": sum(r["jumlah"] for r in aktif if r["jenis"] == "keluar"),
                      "jumlah": len(aktif), "dibatalkan": len(rows) - len(aktif)},
        "transaksi": [dict(r, nota=nota.get(r["id"], [])) for r in rows],
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
    w.writerow(["Tanggal", "Jenis", "Kategori", "Jumlah", "Metode", "Keterangan", "Dicatat oleh", "Status", "Alasan batal"])
    def aman(v):
        # Cegah formula injection saat CSV dibuka di Excel (=, +, -, @ di awal sel)
        v = "" if v is None else str(v)
        return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
    for b in bayar:
        w.writerow([b["tgl_bayar"], "masuk", "Pembayaran langganan", b["jumlah"], "", aman(f"{b['nama']} ({b['username']}) periode {b['periode']}"), "billing", "aktif", ""])
    for t in trx:
        ket = t["keterangan"] or ""
        if t["utang_id"]:
            ket = f"Bayar utang #U{t['utang_id']} {t['pemasok'] or ''}" + (f" · {ket}" if ket else "")
        w.writerow([t["tanggal"], t["jenis"], aman(t["kategori"]), t["jumlah"], t["metode"], aman(ket),
                    aman(t["dicatat_oleh"]), t["status"], aman(t["batal_alasan"])])
    for u in utang:
        w.writerow([u["tanggal"], "kredit (utang)", aman(u["kategori"]), u["jumlah"], "kredit",
                    aman(f"#U{u['id']} {u['pemasok']}" + (f" · jatuh tempo {u['jatuh_tempo']}" if u["jatuh_tempo"] else "")
                         + (f" · {u['keterangan']}" if u["keterangan"] else "")),
                    aman(u["dicatat_oleh"]), u["status"], aman(u["batal_alasan"])])
    # BOM supaya Excel membaca UTF-8 dengan benar
    return "\ufeff" + buf.getvalue()


@app.get("/api/keuangan/ekspor")
def keuangan_ekspor(periode: str, request: Request):
    _wajib_admin(request)
    if not _periode_valid(periode):
        raise HTTPException(status_code=400, detail="Format periode harus YYYY-MM.")
    isi = _buat_csv_keuangan(periode)
    catat_aktivitas(_oleh(request), "kas_ekspor", f"periode {periode}")
    return Response(isi, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="keuangan-{periode}.csv"'})


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
        ins_bayar = conn.execute(
            "SELECT COALESCE(SUM(t.jumlah),0) FROM transaksi t JOIN kategori_kas k ON k.id = t.kategori_id "
            "WHERE t.status='aktif' AND k.nama='Insentif mitra' AND t.tanggal >= ? AND t.tanggal < ?", (awal, akhir)
        ).fetchone()[0]
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
        hc = sqlite3.connect(CONFIG["HISTORY_DB_PATH"])
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
        L.append(f"  • Pemasukan lain: {_rp(r['pendapatan_lain'])}")
    L.append(f"Pengeluaran: <b>{_rp(r['beban'])}</b>{_persen_ubah(r['beban'], lalu['beban'])}"
             + (f" (termasuk kredit {_rp(r['beban_kredit'])})" if r["beban_kredit"] else ""))
    top = [k for k in r["per_kategori"] if k["jenis"] == "keluar" and k["pengaruh_laba"]][:3]
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
        lain.append(f"bayar utang {_rp(r['bayar_utang'])}")
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
        L += ["", "<b>Utang ke pemasok</b>", f"Sisa: <b>{_rp(utang['sisa'])}</b> ({utang['jumlah']} utang)"]
        if utang["lewat_tempo"]:
            L.append(f"⚠️ Lewat jatuh tempo: {_rp(utang['lewat_tempo_total'])} ({utang['lewat_tempo']} utang)")
        if utang["terdekat"]:
            t = utang["terdekat"]
            L.append(f"Terdekat: {_esc(t['pemasok'])} {_rp(t['sisa'])}, {t['jatuh_tempo'][8:10]}/{t['jatuh_tempo'][5:7]}")
    if insentif["total"] or ins_bayar:
        sisa = max(0, insentif["total"] - ins_bayar)
        L += ["", "<b>Insentif mitra</b>",
              f"Terhitung {_rp(insentif['total'])} ({insentif['jumlah']} pelanggan), dibayar {_rp(ins_bayar)}"
              + (f" — <b>kurang {_rp(sisa)}</b>" if sisa else " ✅")]
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

def _hitung_insentif(conn, periode: str) -> dict:
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


def kirim_dokumen_telegram(path: str, caption: str, chat_id: Optional[str] = None) -> bool:
    url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_BOT_TOKEN']}/sendDocument"
    with open(path, "rb") as f:
        r = requests.post(
            url, data={"chat_id": chat_id or CONFIG["TELEGRAM_CHAT_ID"], "caption": caption},
            files={"document": f}, timeout=60,
        )
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
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS status_sistem (kunci TEXT PRIMARY KEY, nilai TEXT NOT NULL)")
        r = conn.execute("SELECT nilai FROM status_sistem WHERE kunci = ?", (kunci,)).fetchone()
        return r[0] if r else bawaan
    finally:
        conn.close()


def _status_set(kunci: str, nilai: str) -> None:
    conn = sqlite3.connect(CONFIG["BILLING_DB_PATH"])
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
    if CONFIG["BACKUP_KIRIM_TELEGRAM"] and (pertama or sekarang.weekday() == CONFIG["BACKUP_HARI_KIRIM"]):
        caption = (f"💾 Backup database {hari}\n{j['pelanggan']} pelanggan, {j['paket']} paket, "
                   f"{j['tagihan']} tagihan, {j.get('transaksi', 0)} transaksi ({info['ukuran_kb']} KB)")
        try:
            if not all(kirim_dokumen_telegram(info["berkas"], caption, cid) for cid in _tujuan_backup()):
                send_telegram_message("⚠️ Backup tersimpan di HP, tapi gagal dikirim ke Telegram.")
        except Exception as e:
            send_telegram_message(f"⚠️ Backup tersimpan di HP, tapi gagal dikirim ke Telegram: {_esc(str(e))}")
        if CONFIG["BACKUP_NOTA_TELEGRAM"]:
            try:
                hn = kirim_backup_nota()
                if not hn["terkirim"]:
                    send_telegram_message(f"⚠️ Backup nota baru terkirim {hn['bagian']}/{hn['total_bagian']} bagian; "
                                          "sisanya dicoba lagi minggu depan (atau kirim manual dari Pengaturan).")
            except Exception as e:
                logger.error(f"Backup nota gagal: {e}")
                send_telegram_message(f"⚠️ Backup nota gagal: {_esc(str(e))}")
    logger.warning(f"Backup database selesai: {info}")


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
        info["terkirim_telegram"] = all(kirim_dokumen_telegram(
            info["berkas"],
            f"💾 Backup manual oleh {_oleh(request)}\n{j['pelanggan']} pelanggan, {j['paket']} paket, "
            f"{j['tagihan']} tagihan, {j.get('transaksi', 0)} transaksi", cid,
        ) for cid in _tujuan_backup())
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
        raise HTTPException(status_code=502, detail=f"Telegram tidak terjangkau: {e}")
    if not ok:
        raise HTTPException(status_code=502, detail="Telegram menolak pesan. Cek TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID.")
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
