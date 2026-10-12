// Dijalankan saat ONU melapor "8 DIAGNOSTICS COMPLETE" (mis. tes ping dari INDESO selesai):
// ambil hasil diagnostik supaya langsung terbaca di dashboard tanpa menunggu.
const sekarang = Date.now();
declare("InternetGatewayDevice.IPPingDiagnostics.*", {value: sekarang});
