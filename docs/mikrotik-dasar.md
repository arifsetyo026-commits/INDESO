# Pengaturan dasar MikroTik (failover & keamanan login)

Dipasang 22 Oktober 2026 dengan Safe Mode, tanpa memutus pelanggan. RouterOS 7.8.

## 1. Failover Linkbit → Starlink yang mengecek internet

Sebelumnya rute cadangan Starlink hanya aktif bila port atau modem Linkbit mati. Bila modem menyala tapi internet
Linkbit putus, pelanggan ikut putus.

**Cara kerjanya sekarang:**
- MikroTik ping 9.9.9.9 lewat modem Linkbit setiap 10 detik.
- Dua kali gagal berturut-turut (±20 detik) → rute Linkbit nonaktif → semua lewat Starlink.
- Ping berhasil lagi → kembali ke Linkbit.

| Rute `0.0.0.0/0` (tabel main) | Distance | Peran |
|---|---|---|
| gateway `9.9.9.9` (rekursif, `check-gateway=ping`), komentar `FAILOVER utama Linkbit` | 1 | utama |
| DHCP Starlink `100.64.0.1` | 2 | cadangan |
| DHCP Linkbit `192.168.100.1` | 3 | cadangan terakhir |

Pendukung rute utama: `9.9.9.9/32` lewat `192.168.100.1` (`scope=10`), komentar `FAILOVER cek internet Linkbit`.

**Script `failover-bersih`:**
- Dijalankan scheduler setiap 10 detik.
- Mengosongkan tabel koneksi **hanya saat** rute utama berubah aktif ↔ nonaktif. Dengan begitu koneksi lama, termasuk
  WireGuard ke VPS, tidak tersangkut di jalur yang mati.
- Saat pertama jalan, script hanya mencatat kondisi (`foAktif`) tanpa menghapus apa pun.
- Setiap perpindahan tercatat di log: `/log print where message~"FAILOVER"`.

**Yang memicu failover:**
- internet Linkbit putus;
- modem mati atau hang;
- kabel ether3 lepas.

**Yang tidak memicu:**
- Linkbit lambat tapi masih membalas ping;
- satu ping hilang;
- gangguan di Starlink atau pelanggan.

**Kelemahan:** hanya satu alamat yang dicek. Bila Quad9 (9.9.9.9) sendiri bermasalah, MikroTik pindah ke Starlink
tanpa perlu. Kemungkinannya kecil, dan pelanggan tetap online.

**Uji (saat sepi, pelanggan pindah ±1 menit):**
```
/ip route disable [find comment="FAILOVER cek internet Linkbit"]
# tunggu 30 detik, cek internet & /log print where message~"FAILOVER"
/ip route enable [find comment="FAILOVER cek internet Linkbit"]
```

**Batalkan (urutan ini tidak memutus pelanggan):**
```
/ip dhcp-client set [find interface="WAN LINKBIT ETH3"] default-route-distance=1
/system scheduler remove [find comment="FAILOVER"]
/system script remove [find comment="FAILOVER"]
/ip route remove [find comment~"FAILOVER"]
```

## 2. Layanan login hanya dari jaringan tepercaya

Sebelumnya Winbox, WebFig, SSH, dan API bisa dibuka dari mana saja, termasuk oleh pelanggan (172.20.52.x). Telnet dan
FTP juga menyala tanpa enkripsi.

Pembatasannya memakai `/ip service ... address=`, bukan aturan drop firewall. Akibatnya internet, DNS (aturan Force DNS
ke router), NTP OLT, dan ping pelanggan tidak terpengaruh sama sekali.

| Layanan | Status |
|---|---|
| telnet, ftp, api-ssl | dimatikan |
| www (80), ssh (22), api (8728, dipakai INDESO), winbox (8291) | hanya dari daftar di bawah |

**Daftar izin:**

| Alamat | Siapa |
|---|---|
| `10.99.0.0/24` | VPS INDESO/ACS dan HP owner (WireGuard `wghp`, tampil sebagai 10.99.0.1) |
| `10.10.10.0/24` | WireGuard dashboard |
| `10.10.1.0/24` | WireGuard Temanggung |
| `192.168.175.0/24` | VPN Pemalang |
| `192.168.2.0/24`, `192.168.200.0/24` | jaringan OLT (laptop teknisi di sisi OLT) |

Jalan darurat di lokasi: **Winbox lewat MAC address** tidak terpengaruh pembatasan IP.

**Menambah alamat** (mis. jalur yang dipakai NOC). Ganti `<alamat-baru>` dengan alamat atau subnet yang akan diizinkan:
```
/ip service set [find name~"^(www|ssh|api|winbox)\$"] address=10.99.0.0/24,10.10.10.0/24,10.10.1.0/24,192.168.175.0/24,192.168.2.0/24,192.168.200.0/24,<alamat-baru>
```

**Akun:**
- `INDESO`: owner, juga dipakai aplikasi INDESO.
- `vicka`: NOC, tetap aktif.

## 3. Ukuran paket ke ACS (MSS)

Jalur laporan ONU ke ACS melewati WireGuard, yang hanya muat paket ±1420 byte. ONU yang memblokir ICMP dari WAN
(mis. Huawei) tidak menerima pesan "paket kebesaran", sehingga paket besarnya bisa hilang di jalan. Dua aturan ini
mengecilkan ukuran segmen TCP **khusus ke/dari 10.99.0.1**. Internet pelanggan tidak tersentuh:

```
/ip firewall mangle add chain=forward protocol=tcp tcp-flags=syn dst-address=10.99.0.1 tcp-mss=1361-65535 action=change-mss new-mss=1360 passthrough=yes comment="ACS MSS ke VPS"
/ip firewall mangle add chain=forward protocol=tcp tcp-flags=syn src-address=10.99.0.1 tcp-mss=1361-65535 action=change-mss new-mss=1360 passthrough=yes comment="ACS MSS dari VPS"
```

Batalkan: `/ip firewall mangle remove [find comment~"ACS MSS"]`.

## 4. Sisa konfigurasi lama

Dihapus 26 Oktober 2026 (tidak pernah aktif): tabel rute `TO_STARLINK` (rute `BCKUP`) dan `to-LINKBIT-GAME`, queue tree
(tidak ada mangle yang memberi penanda paket), NAT "Force DNS to Router" dan address list `IP_PELANGGAN` (untuk
10.10.0.0/16, bukan subnet pelanggan), NAT "TUNNEL-ONT", filter "Isolir: Allow DNS" (address list `ISOLIR_RTRW` tidak
ada), dan pool `pool-pppoe`. DHCP server `dhcp-wifi-server` di port Starlink dinonaktifkan (tanpa lease). Cache DNS
diperkecil ke 20 MB, log penting ditulis ke disk, dan `default-profile` PPPoE server menjadi `paket-15mbps`.

Bila ide jalur per paket (gaming lewat Linkbit, rumahan lewat Starlink) dikerjakan lagi, rancangannya ada di commit
`d030daf`.

## 5. Batasi akses pelanggan ke jaringan internal

Firewall bawaan hanya berisi aturan *accept*. Lalu lintas pelanggan ke `wg-acs` di-masquerade sehingga di VPS tampak
sebagai 10.99.0.2 (alamat MikroTik yang diizinkan nginx), jadi pelanggan bisa membuka halaman login NBI dan web
GenieACS, juga manajemen OLT. ONU hanya butuh port 7547 (TR-069) dan 7567 (file uji) ke 10.99.0.1.

Dipasang di jam sepi dengan Safe Mode (Ctrl+X). Internet pelanggan, PPPoE, antrean, dan NAT ke internet tidak tersentuh:

```
/ip firewall address-list add list=JARINGAN-INTERNAL address=10.0.0.0/8
/ip firewall address-list add list=JARINGAN-INTERNAL address=192.168.0.0/16
/ip firewall filter move [find comment~"Established/Related"] destination=0
/ip firewall filter add chain=forward action=drop connection-state=invalid comment="Drop invalid" place-before=1
/ip firewall filter add chain=forward action=accept src-address-list=pppoe-client dst-address=10.99.0.1 protocol=tcp dst-port=7547,7567 comment="ONU -> ACS (TR-069 & file)" place-before=2
/ip firewall filter add chain=forward action=drop src-address-list=pppoe-client dst-address-list=JARINGAN-INTERNAL comment="Pelanggan tidak boleh ke jaringan internal" place-before=3
/ip firewall filter add chain=input action=accept connection-state=established,related comment="Input: lanjutan koneksi" place-before=0
/ip firewall filter add chain=input action=accept src-address-list=pppoe-client protocol=icmp comment="Input: ping dari pelanggan"
/ip firewall filter add chain=input action=accept src-address-list=pppoe-client protocol=udp dst-port=53,123 comment="Input: DNS & NTP dari pelanggan"
/ip firewall filter add chain=input action=drop src-address-list=pppoe-client comment="Input: pelanggan tidak boleh akses router"
```

Uji sebelum keluar dari Safe Mode: internet pelanggan normal, Diagnosa INDESO "Ambil data terbaru dari ONU"
berhasil, dan `http://10.99.0.1:3000` dari HP pelanggan tidak bisa dibuka.

Batalkan:
```
/ip firewall filter remove [find comment="Drop invalid"]
/ip firewall filter remove [find comment="ONU -> ACS (TR-069 & file)"]
/ip firewall filter remove [find comment="Pelanggan tidak boleh ke jaringan internal"]
/ip firewall filter remove [find comment~"^Input:"]
/ip firewall address-list remove [find list=JARINGAN-INTERNAL]
```
