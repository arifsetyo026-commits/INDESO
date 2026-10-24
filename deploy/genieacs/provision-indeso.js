// Provision bawaan INDESO: dijalankan setiap kali ONU lapor (inform) ke GenieACS.
// Menjaga ONU lapor tiap 5 menit dan menyegarkan data yang dipakai dashboard INDESO.
const sekarang = Date.now();
const jam = 3600 * 1000;

// Kredensial connection request (ACS "mengetuk" ONU agar perintah langsung jalan).
// Dikirim lewat args preset (tidak tersimpan di repo). Password tidak bisa dibaca balik dari ONU,
// jadi disegarkan harian: bila beda, GenieACS memasangnya lagi dan menyimpan nilainya.
if (args && args[0] && args[1]) {
  declare("InternetGatewayDevice.ManagementServer.ConnectionRequestUsername", {value: sekarang - 24 * jam}, {value: args[0]});
  declare("InternetGatewayDevice.ManagementServer.ConnectionRequestPassword", {value: sekarang - 24 * jam}, {value: args[1]});
}
declare("InternetGatewayDevice.ManagementServer.PeriodicInformEnable", {value: 1}, {value: true});
declare("InternetGatewayDevice.ManagementServer.PeriodicInformInterval", {value: 1}, {value: 300});

declare("InternetGatewayDevice.DeviceInfo.SoftwareVersion", {value: sekarang - 24 * jam});
declare("InternetGatewayDevice.DeviceInfo.UpTime", {value: sekarang - jam});
declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.SSID", {path: sekarang - 24 * jam, value: sekarang - jam});
declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.Enable", {value: sekarang - jam});
declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.Channel", {value: sekarang - jam});
declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.TotalAssociations", {value: sekarang - jam});
declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.KeyPassphrase", {path: sekarang - 24 * jam});
declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.PreSharedKey.*.KeyPassphrase", {path: sekarang - 24 * jam});
declare("InternetGatewayDevice.LANDevice.1.Hosts.Host.*.*", {path: sekarang - jam, value: sekarang - jam});
declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.AssociatedDevice.*.*", {path: sekarang - jam, value: sekarang - jam});
declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Username", {path: sekarang - 24 * jam, value: sekarang - 24 * jam});
declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.ExternalIPAddress", {path: sekarang - 24 * jam, value: sekarang - jam});
declare("VirtualParameters.pppoeUsername", {value: sekarang - 24 * jam});

// ZTE: WAN PPPoE yang dibuat OLT (`ont ipconfig`, nama "omci_…") ikut membawa layanan VOIP. Dengan VOIP,
// ZTE menolak ping dari luar (MikroTik tidak bisa ping ONU). INDESO tidak memakai VOIP, jadi samakan dengan
// WAN buatan web: INTERNET_TR069. Diperiksa tiap jam; OLT bisa mengembalikannya setelah ONU restart.
// FiberHome: lebar kanal WiFi (kode angka X_FH_ChannelWidth) untuk pilihan di Diagnosa.
if (/fiberhome/i.test(String((declare("DeviceID.Manufacturer", {value: 1}).value || [""])[0])))
  declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.X_FH_ChannelWidth", {path: sekarang - 24 * jam, value: sekarang - 24 * jam});
if (/zte/i.test(String((declare("DeviceID.Manufacturer", {value: 1}).value || [""])[0]))) {
  // Lebar kanal WiFi (pilihan di Diagnosa → Ganti nama / password).
  declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.X_ZTE-COM_OperatingChannelBandwidth", {path: sekarang - 24 * jam, value: sekarang - 24 * jam});
  declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.*.BandWidth", {path: sekarang - 24 * jam, value: sekarang - 24 * jam});
  const layanan = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.X_ZTE-COM_ServiceList", {path: sekarang - 24 * jam, value: sekarang - jam});
  const namaWan = {};
  for (const n of declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Name", {path: sekarang - 24 * jam, value: sekarang - 24 * jam})) {
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
