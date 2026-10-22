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

## 3. Sisa konfigurasi lama

Tabel rute `TO_STARLINK` (rute `BCKUP`) dan `to-LINKBIT-GAME` adalah sisa percobaan pemisahan jalur. Keduanya tidak
dipakai, karena mangle kosong, dan boleh dihapus.

Bila ide jalur per paket (gaming lewat Linkbit, rumahan lewat Starlink) dikerjakan lagi, rancangannya ada di commit
`d030daf`.
