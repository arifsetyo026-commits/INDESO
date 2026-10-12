#!/usr/bin/env python3
"""Reset password akun dashboard INDESO langsung di server (bila lupa password).

    sudo -u indeso python3 reset-password.py            # di VPS
    python reset-password.py /path/ke/billing.db        # di Termux / lokasi lain

Akun yang direset otomatis diaktifkan lagi dan semua sesinya dicabut (harus login ulang).
"""
import getpass
import hashlib
import secrets
import sqlite3
import sys

db = sys.argv[1] if len(sys.argv) > 1 else "/var/lib/indeso/billing.db"
conn = sqlite3.connect(db)
akun = conn.execute("SELECT username, peran, aktif FROM akun ORDER BY peran, username").fetchall()
if not akun:
    sys.exit("Belum ada akun. Buka dashboard untuk membuat akun pertama.")
print("Akun yang ada:")
for u, peran, aktif in akun:
    print(f"  - {u} ({peran}){'' if aktif else ' — NONAKTIF'}")
nama = input("Username yang direset: ").strip()
if nama not in [a[0] for a in akun]:
    sys.exit(f"Username '{nama}' tidak ada (huruf besar/kecil berpengaruh).")
pw = getpass.getpass("Password baru (min. 8 karakter, tidak terlihat saat diketik): ")
if len(pw) < 8:
    sys.exit("Password minimal 8 karakter.")
if pw != getpass.getpass("Ulangi password baru: "):
    sys.exit("Ulangi password tidak sama.")
iterasi = 200_000   # sama dengan _PBKDF2_ITERASI di main.py
garam = secrets.token_hex(16)
h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(garam), iterasi).hex()
conn.execute("UPDATE akun SET password_hash=?, aktif=1 WHERE username=?", (f"pbkdf2_sha256${iterasi}${garam}${h}", nama))
conn.execute("DELETE FROM sesi WHERE username=?", (nama,))
conn.execute("INSERT INTO aktivitas (waktu, oleh, aksi, detail) VALUES (datetime('now','localtime'), 'server', "
             "'akun_reset_password', ?)", (f"{nama}: password direset dari server",))
conn.commit()
print(f"Password '{nama}' sudah diganti. Silakan login dengan password baru.")
