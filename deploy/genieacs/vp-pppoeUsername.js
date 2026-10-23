// Virtual parameter: username PPPoE ONU, dipakai INDESO untuk mencocokkan ONU dengan pelanggan apa pun
// nomor index WAN-nya. Bila ONU punya beberapa WAN PPPoE (mis. sisa WAN lama yang tidak tersambung),
// yang dipakai adalah WAN yang sedang tersambung (punya IP); bila tidak ada, username pertama.
const umur = {value: Date.now() - 24 * 3600 * 1000};
const user = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Username", umur);
const ip = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.ExternalIPAddress", umur);
const ipPer = {};
for (const x of ip) {
  if (x.value && x.value[0]) ipPer[x.path.replace(/\.ExternalIPAddress$/, "")] = x.value[0];
}
let pertama = "", tersambung = "";
for (const p of user) {
  if (!p.value || !p.value[0]) continue;
  if (!pertama) pertama = p.value[0];
  const a = ipPer[p.path.replace(/\.Username$/, "")];
  if (!tersambung && a && a !== "0.0.0.0") tersambung = p.value[0];
}
return {writable: false, value: [tersambung || pertama, "xsd:string"]};
