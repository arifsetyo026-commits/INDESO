#!/usr/bin/env python3
"""Pemeriksaan kesehatan ACS (GenieACS) & INDESO — HANYA MEMBACA, tidak mengirim apa pun ke ONU.

Jalankan di VPS sebelum menyalakan genieacs-cwmp / INDESO, dan seminggu sekali:
    sudo python3 /opt/indeso/deploy/genieacs/periksa-acs.py [folder data INDESO]

Kondisi sehat: preset hanya indeso-default & indeso-diagnostik, provision sama dengan versi terbaru,
perintah antre 0, perintah gagal 0, setiap ONU satu WAN PPPoE aktif, tidak ada akun / nama WiFi ganda.
Password WiFi & PPPoE tidak ditampilkan (perintah antre hanya nama parameternya).
"""
import json, sqlite3, sys, collections, os, re, urllib.request, urllib.parse

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
        print(" ", str(x.get("timestamp", ""))[:16], str(x.get("device", ""))[-24:], x.get("name"), str(isi)[:110])
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
    b = sqlite3.connect(os.path.join(DATA, "billing.db"))
    q = lambda s: b.execute(s).fetchall()
    print("  Pengaturan tersimpan:", q("SELECT kunci, nilai FROM pengaturan WHERE kunci LIKE 'ISOLIR%' OR kunci IN ('POLL_INTERVAL_SECONDS','UPSTREAM_PANTAU')") or "semua bawaan")
    print("  Secret tertunda (dicoba ulang otomatis):", q("SELECT username, secret_target FROM pelanggan WHERE secret_target IS NOT NULL") or "tidak ada")
    print("  Pelanggan SERVER/INDESO:", q("SELECT username, nama, status FROM pelanggan WHERE username IN ('SERVER','INDESO')") or "tidak ada")
    print("  Sedang diisolir:", q("SELECT username, profile_asal, sejak FROM isolir") or "tidak ada")
    print("  Calon isolir otomatis:", q("SELECT t.username, t.periode, t.jatuh_tempo FROM tagihan t JOIN pelanggan p ON p.username=t.username "
          "WHERE t.status='belum' AND COALESCE(p.gratis,0)=0 AND COALESCE(p.status,'aktif')='aktif' "
          "AND t.jatuh_tempo < date('now','localtime') AND t.username NOT IN (SELECT username FROM isolir)") or "tidak ada")
bagian("GenieACS: preset", preset)
bagian("GenieACS: provision & virtual parameter", provision)
bagian("GenieACS: perintah antre (dijalankan saat ONU lapor)", tugas)
bagian("GenieACS: perintah gagal yang diulang", fault)
bagian("GenieACS: semua ONU (akun, jadwal lapor, WiFi, WAN)", onu)
bagian("INDESO", indeso)
