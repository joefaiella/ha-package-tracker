"""Polls Gmail, feeds the package registry, keeps it on disk."""

import asyncio
from collections.abc import Mapping
from dataclasses import replace
import datetime as dt
from email import message_from_bytes
from email.policy import default as default_policy
import logging
from typing import Any, override

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.ssl import client_context

from . import imap_client
from .address import AddressFilter
from .const import (
    BACKFILL_ATTEMPTS,
    BACKFILL_DAYS,
    BACKFILL_PASSES,
    CONF_ADDRESSES,
    CONF_INFORMED_DELIVERY,
    CONF_KEEP_DELIVERED_DAYS,
    CONF_LOOKBACK_DAYS,
    CONF_SCAN_MINUTES,
    CONF_SHOW_UNKNOWN,
    DEFAULT_KEEP_DELIVERED_DAYS,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_SCAN_MINUTES,
    DOMAIN,
    POLL_BUDGET_SECONDS,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
)
from .kb import Digest, Event, is_candidate, parse_message
from .registry import Registry, TrackerSnapshot

_LOGGER = logging.getLogger(__name__)

type PackageTrackerConfigEntry = ConfigEntry[PackageTrackerCoordinator]


class TrackerStore(Store[dict[str, Any]]):
    """The saved registry, upgraded in place when its layout changes."""

    @override
    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        if old_minor_version < 2:
            # Storage 1.2 added delivery addresses: re-read recent mail once to fill them in.
            # Packages from before the upgrade stay visible until then.
            packages = [p for p in old_data.get("packages") or [] if isinstance(p, dict)]
            old_data = {
                **old_data,
                "backfill": True,
                "packages": [{**p, "legacy": True} for p in packages],
            }
        if old_minor_version < 3:
            # Storage 1.3 added each package's newest email id (for the card's link); same re-read.
            old_data = {**old_data, "backfill": True}
        return old_data


def address_filter(options: Mapping[str, Any]) -> AddressFilter:
    return AddressFilter.build(
        options.get(CONF_ADDRESSES) or [],
        show_unknown=bool(options.get(CONF_SHOW_UNKNOWN, True)),
        informed_delivery_is_mine=bool(options.get(CONF_INFORMED_DELIVERY, True)),
    )


def parse_batch(
    mails: list[imap_client.RawMail], tz: dt.tzinfo
) -> list[tuple[str, Event | Digest | None]]:
    """CPU-bound email parsing; runs in the executor."""
    out: list[tuple[str, Event | Digest | None]] = []
    for mail in mails:
        try:
            msg = message_from_bytes(mail.raw, policy=default_policy)
            result = parse_message(msg, tz, mail.internaldate)  # type: ignore[arg-type]
            out.append((mail.msgid, result))
        except Exception:  # one odd email must never stop the scan
            _LOGGER.exception("Could not parse message %s", mail.msgid)
            out.append((mail.msgid, None))
    return out


class PackageTrackerCoordinator(DataUpdateCoordinator[TrackerSnapshot]):
    """One Gmail session per poll; packages survive Gmail outages and deleted emails."""

    config_entry: PackageTrackerConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PackageTrackerConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=dt.timedelta(
                minutes=int(entry.options.get(CONF_SCAN_MINUTES, DEFAULT_SCAN_MINUTES))
            ),
            always_update=False,
        )
        self.store: Store[dict[str, Any]] = TrackerStore(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.{entry.entry_id}",
            private=True,
            minor_version=STORAGE_MINOR_VERSION,
        )
        self.address_filter = address_filter(entry.options)
        self.registry = Registry()
        self._loaded = False
        self.last_check: dt.datetime | None = None
        self.last_error: str | None = None
        self.last_stats: dict[str, int] = {}
        self._backfill_task: asyncio.Task[None] | None = None
        self._backfill_failures = 0

    @override
    async def _async_setup(self) -> None:
        self.registry = Registry.from_dict(await self.store.async_load())
        self._loaded = True

    @override
    async def _async_update_data(self) -> TrackerSnapshot:
        entry = self.config_entry
        lookback = int(entry.options.get(CONF_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS))
        keep = int(entry.options.get(CONF_KEEP_DELIVERED_DAYS, DEFAULT_KEEP_DELIVERED_DAYS))
        now = dt_util.now()
        changed = False
        try:
            async with asyncio.timeout(POLL_BUDGET_SECONDS):
                scan = await imap_client.scan(
                    entry.data[CONF_EMAIL],
                    entry.data[CONF_PASSWORD],
                    client_context(),
                    cursors=self.registry.cursors,
                    seen=set(self.registry.seen),
                    lookback_days=lookback,
                    today=now.date(),
                    is_candidate=is_candidate,
                )
        except imap_client.GmailAuthError as err:
            raise ConfigEntryAuthFailed(f"Gmail rejected the app password: {err}") from err
        except (imap_client.GmailError, TimeoutError, OSError) as err:
            # Keep showing what we already know; try again next poll.
            self.last_error = f"{type(err).__name__}: {err}" if str(err) else type(err).__name__
            _LOGGER.warning("Gmail check failed, keeping known packages: %s", self.last_error)
        else:
            results = await self.hass.async_add_executor_job(
                parse_batch, scan.mails, dt_util.get_default_time_zone()
            )
            changed |= self.registry.apply(results, now)
            if scan.cursors != self.registry.cursors:
                self.registry.cursors = scan.cursors
                changed = True
            self.last_check, self.last_error = now, None
            if self.registry.last_ok is None or self.registry.last_ok.date() != now.date():
                changed = True  # the date is all that matters on disk; spares SD card writes
            self.registry.last_ok = now
            self.last_stats = {
                "headers_checked": scan.headers_checked,
                "emails_read": len(scan.mails),
                "shipping_emails": sum(1 for _, r in results if r is not None),
            }
            if scan.mails:
                _LOGGER.debug("Gmail check: %s", self.last_stats)
            # Retire packages only after actually reading mail; an outage is not "no news".
            changed |= self.registry.expire(now, keep_delivered_days=keep)
            backfill_idle = self._backfill_task is None or self._backfill_task.done()
            if self.registry.backfill_pending and backfill_idle:
                self._backfill_task = entry.async_create_background_task(
                    self.hass, self._async_backfill(), f"{DOMAIN} address backfill"
                )
        if changed:
            self._schedule_save()
        return self.snapshot(now)

    async def _async_backfill(self) -> None:
        """Re-read recent mail once after a storage upgrade to fill in addresses and email ids."""
        entry = self.config_entry
        now = dt_util.now()
        lookback = max(
            BACKFILL_DAYS, int(entry.options.get(CONF_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS))
        )
        results: list[tuple[str, Event | Digest | None]] = []
        cursors: dict[str, dict[str, int]] = {}
        read: set[str] = set()
        try:
            async with asyncio.timeout(POLL_BUDGET_SECONDS):
                for _ in range(BACKFILL_PASSES):  # a busy mailbox takes several passes
                    scan = await imap_client.scan(
                        entry.data[CONF_EMAIL],
                        entry.data[CONF_PASSWORD],
                        client_context(),
                        cursors=cursors,
                        seen=read,
                        lookback_days=lookback,
                        today=now.date(),
                        is_candidate=is_candidate,
                    )
                    results += await self.hass.async_add_executor_job(
                        parse_batch, scan.mails, dt_util.get_default_time_zone()
                    )
                    read |= {m.msgid for m in scan.mails}
                    if not scan.truncated or scan.cursors == cursors:
                        break
                    cursors = scan.cursors
            updated = self.registry.backfill_addresses(results)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # never crash, and never retry forever
            self._backfill_failures += 1
            expected = (imap_client.GmailError, imap_client.GmailAuthError, TimeoutError, OSError)
            if isinstance(err, expected):
                _LOGGER.debug(
                    "Address look-up failed (attempt %s): %s", self._backfill_failures, err
                )
            else:
                _LOGGER.exception("Address look-up failed (attempt %s)", self._backfill_failures)
            if self._backfill_failures >= BACKFILL_ATTEMPTS:
                self.registry.backfill_pending = False  # give up; new mail still brings addresses
                self._schedule_save()
            return
        self.registry.backfill_pending = False
        _LOGGER.debug("Address look-up: %s package(s) updated", updated)
        self._schedule_save()
        self.async_set_updated_data(self.snapshot(dt_util.now()))

    def _schedule_save(self) -> None:
        # Before the first load the registry is empty; saving it would wipe the stored packages.
        if self._loaded:
            self.store.async_delay_save(self.registry.to_dict, 10)

    def snapshot(self, now: dt.datetime) -> TrackerSnapshot:
        """The filtered package view, plus the last check time and error.

        The check time and error ride along so a failed check still updates the sensors.
        """
        snap = self.registry.snapshot(now, self.address_filter)
        return replace(snap, last_check=self.last_check, last_error=self.last_error)

    def async_set_received(self, key: str, received: bool) -> bool:
        """The user ticked a package off (or back on). False when that changes nothing."""
        now = dt_util.now()
        if not self.registry.set_received(key, received, now):
            return False
        self._schedule_save()
        self.async_set_updated_data(self.snapshot(now))
        return True

    @override
    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        if self._loaded:
            await self.store.async_save(self.registry.to_dict())
