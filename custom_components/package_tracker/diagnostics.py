"""Diagnostics: counts and scan state only, never mail contents or tracking numbers."""

from collections import Counter
from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_ADDRESSES
from .coordinator import PackageTrackerConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PackageTrackerConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    reg = coordinator.registry
    snap = coordinator.data
    options = dict(entry.options)
    if CONF_ADDRESSES in options:  # never share the addresses themselves
        options[CONF_ADDRESSES] = f"{len(options[CONF_ADDRESSES] or [])} address(es)"

    by_status: dict[str, int] = {}
    counts: dict[str, Any] | None = None
    if snap:
        by_status = dict(Counter(f"{p.carrier}/{p.status}" for p in snap.packages))
        counts = {
            "arriving_today": len(snap.arriving_today),
            "out_for_delivery": len(snap.out_for_delivery),
            "on_the_way": len(snap.on_the_way),
            "ordered": len(snap.ordered),
            "delivered_today": len(snap.delivered_today),
            "mail_pieces_today": snap.mail_pieces_today,
        }

    return {
        "options": options,
        "hidden_by_address_filter": snap.hidden if snap else None,
        "packages_with_known_address": sum(1 for p in reg.packages.values() if p.ship_to),
        "last_check": coordinator.last_check.isoformat() if coordinator.last_check else None,
        "last_error": coordinator.last_error,
        "last_stats": coordinator.last_stats,
        "cursors": reg.cursors,
        "seen_messages": len(reg.seen),
        "tombstones": len(reg.tombstones),
        "packages_by_carrier_and_status": by_status,
        "counts": counts,
    }
