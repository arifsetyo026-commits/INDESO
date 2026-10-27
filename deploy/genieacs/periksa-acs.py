#!/usr/bin/env python3
"""Pemeriksaan kesehatan ACS (GenieACS) & INDESO — HANYA MEMBACA, tidak mengirim apa pun ke ONU.

Jalankan di VPS sebelum menyalakan genieacs-cwmp / INDESO, dan seminggu sekali:
    sudo python3 /opt/indeso/deploy/genieacs/periksa-acs.py [folder data INDESO]

Kondisi sehat: semua layanan aktif, file uji kecepatan terjangkau, jadwal lapor INDESO = provision, preset hanya
indeso-default & indeso-diagnostik, provision sama dengan versi terbaru, perintah antre 0 (INDESO tidak pernah
meninggalkan perintah: berhasil, atau gagal lalu dihapus), perintah gagal 0, kredensial ketukan setiap ONU dipasang ≤ 24 jam lalu, setiap ONU satu WAN PPPoE aktif,
tidak ada akun / nama WiFi ganda. Password (WiFi, PPPoE, ketukan, login ACS) tidak pernah ditampilkan.
"""
import json, sqlite3, sys, collections, os, re, subprocess, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

DATA = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("INDESO_DATA_DIR", "/var/lib/indeso")
NBI = "http://127.0.0.1:7557"
IGD = "InternetGatewayDevice"
W = IGD + ".LANDevice.1.WLANConfiguration"
def get(p):
    with urllib.request.urlopen(NBI + p, timeout=30) as r:
        return json.load(r)
def bagian(judul, f):
    print("\n== " + judul)
    try:
        f()
    except Exception as e:
        print("  (gagal dibaca:", e, ")")
def v(o, *p):
    for k in p:
        o = (o or {}).get(k)
    return o.get("_value") if isinstance(o, dict) else None
def waktu(t):
    try:
        return datetime.fromisoformat(str(t).replace("Z", "+00:00")) if t else None
    except ValueError:
        return None
SEKARANG = datetime.now(timezone.utc)
def menit_lalu(t):
    w = waktu(t)
    return int((SEKARANG - w).total_seconds() // 60) if w else None
def wib(t):
    w = waktu(t)
    return (w + timedelta(hours=7)).strftime("%d-%m %H:%M") if w else "-"
def baca_env():
    env = {}
    try:
        for baris in open("/etc/indeso/indeso.env"):
            if "=" in baris and not baris.lstrip().startswith("#"):
                k, x = baris.split("=", 1)
                env[k.strip()] = x.strip().strip("\"'")
    except OSError:
        pass
    return env
def layanan():
    for nama in ("mongod", "genieacs-cwmp", "genieacs-nbi", "genieacs-fs", "genieacs-ui", "nginx", "indeso"):
        r = subprocess.run(["systemctl", "is-active", nama], capture_output=True, text=True)
        print(f"  {nama}: {r.stdout.strip() or r.stderr.strip()}")
    env = baca_env()
    host = urllib.parse.urlparse(env.get("ACS_ONU_URL") or "http://10.99.0.1:7547").hostname or "10.99.0.1"
    uji = f"http://{host}:7567/indeso-uji-20MB.bin"
    try:
        with urllib.request.urlopen(uji, timeout=10) as r:
            r.read(1)
            print(f"  file uji kecepatan {uji}: HTTP {r.status}, {r.headers.get('Content-Length')} byte (sehat: 20000000)")
    except Exception as e:
        print(f"  file uji kecepatan {uji}: GAGAL ({e}) <<< tes kecepatan dari ONU tidak akan jalan")
def jadwal():
    env = baca_env()
    m = re.search(r'PeriodicInformInterval", \{value: 1\}, \{value: (\d+)\}', open("/opt/indeso/deploy/genieacs/provision-indeso.js").read())
    prov = int(m.group(1)) if m else None
    inde = int(env.get("ACS_ONU_INTERVAL") or 900)
    print(f"  provision indeso-default: lapor tiap {prov} detik | INDESO (ACS_ONU_INTERVAL, dikirim OLT): {inde} detik"
          + ("" if prov == inde else "   <<< BEDA: OLT dan ACS saling timpa jadwal lapor"))
    print("  INDESO: GENIEACS_URL =", env.get("GENIEACS_URL") or "(kosong)", "| ACS_ONU_URL =", env.get("ACS_ONU_URL") or "(bawaan)")
    js = ('db.config.find({_id: {$not: /^ui\\./}}).forEach(c => print("  " + c._id + " = " + '
          '(/auth/.test(c._id) ? "(ada, isinya disembunyikan)" : c.value)))')
    try:
        r = subprocess.run(["mongosh", "--quiet", "genieacs", "--eval", js], capture_output=True, text=True, timeout=30)
        print("  Pengaturan GenieACS selain tampilan:")
        print(r.stdout.rstrip() or "  (semua bawaan)")
    except (OSError, subprocess.TimeoutExpired) as e:
        print("  (mongosh tidak bisa dijalankan:", e, ")")
def ketukan():
    pr = next((p for p in get("/presets/") if p["_id"] == "indeso-default"), None)
    a = (((pr or {}).get("configurations") or [{}])[0].get("args")) or []
    print("  preset indeso-default: username ketukan", repr(a[0]) if a else None,
          "| password:", "ada" if len(a) > 1 and a[1] else "TIDAK ADA <<< ONU tidak bisa diketuk")
    ms = IGD + ".ManagementServer"
    proj = ",".join(["_id", "_lastBoot", ms + ".ConnectionRequestURL", ms + ".ConnectionRequestPassword"])
    d = get("/devices/?projection=" + urllib.parse.quote(proj))
    bermasalah = 0
    for dev in sorted(d, key=lambda x: x["_id"]):
        m = (dev.get(IGD) or {}).get("ManagementServer") or {}
        dipasang = (m.get("ConnectionRequestPassword") or {}).get("_timestamp")
        tanda = []
        if not v(m, "ConnectionRequestURL"):
            tanda.append("tanpa URL ketukan")
        if not dipasang:
            tanda.append("kredensial belum pernah dipasang")
        elif menit_lalu(dipasang) > 25 * 60:
            tanda.append(f"kredensial dipasang {menit_lalu(dipasang) // 60} jam lalu (> 24 jam)")
        boot = waktu(dev.get("_lastBoot"))
        if boot and waktu(dipasang) and boot > waktu(dipasang):
            tanda.append(f"ONU restart {wib(dev.get('_lastBoot'))} setelah kredensial dipasang {wib(dipasang)}")
        if tanda:
            bermasalah += 1
            print(f"  {dev['_id'][-30:]}: " + "; ".join(tanda))
    print(f"  ONU: {len(d)} | perlu perhatian: {bermasalah} (dipulihkan otomatis saat ONU lapor berikutnya)")
    tanda = [x["_id"] for x in get("/devices/?query=" + urllib.parse.quote(json.dumps({"_tags": "pulihkan_ketukan"}))
                                   + "&projection=_id")]
    print("  Menunggu pemasangan ulang kredensial (tag pulihkan_ketukan):", [x[-24:] for x in tanda] or "tidak ada")
def log_lapor():
    jalur = "/var/log/genieacs/genieacs-cwmp-access.log"
    try:
        f = open(jalur, "rb")
    except OSError as e:
        print("  (log tidak bisa dibaca:", e, ")")
        return
    with f:
        f.seek(max(0, os.path.getsize(jalur) - 50_000_000))
        baris = f.read().decode("utf-8", "replace").splitlines()
    batas = SEKARANG - timedelta(hours=24)
    per = collections.defaultdict(collections.Counter)
    contoh = None
    for b in baris:
        if ": Inform" not in b:
            continue
        w = waktu(b.split(" ", 1)[0])
        if not w or w < batas:
            continue
        mo = re.search(r"\] \S+ (\S+): Inform", b)
        ev = re.search(r'[Ee]vent="([^"]*)"', b)
        if not mo or not ev:
            contoh = contoh or b[:160]
            continue
        c = per[mo.group(1)]
        c["lapor"] += 1
        for e in ev.group(1).split(","):
            c[e.strip()] += 1
    if not per:
        print("  Tidak ada laporan 24 jam terakhir yang terbaca." + (f" Contoh baris: {contoh}" if contoh else ""))
        return
    print("  24 jam terakhir per ONU: lapor | rutin (2 PERIODIC) | ketukan berhasil (6 CONNECTION REQUEST) |"
          " nilai berubah (4 VALUE CHANGE) | restart (1 BOOT)")
    for dev, c in sorted(per.items()):
        print(f"  {dev[-30:]:30} {c['lapor']:4} | {c['2 PERIODIC']:4} | {c['6 CONNECTION REQUEST']:4} | "
              f"{c['4 VALUE CHANGE']:4} | {c['1 BOOT']:3}")
    t = sum(per.values(), collections.Counter())
    print(f"  Total: {t['lapor']} lapor dari {len(per)} ONU; ONU yang pernah mengirim VALUE CHANGE: "
          f"{sum(1 for c in per.values() if c['4 VALUE CHANGE'])}")
def ketukan_indeso():
    h = sqlite3.connect(f"file:{os.path.join(DATA, 'isp_history.db')}?mode=ro", uri=True)
    batas = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    r = h.execute("SELECT COALESCE(SUM(langsung),0), COUNT(*) FROM acs_tugas WHERE ts >= ?", (batas,)).fetchone()
    print(f"  Perintah 24 jam terakhir: {r[1]} | berhasil diketuk: {r[0]}"
          + (f" ({round(r[0] * 100 / r[1])}%)" if r[1] else ""))
    for alasan, n in h.execute("SELECT alasan, COUNT(*) FROM acs_tugas WHERE ts >= ? AND langsung=0 GROUP BY alasan "
                               "ORDER BY 2 DESC", (batas,)):
        print(f"    gagal/dibatalkan karena: {alasan or '-'} ({n}x)")
def preset():
    for p in get("/presets/"):
        asing = "" if p["_id"] in ("indeso-default", "indeso-diagnostik") else "   <<< BUKAN DARI INDESO"
        print(" ", p["_id"], "| bobot:", p.get("weight"), "| event:", p.get("events") or "{} (tiap lapor)",
              "| syarat:", p.get("precondition") or "-", "| provision:", [c.get("name") for c in p.get("configurations", [])], asing)
def provision():
    repo = open("/opt/indeso/deploy/genieacs/provision-indeso.js").read().strip()
    for p in get("/provisions/"):
        t = ""
        if p["_id"] == "indeso-default":
            t = "  -> SAMA dengan versi terbaru" if (p.get("script") or "").strip() == repo else "  -> BEDA dengan versi terbaru!"
        print(" ", p["_id"] + t)
    print("  virtual parameter:", [x["_id"] for x in get("/virtual_parameters/")])
def tugas():
    t = get("/tasks/")
    for x in t:
        isi = x.get("objectName") or x.get("parameterNames") or [p[0] for p in (x.get("parameterValues") or [])]
        umur = menit_lalu(x.get("timestamp"))
        tanda = ""
        if x.get("name") in ("reboot", "deleteObject", "factoryReset"):
            tanda = "   <<< BERISIKO: jalan mendadak saat ONU lapor"
        elif umur is not None and umur > 3:
            tanda = "   <<< sisa perintah (dihapus otomatis INDESO dalam ±5 menit)"
        print(f"  {wib(x.get('timestamp'))} ({umur} mnt) {str(x.get('device', ''))[-24:]} {x.get('name')} {str(isi)[:90]}{tanda}")
    print("  jumlah:", len(t))
def fault():
    f = get("/faults/")
    for x in f:
        print(" ", str(x.get("timestamp", ""))[:16], x["_id"][-50:], x.get("code"), str(x.get("message"))[:70], "| ulang:", x.get("retries"))
    print("  jumlah:", len(f))
def onu():
    proj = ",".join(["_id", "_lastInform", "VirtualParameters.pppoeUsername", IGD + ".WANDevice", W + ".1.SSID", W + ".5.SSID",
                     IGD + ".ManagementServer.PeriodicInformInterval"])
    d = get("/devices/?projection=" + urllib.parse.quote(proj))
    akun, ssid, ganda = collections.defaultdict(list), collections.defaultdict(set), 0
    for dev in sorted(d, key=lambda x: str(v(x, "VirtualParameters", "pppoeUsername"))):
        i = dev["_id"]
        wl = (((dev.get(IGD) or {}).get("LANDevice") or {}).get("1") or {}).get("WLANConfiguration") or {}
        s1, s5 = v(wl, "1", "SSID"), v(wl, "5", "SSID")
        wan = []
        for wd in ((dev.get(IGD) or {}).get("WANDevice") or {}).values():
            if not isinstance(wd, dict): continue
            for cd in (wd.get("WANConnectionDevice") or {}).values():
                if not isinstance(cd, dict): continue
                for jenis in ("WANPPPConnection", "WANIPConnection"):
                    for w in (cd.get(jenis) or {}).values():
                        if not isinstance(w, dict) or not any(not k.startswith("_") for k in w): continue
                        lay = next((v(w, k) for k in w if re.search("servicelist", k, re.I)), None)
                        wan.append((jenis[3:6], v(w, "Name"), v(w, "Enable"), v(w, "Username"), v(w, "ConnectionStatus"), lay))
        aktif = [x for x in wan if x[0] == "PPP" and x[2] in (True, "true", "1", 1)]
        tanda = "   <<< LEBIH DARI 1 WAN PPPoE AKTIF" if len(aktif) > 1 else ""
        ganda += bool(tanda)
        u = v(dev, "VirtualParameters", "pppoeUsername")
        print(f"\n  {i[-26:]} | akun: {u} | lapor: {str(dev.get('_lastInform'))[:16]} | interval: {v(dev, IGD, 'ManagementServer', 'PeriodicInformInterval')} | WiFi: {s1} / {s5}{tanda}")
        for x in wan:
            print(f"      {x[0]:3} nama={x[1]} aktif={x[2]} user={x[3]} status={x[4]} layanan={x[5]}")
        akun[u].append(i)
        for s in (s1, s5):
            if s: ssid[s].add(i)
    print(f"\n  jumlah ONU di ACS: {len(d)} | ONU dengan lebih dari 1 WAN PPPoE aktif: {ganda}")
    for u, ids in akun.items():
        if u and len(ids) > 1: print("  !! Akun", u, "tercatat di", len(ids), "ONU:", [x[-20:] for x in ids])
    for s, ids in ssid.items():
        if len(ids) > 1: print("  !! Nama WiFi", repr(s), "dipakai", len(ids), "ONU:", [x[-20:] for x in ids])
def indeso():
    b = sqlite3.connect(f"file:{os.path.join(DATA, 'billing.db')}?mode=ro", uri=True)
    q = lambda s: b.execute(s).fetchall()
    print("  Pengaturan tersimpan:", q("SELECT kunci, nilai FROM pengaturan WHERE kunci LIKE 'ISOLIR%' OR kunci IN ('POLL_INTERVAL_SECONDS','UPSTREAM_PANTAU')") or "semua bawaan")
    print("  Secret tertunda (dicoba ulang otomatis):", q("SELECT username, secret_target FROM pelanggan WHERE secret_target IS NOT NULL") or "tidak ada")
    print("  Pelanggan SERVER/INDESO:", q("SELECT username, nama, status FROM pelanggan WHERE username IN ('SERVER','INDESO')") or "tidak ada")
    print("  Sedang diisolir:", q("SELECT username, profile_asal, sejak FROM isolir") or "tidak ada")
    print("  Calon isolir otomatis:", q("SELECT t.username, t.periode, t.jatuh_tempo FROM tagihan t JOIN pelanggan p ON p.username=t.username "
          "WHERE t.status='belum' AND COALESCE(p.gratis,0)=0 AND COALESCE(p.status,'aktif')='aktif' "
          "AND t.jatuh_tempo < date('now','localtime') AND t.username NOT IN (SELECT username FROM isolir)") or "tidak ada")
bagian("Layanan & file uji kecepatan", layanan)
bagian("Jadwal lapor & pengaturan", jadwal)
bagian("GenieACS: preset", preset)
bagian("GenieACS: provision & virtual parameter", provision)
bagian("GenieACS: perintah antre (dijalankan saat ONU lapor)", tugas)
bagian("GenieACS: perintah gagal yang diulang", fault)
bagian("Ketukan (connection request): kredensial per ONU", ketukan)
bagian("Ketukan menurut catatan INDESO", ketukan_indeso)
bagian("Laporan ONU ke ACS (log genieacs-cwmp)", log_lapor)
bagian("GenieACS: semua ONU (akun, jadwal lapor, WiFi, WAN)", onu)
bagian("INDESO", indeso)
