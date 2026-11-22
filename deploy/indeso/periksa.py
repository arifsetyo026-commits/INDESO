"""Periksa kesehatan INDESO dalam sekali jalan. Dipanggil lewat:

    sudo bash /opt/indeso/deploy/indeso/cek.sh

Hanya MEMBACA (tidak mengubah apa pun) dan tidak pernah mencetak password/token. Hasilnya aman dikirim ke pengembang.
Setiap baris: ✅ baik · ⚠️ perlu diperhatikan · ❌ bermasalah, diikuti saran perbaikan dalam bahasa sehari-hari.
"""
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

APP = os.environ.get("INDESO_APP", "/opt/indeso")
DATA = os.environ.get("INDESO_DATA_DIR", "/var/lib/indeso")
ENV = os.environ.get("INDESO_ENV", "/etc/indeso/indeso.env")
# Sama dengan main.py: hanya isian _env_wajib yang membuat INDESO tidak mau menyala bila kosong.
WAJIB = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "MIKROTIK_PASSWORD")
# Nilai bawaan main.py bila isian kosong (supaya perangkatnya tetap ikut diperiksa).
BAWAAN = {"MIKROTIK_HOST": "192.168.50.1", "MIKROTIK_PORT": "8728", "OLT_HOST": "192.168.2.100", "OLT_TELNET_PORT": "23"}
RAHASIA = re.compile(r"(?i)(pass|token|sandi|secret|community)")

hasil = {"ok": 0, "peringatan": 0, "masalah": 0}
_rahasia: list = []


def samarkan(teks: str) -> str:
    teks = str(teks)
    for r in _rahasia:
        if r and len(r) >= 4:
            teks = teks.replace(r, "***")
    teks = re.sub(r"bot\d+:[A-Za-z0-9_-]{20,}", "bot***", teks)
    return re.sub(r"(?i)(password\s*[:=]\s*)\S+", r"\1***", teks)


def lapor(tingkat: str, judul: str, rinci: str = "", saran: str = "") -> None:
    ikon = {"ok": "✅", "peringatan": "⚠️ ", "masalah": "❌"}[tingkat]
    hasil[tingkat] += 1
    rinci = " ".join(str(rinci).split())[:160]
    print(f"  {ikon} {judul}" + (f" — {samarkan(rinci)}" if rinci else ""))
    if saran and tingkat != "ok":
        print(f"       ↳ {saran}")


def bagian(judul: str) -> None:
    print(f"\n▶ {judul}")


def jalankan(perintah: list, batas: int = 10) -> tuple:
    try:
        p = subprocess.run(perintah, capture_output=True, text=True, timeout=batas)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def baca_env() -> dict:
    nilai, salah = {}, []
    try:
        baris = open(ENV, encoding="utf-8").read().splitlines()
    except OSError as e:
        lapor("masalah", f"Berkas pengaturan {ENV} tidak terbaca", str(e), "Jalankan dengan sudo, atau periksa berkasnya ada.")
        return nilai
    for i, b in enumerate(baris, 1):
        s = b.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip()
        if v[:1] in "\"'" and v[-1:] == v[:1] and len(v) >= 2:
            v = v[1:-1]
        elif " " in v and not v.startswith("#"):
            salah.append(f"baris {i} ({k})")
        nilai[k] = v
        if RAHASIA.search(k) and v:
            _rahasia.append(v)
    lapor("ok", f"Berkas pengaturan terbaca ({len(nilai)} isian)")
    for k, v in BAWAAN.items():
        if not nilai.get(k):
            nilai[k] = v
    kosong = [k for k in WAJIB if not nilai.get(k)]
    if kosong:
        lapor("masalah", "Isian wajib masih kosong: " + ", ".join(kosong), saran=f"Lengkapi di {ENV}, lalu: sudo systemctl restart indeso")
    if salah:
        lapor("peringatan", "Nilai berisi spasi tanpa tanda kutip: " + ", ".join(salah),
              saran='Beri tanda kutip, mis. NAMA_USAHA="Internet Desa". Tanpa kutip, sebagian program membacanya terpotong.')
    return nilai


def cek_layanan() -> None:
    bagian("Layanan INDESO")
    kode, out = jalankan(["systemctl", "is-active", "indeso"])
    if out.strip() == "active":
        _, sejak = jalankan(["systemctl", "show", "indeso", "-p", "ActiveEnterTimestamp", "--value"])
        lapor("ok", "Layanan berjalan", f"menyala sejak {sejak.strip()}")
    else:
        lapor("masalah", "Layanan tidak berjalan", out.strip(),
              "Lihat penyebabnya: sudo journalctl -u indeso -n 50 --no-pager · nyalakan: sudo systemctl restart indeso")
    kode, out = jalankan(["git", "-C", APP, "log", "-1", "--format=%h %cd %s", "--date=format:%d %b %Y"])
    lapor("ok" if kode == 0 else "peringatan", "Versi kode", out.strip()[:90])
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=8) as r:
            d = json.loads(r.read() or b"{}")
        lapor("ok", "Pemantauan latar belakang berputar", f"putaran terakhir {d.get('detik_sejak_putaran')} detik lalu")
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read() or b"{}")
        except ValueError:
            d = {}
        lapor("masalah", "Pemantauan latar belakang macet", d.get("masalah", f"HTTP {e.code}"),
              "Restart: sudo systemctl restart indeso. Bila berulang, kirim hasil cek ini ke pengembang.")
    except OSError as e:
        lapor("masalah", "INDESO tidak menjawab di port 8000", str(e), "Pastikan layanan berjalan (lihat di atas).")


def cek_server() -> None:
    bagian("Server (VPS)")
    try:
        du = shutil.disk_usage(DATA if os.path.isdir(DATA) else "/")
        sisa_gb, persen = du.free / 1e9, du.free * 100 / du.total
        t = "ok" if sisa_gb >= 2 and persen >= 10 else ("peringatan" if sisa_gb >= 0.5 else "masalah")
        lapor(t, "Ruang disk", f"sisa {sisa_gb:.1f} GB ({persen:.0f}%)",
              "Hapus berkas besar yang tidak perlu (mis. sudo journalctl --vacuum-size=200M).")
    except OSError as e:
        lapor("peringatan", "Ruang disk tidak terbaca", str(e))
    try:
        info = dict(re.findall(r"(\w+):\s+(\d+)", open("/proc/meminfo").read()))
        sisa = int(info.get("MemAvailable", 0)) / 1024
        lapor("ok" if sisa >= 150 else "peringatan", "Memori (RAM) tersedia", f"{sisa:.0f} MB",
              "Memori hampir habis: cek program lain yang berjalan (top), atau restart VPS di jam sepi.")
    except (OSError, ValueError):
        pass
    kode, out = jalankan(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if out.strip() == "yes":
        lapor("ok", "Jam server sinkron (NTP)", datetime.now().strftime("%d %b %Y %H:%M"))
    elif kode == 0:
        lapor("peringatan", "Jam server belum sinkron", out.strip(),
              "Tagihan & isolir memakai jam server. Nyalakan: sudo timedatectl set-ntp true")


def cek_database() -> None:
    bagian("Database & backup")
    for nama in ("billing.db", "isp_history.db"):
        p = os.path.join(DATA, nama)
        if not os.path.exists(p):
            lapor("masalah" if nama == "billing.db" else "peringatan", f"{nama} tidak ditemukan", p,
                  "Pastikan INDESO_DATA_DIR benar. Data pelanggan & tagihan ada di billing.db.")
            continue
        try:
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
            cek = conn.execute("PRAGMA quick_check").fetchone()[0]
            tambahan = ""
            if nama == "billing.db":
                n = conn.execute("SELECT COUNT(*) FROM pelanggan WHERE status='aktif'").fetchone()[0]
                tambahan = f", {n} pelanggan aktif"
            conn.close()
            ukuran = os.path.getsize(p) / 1e6
            if cek == "ok":
                lapor("ok", f"{nama} utuh", f"{ukuran:.1f} MB{tambahan}")
            else:
                lapor("masalah", f"{nama} RUSAK", cek[:120],
                      "Jangan ubah apa pun. Hubungi pengembang; data bisa dipulihkan dari backup terakhir.")
        except sqlite3.Error as e:
            lapor("masalah", f"{nama} tidak bisa dibuka", str(e), "Kirim hasil cek ini ke pengembang.")
    folder = os.path.join(DATA, "backup")
    berkas = sorted(f for f in (os.listdir(folder) if os.path.isdir(folder) else []) if re.fullmatch(r"billing-\d{4}-\d{2}-\d{2}\.db", f))
    if not berkas:
        lapor("masalah", "Belum ada backup billing.db", folder, "Buat sekarang dari dashboard: Pengaturan → Backup.")
    else:
        umur = (time.time() - os.path.getmtime(os.path.join(folder, berkas[-1]))) / 3600
        lapor("ok" if umur <= 30 else "peringatan", "Backup terakhir", f"{berkas[-1]} ({umur:.0f} jam lalu, {len(berkas)} tersimpan)",
              "Backup harian tidak berjalan. Cek log: sudo journalctl -u indeso | grep -i backup")


def cek_tcp(judul: str, host: str, port: int, saran: str) -> bool:
    mulai = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=5):
            lapor("ok", judul, f"{host}:{port} terjangkau ({(time.monotonic() - mulai) * 1000:.0f} ms)")
            return True
    except OSError as e:
        lapor("masalah", judul, f"{host}:{port} tidak terjangkau ({e})", saran)
        return False


def cek_perangkat(env: dict) -> None:
    bagian("Perangkat jaringan")
    if env.get("MIKROTIK_HOST"):
        cek_tcp("MikroTik lokasi 1 (API)", env["MIKROTIK_HOST"], int(env.get("MIKROTIK_PORT") or 8728),
                "Cek terowongan VPN ke kantor (lihat bagian Terowongan) dan /ip service api di MikroTik.")
    try:
        conn = sqlite3.connect(f"file:{os.path.join(DATA, 'billing.db')}?mode=ro", uri=True, timeout=10)
        lain = conn.execute("SELECT kode, nama, mt_host, COALESCE(mt_port, 8728) FROM lokasi "
                            "WHERE kode != 1 AND COALESCE(mt_host, '') != ''").fetchall()
        conn.close()
    except sqlite3.Error:
        lain = []
    for kode, nama, host, port in lain:
        cek_tcp(f"MikroTik {nama} (lokasi {kode})", host, int(port or 8728),
                "Cek terowongan OpenVPN lokasi itu: sudo bash /opt/indeso/deploy/genieacs/mikrotik-ovpn.sh status")
    olt = env.get("OLT_HOST")
    if olt:
        kode, out = jalankan(["ping", "-c", "2", "-W", "2", olt], 10)
        if kode == 0:
            ms = re.search(r"= [\d.]+/([\d.]+)/", out)
            lapor("ok", "OLT menjawab ping", f"{olt}" + (f" rata-rata {float(ms.group(1)):.0f} ms" if ms else ""))
        else:
            lapor("masalah", "OLT tidak menjawab ping", olt, "Cek listrik OLT dan terowongan VPN ke kantor.")
        if env.get("OLT_TELNET_USER"):
            cek_tcp("OLT telnet (untuk TR-069/restart dari INDESO)", olt, int(env.get("OLT_TELNET_PORT") or 23),
                    "Tombol 'Kirim TR-069'/'Restart lewat OLT' tidak akan jalan sampai telnet OLT terjangkau.")
    acs = env.get("GENIEACS_URL")
    if acs:
        try:
            req = urllib.request.Request(acs.rstrip("/") + "/devices/?projection=_id&limit=1")
            if env.get("GENIEACS_USER"):
                import base64
                tok = base64.b64encode(f"{env['GENIEACS_USER']}:{env.get('GENIEACS_PASS', '')}".encode()).decode()
                req.add_header("Authorization", "Basic " + tok)
            with urllib.request.urlopen(req, timeout=8):
                lapor("ok", "Server ACS (GenieACS) menjawab")
        except urllib.error.HTTPError as e:
            lapor("masalah", "Server ACS menolak", f"HTTP {e.code}",
                  "401 = GENIEACS_USER/PASS salah. Lainnya: sudo systemctl status genieacs-nbi")
        except OSError as e:
            lapor("masalah", "Server ACS tidak terjangkau", str(e), "Cek: sudo systemctl status genieacs-nbi")
    else:
        lapor("peringatan", "Server ACS belum diatur", "GENIEACS_URL kosong", "Fitur WiFi/ONU dari INDESO tidak aktif.")


def cek_telegram(env: dict) -> None:
    bagian("Telegram")
    tok = env.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        return
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/getMe", timeout=10) as r:
            d = json.loads(r.read())
        lapor("ok", "Bot Telegram aktif", "@" + d.get("result", {}).get("username", "?"))
    except urllib.error.HTTPError as e:
        lapor("masalah", "Token bot Telegram ditolak", f"HTTP {e.code}", "Periksa TELEGRAM_BOT_TOKEN di berkas pengaturan.")
    except OSError as e:
        lapor("peringatan", "Telegram tidak terjangkau dari VPS", str(e), "Cek koneksi internet VPS.")


def cek_terowongan() -> None:
    bagian("Terowongan VPN")
    kode, out = jalankan(["wg", "show", "all", "latest-handshakes"])
    if kode != 0:
        lapor("peringatan", "WireGuard tidak terbaca", out.strip()[:80] or "perintah wg tidak ada")
    else:
        sekarang = time.time()
        for baris in out.strip().splitlines():
            bag = baris.split()
            if len(bag) != 3:
                continue
            antarmuka, _peer, jabat = bag
            umur = sekarang - int(jabat) if jabat != "0" else None
            if antarmuka == "wghp":
                continue        # HP owner: wajar sering tidak tersambung
            if umur is not None and umur < 300:
                lapor("ok", f"WireGuard {antarmuka}", f"tersambung ({umur:.0f} detik lalu)")
            else:
                lapor("masalah", f"WireGuard {antarmuka}", "belum pernah tersambung" if umur is None else f"terakhir {umur / 60:.0f} menit lalu",
                      "MikroTik/perangkat di ujung lain mati, internet kantor putus, atau terowongan di MikroTik dinonaktifkan.")
    status = "/run/openvpn-server/indeso-mt.status"
    if os.path.exists(status):
        klien = [b.split(",")[1] for b in open(status).read().splitlines() if b.startswith("CLIENT_LIST,")]
        lapor("ok" if klien else "peringatan", "OpenVPN MikroTik wilayah", ", ".join(klien) + " tersambung" if klien else "tidak ada yang tersambung",
              "Cek MikroTik wilayah: /interface ovpn-client monitor ovpn-vps once")


def cek_log() -> None:
    bagian("Galat 24 jam terakhir")
    kode, out = jalankan(["journalctl", "-u", "indeso", "--since", "24 hours ago", "-p", "warning", "--no-pager", "-o", "cat"], 20)
    if kode != 0:
        lapor("peringatan", "Log tidak terbaca", out.strip()[:80], "Jalankan dengan sudo.")
        return
    baris = [b for b in out.splitlines() if b.strip() and not b.startswith("-- ")]
    galat = [b for b in baris if "[ERROR]" in b or "Traceback" in b]
    if not baris:
        lapor("ok", "Tidak ada peringatan/galat")
        return
    lapor("ok" if not galat else "peringatan", f"{len(baris)} peringatan, {len(galat)} galat")
    sering: dict = {}
    for b in baris:
        kunci = re.sub(r"\d+", "#", b.split("] ", 1)[-1])[:90]
        sering[kunci] = sering.get(kunci, 0) + 1
    for kunci, n in sorted(sering.items(), key=lambda x: -x[1])[:5]:
        print(f"       {n:>4}× {samarkan(kunci)}")


def main() -> int:
    print(f"PERIKSA INDESO — {datetime.now():%d %b %Y %H:%M}")
    print("Hanya membaca, tidak mengubah apa pun. Password/token tidak ditampilkan.")
    bagian("Pengaturan")
    env = baca_env()
    cek_layanan()
    cek_server()
    cek_database()
    cek_perangkat(env)
    cek_telegram(env)
    cek_terowongan()
    cek_log()
    print(f"\nRINGKASAN: ✅ {hasil['ok']} baik · ⚠️  {hasil['peringatan']} perlu diperhatikan · ❌ {hasil['masalah']} bermasalah")
    if hasil["masalah"]:
        print("Ikuti saran ↳ pada baris ❌ dari atas ke bawah. Bila belum beres, kirim seluruh hasil ini ke pengembang.")
    return 1 if hasil["masalah"] else 0


if __name__ == "__main__":
    sys.exit(main())
