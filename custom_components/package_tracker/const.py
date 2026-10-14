"""Constants for the Package tracker integration."""

DOMAIN = "package_tracker"

CONF_SCAN_MINUTES = "scan_minutes"
CONF_LOOKBACK_DAYS = "lookback_days"
CONF_KEEP_DELIVERED_DAYS = "keep_delivered_days"
CONF_ADDRESSES = "addresses"  # only show packages going to these; empty = show everything
CONF_SHOW_UNKNOWN = "show_unknown_address"
CONF_INFORMED_DELIVERY = "informed_delivery_is_mine"

DEFAULT_SCAN_MINUTES = 10
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_KEEP_DELIVERED_DAYS = 2
MAX_ADDRESSES = 10

POLL_BUDGET_SECONDS = 240
BACKFILL_DAYS = 30  # one-off re-read after a storage upgrade; Gmail keeps Trash for 30 days
BACKFILL_ATTEMPTS = 3
BACKFILL_PASSES = 4  # enough MAX_PER_POLL-sized passes to cover MAX_FIRST_SCAN
STORAGE_VERSION = 1
STORAGE_MINOR_VERSION = 3  # 2: delivery addresses; 3: newest email id
