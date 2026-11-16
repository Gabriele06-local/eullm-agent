"""The headers every email needs: without Message-ID and Date SpamAssassin
gave the weekly report 5.1 points and called it spam (Francesco 2026-11-16:
MISSING_MID, MISSING_DATE, DOS_BODY_HIGH_NO_MID)."""

from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr


def stamp(msg: EmailMessage, sender: str | None) -> EmailMessage:
    domain = parseaddr(sender or "")[1].rpartition("@")[2] or None
    if "Date" not in msg:
        msg["Date"] = formatdate(localtime=True)
    if "Message-ID" not in msg:
        msg["Message-ID"] = make_msgid(domain=domain)
    return msg
