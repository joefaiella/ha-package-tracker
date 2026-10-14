"""Where clicking a package goes: the carrier's tracking page, Amazon's, or the email itself.

Pure Python. Every link comes from a fixed https template; only a tracking number, order id or
Gmail id, each checked to be plain letters, digits and dashes, is filled in.
"""

import re
from urllib.parse import quote

# Public tracking pages that work without signing in.
CARRIER_URLS = {
    "usps": "https://tools.usps.com/go/TrackConfirmAction?tLabels={n}",
    "ups": "https://www.ups.com/track?loc=en_US&tracknum={n}",
    "fedex": "https://www.fedex.com/fedextrack/?trknbr={n}",
    "dhl": "https://www.dhl.com/us-en/home/tracking.html?tracking-id={n}&submit=1",
    "ontrac": "https://www.ontrac.com/tracking/?number={n}",
    "lasership": "https://www.ontrac.com/tracking/?number={n}",  # LaserShip is OnTrac now
    "gofo": "https://www.gofo.com/us/track?searchID={n}",
}
AMAZON_BOX = (
    "https://www.amazon.com/progress-tracker/package?orderId={o}&shipmentId={s}&packageIndex=0"
)
AMAZON_ORDER = "https://www.amazon.com/your-orders/order-details?orderID={o}"
GMAIL = "https://mail.google.com/mail/u/?authuser={account}#all/{hex}"

_SAFE = re.compile(r"^[A-Za-z0-9-]{1,40}$")
_AMAZON_ORDER = re.compile(r"^\d{3}-\d{7}-\d{7}$")


def package_url(
    carrier: str | None,
    tracking: list[str],
    order_id: str | None,
    shipment_ids: list[str],
    msgid: str | None,
    account: str | None,
) -> str | None:
    """The best page for one package, or None when there's nothing to open."""
    numbers = [t for t in tracking if _SAFE.match(t)]
    if carrier in CARRIER_URLS and numbers:
        return CARRIER_URLS[carrier].format(n=numbers[0])
    if carrier == "amazon" and order_id and _AMAZON_ORDER.match(order_id):
        boxes = [s for s in shipment_ids if _SAFE.match(s)]
        if boxes:
            return AMAZON_BOX.format(o=order_id, s=boxes[0])
        return AMAZON_ORDER.format(o=order_id)
    return email_url(msgid, account)


def email_url(msgid: str | None, account: str | None) -> str | None:
    """Gmail's link to one message; X-GM-MSGID is the decimal form of the web app's hex id."""
    if not msgid or not msgid.isdigit() or not account:
        return None
    return GMAIL.format(account=quote(account, safe=""), hex=format(int(msgid), "x"))
