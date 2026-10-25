// Provision bawaan INDESO: dijalankan setiap kali ONU lapor (inform) ke GenieACS.
// Prinsipnya "seperlunya": ONU cukup lapor tiap 15 menit dan provision ini hanya menyegarkan data kecil
// sekali sehari. Data yang berubah cepat (perangkat terhubung, klien WiFi, uptime) dibaca saat Diagnosa
// dibuka (INDESO mengetuk ONU lewat connection request), bukan otomatis tiap jam. Prosesor ONU murah
// dipakai bersama oleh WiFi dan TR-069, jadi makin jarang dibaca makin baik.
const sekarang = Date.now();
const jam = 3600 * 1000;
const harian = sekarang - 24 * jam;
// ONU yang baru restart disegarkan saat itu juga (pengaturan OLT bisa kembali setelah ONU restart).
const bootTerakhir = (() => {
  const v = (declare("Events.1_BOOT", {value: 1}).value || [])[0];
  const t = v ? new Date(v).getTime() : 0;
  return isNaN(t) ? 0 : t;
})();
const harianAtauBoot = Math.max(harian, bootTerakhir);

// Kredensial connection request (ACS "mengetuk" ONU agar perintah langsung jalan).
// Dikirim lewat args preset (tidak tersimpan di repo). Password tidak bisa dibaca balik dari ONU,
// jadi disegarkan harian: bila beda, GenieACS memasangnya lagi dan menyimpan nilainya.
if (args && args[0] && args[1]) {
  declare("InternetGatewayDevice.ManagementServer.ConnectionRequestUsername", {value: harian}, {value: args[0]});
  declare("InternetGatewayDevice.ManagementServer.ConnectionRequestPassword", {value: harian}, {value: args[1]});
}
declare("InternetGatewayDevice.ManagementServer.PeriodicInformEnable", {value: 1}, {value: true});
declare("InternetGatewayDevice.ManagementServer.PeriodicInformInterval", {value: 1}, {value: 900});

declare("InternetGatewayDevice.DeviceInfo.SoftwareVersion", {value: harian});
declare("InternetGatewayDevice.DeviceInfo.UpTime", {value: harian});
declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.SSID", {path: harian, value: harian});
declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.Enable", {value: harian});
declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.Channel", {value: harian});
declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.TotalAssociations", {value: harian});
declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.KeyPassphrase", {path: harian});
declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.PreSharedKey.*.KeyPassphrase", {path: harian});
declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Username", {path: harian, value: harian});
declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.ExternalIPAddress", {path: harian, value: harian});
declare("VirtualParameters.pppoeUsername", {value: harian});

// ZTE: WAN PPPoE yang dibuat OLT (`ont ipconfig`, nama "omci_…") ikut membawa layanan VOIP. Dengan VOIP,
// ZTE menolak ping dari luar (MikroTik tidak bisa ping ONU). INDESO tidak memakai VOIP, jadi samakan dengan
// WAN buatan web: INTERNET_TR069. Diperiksa sekali sehari dan setiap ONU restart (OLT bisa mengembalikannya setelah ONU restart).
// FiberHome: lebar kanal WiFi (kode angka X_FH_ChannelWidth) untuk pilihan di Diagnosa.
if (/fiberhome/i.test(String((declare("DeviceID.Manufacturer", {value: 1}).value || [""])[0])))
  declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.X_FH_ChannelWidth", {path: harian, value: harian});
if (/zte/i.test(String((declare("DeviceID.Manufacturer", {value: 1}).value || [""])[0]))) {
  // Lebar kanal WiFi (pilihan di Diagnosa → Ganti nama / password).
  declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.X_ZTE-COM_OperatingChannelBandwidth", {path: harian, value: harian});
  declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.BandWidth", {path: harian, value: harian});
  const layanan = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.X_ZTE-COM_ServiceList", {path: harianAtauBoot, value: harianAtauBoot});
  const namaWan = {};
  for (const n of declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Name", {path: harian, value: harian})) {
    if (n.value) namaWan[n.path.replace(/\.Name$/, "")] = String(n.value[0]);
  }
  for (const l of layanan) {
    const wan = l.path.replace(/\.X_ZTE-COM_ServiceList$/, "");
    // Ejaan beda per firmware: F670L "INTERNET_TR069_VOIP", F609 V7 "INTERNET_VoIP_TR069". Buang kata VoIP saja,
    // sisanya (urutan & huruf) dibiarkan seperti tulisan firmware itu sendiri.
    const v = l.value ? String(l.value[0]) : "";
    const tanpaVoip = v.split("_").filter(k => !/^voip$/i.test(k)).join("_");
    if (/^omci_/i.test(namaWan[wan] || "") && /internet/i.test(v) && tanpaVoip !== v)
      declare(l.path, null, {value: tanpaVoip});
  }
}
