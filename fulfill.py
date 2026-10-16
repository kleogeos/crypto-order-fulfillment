#!/usr/bin/env python3
"""Check for USDT (TRC20) crypto order emails, verify the claimed tx on-chain, and
email the buyer their download link automatically.

Why this exists: the checkout pages at kleogeos.github.io/freelancer-tools tell
buyers to send USDT and email the tx hash. Until now that inbox was only checked
by hand during occasional sessions — a real sale could sit unfulfilled for hours
(bad buyer experience, risk of chargeback-style disputes/refund requests) purely
because nobody happened to look. This runs on a schedule instead.

Requires repo secrets: MAIL_USER, MAIL_PASSWORD (Gmail address + app password,
same inbox buyers are told to email). Wallet address and USDT contract are public
information already printed on the checkout pages, so they're not secrets.

State: fulfilled.json (txid -> delivery record) is committed back each run so a
tx is never delivered twice. pending-manual-followup.json records order emails
that can't be auto-fulfilled (missing txid or buyer email) so a human can reply.
"""
import email
import email.header
import imaplib
import json
import os
import re
import smtplib
import sys
import time
import urllib.request
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime

ADDRESS = "TSDqzM1aAc5FKWUnmZQBtajLzgW7uneYhh"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
BASE_URL = "https://kleogeos.github.io/freelancer-tools"

# subject substring -> (expected USDT amount, download path, display name)
PRODUCTS = {
    "starter kit order": (15, "files/freelancer-kit.zip", "Freelancer Starter Kit"),
    "prompt pack order": (9, "files/prompt-pack.zip", "40 AI Prompts for Freelancers"),
    "dev pack order": (12, "files/dev-pack.zip", "Web Developer Pack"),
    "ru kit order": (7, "files/ru-crypto-kit.zip", "RU Crypto Freelancer Kit"),
    "bundle order": (25, None, "Bundle (all three)"),  # handled specially below
}
BUNDLE_FILES = ["files/freelancer-kit.zip", "files/prompt-pack.zip", "files/dev-pack.zip"]
AMOUNT_TOLERANCE = 0.5  # USDT; price is pegged so should be near-exact

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "fulfilled.json")
PENDING_PATH = os.path.join(HERE, "pending-manual-followup.json")
TXID_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
SELLER_EMAILS = {"kleogeos@gmail.com"}  # checkout pages tell buyers to email this address,
# so it's commonly quoted back in the order body — must not be mistaken for the buyer's own.


def load_json(path):
    if os.path.exists(path):
        return json.load(open(path))
    return {}


def save_json(path, d):
    json.dump(d, open(path, "w"), indent=2)


def recent_usdt_transfers(limit=30):
    url = (
        f"https://api.trongrid.io/v1/accounts/{ADDRESS}/transactions/trc20"
        f"?limit={limit}&contract_address={USDT_CONTRACT}&only_to=true"
    )
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.load(r)
    out = {}
    for tx in data.get("data", []):
        amount = int(tx["value"]) / (10 ** int(tx["token_info"]["decimals"]))
        out[tx["transaction_id"]] = amount
    return out


def _dec(v):
    return "".join(
        t.decode(c or "utf-8", "replace") if isinstance(t, bytes) else t
        for t, c in email.header.decode_header(v or "")
    )


def _body(msg):
    parts = [msg] if not msg.is_multipart() else list(msg.walk())
    out = ""
    for p in parts:
        if p.get_content_type() in ("text/plain", "text/html") and not p.get_filename():
            raw = p.get_payload(decode=True) or b""
            t = raw.decode(p.get_content_charset() or "utf-8", "replace")
            if p.get_content_type() == "text/html":
                t = re.sub(r"<(script|style).*?</\1>", " ", t, flags=re.S)
                t = re.sub(r"<[^>]+>", " ", t)
            out += " " + t
    return re.sub(r"\s+", " ", out).strip()


def recent_mail(n=40):
    user = os.environ["MAIL_USER"]
    pw = os.environ["MAIL_PASSWORD"]
    box = imaplib.IMAP4_SSL("imap.gmail.com")
    box.login(user, pw)
    box.select("INBOX")
    ids = box.search(None, "ALL")[1][0].split()[-n:]
    rows = []
    for i in reversed(ids):
        msg = email.message_from_bytes(box.fetch(i, "(BODY.PEEK[])")[1][0][1])
        try:
            ts = parsedate_to_datetime(msg["Date"]).timestamp()
        except Exception:  # noqa: BLE001
            ts = 0
        rows.append({"ts": ts, "from": _dec(msg["From"]), "subject": _dec(msg["Subject"]), "body": _body(msg)})
    box.logout()
    return rows


def send_mail(to_addr, subject, body):
    user = os.environ["MAIL_USER"]
    pw = os.environ["MAIL_PASSWORD"]
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, [to_addr], msg.as_string())


def process_orders(orders, transfers, log, pending):
    """Pure decision logic, no I/O side effects beyond the log/pending dicts and the
    returned list of (buyer_email, name, amount, body) to actually send."""
    to_send = []
    for o in orders:
        subj = o["subject"].lower()
        matched = next((k for k in PRODUCTS if k in subj), None)
        if not matched:
            continue
        txids = TXID_RE.findall(o["body"])
        body_emails = [e for e in EMAIL_RE.findall(o["body"]) if e.lower() not in SELLER_EMAILS]
        from_match = EMAIL_RE.search(o["from"] or "")
        buyer_email = body_emails[0] if body_emails else (from_match.group(0) if from_match else None)
        if not txids or not buyer_email:
            key = f"{o.get('from', '?')}|{o['subject']}"
            if key not in pending:
                pending[key] = {
                    "from": o.get("from"), "subject": o["subject"],
                    "reason": "no txid in body" if not txids else "no usable email",
                }
            continue
        txid = txids[0]
        if txid in log:
            continue  # already fulfilled
        if txid not in transfers:
            continue  # not landed on-chain yet
        expected_amount, path, name = PRODUCTS[matched]
        actual = transfers[txid]
        if abs(actual - expected_amount) > AMOUNT_TOLERANCE:
            continue

        if matched == "bundle order":
            links = "\n".join(f"- {BASE_URL}/{p}" for p in BUNDLE_FILES)
            body = f"Thanks for your order! Here are your downloads:\n\n{links}\n\nEnjoy!"
        else:
            links = f"{BASE_URL}/{path}"
            body = f"Thanks for your order! Here's your download:\n\n{links}\n\nEnjoy!"

        to_send.append((buyer_email, name, actual, body, txid))
        # a later, better-formed email from the same sender clears the earlier flag
        for k in [k for k in pending if k.startswith(f"{o.get('from', '?')}|")]:
            pending.pop(k)
    return to_send


def main():
    log = load_json(LOG_PATH)
    pending = load_json(PENDING_PATH)
    transfers = recent_usdt_transfers()
    orders = recent_mail(40)

    to_send = process_orders(orders, transfers, log, pending)
    sent = []
    for buyer_email, name, actual, body, txid in to_send:
        send_mail(buyer_email, f"Your {name} download", body)
        log[txid] = {"email": buyer_email, "product": name, "amount": actual}
        sent.append((buyer_email, name, actual))
        print(f"[sent] {name} (${actual}) -> {buyer_email}")

    save_json(LOG_PATH, log)
    save_json(PENDING_PATH, pending)
    if not sent:
        print("No new orders to fulfill.")
    if pending:
        print(f"{len(pending)} order email(s) still need a MANUAL reply (missing txid/email) — see {PENDING_PATH}")
    return sent


if __name__ == "__main__":
    main()
