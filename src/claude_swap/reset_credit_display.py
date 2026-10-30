"""Read-only presentation of the earliest available Codex reset expiry."""
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ResetCreditDisplay:
    text: str
    severity: str = "normal"

    @property
    def urgent(self) -> bool:
        return self.severity != "normal"


def reset_credit_display(usage: dict | None, now: float) -> ResetCreditDisplay | None:
    if not isinstance(usage, dict):
        return None
    credits = usage.get("reset_credits")
    if not isinstance(credits, dict):
        return None
    count = credits.get("available")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return None
    text = f"{count} banked"
    if count == 0:
        return ResetCreditDisplay(text)
    try:
        expiry = datetime.fromisoformat(credits["expires_at"].replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            raise ValueError("Missing expiry timezone")
        remaining = expiry.timestamp() - now
        local = expiry.astimezone().strftime("%Y-%m-%d %H:%M %Z (%z)")
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError, OSError):
        return ResetCreditDisplay(f"{text} · expiry unknown")
    if remaining <= 0:
        return ResetCreditDisplay(
            f"{text} · earliest expiry passed ({local}); refresh usage to confirm", "expired"
        )
    minutes = int(remaining // 60)
    days, hours = divmod(minutes // 60, 24)
    if remaining < 60:
        countdown = "less than 1m"
    elif days:
        countdown = f"{days}d {hours}h"
    else:
        hours, minutes = divmod(minutes, 60)
        countdown = f"{hours}h {minutes}m" if hours else f"{minutes}m"
    severity = "critical" if remaining <= 3600 else "warning" if remaining <= 86400 else "normal"
    return ResetCreditDisplay(f"{text} · earliest expires in {countdown} ({local})", severity)
