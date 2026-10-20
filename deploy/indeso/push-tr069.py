#!/usr/bin/env python3
"""Kirim pengaturan TR-069 (ACS URL & login) ke ONU lewat OLT C-Data, untuk banyak ONU sekaligus.

Jalankan di VPS (membaca /etc/indeso/indeso.env):

    sudo /opt/indeso/.venv/bin/python /opt/indeso/deploy/indeso/push-tr069.py            # lihat dulu (tidak mengubah apa pun)
    sudo /opt/indeso/.venv/bin/python /opt/indeso/deploy/indeso/push-tr069.py --kirim    # kirim + save + cek

Pilihan:
    --semua-merek   ikut kirim ke ONU non-ZTE (Fiberhome dll.; biasanya diatur lewat web ONU sendiri)
    --ulang         kirim juga ke ONU yang sudah punya pengaturan TR-069 di OLT
    --onu 2/5       hanya ONU port 2 nomor 5 (boleh diulang: --onu 1/3 --onu 2/5)
    --reboot        restart ONU yang berhasil dikirimi (satu per satu, lewat OLT). Perlu untuk ZTE F609
                    dkk. yang baru memakai pengaturan TR-069 setelah restart. Internet pelanggan putus ±1–2 menit.

Hanya bagian ACS yang diubah; WAN/PPPoE pelanggan tidak disentuh. Tanpa --reboot internet tidak terputus.
ONU ZTE tetap perlu WAN PPPoE berjenis INTERNET_TR069 supaya bisa lapor ke ACS.
"""
import argparse
import os
import re
import sys
import tempfile

ENV = "/etc/indeso/indeso.env"
APP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def muat_env(path: str) -> None:
    try:
        with open(path) as f:
            for baris in f:
                baris = baris.strip()
                if baris and not baris.startswith("#") and "=" in baris:
                    k, v = baris.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except PermissionError:
        sys.exit(f"Tidak bisa membaca {path}. Jalankan dengan sudo.")
    except FileNotFoundError:
        pass


def baca_onu_olt(main) -> list:
    """Daftar ONU dari `show current-config`: [{pon, port, onu, sn, tr069}]."""
    t = main._TelnetOLT(main.CONFIG["OLT_HOST"], main.CONFIG["OLT_TELNET_PORT"], timeout=60)
    try:
        t.tunggu(r"(?i)(user ?name|login)\s*:\s*$")
        t.kirim(main.CONFIG["OLT_TELNET_USER"], r"(?i)password\s*:\s*$")
        masuk = t.kirim(main.CONFIG["OLT_TELNET_PASS"], r"(?i)([>#]\s*$|user ?name\s*:\s*$|fail|incorrect|invalid)")
        if not re.search(r"[>#]\s*$", masuk):
            sys.exit("Login telnet OLT ditolak. Periksa OLT_TELNET_USER/OLT_TELNET_PASS di " + ENV)
        if masuk.rstrip().endswith(">"):
            t.kirim("enable")
        teks = t.kirim("show current-config", main._POLA_PROMPT)
    finally:
        t.tutup()
    onu, antarmuka = {}, None
    for baris in teks.splitlines():
        b = baris.strip()
        m = re.match(r"interface gpon (\d+/\d+)$", b)
        if m:
            antarmuka = m.group(1)
            continue
        if b == "exit":
            antarmuka = None
            continue
        if not antarmuka:
            continue
        m = re.match(r'ont add (\d+) (\d+) sn-auth "([^"]+)"', b)
        if m:
            k = (antarmuka, int(m.group(1)), int(m.group(2)))
            onu[k] = {"pon": f"{antarmuka}/{m.group(1)}", "port": int(m.group(1)), "onu": int(m.group(2)),
                      "sn": m.group(3), "tr069": False}
            continue
        m = re.match(r"ont tr069 (\d+) (\d+) acs-url", b)
        if m and (antarmuka, int(m.group(1)), int(m.group(2))) in onu:
            onu[(antarmuka, int(m.group(1)), int(m.group(2)))]["tr069"] = True
    return sorted(onu.values(), key=lambda x: (x["pon"], x["onu"]))


def cek_config_state(main, daftar: list) -> dict:
    t = main._TelnetOLT(main.CONFIG["OLT_HOST"], main.CONFIG["OLT_TELNET_PORT"], timeout=60)
    hasil = {}
    try:
        t.tunggu(r"(?i)(user ?name|login)\s*:\s*$")
        t.kirim(main.CONFIG["OLT_TELNET_USER"], r"(?i)password\s*:\s*$")
        if t.kirim(main.CONFIG["OLT_TELNET_PASS"]).rstrip().endswith(">"):
            t.kirim("enable")
        t.kirim("config")
        antarmuka = None
        for o in daftar:
            a = o["pon"].rsplit("/", 1)[0]
            if a != antarmuka:
                if antarmuka:
                    t.kirim("exit")
                t.kirim(f"interface gpon {a}")
                antarmuka = a
            out = t.kirim(f"show ont info {o['port']} {o['onu']}", main._POLA_PROMPT)
            m = re.search(r"Config state\s*:\s*(\S+)", out)
            r = re.search(r"Run state\s*:\s*(\S+)", out)
            hasil[(o["pon"], o["onu"])] = f"{r.group(1) if r else '?'} / config {m.group(1) if m else '?'}"
        t.kirim("end")
    finally:
        t.tutup()
    return hasil


def main_cli() -> None:
    ap = argparse.ArgumentParser(description="Kirim TR-069 ke ONU lewat OLT C-Data.")
    ap.add_argument("--kirim", action="store_true", help="benar-benar kirim (tanpa ini hanya menampilkan daftar)")
    ap.add_argument("--semua-merek", action="store_true", help="ikut ONU non-ZTE")
    ap.add_argument("--ulang", action="store_true", help="kirim juga ke ONU yang sudah punya pengaturan TR-069")
    ap.add_argument("--onu", action="append", default=[], help="hanya ONU tertentu, format PORT/NOMOR, mis. 2/5")
    ap.add_argument("--reboot", action="store_true", help="restart ONU setelah dikirim (internet putus ±1–2 menit)")
    a = ap.parse_args()

    muat_env(ENV)
    # Jangan menyentuh database INDESO yang sedang dipakai layanan (skrip ini jalan sebagai root).
    os.environ["INDESO_DATA_DIR"] = tempfile.mkdtemp(prefix="push-tr069-")
    sys.path.insert(0, APP)
    import logging
    logging.disable(logging.WARNING)
    import main  # noqa: E402

    if not main._olt_telnet_aktif():
        sys.exit("Isi dulu OLT_TELNET_USER, OLT_TELNET_PASS, ACS_ONU_USER, ACS_ONU_PASS di " + ENV)
    print(f"OLT {main.CONFIG['OLT_HOST']} · ACS dikirim: {main.CONFIG['ACS_ONU_URL']} (user {main.CONFIG['ACS_ONU_USER']})")
    print("Membaca daftar ONU dari OLT…")
    semua = baca_onu_olt(main)
    di_acs = main._sn_terdaftar_acs() or set()

    pilih_onu = set()
    for x in a.onu:
        m = re.fullmatch(r"(\d+)/(\d+)", x)
        if not m:
            sys.exit(f"Format --onu salah: {x} (contoh: 2/5)")
        pilih_onu.add((int(m.group(1)), int(m.group(2))))

    target = []
    print(f"\n{'PORT/ONU':<9} {'SN':<14} {'TR-069 di OLT':<14} {'Di ACS':<7} Keterangan")
    for o in semua:
        zte = o["sn"].upper().startswith("ZTEG")
        acs = o["sn"].upper() in di_acs
        if pilih_onu:
            ikut = (o["port"], o["onu"]) in pilih_onu
            ket = "DIKIRIM" if ikut else "-"
        elif not zte and not a.semua_merek:
            ikut, ket = False, "dilewati (bukan ZTE, atur lewat web ONU)"
        elif o["tr069"] and not a.ulang:
            ikut, ket = False, "dilewati (sudah ada)"
        else:
            ikut, ket = True, "DIKIRIM"
        if ikut:
            target.append(o)
        print(f"{o['port']}/{o['onu']:<7} {o['sn']:<14} {'ya' if o['tr069'] else 'belum':<14} {'ya' if acs else '-':<7} {ket}")
    print(f"\n{len(semua)} ONU di OLT · {len(target)} akan dikirim.")

    if not target:
        return
    if not a.kirim:
        print("\nBelum ada yang diubah. Jalankan lagi dengan --kirim untuk mengirim, menyimpan, dan memeriksa.")
        return

    if a.reboot:
        jawab = input(f"\n{len(target)} ONU akan di-RESTART setelah dikirim (internet pelanggan putus ±1–2 menit). "
                      "Ketik YA untuk lanjut: ")
        if jawab.strip().upper() != "YA":
            sys.exit("Dibatalkan.")
    print("\nMengirim lewat OLT…")
    try:
        h = main.olt_kirim_tr069([(o["pon"], o["onu"]) for o in target], reboot=a.reboot)
    except main.HTTPException as e:
        sys.exit(f"GAGAL: {e.detail}")
    for r in h["hasil"]:
        ket = "" if "reboot" not in r else (" · di-restart" if r["reboot"] else " · RESTART GAGAL")
        print(f"  {r['pon'].rsplit('/', 1)[1]}/{r['onu_id']}: {'OK' + ket if r['ok'] else 'DITOLAK — ' + r['pesan']}")
    ok = sum(1 for r in h["hasil"] if r["ok"])
    print(f"{ok} dari {len(h['hasil'])} berhasil.")
    if h["tersimpan"]:
        print("Konfigurasi OLT TERSIMPAN permanen.")
    elif h["tersimpan"] is False:
        print("PERHATIAN: konfigurasi OLT BELUM tersimpan (hilang bila OLT restart). Simpan manual lewat web OLT.")

    if a.reboot:
        print("\nMenunggu ONU menyala kembali (90 detik)…")
        import time
        time.sleep(90)
    print("\nMemeriksa status ONU di OLT…")
    st = cek_config_state(main, [o for o in target])
    gagal = 0
    for o in target:
        s = st.get((o["pon"], o["onu"]), "?")
        gagal += "failed" in s
        print(f"  {o['port']}/{o['onu']} {o['sn']}: {s}")
    print("\nSelesai." + (f" {gagal} ONU berstatus config failed (ONU menolak; internet tetap jalan)." if gagal else ""))
    print("ONU ZTE muncul di ACS ±5 menit kemudian, asalkan WAN PPPoE-nya berjenis INTERNET_TR069."
          + ("" if a.reboot else "\nZTE F609 dkk. yang belum muncul: jalankan lagi dengan --onu P/O --reboot."))


if __name__ == "__main__":
    try:
        main_cli()
    except (OSError, TimeoutError, ConnectionError) as e:
        sys.exit(f"\nGAGAL berbicara dengan OLT: {e}\n"
                 "Kemungkinan: masih ada sesi telnet lain yang terbuka ke OLT (tutup dengan 'exit'), "
                 "atau OLT sedang sibuk menyimpan konfigurasi. Tunggu 1 menit lalu jalankan lagi.\n"
                 "Bila terjadi saat --kirim, jalankan lagi tanpa --kirim untuk melihat ONU mana yang sudah terkirim.")
