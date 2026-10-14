"""Live data for the bundled Packages card: subscribe to the list, tick a package off."""

import hashlib
from typing import Any

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.util import dt as dt_util
import probatio

from .const import DOMAIN
from .coordinator import PackageTrackerCoordinator
from .links import email_url, package_url
from .registry import PROBABLY_DELIVERED, PackageView

SIGNAL_CARD = f"{DOMAIN}_card_update"
NOT_LOADED: dict[str, Any] = {"loaded": False, "packages": []}
GROUPS = ("arriving_today", "on_the_way", "delivered_today", "ordered")


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_set_received)


def row_id(entry_id: str, key: str) -> str:
    """The card's opaque id for a package, unique per account (two mailboxes can share one)."""
    return hashlib.sha1(f"{entry_id}|{key}".encode()).hexdigest()[:16]


def _entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Accounts shown on the card: loaded or still setting up, not ones being unloaded."""
    states = (ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS)
    return [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state in states and getattr(e, "runtime_data", None) is not None
    ]


def _visible(
    coordinator: PackageTrackerCoordinator,
) -> tuple[dict[str, Any], list[tuple[PackageView, list[str]]]]:
    """The card's header data for one account, and its rows with the groups each belongs to."""
    snap = coordinator.snapshot(dt_util.now())
    groups = {
        "arriving_today": snap.arriving_today,
        "on_the_way": snap.on_the_way,
        "delivered_today": snap.delivered_today + snap.ticked_today,
        "ordered": snap.ordered,
    }
    members = {g: {v.key for v in views} for g, views in groups.items()}
    rows = []
    for v in snap.packages:
        in_groups = [g for g in GROUPS if v.key in members[g]]
        # From "probably delivered" up, a package only shows if it was delivered (or ticked) today.
        if v.status_code >= PROBABLY_DELIVERED and "delivered_today" not in in_groups:
            continue  # delivered earlier, or probably delivered: off the card
        rows.append((v, in_groups))
    head = {
        "as_of": snap.as_of.isoformat(),
        "counts": {g: len(members[g]) for g in GROUPS},
        "mail_today": snap.mail_pieces_today,
        "hidden": snap.hidden,
        "last_error": snap.last_error,
    }
    if not coordinator.last_update_success:  # e.g. Gmail rejected the app password
        head["last_error"] = str(coordinator.last_exception or "Gmail check failed")
    return head, rows


def card_payload(coordinator: PackageTrackerCoordinator) -> dict[str, Any]:
    """Everything the card shows for one account."""
    entry = coordinator.config_entry
    account = entry.data.get(CONF_EMAIL)
    head, rows = _visible(coordinator)
    out = []
    for v, in_groups in rows:
        pkg = coordinator.registry.packages.get(v.key)
        url = mail = None
        if pkg is not None:
            url = package_url(
                pkg.carrier, pkg.tracking, pkg.order_id, pkg.shipment_ids, pkg.msgid, account
            )
            mail = email_url(pkg.msgid, account)
        done = v.status_code >= PROBABLY_DELIVERED
        out.append(
            {
                "id": row_id(entry.entry_id, v.key),
                "item": v.item,
                "carrier": v.carrier,
                "status": v.status,
                "expected": v.expected,
                "expected_end": v.expected_end,
                "window": v.window,
                "late": v.late,
                "exception": v.exception,
                "delivered_at": v.delivered_at,
                "url": url,
                "email_url": mail if mail != url else None,
                "groups": in_groups,
                "done": done,
                "ticked": v.ticked,
                "can_tick": not done or v.ticked,  # a carrier's "delivered" can't be unticked
            }
        )
    return {"loaded": True, **head, "packages": out}


@callback
def current_payload(hass: HomeAssistant) -> dict[str, Any]:
    """The card's data across every Gmail account set up (usually just one)."""
    parts = [card_payload(e.runtime_data) for e in _entries(hass)]
    if not parts:
        return NOT_LOADED
    if len(parts) == 1:
        return parts[0]
    mail = [p["mail_today"] for p in parts if p["mail_today"] is not None]
    rows = [r for p in parts for r in p["packages"]]
    rows.sort(key=lambda r: (r["done"], r["expected"] or "9999", r["item"]))
    return {
        "loaded": True,
        "as_of": parts[0]["as_of"],
        "counts": {g: sum(p["counts"][g] for p in parts) for g in GROUPS},
        "mail_today": sum(mail) if mail else None,
        "hidden": sum(p["hidden"] for p in parts),
        "last_error": "; ".join(p["last_error"] for p in parts if p["last_error"]) or None,
        "packages": rows,
    }


@callback
def async_push(hass: HomeAssistant) -> None:
    async_dispatcher_send(hass, SIGNAL_CARD, current_payload(hass))


@websocket_api.websocket_command({probatio.Required("type"): f"{DOMAIN}/subscribe"})
@callback
def ws_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Send the list now and after every change, including across reloads."""

    @callback
    def forward(payload: dict[str, Any]) -> None:
        connection.send_event(msg["id"], payload)

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, SIGNAL_CARD, forward)
    connection.send_result(msg["id"])
    forward(current_payload(hass))


@websocket_api.websocket_command(
    {
        probatio.Required("type"): f"{DOMAIN}/set_received",
        probatio.Required("package"): cv.string,
        probatio.Required("received"): cv.boolean,
    }
)
@callback
def ws_set_received(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Tick a package from the card; needs the same permission as its Packages to-do list."""
    for entry in _entries(hass):
        coordinator: PackageTrackerCoordinator = entry.runtime_data
        _head, rows = _visible(coordinator)
        key = next(
            (v.key for v, _groups in rows if row_id(entry.entry_id, v.key) == msg["package"]),
            None,
        )
        if key is None:
            continue
        todo = er.async_get(hass).async_get_entity_id("todo", DOMAIN, f"{entry.entry_id}_packages")
        if todo:
            allowed = connection.user.permissions.check_entity(todo, POLICY_CONTROL)
        else:
            allowed = connection.user.is_admin
        if not allowed:
            raise Unauthorized(
                context=connection.context(msg), entity_id=todo, permission=POLICY_CONTROL
            )
        if not coordinator.async_set_received(key, msg["received"]):
            connection.send_error(
                msg["id"], "not_supported", "The carrier already marked this package delivered"
            )
            return
        connection.send_result(msg["id"])
        return
    connection.send_error(
        msg["id"], websocket_api.ERR_NOT_FOUND, "That package isn't on the list any more"
    )
