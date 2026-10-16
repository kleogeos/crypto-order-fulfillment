# crypto-order-fulfillment

Scheduled bot that checks for USDT (TRC20) crypto order emails sent to the
[freelancer-tools](https://kleogeos.github.io/freelancer-tools/) checkout
pages, verifies the claimed transaction actually landed on-chain, and emails
the buyer their download link — without a human needing to notice the sale.

## Why

The checkout pages tell buyers to send USDT and email the tx hash. Checking
that inbox by hand only when someone happens to remember means a real sale
could sit unfulfilled for hours. This runs on a schedule instead, same idea
as [telegram-stars-checkout](https://github.com/kleogeos/telegram-stars-checkout)
but for the USDT channel.

## How it works

1. `fulfill.py` reads the last 40 inbox emails over IMAP, looks for subjects
   matching a known product ("starter kit order", "bundle order", etc.).
2. For each match it looks for a 64-hex-char TXID and a buyer email in the
   body, then checks that TXID against recent inbound USDT (TRC20) transfers
   via the public TronGrid API.
3. If the amount matches the product's price (within $0.50 tolerance), it
   emails the buyer their download link and records the TXID in
   `fulfilled.json` so it's never sent twice.
4. Orders that can't be auto-matched (missing TXID or email) are recorded in
   `pending-manual-followup.json` for a human to handle manually.

Runs every 10 minutes via GitHub Actions (free, unlimited minutes on public
repos) and commits the updated state files back to the repo.

## Secrets required

- `MAIL_USER` — the Gmail address buyers are told to email.
- `MAIL_PASSWORD` — a Gmail app password for that address (IMAP + SMTP).

The USDT wallet address is not a secret — it's already printed on every
checkout page.

## Tests

```bash
python3 -m unittest test_fulfill.py -v
```

`test_fulfill.py` covers the decision logic (`process_orders`) with fake
orders/transfers — no network or real mailbox needed.
