"""The Gmail client against a local fake IMAP server (no network)."""

import asyncio
import datetime as dt
from email import message_from_bytes
from email.policy import default as default_policy
import random
import re

from aioimaplib import IMAP4
import pytest

from custom_components.package_tracker import imap_client, kb

from .common import raw

pytestmark = pytest.mark.usefixtures("socket_enabled")  # the fake server listens on 127.0.0.1

FEDEX = raw("fedex_out_for_delivery.eml")
AMAZON = raw("amazon_shipped.eml")
DIGEST = raw("usps_informed_delivery.eml")
NEWSLETTER = raw("store_newsletter.eml")
ACCOUNT = "jane.doe@example.com"
TODAY = dt.date(2026, 10, 9)


def header_fields(message: bytes) -> bytes:
    """From and Subject (with folded lines), as BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)]."""
    out: list[bytes] = []
    keep = False
    for line in message.split(b"\r\n\r\n", 1)[0].split(b"\r\n"):
        if line[:1] not in (b" ", b"\t"):
            keep = line.lower().startswith((b"from:", b"subject:"))
        if keep:
            out.append(line + b"\r\n")
    return b"".join(out) + b"\r\n"


class FakeGmail(asyncio.Protocol):
    """Just enough of Gmail's IMAP server; records every command that could change mail."""

    MUTATING = (b"UID STORE", b"STORE", b"EXPUNGE", b"CLOSE", b"UID MOVE", b"UID COPY")

    def __init__(self, server: "Server") -> None:
        self.server = server
        self.buffer = b""
        self.selected: str | None = None

    def connection_made(self, transport):
        self.transport = transport
        self.write(b"* OK Gimap ready for requests from 127.0.0.1\r\n")

    def write(self, data: bytes) -> None:
        if not self.server.chunked:
            self.transport.write(data)
            return
        i = 0
        while i < len(data):  # arbitrary TCP segment boundaries
            n = random.randint(1, 900)
            self.transport.write(data[i : i + n])
            i += n

    def data_received(self, data):
        self.buffer += data
        while b"\r\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\r\n", 1)
            self.handle(line)

    def handle(self, line: bytes) -> None:
        tag, _, rest = line.partition(b" ")
        cmd = rest.upper()
        if cmd.startswith(self.MUTATING):
            self.server.violations.append(line)
            reply = b""
        elif cmd.startswith(b"UID FETCH"):
            if self.server.stall:
                return  # never answer, so the client's command timeout fires
            reply = self.fetch(line, rest)
        elif cmd.startswith(b"LOGOUT"):
            self.write(b"* BYE LOGOUT Requested\r\n" + tag + b" OK 73 good day (Success)\r\n")
            self.transport.close()
            return
        else:
            command = {
                b"CAPABILITY": self.capability,
                b"LOGIN": self.login,
                b"LIST": self.list_mailboxes,
                b"SELECT": self.select,
                b"UID SEARCH": self.search,
            }
            name = next((c for c in command if cmd.startswith(c)), None)
            reply = command[name](rest) if name else b""
        if reply.startswith(b"NO "):
            self.write(tag + b" " + reply + b"\r\n")
        else:
            self.write(reply + tag + b" OK Success\r\n")

    def capability(self, rest: bytes) -> bytes:
        return b"* CAPABILITY IMAP4rev1 X-GM-EXT-1 UIDPLUS SPECIAL-USE\r\n"

    def login(self, rest: bytes) -> bytes:
        if b"wrongpass" in rest:
            return b"NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)"
        if b"busypass" in rest:
            return b"NO [ALERT] Too many simultaneous connections. (Failure)"
        return b""

    def list_mailboxes(self, rest: bytes) -> bytes:
        if b"[Gmail]" not in rest and b'"*"' not in rest:
            return b""
        return (
            b'* LIST (\\HasNoChildren \\All) "/" "[Gmail]/All Mail"\r\n'
            b'* LIST (\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"\r\n'
            b'* LIST (\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"\r\n'
        )

    def select(self, rest: bytes) -> bytes:
        name = re.match(rb'SELECT "?(.*?)"?$', rest, re.I)[1].decode()
        if name not in self.server.boxes:
            return b"NO [NONEXISTENT] Unknown Mailbox (Failure)"
        self.selected = name
        box = self.server.boxes[name]
        reply = b"* FLAGS (\\Seen)\r\n* OK [UIDVALIDITY %d] UIDs valid.\r\n* %d EXISTS\r\n" % (
            box["uidvalidity"],
            len(box["msgs"]),
        )
        if not self.server.no_uidnext:
            uidnext = max(box["msgs"], default=0) + 1
            reply += b"* OK [UIDNEXT %d] Predicted next UID.\r\n" % uidnext
        return reply

    def search(self, rest: bytes) -> bytes:
        box = self.server.boxes[self.selected]["msgs"]
        uids = sorted(box)
        if m := re.search(rb"UID (\d+):\*", rest):
            # "n:*" always includes the last message, even when n is past it.
            uids = [u for u in uids if u >= int(m[1])] or ([max(box)] if box else [])
        if m := re.search(rb"SINCE (\d+-\w+-\d+)", rest):
            since = dt.datetime.strptime(m[1].decode(), "%d-%b-%Y").date()
            uids = [u for u in uids if self.server.dates.get(box[u][0], TODAY) >= since]
        return b"* SEARCH" + b"".join(b" %d" % u for u in uids) + b"\r\n"

    def fetch(self, line: bytes, rest: bytes) -> bytes:
        srv = self.server
        m = re.match(rb"UID FETCH (\S+) \((.*)\)$", rest, re.I)
        uidset, items = m[1].decode(), m[2]
        if re.search(rb"BODY\[", items) or b"RFC822" in items:
            srv.violations.append(line)  # anything but BODY.PEEK marks mail as read
        box = srv.boxes[self.selected]["msgs"]
        out = b"* 1 FETCH (FLAGS (\\Seen))\r\n" if srv.unsolicited else b""
        for seq, uid in enumerate((int(x) for x in uidset.split(",")), start=1):
            if uid not in box:
                continue
            gid, message = box[uid]
            if b"HEADER.FIELDS" in items:
                if srv.delete_after_header == (self.selected, uid):  # deleted mid-scan
                    srv.delete_after_header = None
                    srv.boxes["[Gmail]/Trash"]["msgs"][900] = box.pop(uid)
                hdr = header_fields(message)
                meta = b"UID %d X-GM-MSGID %d BODY[HEADER.FIELDS (FROM SUBJECT)]" % (uid, gid)
                out += b"* %d FETCH (%s {%d}\r\n%s)\r\n" % (seq, meta, len(hdr), hdr)
            else:
                srv.body_fetches.append((self.selected, uid))
                meta = b'X-GM-MSGID %d INTERNALDATE " 9-Oct-2026 13:01:02 +0000" BODY[]' % gid
                out += b"* %d FETCH (%s {%d}\r\n%s UID %d)\r\n" % (
                    seq,
                    meta,
                    len(message),
                    message,
                    uid,
                )
        return out


class Server:
    def __init__(self, chunked: bool = False, unsolicited: bool = False) -> None:
        self.chunked, self.unsolicited = chunked, unsolicited
        self.stall = False
        self.no_uidnext = False
        self.delete_after_header: tuple[str, int] | None = None
        self.dates: dict[int, dt.date] = {}  # X-GM-MSGID -> arrival date (default: TODAY)
        self.violations: list[bytes] = []
        self.body_fetches: list[tuple[str, int]] = []
        self.boxes = {
            "[Gmail]/All Mail": {
                "uidvalidity": 11,
                "msgs": {
                    100: (9001, NEWSLETTER),
                    101: (9002, FEDEX),
                    102: (9003, AMAZON),
                    103: (9004, DIGEST),
                },
            },
            "[Gmail]/Trash": {"uidvalidity": 17, "msgs": {}},
        }

    async def start(self) -> "Server":
        loop = asyncio.get_running_loop()
        self.srv = await loop.create_server(lambda: FakeGmail(self), "127.0.0.1", 0)
        self.port = self.srv.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        self.srv.close()
        await self.srv.wait_closed()


@pytest.fixture
async def server():
    srv = await Server().start()
    yield srv
    await srv.stop()


async def scan(
    srv: Server, cursors, seen, password: str = "app password"
) -> imap_client.ScanResult:
    return await imap_client.scan(
        ACCOUNT,
        password,
        None,
        cursors=cursors,
        seen=seen,
        lookback_days=14,
        today=TODAY,
        is_candidate=kb.is_candidate,
        host="127.0.0.1",
        port=srv.port,
        client_cls=IMAP4,
    )


@pytest.mark.parametrize(
    ("chunked", "unsolicited", "no_uidnext"),
    [(False, False, False), (True, False, False), (False, True, False), (False, False, True)],
)
async def test_scan_lifecycle(chunked, unsolicited, no_uidnext):
    srv = await Server(chunked, unsolicited).start()
    srv.no_uidnext = no_uidnext
    try:
        first = await scan(srv, {}, set())
        assert sorted(m.uid for m in first.mails) == [101, 102, 103]  # the newsletter: headers only
        assert {m.msgid for m in first.mails} == {"9002", "9003", "9004"}
        assert next(m for m in first.mails if m.uid == 101).raw == FEDEX  # literal intact
        assert first.cursors["all"] == {"uidvalidity": 11, "last_uid": 103}
        assert first.headers_checked == 4
        seen = {m.msgid for m in first.mails}

        # Nothing new: no bodies downloaded, cursor unchanged.
        before = len(srv.body_fetches)
        second = await scan(srv, first.cursors, seen)
        assert second.mails == [] and len(srv.body_fetches) == before

        # The FedEx email is deleted (Trash, new UID, same message id) and a new email arrives.
        srv.boxes["[Gmail]/Trash"]["msgs"][5] = (9002, FEDEX)
        srv.boxes["[Gmail]/All Mail"]["msgs"][104] = (9005, AMAZON.replace(b"Kitchen", b"Lamps"))
        third = await scan(srv, second.cursors, seen)
        assert [(m.folder, m.uid) for m in third.mails] == [("all", 104)]
        assert third.cursors["trash"]["last_uid"] == 5

        # A new UIDVALIDITY rescans the folder from the look-back window.
        srv.boxes["[Gmail]/All Mail"]["uidvalidity"] = 12
        fourth = await scan(srv, third.cursors, seen | {"9005"})
        assert fourth.mails == [] and fourth.cursors["all"] == {"uidvalidity": 12, "last_uid": 104}

        assert srv.violations == []  # nothing marked read, stored, moved or deleted
    finally:
        await srv.stop()


async def test_downloaded_mail_parses(server):
    result = await scan(server, {}, set())
    messages = [message_from_bytes(m.raw, policy=default_policy) for m in result.mails]
    assert all(kb.parse_message(msg, dt.UTC) is not None for msg in messages)


async def test_wrong_password(server):
    with pytest.raises(imap_client.GmailAuthError, match="AUTHENTICATIONFAILED"):
        await scan(server, {}, set(), password="wrongpass")


async def test_too_many_connections_is_not_a_bad_password(server):
    with pytest.raises(imap_client.GmailError) as err:
        await scan(server, {}, set(), password="busypass")
    assert not isinstance(err.value, imap_client.GmailAuthError)


async def test_check_login(server):
    await imap_client.check_login(
        ACCOUNT, "pw", None, host="127.0.0.1", port=server.port, client_cls=IMAP4
    )


async def test_connection_refused(monkeypatch):
    monkeypatch.setattr(imap_client, "CMD_TIMEOUT", 1.0)
    with pytest.raises(imap_client.GmailError, match="no IMAP greeting"):
        async with asyncio.timeout(60):
            await imap_client.scan(
                ACCOUNT,
                "pw",
                None,
                cursors={},
                seen=set(),
                lookback_days=3,
                today=TODAY,
                is_candidate=kb.is_candidate,
                host="127.0.0.1",
                port=1,
                client_cls=IMAP4,
            )


async def test_stalled_fetch_becomes_gmail_error(server, monkeypatch):
    monkeypatch.setattr(imap_client, "CMD_TIMEOUT", 2.0)
    server.stall = True
    with pytest.raises(imap_client.GmailError, match="CommandTimeout"):
        async with asyncio.timeout(60):
            await scan(server, {}, set())


async def test_mail_deleted_between_passes_is_read_from_trash(server):
    server.delete_after_header = ("[Gmail]/All Mail", 101)  # the FedEx email
    result = await scan(server, {}, set())
    fedex = next(m for m in result.mails if m.msgid == "9002")
    assert fedex.folder == "trash"


async def test_bulk_deleted_old_mail_is_not_downloaded(server):
    first = await scan(server, {}, set())
    seen = {m.msgid for m in first.mails}
    # 300 old Amazon emails from 2024 are deleted: they land in Trash under new UIDs.
    for i in range(300):
        gid = 50000 + i
        server.dates[gid] = dt.date(2024, 1, 1)
        server.boxes["[Gmail]/Trash"]["msgs"][1000 + i] = (gid, AMAZON)
    before = len(server.body_fetches)
    second = await scan(server, first.cursors, seen)
    assert second.mails == [] and len(server.body_fetches) == before
    assert second.cursors["trash"]["last_uid"] == 1299  # the cursor still moves past them
