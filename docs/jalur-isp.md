# Jalur ISP per paket (Gaming → Linkbit, Rumahan → Starlink)

Setiap paket di INDESO punya **jalur**:

| Jalur | Lewat | Cadangan otomatis |
|---|---|---|
| **Gaming** | Linkbit (ISP inti, ping rendah) | Starlink |
| **Rumahan** | Starlink | Linkbit |

**Cara kerjanya:**
- Profil PPP paket gaming diberi address-list `JALUR-GAMING`. INDESO mengaturnya otomatis dari **Kelola paket**.
- Saat pelanggan gaming login PPPoE, IP-nya otomatis masuk daftar itu.
- Pelanggan lain dari `172.20.52.0/24` diarahkan ke tabel rute `ke-starlink`.
- Lalu lintas MikroTik sendiri (INDESO, VPN, WireGuard, ACS) tetap lewat Linkbit.

Satu pelanggan selalu lewat satu ISP, jadi aman untuk game dan aplikasi bank. Perbedaan kecepatan kedua ISP juga tidak
masalah, karena pembagiannya per paket, bukan dibagi rata.

## 1. Pasang aturan di MikroTik (sekali saja)

> **Lakukan saat sepi (malam).** Sebelum menempel skrip, aktifkan **Safe Mode**: di Winbox klik tombol *Safe Mode*,
> atau di terminal tekan **Ctrl+X**. Kalau ada yang salah dan koneksi Mas ke MikroTik putus, MikroTik otomatis
> membatalkan semua perubahan. Setelah semuanya dicek benar, matikan Safe Mode lagi (Ctrl+X) supaya perubahan
> tersimpan.

```
# --- Tabel rute untuk pelanggan rumahan ---
/routing table add name=ke-starlink fib comment="INDESO-JALUR"

# --- Tujuan lokal yang TIDAK boleh dibelokkan (ACS, WireGuard, OLT, VPN, sesama pelanggan) ---
/ip firewall address-list
add list=TUJUAN-LOKAL address=10.0.0.0/8 comment="INDESO-JALUR"
add list=TUJUAN-LOKAL address=172.16.0.0/12 comment="INDESO-JALUR"
add list=TUJUAN-LOKAL address=192.168.0.0/16 comment="INDESO-JALUR"
add list=TUJUAN-LOKAL address=100.64.0.0/10 comment="INDESO-JALUR"

# --- Tandai koneksi pelanggan rumahan, arahkan ke Starlink ---
/ip firewall mangle
add chain=prerouting src-address=172.20.52.0/24 protocol=udp dst-port=53 action=accept comment="INDESO-JALUR DNS tetap ke router"
add chain=prerouting src-address=172.20.52.0/24 protocol=tcp dst-port=53 action=accept comment="INDESO-JALUR DNS tetap ke router"
add chain=prerouting src-address=172.20.52.0/24 src-address-list=!JALUR-GAMING dst-address-list=!TUJUAN-LOKAL connection-state=new action=mark-connection new-connection-mark=ke-starlink passthrough=yes comment="INDESO-JALUR rumahan: tandai koneksi baru"
add chain=prerouting src-address=172.20.52.0/24 connection-mark=ke-starlink action=mark-routing new-routing-mark=ke-starlink passthrough=no comment="INDESO-JALUR rumahan: lewat Starlink"

# --- Rute tabel ke-starlink: Starlink, cadangan Linkbit ---
/ip route
add dst-address=0.0.0.0/0 gateway="100.64.0.1%WAN STARLINK ETH4" routing-table=ke-starlink distance=1 check-gateway=ping comment="INDESO-JALUR rumahan via Starlink"
add dst-address=0.0.0.0/0 gateway="192.168.100.1%WAN LINKBIT ETH3" routing-table=ke-starlink distance=2 comment="INDESO-JALUR cadangan rumahan via Linkbit"

# --- Failover utama yang mengecek INTERNET Linkbit (bukan sekadar modemnya) ---
add dst-address=9.9.9.9/32 gateway=192.168.100.1 scope=10 comment="INDESO-JALUR cek internet Linkbit"
add dst-address=0.0.0.0/0 gateway=9.9.9.9 target-scope=11 check-gateway=ping distance=1 comment="INDESO-JALUR utama via Linkbit"
/ip dhcp-client set [find interface="WAN LINKBIT ETH3"] default-route-distance=3

# --- Fasttrack melewati mangle: koneksi rumahan yang sudah ditandai tidak boleh di-fasttrack ---
/ip firewall filter set [find action=fasttrack-connection] connection-mark=no-mark
```

Setelah skrip ini, tabel utama berisi:
1. Linkbit lewat cek internet (distance 1);
2. Starlink (DHCP, distance 2);
3. Linkbit lewat DHCP (distance 3, cadangan terakhir).

Kalau internet Linkbit putus walaupun modemnya menyala, MikroTik sekarang ikut pindah ke Starlink.

## 2. Buat profil gaming dan paket gaming

Paket gaming dan rumahan dengan kecepatan sama butuh profil PPP sendiri. Salin dari profil paket yang ada:

```
/ppp profile add copy-from=paket-20mbps name=gaming-20mbps comment="Paket Gaming 20 Mbps"
```

Lalu di INDESO buka **Pelanggan → Kelola paket → Tambah paket**:
- **Nama:** Gaming 20 Mbps
- **Kecepatan:** 20
- **Harga:** sesuai keinginan
- **Jalur internet:** Gaming
- **Profil PPPoE:** gaming-20mbps

INDESO otomatis memasang `address-list=JALUR-GAMING` di profil itu.

Untuk memindahkan pelanggan, ubah paketnya di **Pelanggan → (nama) → Paket**. Profil PPPoE di MikroTik ikut diganti dan
pelanggan tersambung ulang beberapa detik. Kalau jalur sebuah paket diubah, tekan **Sambung ulang pelanggan** di
Kelola paket supaya pelanggan yang sedang online langsung pindah jalur.

## 3. Cek hasilnya

```
/ip route print where routing-table=ke-starlink
/ip firewall mangle print stats where comment~"INDESO-JALUR"
/ip firewall address-list print where list=JALUR-GAMING
/tool traceroute 8.8.8.8 routing-table=ke-starlink count=1
```

**Yang harus terlihat:**
- Penghitung (*packets*) mangle rumahan bertambah.
- Rute `ke-starlink` via Starlink berstatus aktif (A).
- Pelanggan gaming yang online tercantum di `JALUR-GAMING`.

**Cek dari HP pelanggan:** buka situs cek IP (mis. *ifconfig.me*).
- Pelanggan rumahan harus tampil dengan IP Starlink.
- Pelanggan gaming harus tampil dengan IP Linkbit.

## 4. Batalkan semua (bila perlu)

```
/ip firewall mangle remove [find comment~"INDESO-JALUR"]
/ip route remove [find comment~"INDESO-JALUR"]
/ip firewall address-list remove [find comment~"INDESO-JALUR"]
/routing table remove [find name=ke-starlink]
/ip dhcp-client set [find interface="WAN LINKBIT ETH3"] default-route-distance=1
/ip firewall filter unset [find action=fasttrack-connection] connection-mark
```

Setelah dibatalkan, semua pelanggan kembali lewat Linkbit dengan Starlink sebagai cadangan, seperti sebelumnya. Profil
gaming dan address-list-nya boleh dibiarkan, karena tanpa aturan mangle keduanya tidak berpengaruh.
