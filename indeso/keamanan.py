"""Hash password (PBKDF2-SHA256) dan token sesi.

Dipindah apa adanya dari main.py (Tahap 4, docs/rencana-perbaikan.md); main.py mengimpor kembali
semua nama di sini.
"""
import hashlib
import secrets
from typing import Optional


_PBKDF2_ITERASI = 600_000   # rekomendasi OWASP untuk PBKDF2-SHA256; hash lama di-upgrade saat login


def _hash_password(pw: str) -> str:
    garam = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(garam), _PBKDF2_ITERASI).hex()
    return f"pbkdf2_sha256${_PBKDF2_ITERASI}${garam}${h}"


def _cek_password(pw: str, tersimpan: Optional[str]) -> bool:
    try:
        _alg, iterasi, garam, h = (tersimpan or "").split("$")
        hitung = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(garam), int(iterasi)).hex()
        return secrets.compare_digest(hitung, h)
    except Exception:
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
