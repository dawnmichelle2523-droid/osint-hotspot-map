"""UK/EU GDPR storage limitation and erasure for check-in events.

Art. 5(1)(e) limits how long check-in events are kept. Art. 17 is the right
to erase them immediately. Opaque callsigns are not names, but they can be
pseudonymised personal data when the client holds a separate lookup key.
The client organisation remains the Data Controller.

The check-in blueprint lives in checkin_beacon and calls this policy.
"""

from __future__ import annotations

import os

DEFAULT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 3650


def retention_days() -> int:
    """CHECKIN_RETENTION_DAYS, or 30 when unset or not a positive day count."""
    raw = (os.environ.get("CHECKIN_RETENTION_DAYS") or "").strip()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    try:
        days = int(raw)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    if days < 1 or days > MAX_RETENTION_DAYS:
        return DEFAULT_RETENTION_DAYS
    return days


def gdpr_notice(days: int | None = None) -> str:
    window = DEFAULT_RETENTION_DAYS if days is None else days
    return (
        "UK/EU GDPR Pseudonymisation Notice: GeoRiskIntel stores no names, emails, or phone numbers. "
        "An opaque callsign can still be pseudonymised personal data under UK/EU GDPR if your organisation "
        "holds a separate lookup key. You remain the Data Controller. Check-in events are deleted automatically "
        f"after {window} days (Art. 5(1)(e) storage limitation) and can be erased immediately (Art. 17)."
    )
