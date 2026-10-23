// Dijalankan saat ONU melapor "8 DIAGNOSTICS COMPLETE" (tes ping atau tes kecepatan dari INDESO selesai):
// ambil hasil diagnostik di sesi yang sama supaya langsung terbaca di dashboard tanpa menunggu
// INDESO memanggil ONU lagi (Huawei HG8145V5: ±47 detik lebih lambat tanpa ini).
const sekarang = Date.now();
declare("InternetGatewayDevice.IPPingDiagnostics.*", {value: sekarang});
for (const k of ["DiagnosticsState", "BOMTime", "EOMTime", "ROMTime", "TestBytesReceived", "TotalBytesReceived"])
  declare("InternetGatewayDevice.DownloadDiagnostics." + k, {value: sekarang});
