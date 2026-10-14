"""Read-only Gmail IMAP scanner built on aioimaplib.

Mail is only ever fetched with BODY.PEEK[...] (PEEK never sets \\Seen, RFC 3501 6.4.5); nothing
is ever stored, copied, moved or expunged.

One poll: connect, LIST, then for All Mail and Trash: SELECT, UID SEARCH for mail newer than the
folder cursor (or SINCE the look-back date on a first run or after UIDVALIDITY changes), fetch
headers in batches, then fetch the bodies of shipping-looking mail whose Gmail message id
(X-GM-MSGID, the same in every folder) is new. A deleted message reappears in Trash under a new
UID, but its message id is already known, so it is never downloaded twice.
"""

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
import datetime as dt
from email.parser import BytesHeaderParser
from email.policy import default as default_policy
import logging
import re
import ssl

from aioimaplib import IMAP4, IMAP4_SSL, NONAUTH, AioImapException, Response

_LOGGER = logging.getLogger(__name__)

GMAIL_HOST = "imap.gmail.com"
GMAIL_PORT = 993
CMD_TIMEOUT = 30.0
HEADER_BATCH = 100  # keep responses well under aioimaplib's ~950-lines-per-read recursion limit
BODY_BATCH = 10
MAX_FIRST_SCAN = 4000  # newest messages considered on a first scan of a folder
MAX_PER_POLL = 1500  # header checks per folder per poll; the rest continue next poll
ROLES = (("all", "\\all"), ("trash", "\\trash"))
# Login refusals that mean "try again later" rather than "wrong password".
_TRANSIENT_LOGIN = (
    "[UNAVAILABLE]",
    "[INUSE]",
    "[LIMIT]",
    "[OVERQUOTA]",
    "TOO MANY SIMULTANEOUS",
    "EXCEEDED",
    "BANDWIDTH",
    "TRY AGAIN",
    "TEMPORAR",  # temporary, temporarily
)

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)$')
_FETCH_START = re.compile(rb"^(?:\* )?\d+ FETCH \(")
_UID = re.compile(rb"\bUID (\d+)")
_MSGID = re.compile(rb"\bX-GM-MSGID (\d+)")
_IDATE = re.compile(rb'\bINTERNALDATE "([^"]+)"')
_UIDVALIDITY = re.compile(rb"\[UIDVALIDITY (\d+)\]")
_UIDNEXT = re.compile(rb"\[UIDNEXT (\d+)\]")
_HEADER_ITEMS = "(UID X-GM-MSGID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])"
_BODY_ITEMS = "(UID X-GM-MSGID INTERNALDATE BODY.PEEK[])"


class GmailAuthError(Exception):
    """Gmail rejected the address / app password."""


class GmailError(Exception):
    """Network trouble or an unexpected IMAP answer; try again next poll."""


@dataclass(frozen=True, slots=True)
class RawMail:
    msgid: str
    folder: str
    uid: int
    internaldate: dt.datetime | None
    raw: bytes


@dataclass
class ScanResult:
    mails: list[RawMail] = field(default_factory=list)
    cursors: dict[str, dict[str, int]] = field(default_factory=dict)
    headers_checked: int = 0
    # A folder had more new mail than MAX_PER_POLL; the rest comes next scan.
    truncated: bool = False


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _status_text(resp: Response) -> str:
    return b" ".join(x for x in resp.lines if isinstance(x, bytes)).decode(errors="replace")


def parse_list(resp: Response) -> dict[str, str]:
    """Special-use flag (lower-case, e.g. "\\all") -> mailbox name ready for SELECT."""
    found: dict[str, str] = {}
    lines = resp.lines[:-1]
    for i, line in enumerate(lines):
        if not isinstance(line, bytes) or not (m := _LIST_RE.match(line)):
            continue
        name = m["name"].decode(errors="replace")
        if name.endswith("}") and i + 1 < len(lines) and isinstance(lines[i + 1], bytearray):
            name = '"' + bytes(lines[i + 1]).decode(errors="replace").replace('"', '\\"') + '"'
        elif not name.startswith('"'):
            name = f'"{name}"'
        for flag in m["flags"].decode(errors="replace").lower().split():
            found.setdefault(flag, name)
    return found


def parse_search(resp: Response) -> list[int]:
    return sorted(
        {
            int(tok)
            for line in resp.lines[:-1]
            if isinstance(line, bytes)
            for tok in line.split()
            if tok.isdigit()
        }
    )


def parse_fetch(resp: Response) -> list[tuple[bytes, bytes | None]]:
    """Group a FETCH response into (metadata, literal-or-None) per message."""
    records: list[tuple[bytes, bytes | None]] = []
    meta: bytes | None = None
    literal: bytes | None = None
    for line in resp.lines[:-1]:
        if isinstance(line, bytearray):
            if meta is not None:
                literal = bytes(line)
            continue
        if _FETCH_START.match(line):
            if meta is not None:
                records.append((meta, literal))
            meta, literal = bytes(line), None
        elif meta is not None:
            meta += b" " + line
    if meta is not None:
        records.append((meta, literal))
    return records


def parse_internaldate(value: str) -> dt.datetime | None:
    """RFC 3501 date-time "dd-Mon-yyyy hh:mm:ss +zzzz" (locale-proof)."""
    try:
        date_part, time_part, zone = value.strip().split()
        day, mon, year = date_part.split("-")
        hh, mm, ss = (int(x) for x in time_part.split(":"))
        offset = dt.timedelta(hours=int(zone[1:3]), minutes=int(zone[3:5]))
        tz = dt.timezone(offset if zone[0] == "+" else -offset)
        return dt.datetime(int(year), _MONTHS.index(mon) + 1, int(day), hh, mm, ss, tzinfo=tz)
    except (ValueError, IndexError):
        return None


def imap_date(d: dt.date) -> str:
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


# ---------------------------------------------------------------------------
# IMAP steps
# ---------------------------------------------------------------------------


def _check(resp: Response, what: str) -> Response:
    if resp.result != "OK":
        raise GmailError(f"{what}: {resp.result} {_status_text(resp)}")
    return resp


async def close_quietly(client: IMAP4) -> None:
    try:
        if client.get_state() not in ("LOGOUT", "STARTED", "CONNECTED"):
            async with asyncio.timeout(5):
                await client.logout()
    except (AioImapException, TimeoutError, OSError, asyncio.CancelledError):
        pass
    finally:
        transport = client.protocol.transport if client.protocol else None
        if transport is not None and not transport.is_closing():
            transport.close()
        task = getattr(client, "_client_task", None)
        if task is not None and not task.done():
            task.cancel()


async def connect(
    username: str,
    password: str,
    ssl_context: ssl.SSLContext | None,
    *,
    host: str = GMAIL_HOST,
    port: int = GMAIL_PORT,
    client_cls: Callable[..., IMAP4] = IMAP4_SSL,
) -> IMAP4:
    if client_cls is IMAP4_SSL:
        client = IMAP4_SSL(host, port, timeout=CMD_TIMEOUT, ssl_context=ssl_context)
    else:  # plain IMAP4 only for the local fake-server tests
        client = client_cls(host, port, timeout=CMD_TIMEOUT)
    try:
        try:
            await client.wait_hello_from_server()
        except (TimeoutError, AioImapException) as err:
            task = getattr(client, "_client_task", None)
            cause = None
            if task is not None and task.done() and not task.cancelled():
                cause = task.exception()  # e.g. the TLS or DNS error behind the timeout
            raise GmailError(f"no IMAP greeting from {host}: {cause or 'timeout'}") from err
        if client.get_state() == NONAUTH:
            resp = await client.login(username, password)
            if resp.result != "OK":
                text = _status_text(resp)
                # A wrong or revoked app password gets "NO [AUTHENTICATIONFAILED] ...". Other NOs
                # are temporary (too many connections, server busy) and mustn't ask for a new one.
                if resp.result == "NO" and not any(t in text.upper() for t in _TRANSIENT_LOGIN):
                    raise GmailAuthError(text)
                raise GmailError(f"LOGIN {resp.result}: {text}")
    except BaseException:
        await close_quietly(client)
        raise
    return client


async def special_folders(client: IMAP4) -> dict[str, str]:
    folders = parse_list(_check(await client.list('""', '"[Gmail]/%"'), "LIST"))
    if "\\all" not in folders:
        folders.update(parse_list(_check(await client.list('""', '"[GoogleMail]/%"'), "LIST")))
    if "\\all" not in folders:
        folders.update(parse_list(_check(await client.list('""', '"*"'), "LIST")))
    return folders


async def _select(client: IMAP4, wire_name: str) -> tuple[int, int | None]:
    """SELECT a folder; return (UIDVALIDITY, UIDNEXT or None)."""
    resp = _check(await client.select(wire_name), f"SELECT {wire_name}")
    uidvalidity = uidnext = None
    for line in resp.lines:
        if not isinstance(line, bytes):
            continue
        if m := _UIDVALIDITY.search(line):
            uidvalidity = int(m[1])
        if m := _UIDNEXT.search(line):
            uidnext = int(m[1])
    if uidvalidity is None:
        raise GmailError(f"SELECT {wire_name}: no UIDVALIDITY")
    return uidvalidity, uidnext


async def _uid_fetch(
    client: IMAP4, uids: list[int], items: str
) -> list[tuple[bytes, bytes | None]]:
    resp = await client.uid("fetch", ",".join(str(u) for u in uids), items)
    return parse_fetch(_check(resp, "UID FETCH"))


def _chunks(seq: list[int], size: int) -> Iterator[list[int]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _header_info(raw: bytes) -> tuple[str, str]:
    msg = BytesHeaderParser(policy=default_policy).parsebytes(raw)
    try:
        frm, subj = str(msg["From"] or ""), str(msg["Subject"] or "")
    except (ValueError, IndexError, AttributeError):
        frm, subj = "", ""
    return frm, " ".join(subj.split())


async def scan(
    username: str,
    password: str,
    ssl_context: ssl.SSLContext | None,
    *,
    cursors: dict[str, dict[str, int]],
    seen: set[str],
    lookback_days: int,
    today: dt.date,
    is_candidate: Callable[[str, str], bool],
    **connect_kw,
) -> ScanResult:
    """One poll of All Mail and Trash. The caller wraps this in asyncio.timeout()."""
    try:
        return await _scan(
            username,
            password,
            ssl_context,
            cursors=cursors,
            seen=seen,
            lookback_days=lookback_days,
            today=today,
            is_candidate=is_candidate,
            **connect_kw,
        )
    except AioImapException as err:
        # e.g. CommandTimeout when a FETCH stalls. Its str() is the whole command; use the type.
        raise GmailError(f"IMAP {type(err).__name__}") from err


async def _scan(
    username: str,
    password: str,
    ssl_context: ssl.SSLContext | None,
    *,
    cursors: dict[str, dict[str, int]],
    seen: set[str],
    lookback_days: int,
    today: dt.date,
    is_candidate: Callable[[str, str], bool],
    **connect_kw,
) -> ScanResult:
    result = ScanResult(cursors={k: dict(v) for k, v in cursors.items()})
    got: set[str] = set()
    since = imap_date(today - dt.timedelta(days=lookback_days))
    client = await connect(username, password, ssl_context, **connect_kw)
    try:
        folders = await special_folders(client)
        for role, flag in ROLES:
            if flag not in folders:
                if role == "all":
                    raise GmailError("No All Mail folder; is this a Gmail account?")
                continue
            uidvalidity, uidnext = await _select(client, folders[flag])
            cur = result.cursors.get(role) or {}
            last_uid = int(cur.get("last_uid", 0)) if cur.get("uidvalidity") == uidvalidity else 0
            if last_uid:
                # Only mail that arrived in the look-back window is worth reading. INTERNALDATE
                # survives a move to Trash, so bulk-deleting old orders downloads nothing.
                resp = await client.uid_search(
                    "UID", f"{last_uid + 1}:*", "SINCE", since, charset=None
                )
                uids = [u for u in parse_search(_check(resp, "UID SEARCH")) if u > last_uid]
            else:
                resp = await client.uid_search("SINCE", since, charset=None)
                uids = parse_search(_check(resp, "UID SEARCH"))

            # The cursor moves past everything in the folder, old mail included.
            if uidnext is not None:
                newest = max(uidnext - 1, *uids) if uids else uidnext - 1
            elif last_uid:
                resp = await client.uid_search("UID", f"{last_uid + 1}:*", charset=None)
                newer = [u for u in parse_search(_check(resp, "UID SEARCH")) if u > last_uid]
                newest = max(newer, default=last_uid)
            else:
                newest = max(uids, default=0)
            if not last_uid and len(uids) > MAX_FIRST_SCAN:
                _LOGGER.info(
                    "First scan of %s: checking the newest %d of %d messages",
                    role,
                    MAX_FIRST_SCAN,
                    len(uids),
                )
                uids = uids[-MAX_FIRST_SCAN:]
            if len(uids) > MAX_PER_POLL:
                # Oldest first: the cursor stops here and the rest wait for the next poll.
                uids = uids[:MAX_PER_POLL]
                newest = uids[-1]
                result.truncated = True

            wanted: list[int] = []
            for chunk in _chunks(uids, HEADER_BATCH):
                for meta, literal in await _uid_fetch(client, chunk, _HEADER_ITEMS):
                    uid_m, id_m = _UID.search(meta), _MSGID.search(meta)
                    if not (uid_m and id_m and literal is not None):
                        continue
                    result.headers_checked += 1
                    msgid = id_m[1].decode()
                    if msgid in seen or msgid in got:
                        continue
                    sender, subject = _header_info(literal)
                    if is_candidate(sender, subject):
                        wanted.append(int(uid_m[1]))

            for chunk in _chunks(wanted, BODY_BATCH):
                for meta, literal in await _uid_fetch(client, chunk, _BODY_ITEMS):
                    uid_m, id_m = _UID.search(meta), _MSGID.search(meta)
                    if not (uid_m and id_m and literal is not None):
                        continue  # e.g. an unsolicited "* n FETCH (FLAGS ...)"
                    msgid = id_m[1].decode()
                    if msgid in got:
                        continue
                    # Marked only once the body is in hand; mail deleted meanwhile comes from Trash.
                    got.add(msgid)
                    date_m = _IDATE.search(meta)
                    mail = RawMail(
                        msgid=msgid,
                        folder=role,
                        uid=int(uid_m[1]),
                        internaldate=parse_internaldate(date_m[1].decode()) if date_m else None,
                        raw=literal,
                    )
                    result.mails.append(mail)
            result.cursors[role] = {"uidvalidity": uidvalidity, "last_uid": max(last_uid, newest)}
    finally:
        await close_quietly(client)
    return result


async def check_login(
    username: str, password: str, ssl_context: ssl.SSLContext | None, **connect_kw
) -> None:
    """Log in, confirm it's a Gmail account (has All Mail), log out."""
    try:
        client = await connect(username, password, ssl_context, **connect_kw)
        try:
            if "\\all" not in await special_folders(client):
                raise GmailError("not_gmail")
        finally:
            await close_quietly(client)
    except AioImapException as err:
        raise GmailError(f"IMAP {type(err).__name__}") from err
