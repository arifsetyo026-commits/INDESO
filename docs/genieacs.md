# Kendali ONU dari pusat (TR-069 + GenieACS)

Panduan ini menghubungkan ONU pelanggan (ZTE, Fiberhome, dan merek lain yang punya
TR-069) ke server **GenieACS** di VPS. Setelah terhubung, dari menu
**Diagnosa & ONU** di dashboard INDESO Anda bisa:

- melihat model, firmware, lama ONU menyala, dan jumlah perangkat di rumah pelanggan;
- mengganti **nama WiFi** dan **password WiFi** (2,4 GHz dan 5 GHz);
- **reboot ONU**;
- mengambil data terbaru dari ONU kapan saja.

Fitur diagnosa lain (trafik langsung, tes ping, redaman, riwayat putus, kesimpulan
otomatis) **sudah jalan tanpa VPS**, karena memakai MikroTik dan OLT yang sudah ada.

## Gambaran jaringan

```
ONU pelanggan ──PPPoE──> MikroTik kantor ══ WireGuard (10.99.0.0/24) ══> VPS (GenieACS)
                               │                                          10.99.0.1
HP Termux (INDESO) ── LAN ─────┘
```

- ONU melapor ke `http://10.99.0.1:7547/` lewat terowongan, jadi port ACS **tidak dibuka ke internet**.
- GenieACS bisa menghubungi ONU balik (*connection request*) karena VPS mengenal pool IP PPPoE lewat terowongan. Itu sebabnya perintah langsung dijalankan dalam hitungan detik.
- INDESO mengakses API GenieACS (`http://10.99.0.1:7557`, ber-password) lewat jalur yang sama.

## 1. Siapkan VPS

- Ubuntu **22.04** atau **24.04**, disk minimal 20 GB. Lokasi Indonesia (mis. Jakarta) supaya respons cepat.
- **RAM 1 GB cukup** untuk ratusan ONU. Bila RAM < 2 GB, skrip otomatis:
  - membuat **swap 2 GB**;
  - membatasi cache MongoDB ke 256 MB;
  - membatasi memori tiap layanan GenieACS;
  - **tidak** menyalakan server firmware (FS). Server firmware hanya perlu untuk update firmware massal; nyalakan dengan `WITH_FS=1`.
- Web admin GenieACS (UI) tetap dinyalakan untuk pemeriksaan. INDESO tidak memerlukannya, jadi bisa dimatikan dengan `WITH_UI=0` untuk menghemat ±100 MB.
- Perkiraan pemakaian memori di VPS 1 GB: MongoDB ±250–350 MB, GenieACS ±250–350 MB, nginx + WireGuard + sistem ±150 MB.
- CPU harus mendukung **AVX** (syarat MongoDB). Hampir semua VPS modern sudah mendukung. Skrip akan memperingatkan bila tidak.
- MikroTik kantor memakai **RouterOS v7**, karena WireGuard baru ada di v7. Cek di MikroTik: `/system resource print`. Kalau masih v6, upgrade dulu ke v7.

## 2. Jalankan pemasang di VPS

Masuk ke VPS lewat SSH, lalu:

```bash
git clone https://github.com/arifsetyo026-commits/INDESO.git
cd INDESO/deploy/genieacs
sudo PPPOE_SUBNET=172.20.52.0/24 LAN_SUBNET=192.168.50.0/24 bash install.sh
```

Ganti kedua nilai sesuai jaringan Anda:

| Isian | Artinya | Cara melihat di MikroTik |
|---|---|---|
| `PPPOE_SUBNET` | Pool IP yang dibagikan ke pelanggan PPPoE | `/ip pool print` |
| `LAN_SUBNET` | Jaringan kantor tempat HP Termux INDESO terhubung | `/ip address print` |

Skrip ini memasang MongoDB, GenieACS, nginx, dan WireGuard. Skrip aman untuk VPS yang sudah
menjalankan layanan lain:
- memakai antarmuka WireGuard sendiri (`wgacs`) dan port UDP yang belum terpakai (51820–51840);
- semua layanan ACS hanya mendengarkan di 10.99.0.1 atau localhost;
- firewall `ufw` tidak dinyalakan paksa. Bila sudah aktif, skrip hanya menambah izin yang diperlukan.
Di akhir, skrip mencetak tiga hal. Simpan ketiganya:

1. **Password NBI** untuk `.env` INDESO (juga tersimpan di `/root/genieacs-nbi-password.txt`).
2. **Perintah MikroTik** untuk membuat terowongan.
3. **URL ACS** untuk diisi di ONU.

## 3. Sambungkan MikroTik

Tempel perintah yang dicetak skrip ke **Terminal** MikroTik (Winbox → New Terminal). Lalu uji:

```
/ping 10.99.0.1 count=3
/interface wireguard peers print
```

Ping harus dibalas, dan kolom *last-handshake* peer harus berisi waktu (bukan kosong).

## 4. Buat akun admin GenieACS

Dari laptop atau HP yang tersambung ke LAN kantor, buka `http://10.99.0.1:3000/`.
Pada kunjungan pertama GenieACS menampilkan wizard inisialisasi untuk membuat akun admin. Gunakan password yang kuat.

**Di wizard itu, jangan centang Presets dan Provisions.** Bila dicentang, GenieACS membuat preset bawaan `bootstrap`,
`default`, dan `inform`. Preset `inform` memasang jadwal lapor 300 detik dan kredensial connection request per ONU,
sehingga bentrok dengan `indeso-default` (900 detik, kredensial bersama) dan saling timpa setiap ONU lapor. Preset
`default` membaca daftar perangkat tiap jam. Preset yang benar hanya `indeso-default` dan `indeso-diagnostik`
(dipasang `install.sh`); periksa dengan `deploy/genieacs/periksa-acs.py`.

## 5. Arahkan TR-069 di setiap ONU

Ini dilakukan **sekali per ONU**. Pakai kesempatan saat pasang baru atau kunjungan teknisi,
atau buka web ONU dari kantor. Nama menu bisa sedikit berbeda antar firmware.

**ZTE (F609, F660, F670L, dll.)**: *Administration → TR-069* (kadang *Remote Management → TR-069*)

| Kolom | Isi |
|---|---|
| Enable / ACS | Centang |
| ACS URL | `http://10.99.0.1:7547/` |
| ACS Username / Password | kosongkan |
| Periodic Inform | Aktif, interval `900` detik (15 menit) |
| Connection Request Username / Password | samakan dengan ACS username/password (lihat panduan teknisi internal); server memasangnya otomatis dari `/root/genieacs-cr-user.txt` & `/root/genieacs-cr-password.txt` |
| WAN Connection / Interface | pilih koneksi **INTERNET (PPPoE)** |

**Fiberhome (HG6243C, AN5506, dll.)**: *Management / Application → TR-069* (atau *Remote Management*). Isinya sama: aktifkan, ACS URL `http://10.99.0.1:7547/`, periodic inform 900 detik, WAN = koneksi internet/PPPoE.

Simpan. Dalam 1–15 menit ONU muncul di menu *Devices* GenieACS.

INDESO mencocokkan ONU dengan pelanggan secara otomatis. Pertama lewat **nomor seri GPON**
(sama dengan yang dibaca dari OLT); kalau tidak cocok, lewat **username PPPoE** yang tersimpan di ONU.

### Dari INDESO lewat OLT C-Data (tanpa membuka web OLT)
INDESO bisa mengirim ACS URL & login ke ONU lewat telnet OLT, sama dengan menu *TR-069 Service Configuration* di web OLT. Perintah yang dijalankan per ONU (di `interface gpon 0/0`):
```
ont tr069 <port> <onu> tr069-management enable
ont tr069 <port> <onu> acs-url http://10.99.0.1:7547 acs-username <user> acs-password <sandi>
ont tr069 <port> <onu> inform enable interval 900
```
Isi di `/etc/indeso/indeso.env` lalu restart INDESO:
```
OLT_TELNET_USER=<login OLT>
OLT_TELNET_PASS=<sandi OLT>
ACS_ONU_USER=<ACS username ONU>
ACS_ONU_PASS=<ACS password ONU>
```
Untuk **semua ONU sekaligus** dari terminal VPS (membaca daftar ONU langsung dari konfigurasi OLT, melewati ONU yang sudah punya pengaturan TR-069 dan ONU non-ZTE, lalu menyimpan dan memeriksa *Config state*):
```
sudo /opt/indeso/.venv/bin/python /opt/indeso/deploy/indeso/push-tr069.py           # lihat daftar dulu
sudo /opt/indeso/.venv/bin/python /opt/indeso/deploy/indeso/push-tr069.py --kirim   # kirim + save + cek
```

**ZTE F609 (dan ONU lain yang punya WAN bawaan "TR-069" yang tidak bisa dihapus)** menerima pengaturan dari OLT,
tapi baru melapor ke ACS setelah restart. Sebelum restart, ONU melapor lewat WAN "TR-069" yang kosong.
Pakai opsi restart, yang menjalankan `ont tr069 <port> <onu> ip-index 0`, `ont tr069 <port> <onu> ip-index unbind`, `ont reboot <port> <onu>`, lalu `save`, dengan jeda 5 detik antartahap (`OLT_JEDA_DETIK`). Tanpa jeda, `unbind` tiba sebelum ONU menerima ikatannya dan tidak ada yang berubah. Urutan ini terbukti di ONU Gunawan (1/23) dan Endah (1/9). Unbind saja atau restart saja tidak cukup. ONU yang WAN-nya disimpan OLT (`show ont ipconfig` berisi PPPoE, mis. hasil aktivasi ONU Baru) dilewati dari `unbind`, karena ONU seperti Huawei hanya melapor lewat WAN itu. Internet pelanggan putus ±1–2 menit:
- **Diagnosa**: tombol *Kirim TR-069 dari OLT + restart ONU*.
- **Pengaturan → Sistem → TR-069 ONU lewat OLT**: centang *Restart ONU setelah dikirim*.
- **Terminal**, untuk ONU yang sudah pernah dikirimi tapi belum muncul di ACS:
  ```
  sudo /opt/indeso/.venv/bin/python /opt/indeso/deploy/indeso/push-tr069.py --reboot                   # lihat daftar dulu
  sudo /opt/indeso/.venv/bin/python /opt/indeso/deploy/indeso/push-tr069.py --onu 1/23 --kirim --reboot  # satu ONU
  ```
INDESO tidak pernah memakai `ont reboot <port> all`.
Pilihan: `--onu 2/5` (hanya ONU tertentu), `--semua-merek`, `--ulang`.

Atau dari dashboard: **Pengaturan → Sistem → TR-069 ONU lewat OLT** (pilih banyak ONU sekaligus), atau tombol **Kirim pengaturan TR-069 dari OLT** di Diagnosa pelanggan yang ONU-nya belum terdaftar. Setelah itu INDESO menjalankan `save` supaya tetap ada saat OLT restart. ONU ZTE tetap perlu WAN PPPoE berjenis `INTERNET_TR069`.

### Menu "ONU Baru" (aktivasi ONU yang baru dicolok)

OLT memakai `ont authmode ... auto`, jadi ONU yang dicolok langsung didaftarkan OLT. INDESO mencatat nomor seri
(SN) setiap ONU yang terbaca. SN yang belum pernah terlihat masuk menu **ONU Baru**, dan grup Telegram dikabari.
Saat fitur ini pertama kali jalan, semua ONU yang sudah ada dianggap lama, jadi daftar dan Telegram tidak banjir.

Setiap ONU baru punya ceklis: terdaftar di OLT → sinyal fiber bagus → PPPoE tersambung → ada di data pelanggan →
lapor ke ACS → WiFi diatur. Tombol yang tersedia:
- **Kirim TR-069 + restart ONU**: urutan yang sama dengan bagian di atas (`ip-index 0` → `unbind` → `ont reboot`, lalu `save`).
- **Kirim TR-069 saja** dan **Restart ONU**: lewat OLT, untuk satu ONU itu saja.
- **Atur WiFi**: nama dan password diatur sekaligus untuk 2,4 GHz dan 5 GHz (nama 5 GHz diberi akhiran, bawaannya `-5G`).
  Langkah ini perlu ONU sudah lapor ke ACS. Password WiFi tidak dicatat di log.
- **Dipasang untuk pelanggan**: catatan sebelum PPPoE ONU tersambung. **Tandai selesai** / **Abaikan** memindahkan
  ONU ke Riwayat 30 hari.

**Isi PPPoE & WiFi dari OLT** (tanpa web ONU atau kunjungan) mengirim dalam satu sesi telnet:
```
ont ipconfig P O ip-index 0 pppoe username U password P vlan 1 priority 0
ont ipconfig P O ip-index 0 connection-type route
ont ipconfig P O ip-index 0 nat enable
ont ipconfig P O ip-index 0 binding eth 1-4 wlan 1,5   (port LAN + SSID utama 2,4 GHz & 5 GHz; ONU satu band: wlan 1)
ont internet-config P O ip-index 0
ont wifi P O 2.4g 1 enable username NAMA password SANDI authentication wpa2-psk encryption aes
ont wifi P O 5g 1 enable username NAMA-5G password SANDI authentication wpa2-psk encryption aes
(+ pengaturan TR-069, lalu save)
```

**Hasil uji di ONU uji ZTE F670L V9 (PON 0/0/2, ONU 4):**
- PPPoE login dari OLT;
- ONU lapor ke ACS tanpa perlu mengikat `ip-index`;
- WiFi muncul.

**Catatan:**
- Perintah WAN khusus C-Data (`ont wan …`) **diabaikan** ONU ZTE, jadi tidak dipakai.
- Password PPPoE diambil dari secret MikroTik. Password tidak pernah dikirim ke browser dan tidak dicatat di log.
- Akun yang sedang dipakai ONU lain ditolak, karena profil memakai `only-one`.
- Nama dan password WiFi lewat OLT tidak boleh berisi spasi.
- VLAN bisa diubah lewat `OLT_PPPOE_VLAN` di `.env` (bawaan 1, untuk jaringan tanpa tag).
- **Huawei HG8145V5** (uji 23 Okt 2026, PON 0/0/2, ONU 5):
  - PPPoE dari OLT berhasil, dan internet jalan;
  - **TR-069 wajib diikat ke WAN PPPoE dari OLT** (`ont tr069 P O ip-index 0`). Dengan `TR069 IP index : unbind`
    (lihat `show ont tr069 P O`), Huawei tidak mengirim laporan sama sekali: penghitung MikroTik dan tcpdump di VPS
    sama-sama nol, walau internetnya jalan. ZTE yang WAN-nya dibuat di web ONU justru butuh `unbind`. Karena itu
    INDESO kini memeriksa `show ont ipconfig P O`: bila OLT menyimpan WAN untuk ONU itu, ikatan `ip-index 0`
    dibiarkan. Aktivasi di ONU Baru juga langsung mengikat TR-069 ke WAN PPPoE yang dibuatnya;
  - password WiFi lewat ACS: Huawei menolak `KeyPassphrase` (`9007 Invalid parameter value`), jadi INDESO mengisi
    `PreSharedKey.1.PreSharedKey` untuk ONU Huawei. SSID `BackhaulSSID…` (jalur mesh) disembunyikan dari daftar WiFi;
  - menu WAN dan TR-069 di web Huawei firmware operator tersembunyi, termasuk untuk super admin, jadi pengaturan ini
    hanya bisa lewat OLT;
  - Huawei melapor ke ACS dengan SN versi angka: `4857544398B2DFBA` (`48575443` = "HWTC") untuk SN OLT `HWTC98B2DFBA`.
    INDESO menyamakan kedua bentuk ini otomatis. Di log ACS, cari dengan bentuk angkanya:
    `sudo grep -i "4857544398B2DFBA" /var/log/genieacs/genieacs-cwmp-access.log | tail`;
  - Huawei lambat menjawab `GetParameterNames` saat ACS membaca seluruh parameternya. Dengan batas bawaan GenieACS
    (`cwmp.sessionTimeout` = 30 detik) sesi pertamanya berakhir `Session timeout` tepat 30 detik setelah permintaan
    terakhir, sehingga data ONU tidak pernah lengkap. Naikkan batasnya sekali (berlaku untuk semua ONU, tidak perlu
    restart GenieACS):
    `sudo mongosh --quiet genieacs --eval 'db.config.updateOne({_id: "cwmp.sessionTimeout"}, {$set: {value: "120"}}, {upsert: true})'`
    lalu picu sesi baru (tombol **Restart ONU**, atau `POST /devices/<id>/tasks?connection_request` lewat NBI);
  - **WiFi dari OLT diabaikan** (SSID tidak muncul). Atur WiFi lewat **Atur WiFi** (jalur ACS), yang juga menyalakan WiFi
    bila mati;
  - Huawei menolak ping dari arah WAN secara bawaan, jadi ping dari MikroTik ke IP PPPoE-nya selalu "loss". Itu bukan
    gangguan. Pakai **Ping dari ONU ke Google** atau **Tes kecepatan**.
- Ceklis **WiFi diatur** di ONU Baru dicek dari laporan ONU ke ACS: tercentang hanya bila SSID yang dikirim benar-benar
  menyala di ONU.
- Model lain (mis. F609, FiberHome) belum diuji. Bila PPPoE belum tersambung 2 menit setelah dikirim, tekan
  **Restart ONU** sekali. Bila tetap belum, isi lewat web ONU.

### ONU bekas & pelanggan ganti ONU

INDESO mencatat akun PPPoE terakhir yang terlihat lewat setiap SN. Bila akun itu kini tersambung lewat SN lain dan SN
lamanya offline, SN lama **dilepas** dari catatan (tercatat di Aktivitas: "ganti ONU"). Bila ONU lama itu kelak menyala
lagi, misalnya sudah direset dan dipasang di rumah lain, ia muncul di **ONU Baru** sebagai ONU baru dan dikabarkan lewat
Telegram. SN asing yang offline tidak dicatat sampai benar-benar online.

Pendaftaran ONU mati di OLT dibersihkan lewat **ONU & Diagnosa → 🧹 Bersihkan ONU mati** (hanya owner). INDESO menghapusnya
satu per satu dengan `ont delete P O` lalu `save`; `ont delete P all` dan `ont delete P offline-list` tidak pernah dipakai,
karena ONU pelanggan yang mati listrik juga offline. Yang boleh dihapus hanya ONU offline yang:
- pemakai terakhirnya kini online lewat ONU lain (ganti ONU), atau
- pelanggannya sudah berhenti, atau
- tanpa pemakai yang diketahui dan sudah offline minimal 3 hari (atau tidak pernah terlihat online).

Bila ONU yang dihapus menyala lagi, OLT mendaftarkannya ulang (authmode auto) dan INDESO menampilkannya di ONU Baru.

### ONU pelanggan lama yang belum lapor ACS: reset & isi ulang dari OLT

Untuk ONU yang PPPoE-nya diisi di web ONU dengan WAN bertipe INTERNET saja (mis. F670L), sehingga urutan
`ip-index 0` → `unbind` → restart tidak membuatnya melapor:

1. Buka **ONU Baru → ♻ Isi ulang ONU pelanggan lama**, ketik username PPPoE atau SN, lalu tekan **Masukkan ke daftar**.
2. Pada kartunya, tekan **♻ Reset & isi ulang dari OLT**. Isi nama dan password WiFi **yang dipakai pelanggan
   sekarang** (tanpa spasi), supaya HP di rumah tersambung sendiri, lalu ketik ulang username sebagai konfirmasi.
3. INDESO mengirim PPPoE, binding, WiFi, dan TR-069 (`ip-index 0`) lewat OLT, lalu `save`. **Hanya bila PPPoE
   diterima dan tersimpan**, INDESO menjalankan `ont restore-factory P O` (tidak pernah `all`). ONU menyala dalam
   kondisi pabrik dan menerima semuanya dari OLT. WAN lama dari web ikut terhapus, jadi tidak ada akun ganda.
4. Tunggu ±3 menit lalu **Muat ulang**: PPPoE, ACS, dan WiFi harus tercentang.

Nama atau password WiFi yang berisi spasi atau tanda di luar `A-Z a-z 0-9 . _ @ + -` tidak bisa dibawa CLI OLT (OLT memisah
kata di spasi). INDESO lalu tidak mengirim WiFi lewat OLT, tetapi menyimpannya **di memori** (tidak ditulis ke disk) dan
mengirimnya lewat ACS begitu ONU lapor setelah perintah (±1–15 menit; batas 60 menit). Huawei selalu lewat jalur ini
karena mengabaikan WiFi dari OLT. Bila INDESO restart sebelum terkirim, atur lewat tombol **Atur WiFi**.

Internet pelanggan putus ±3 menit. Pengaturan lain di ONU (port forwarding, SSID tambahan, sandi admin) hilang.

ZTE memberi WAN buatan OLT (nama `omci_ipv4_pppoe_1`) jenis layanan bertambah VoIP (F670L `INTERNET_TR069_VOIP`,
F609 V7 `INTERNET_VoIP_TR069`), dan dengan VoIP ZTE
menolak ping dari luar (Tes ping dari MikroTik gagal, padahal internet normal). Provision `indeso-default`
(`deploy/genieacs/provision-indeso.js`) membuang kata VoIP dari jenis layanan itu (hasilnya `INTERNET_TR069`) setiap kali ONU
ZTE lapor (dicek sekali sehari dan setiap ONU restart).
Terbukti di Astri (F670L V9): ping dari MikroTik kembali normal. Setelah file itu berubah, pasang ulang di VPS:
`curl -fsS -X PUT --data-binary @/opt/indeso/deploy/genieacs/provision-indeso.js http://127.0.0.1:7557/provisions/indeso-default`
Kerjakan saat sepi dan pada ONU yang mudah dikunjungi bila gagal.

### Tes kecepatan dari ONU (Diagnosa → ⚡ Tes kecepatan dari ONU)

Ping hanya mengirim paket kecil, jadi keluhan "lemot tapi ping normal" tidak bisa dibuktikan dengan ping. Tombol
ini menyuruh ONU pelanggan mengunduh file uji lewat TR-069 (TR-143 `DownloadDiagnostics`). INDESO lalu menghitung
Mbps dari jumlah byte dan waktu yang dilaporkan ONU, dan membandingkannya dengan paket serta pemakaian pelanggan
sesaat sebelum tes.

File uji bawaan disimpan di **GenieACS FS** di VPS (`http://10.99.0.1:7567/indeso-uji-20MB.bin`). ONU menjangkaunya
lewat terowongan WireGuard yang sama dengan jalur lapor ACS. Siapkan sekali di VPS:

```
sudo systemctl enable --now genieacs-fs
head -c 20000000 /dev/urandom > /tmp/uji.bin
curl -fsS -X PUT --data-binary @/tmp/uji.bin -H "fileType: X INDESO Speedtest" \
  http://127.0.0.1:7557/files/indeso-uji-20MB.bin && rm /tmp/uji.bin
curl -fsS -o /dev/null -w 'ukuran: %{size_download} byte\n' http://10.99.0.1:7567/indeso-uji-20MB.bin
```

Baris terakhir harus menampilkan `ukuran: 20000000 byte`.

Hasil tes mencakup jalur sampai VPS. Kalau VPS jauh atau kecepatannya terbatas, isi **Pengaturan → File uji
speedtest** dengan file HTTP 10–50 MB di server yang dekat, misalnya mirror di Indonesia. ONU tidak bisa memakai HTTPS.
Tidak semua ONU mendukung tes ini. Kalau ONU tidak mendukung, INDESO menampilkan pesan yang jelas.
Tes (juga ping dari ONU) hanya dimulai bila ONU bisa diketuk langsung. Kalau tidak, INDESO langsung memberi tahu
alasannya dan tidak ada perintah yang ditinggal; tekan tombolnya lagi setelah ONU bisa diketuk.

Hasil tes diambil di sesi yang sama saat ONU melapor `8 DIAGNOSTICS COMPLETE`, oleh provision `indeso-diagnostik`
(`deploy/genieacs/provision-diagnostik.js`). Tanpa itu hasil baru terbaca saat INDESO memanggil ONU lagi (Huawei:
±47 detik). Setelah file itu berubah, pasang ulang di VPS:
`curl -fsS -X PUT --data-binary @/opt/indeso/deploy/genieacs/provision-diagnostik.js http://127.0.0.1:7557/provisions/indeso-diagnostik`


## ACS seperlunya (beban ONU)

Prosesor ONU murah dipakai bersama oleh WiFi dan TR-069. Karena itu ACS hanya bekerja saat diperlukan:

- ONU lapor rutin tiap **15 menit** (`PeriodicInformInterval` 900). Laporan rutin hanya beberapa KB.
- Provision `indeso-default` menyegarkan data kecil (nama WiFi, channel, uptime, IP WAN) **sekali sehari**.
- Daftar perangkat terhubung dan klien WiFi **tidak lagi dibaca otomatis tiap jam**. INDESO mengetuk ONU
  (connection request) dan membacanya saat **Diagnosa dibuka**.
- Kredensial connection request dipasang ulang **sekali sehari dan sekali setelah ONU restart**. Hanya 2 parameter
  dan tidak memutus internet. ONU yang **di-reset** (lapor `0 BOOTSTRAP`) atau **dikirimi ulang pengaturan TR-069 dari
  OLT lalu restart** kembali memakai kredensial lain. Ini terbukti pada FiberHome HG6145D2 dan ZTE F670L (27-10).
  Restart biasa tidak menghapusnya. Password tidak bisa dibaca balik dari ONU, jadi GenieACS tidak tahu, dan setiap
  ketukan ditolak sampai kredensial dipasang ulang. Kedua kejadian itu diakhiri restart, sehingga provision langsung
  memasangnya lagi. Bila ketukan tetap ditolak di antara jadwal itu, INDESO memberi ONU tanda (tag
  `pulihkan_ketukan`, bukan perintah antre); provision memasang ulang kredensial saat ONU lapor berikutnya
  (≤15 menit) lalu menghapus tandanya.
- Penghapusan VoIP pada WAN PPPoE `omci_…` ZTE: jenis layanan hanya **dibaca setelah ONU restart**, dan **diubah
  hanya bila masih ada VoIP**. Setelah dibuang, VoIP tidak kembali walau ONU restart; VoIP muncul lagi hanya setelah
  ONU di-reset pabrik lalu diisi ulang dari OLT. Mengubah jenis layanan membuat WAN tersambung ulang.
- **Tanpa perintah antre.** Setiap perintah dari INDESO (WiFi, ping, tes kecepatan, reboot, hapus WAN, Amankan ONU,
  ambil data terbaru) hanya **berhasil** atau **gagal**. INDESO mengetuk ONU dan menunggu hasilnya. Bila ketukan gagal,
  ONU menolak, atau ONU tidak selesai dalam batas waktu, perintah beserta catatan fault-nya dihapus dari GenieACS dan
  alasannya ditampilkan. Perintah baru dikirim lagi bila admin mengulanginya, jadi tidak ada yang jalan belakangan
  tanpa ditunggui (reboot mendadak, hapus WAN dengan nomor yang sudah bergeser, tes kecepatan setelah jendela ditutup).
  Sisa perintah lain di GenieACS (mis. dibuat dari web GenieACS) yang berumur lebih dari 3 menit dihapus otomatis.
- WiFi ONU baru yang dikirim lewat ACS (nama berspasi, ONU Huawei) dikirim **satu kali** begitu ONU lapor setelah
  aktivasi. Bila gagal, tercatat di Aktivitas dan diatur ulang lewat tombol *Atur WiFi*.

Setelah update, pasang ulang provision:
`curl -fsS -X PUT --data-binary @/opt/indeso/deploy/genieacs/provision-indeso.js http://127.0.0.1:7557/provisions/indeso-default`.
Bila `.env` berisi `ACS_ONU_INTERVAL=300`, ubah menjadi `900` (atau hapus barisnya) supaya TR-069 yang dikirim dari OLT sama.

## Pegangan ACS yang sehat

**Pembagian tugas.** OLT (lewat `ont ipconfig`, OMCI) adalah pemilik WAN: satu WAN PPPoE per ONU, binding LAN &
WiFi, dan TR-069 menumpang di WAN itu (`ont tr069 … ip-index 0`). ACS tidak mengubah WAN secara otomatis; ACS
membaca data dan menjalankan perintah manusia (WiFi, reboot, diagnosa) satu ONU pada satu waktu. Bila OLT dan ACS
sama-sama mengatur WAN, keduanya saling timpa (OLT bisa mendorong ulang pengaturannya saat ONU restart).

**Satu ONU = satu WAN PPPoE aktif.** Dua WAN PPPoE aktif di satu ONU saling sikut di MikroTik:
`one-session-per-host=yes` menutup sesi lama bila MAC yang sama membuka sesi baru, dan `only-one=yes` menolak
login kedua dengan akun yang sama. Internet pelanggan jadi putus-sambung. ONU bekas wajib di-reset pabrik
supaya WAN pemilik lama hilang.

**WAN "TR069" bawaan pabrik ZTE itu normal.** ONU ZTE baru (atau setelah reset pabrik) membawa WAN manajemen
jenis IP bernama `TR069` (layanan `TR069_VOIP` / `VoIP_TR069`) berstatus *Connecting*. Ini bukan sesi PPPoE kedua
dan tidak perlu dihapus; lapor ke ACS tetap lewat WAN PPPoE (`INTERNET_TR069`).

**Sebelum menyalakan `genieacs-cwmp`, dan seminggu sekali,** jalankan pemeriksaan (hanya membaca):
`sudo python3 /opt/indeso/deploy/genieacs/periksa-acs.py`. Kondisi sehat:

| Bagian | Sehat |
|---|---|
| Layanan | `mongod`, `genieacs-cwmp/nbi/fs/ui`, `nginx`, `indeso` aktif; file uji kecepatan `20000000 byte` |
| Jadwal lapor | Provision dan `ACS_ONU_INTERVAL` INDESO (yang dikirim OLT) sama; tidak ada `cwmp.auth` kecuali sengaja |
| Preset | Hanya `indeso-default` dan `indeso-diagnostik` |
| Provision | `indeso-default` sama dengan versi terbaru |
| Perintah antre | 0 (INDESO tidak pernah meninggalkan perintah; sisa dari tempat lain dihapus setelah 3 menit) |
| Perintah gagal | 0 (GenieACS mengulangnya setiap ONU lapor) |
| Ketukan | Kredensial setiap ONU dipasang ≤ 24 jam lalu; catatan INDESO: hampir semua perintah langsung jalan |
| Laporan ONU (24 jam) | ±96 lapor rutin per ONU (15 menit); `6 CONNECTION REQUEST` muncul saat Diagnosa dipakai |
| ONU | Satu WAN PPPoE aktif per ONU, tidak ada akun atau nama WiFi yang dipakai dua ONU |

**Tidak ada perintah antre.** Perintah yang antre baru jalan saat ONU lapor (bisa belasan menit kemudian, tanpa ada
yang menunggui): reboot jadi mendadak, atau dua kali bila ONU sudah di-restart lewat OLT, dan hapus WAN bisa mengenai
WAN lain bila nomor WAN bergeser. Karena itu setiap perintah hanya berhasil atau gagal (lihat *ACS seperlunya*). Reboot
yang gagal diketuk menyarankan *Restart lewat OLT*, dan *Restart lewat OLT* membuang reboot ACS yang mungkin tersisa.

Hapus ONU uji atau ONU yang sudah dicabut dari ACS (aman, ONU yang dinyalakan lagi mendaftar ulang sendiri):
`curl -X DELETE http://127.0.0.1:7557/devices/<id>` (id lengkap, karakter khusus di-encode, mis. spasi `%20`).

**Keamanan.** GenieACS 1.2.13 punya kerentanan NBI tanpa autentikasi (CVE-2025-56015). `install.sh` membuat NBI
hanya mendengarkan di `127.0.0.1` dan dibuka ke terowongan lewat nginx berpassword. MikroTik me-masquerade lalu
lintas pelanggan ke `wg-acs` sehingga di VPS pelanggan tampak sebagai 10.99.0.2 (alamat yang diizinkan nginx);
karena itu MikroTik wajib memblokir pelanggan ke 10.0.0.0/8 kecuali port 7547 dan 7567 ke 10.99.0.1
(lihat `docs/mikrotik-dasar.md`).

## 6. Isi `.env` INDESO (di HP Termux)

```
GENIEACS_URL=http://10.99.0.1:7557
GENIEACS_USER=indeso
GENIEACS_PASS=<password NBI dari langkah 2>
```

Lalu restart INDESO (`pkill -f uvicorn`, jalankan lagi).

## 7. Coba

Buka dashboard → **Diagnosa & ONU** → pilih pelanggan yang ONU-nya sudah diarahkan →
**Ambil data terbaru dari ONU**. Setelah itu coba ganti password WiFi atau reboot ONU.

Setiap ganti WiFi atau reboot tercatat di *Log aktivitas*. Password WiFi tidak disimpan
dan tidak dicatat di INDESO. Setelah ganti password, ada tombol untuk mengirimkannya ke WA pelanggan.

## Bila ada masalah

| Gejala | Penyebab umum | Periksa |
|---|---|---|
| ONU tidak muncul di GenieACS | ACS URL salah, WAN TR-069 bukan koneksi internet, terowongan putus | Di MikroTik: `/ping 10.99.0.1`, `/interface wireguard peers print`. Di VPS: `sudo wg show`, `sudo tail -f /var/log/genieacs/genieacs-cwmp-access.log` |
| Perintah gagal: "ONU tidak bisa diketuk langsung" | GenieACS tidak bisa menghubungi ONU balik | ONU mati/offline, atau aturan firewall `forward` MikroTik dari skrip tidak di urutan atas. Connection Request di ONU harus aktif (ZTE: cek *Security → Service Control/ACL*, izinkan TR-069 dari WAN) |
| Perintah gagal: "ONU menolak ketukan … kredensialnya berubah" | Kredensial ketukan di ONU berbeda dengan yang dicatat ACS, biasanya setelah ONU di-reset atau TR-069 dikirim ulang dari OLT | Pulih otomatis saat ONU lapor berikutnya (INDESO memberi tanda `pulihkan_ketukan`; paling lambat sehari lewat jadwal provision), lalu ulangi perintahnya. Riwayatnya terlihat di `periksa-acs.py`: bagian *Ketukan menurut catatan INDESO* (alasan GenieACS per perintah) dan *Laporan ONU ke ACS* (`6 CONNECTION REQUEST` = ketukan berhasil). **Jangan menguji ketukan dengan curl**: ZTE F609/F670 lama menolak jawaban login Digest dari curl (401) walau kredensialnya benar, sementara ketukan GenieACS diterima (27-10: curl ditolak 08:14 & 12:02, GenieACS berhasil 09:11 ke ONU yang sama) |
| Ping dari ONU: "DNS bermasalah" | ONU tidak bisa resolve nama | INDESO otomatis mengulang ke 8.8.8.8. Bila 8.8.8.8 berhasil, perbaiki DNS di WAN PPPoE ONU / profil PPPoE MikroTik |
| Ping/perintah ke satu ONU selalu ditolak (mis. `cwmp.9003 Invalid arguments`), padahal ONU lain normal | Perintah lama yang pernah gagal masih tertahan dan **digabung** dengan perintah baru, jadi ikut ditolak (terjadi pada ZTE F609). INDESO kini membersihkan perintah ping lama otomatis | Lihat `retries` di daftar fault di bawah; hapus tugas & fault-nya (tombol Batalkan di dashboard, atau `DELETE /tasks/<id>` dan `DELETE /faults/<id>` lewat NBI) |
| Perintah ke ONU tertentu selalu gagal | Ada *fault* tertahan | `curl -s http://127.0.0.1:7557/faults/ \| python3 -m json.tool` di VPS. Fault tugas bisa dihapus dari dashboard (tombol Batalkan) |
| Dashboard: "Server ACS tidak terjangkau" | HP Termux tidak bisa mencapai 10.99.0.1 | Dari Termux: `curl -u indeso:PASSWORD http://10.99.0.1:7557/devices/?projection=_id`. Pastikan `LAN_SUBNET` di skrip sesuai |
| "Model ONU ini tidak membuka pengaturan password" | Firmware ONU tidak menyediakan parameter password lewat TR-069 | Tekan *Ambil data terbaru dari ONU* sekali; kalau tetap, ganti lewat web ONU |
| "Server ACS menolak login" | Isi `GENIEACS_USER/PASS` salah | Bandingkan dengan `/root/genieacs-nbi-password.txt` di VPS |

## Keamanan & perawatan

- **Wajibkan login ACS** setelah semua ONU diisi ACS Username/Password yang sama (lihat panduan teknisi internal).
  Tanpa ini, pelanggan mana pun di jaringan PPPoE bisa mendaftarkan "ONU palsu" ke ACS:
  ```
  sudo bash ~/INDESO/deploy/genieacs/wajibkan-login-acs.sh aktifkan
  ```
  Skrip mencatat ONU yang aktif, mengaktifkan login, menunggu 7 menit, lalu **membatalkan otomatis**
  bila ada ONU yang tidak bisa lapor (dan menampilkan daftarnya). Kembalikan kapan saja dengan `… matikan`.
  Setelah aktif, setiap ONU baru wajib diisi ACS Username/Password tersebut.
- Pemeriksaan cepat kesehatan ACS di VPS:
  ```
  systemctl is-active mongod genieacs-cwmp genieacs-nbi wg-quick@wgacs
  sudo wg show wgacs latest-handshakes          # harus < 2-3 menit yang lalu
  curl -s 'http://127.0.0.1:7557/devices/?projection=_id,_lastInform' | python3 -m json.tool | grep -c _id
  curl -s http://127.0.0.1:7557/faults/ | python3 -m json.tool | grep -c '"_id"'   # idealnya 0
  ```

- Port 7547/7557/3000 hanya terbuka lewat WireGuard (diatur `ufw` oleh skrip). Jangan membukanya ke internet.
- Backup database GenieACS mingguan di VPS, misalnya lewat cron:
  `0 3 * * 0 mongodump --archive=/root/genieacs-$(date +\%F).gz --gzip`
- Perbarui VPS berkala: `sudo apt update && sudo apt upgrade`.

## Akses MikroTik dari HP owner (WireGuard)
Winbox/WebFig MikroTik bisa dibuka dari HP di mana saja (data seluler pun bisa), tanpa membuka port
MikroTik ke internet, dan tetap jalan walaupun WAN kantor CGNAT (Starlink). HP tersambung ke VPS
lewat antarmuka WireGuard sendiri (`wghp`, 10.98.0.0/24). VPS meneruskan ke MikroTik lewat
terowongan INDESO (`wgacs`) atas nama VPS (10.99.0.1). Terowongan INDESO tidak diubah.

```
cd /opt/indeso/deploy/genieacs
sudo bash tambah-hp.sh                 # kode QR untuk "hp-owner" (jalankan lagi untuk menampilkan ulang)
sudo bash tambah-hp.sh hp-teknisi      # perangkat lain
sudo bash tambah-hp.sh hapus hp-lama   # cabut akses (HP hilang)
sudo bash tambah-hp.sh berkas          # salin konfigurasi ke folder home untuk diimpor sebagai berkas
```
Kalau skrip dijalankan dari HP yang sama lewat Termux, kode QR di layar tidak bisa dipindai, dan
tampilannya sering pecah. Pakai opsi `berkas`, ambil berkasnya dengan `scp` ke folder Download HP,
lalu di aplikasi WireGuard pilih **+ → Impor dari berkas**. Hapus salinan berkasnya setelah diimpor.
Di HP: pasang aplikasi **WireGuard**, ketuk **+**, pilih **Pindai dari kode QR**, lalu nyalakan tombolnya.
Setelah itu buka Winbox ke `10.99.0.2` atau WebFig ke `http://10.99.0.2/`.

Kalau ada kendala:
- **HP tidak tersambung**: izinkan port UDP yang tertulis di akhir skrip pada firewall penyedia VPS.
- **Winbox/WebFig ditolak**: di MikroTik, tambahkan `10.99.0.0/24` pada kolom address layanan `winbox` dan `www`
  (`/ip service print`).
