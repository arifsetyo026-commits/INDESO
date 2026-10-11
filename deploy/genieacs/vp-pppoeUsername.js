// Virtual parameter: username PPPoE pertama yang ditemukan di ONU.
// Dipakai INDESO untuk mencocokkan ONU dengan pelanggan, apa pun nomor index WAN-nya.
let hasil = "";
const daftar = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Username",
                       {value: Date.now() - 24 * 3600 * 1000});
for (const p of daftar) {
  if (p.value && p.value[0]) { hasil = p.value[0]; break; }
}
return {writable: false, value: [hasil, "xsd:string"]};
