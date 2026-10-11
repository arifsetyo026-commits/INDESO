// Provision bawaan INDESO: dijalankan setiap kali ONU lapor (inform) ke GenieACS.
// Menjaga ONU lapor tiap 5 menit dan menyegarkan data yang dipakai dashboard INDESO.
const sekarang = Date.now();
const jam = 3600 * 1000;

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
declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Username", {path: sekarang - 24 * jam, value: sekarang - 24 * jam});
declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.ExternalIPAddress", {path: sekarang - 24 * jam, value: sekarang - jam});
declare("VirtualParameters.pppoeUsername", {value: sekarang - 24 * jam});
