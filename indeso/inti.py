"""Pintu ke pembantu inti INDESO yang masih tinggal di main.py (database, cek login, aktivitas, Telegram, pengaturan).

Modul fitur di folder indeso/ memakai `inti.db()`, `inti.CONFIG[...]`, `inti._wajib_admin(request)`, dst. Nilainya diambil
dari main.py SAAT DIPAKAI (bukan saat modul dimuat), sehingga:
- tidak terjadi impor melingkar (main.py memuat modul fitur di bagian paling akhir);
- pengaturan yang diubah dari dashboard dan tiruan di tes (mis. MikroTik palsu) tetap berlaku.
"""
import sys


def __getattr__(nama: str):
    main = sys.modules.get("main") or sys.modules.get("__main__")
    if main is None or not hasattr(main, nama):
        raise AttributeError(f"indeso.inti: '{nama}' tidak ada di main.py")
    return getattr(main, nama)
