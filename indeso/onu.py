"""Membaca data ONU dari OLT/ACS: nomor seri (SN) GPON, merek, MAC, alamat PON, dan query GenieACS.

Dipindah apa adanya dari main.py (Tahap 4, docs/rencana-perbaikan.md); main.py mengimpor kembali
semua nama di sini.
"""
import re
from typing import Optional


def _parse_pon_onu(location_raw: str) -> Optional[tuple[str, int]]:
    """'\"0/0/1 onu 3\"' -> ("0/0/1", 3)"""
    text = location_raw.strip('"')
    m = re.search(r"(\d+/\d+/\d+)\s+onu\s+(\d+)", text, re.IGNORECASE)
    return (m.group(1), int(m.group(2))) if m else None


def _tanpa_kutip(teks: Optional[str]) -> Optional[str]:
    """Nilai teks SNMP datang berkutip ('"F670LV9.0"'); buang kutipnya supaya rapi di pesan & dashboard."""
    if teks is None:
        return None
    t = str(teks).strip().strip('"').strip()
    return t or None


def _to_scaled_float(raw_value: Optional[str], scale: float = 0.01) -> Optional[float]:
    if raw_value is None:
        return None
    try:
        return round(int(raw_value) * scale, 2)
    except (ValueError, TypeError):
        return None


def _decode_serial(hex_raw: Optional[str]) -> Optional[str]:
    """SN OLT C-Data: 4 byte pertama = vendor ID ASCII, sisanya hex uppercase."""
    if not hex_raw:
        return None
    try:
        parts = hex_raw.strip().split()
        if len(parts) < 5:
            return None
        vendor = "".join(chr(int(b, 16)) for b in parts[:4])
        return vendor + "".join(b.upper() for b in parts[4:])
    except Exception:
        return None


def _mac_int(mac: str) -> Optional[int]:
    try:
        return int(mac.replace(":", "").replace("-", ""), 16)
    except (ValueError, AttributeError):
        return None


_IGD = "InternetGatewayDevice"


def _zte(doc: dict) -> bool:
    return "zte" in str((doc.get("_deviceId") or {}).get("_Manufacturer") or "").lower()


def _query_pppoe(username: str) -> dict:
    jalur = [f"{_IGD}.WANDevice.1.WANConnectionDevice.{w}.WANPPPConnection.{p}.Username._value"
             for w in range(1, 11) for p in range(1, 6)]
    return {"$or": [{"VirtualParameters.pppoeUsername._value": username}] + [{j: username} for j in jalur]}


_POLA_SN_GPON = re.compile(r"^[A-Z]{4}[0-9A-F]{8}$")


def _sn_gpon(sn) -> str:
    """Samakan bentuk nomor seri: sebagian ONU (mis. Huawei) melapor ke ACS dengan 4 huruf vendor dalam heksadesimal,
    '4857544398B2DFBA' = 'HWTC' + '98B2DFBA' = SN di OLT 'HWTC98B2DFBA'."""
    s = str(sn or "").strip().upper()
    if re.fullmatch(r"[0-9A-F]{16}", s):
        try:
            vendor = bytes.fromhex(s[:8]).decode("ascii")
        except (ValueError, UnicodeDecodeError):
            return s
        if re.fullmatch(r"[A-Z]{4}", vendor):
            return vendor + s[8:]
    return s


def _query_sn(sn: str) -> dict:
    """Query GenieACS untuk SN GPON dalam dua bentuk: 'HWTC98B2DFBA' dan '4857544398B2DFBA'."""
    s = _sn_gpon(sn)
    bentuk = [s] + ([s[:4].encode().hex().upper() + s[4:]] if _POLA_SN_GPON.match(s) else [])
    if str(sn) not in bentuk:
        bentuk.append(str(sn))
    return {"$or": [{"_deviceId._SerialNumber": b} for b in bentuk]}


_MEREK_SN = {"ZTEG": "zte", "FHTT": "fiberhome", "HWTC": "huawei", "ALCL": "nokia", "GPON": None}


def _merek(teks: str) -> Optional[str]:
    t = (teks or "").lower()
    for kata, m in (("zte", "zte"), ("fiberhome", "fiberhome"), ("fhtt", "fiberhome"), ("huawei", "huawei"),
                    ("hwtc", "huawei"), ("nokia", "nokia"), ("alcl", "nokia")):
        if kata in t:
            return m
    return None


def _onu_lama(doc: dict, sn_olt: Optional[str]) -> bool:
    """True bila dokumen ACS jelas milik ONU lain dari yang sekarang terpasang menurut OLT
    (ONU pelanggan sudah diganti, ONU baru belum lapor ke ACS)."""
    if not sn_olt:
        return False
    di = doc.get("_deviceId") or {}
    sn_acs = _sn_gpon(di.get("_SerialNumber"))
    sn_olt = _sn_gpon(sn_olt)
    if sn_acs == sn_olt:
        return False
    # Nomor seri ACS berformat SN GPON tapi berbeda → ONU lain. Format lain (sebagian ZTE melapor
    # dengan nomor seri versi TR-069) hanya dianggap lain bila mereknya jelas berbeda.
    if _POLA_SN_GPON.match(sn_acs) and _POLA_SN_GPON.match(sn_olt):
        return True
    m_olt = _MEREK_SN.get(sn_olt[:4]) or _merek(sn_olt[:4])
    m_acs = _merek(f"{di.get('_Manufacturer') or ''} {sn_acs[:4]}")
    return bool(m_olt and m_acs and m_olt != m_acs)


def _model_onu(doc: dict) -> str:
    di = doc.get("_deviceId") or {}
    return f"{di.get('_Manufacturer') or ''} {di.get('_ProductClass') or ''}".strip() or "?"
