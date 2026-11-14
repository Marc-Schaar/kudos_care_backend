"""
Verschlüsselung für Radcomputer-Daten, die nicht im Klartext in der Datenbank liegen sollen
(gespeicherte Orte wie „Zuhause" verraten die Wohnadresse).

Fernet = AES-128-CBC + HMAC-SHA256: ein manipulierter oder mit falschem Schlüssel
gelesener Wert wirft `InvalidToken`, statt still Unsinn zu liefern.

Schlüssel aus `settings.FIELD_ENCRYPTION_KEYS` (kommagetrennt). Der erste verschlüsselt,
alle entschlüsseln — so lässt sich rotieren: neuen Schlüssel vorne anfügen, alten dahinter
stehen lassen, bis alle Einträge neu geschrieben sind.
"""

import json
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings

__all__ = ["InvalidToken", "decrypt_json", "encrypt_json"]


@lru_cache(maxsize=1)
def _fernet() -> MultiFernet:
    return MultiFernet([Fernet(key) for key in settings.FIELD_ENCRYPTION_KEYS])


def encrypt_json(value: Any) -> str:
    return _fernet().encrypt(json.dumps(value).encode()).decode()


def decrypt_json(token: str) -> Any:
    return json.loads(_fernet().decrypt(token.encode()))
