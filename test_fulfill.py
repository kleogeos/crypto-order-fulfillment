#!/usr/bin/env python3
"""Offline tests for fulfill.py's decision logic. No network/mail/token needed —
process_orders() is pure (takes orders/transfers/log/pending dicts, returns what
to send) so the actual send_mail/IMAP side effects never need to run in tests."""
import os
import smtplib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fulfill  # noqa: E402

GOOD_TXID = "a" * 64
OTHER_TXID = "b" * 64


class TestProcessOrders(unittest.TestCase):
    def test_matched_order_with_landed_tx_is_sent(self):
        orders = [{
            "from": "Buyer <buyer@example.com>",
            "subject": "Starter Kit order",
            "body": f"here you go, tx {GOOD_TXID}, email me at buyer@example.com",
        }]
        transfers = {GOOD_TXID: 15.0}
        to_send = fulfill.process_orders(orders, transfers, {}, {})
        self.assertEqual(len(to_send), 1)
        buyer_email, name, actual, body, txid = to_send[0]
        self.assertEqual(buyer_email, "buyer@example.com")
        self.assertEqual(name, "Freelancer Starter Kit")
        self.assertEqual(actual, 15.0)
        self.assertEqual(txid, GOOD_TXID)

    def test_already_fulfilled_txid_is_not_resent(self):
        orders = [{
            "from": "Buyer <buyer@example.com>",
            "subject": "Starter Kit order",
            "body": f"tx {GOOD_TXID}",
        }]
        transfers = {GOOD_TXID: 15.0}
        log = {GOOD_TXID: {"email": "buyer@example.com", "product": "Freelancer Starter Kit", "amount": 15.0}}
        to_send = fulfill.process_orders(orders, transfers, log, {})
        self.assertEqual(to_send, [])

    def test_txid_not_yet_landed_on_chain_is_skipped_not_dropped(self):
        orders = [{
            "from": "Buyer <buyer@example.com>",
            "subject": "Starter Kit order",
            "body": f"tx {GOOD_TXID}",
        }]
        to_send = fulfill.process_orders(orders, {}, {}, {})
        self.assertEqual(to_send, [])

    def test_wrong_amount_is_not_sent_but_flagged_for_manual_followup(self):
        orders = [{
            "from": "Buyer <buyer@example.com>",
            "subject": "Starter Kit order",
            "body": f"tx {GOOD_TXID}",
        }]
        transfers = {GOOD_TXID: 3.0}  # way below the $15 Starter Kit price
        pending = {}
        to_send = fulfill.process_orders(orders, transfers, {}, pending)
        self.assertEqual(to_send, [])
        self.assertEqual(len(pending), 1)  # must not be silently dropped

    def test_bundle_payment_under_wrong_subject_is_flagged_not_lost(self):
        # Buyer paid the $25 bundle price but didn't know to write "Bundle order"
        # as the subject, so it matches the $15 Starter Kit product instead and
        # mismatches on amount. This must land in pending, not vanish silently.
        orders = [{
            "from": "Buyer <buyer@example.com>",
            "subject": "Starter Kit order",
            "body": f"tx {GOOD_TXID}",
        }]
        transfers = {GOOD_TXID: 25.0}
        pending = {}
        to_send = fulfill.process_orders(orders, transfers, {}, pending)
        self.assertEqual(to_send, [])
        self.assertEqual(len(pending), 1)
        self.assertIn("25", next(iter(pending.values()))["reason"])

    def test_missing_txid_is_logged_for_manual_followup(self):
        orders = [{
            "from": "Buyer <buyer@example.com>",
            "subject": "Starter Kit order",
            "body": "sent the payment, no hash pasted though",
        }]
        pending = {}
        fulfill.process_orders(orders, {}, {}, pending)
        self.assertEqual(len(pending), 1)
        reason = next(iter(pending.values()))["reason"]
        self.assertEqual(reason, "no txid in body")

    def test_seller_email_quoted_in_body_is_not_mistaken_for_buyer(self):
        orders = [{
            "from": "Buyer <buyer@example.com>",
            "subject": "Starter Kit order",
            "body": f"sending to kleogeos@gmail.com as instructed, tx {GOOD_TXID}",
        }]
        transfers = {GOOD_TXID: 15.0}
        to_send = fulfill.process_orders(orders, transfers, {}, {})
        self.assertEqual(len(to_send), 1)
        self.assertEqual(to_send[0][0], "buyer@example.com")

    def test_bundle_order_lists_all_three_files(self):
        orders = [{
            "from": "Buyer <buyer@example.com>",
            "subject": "Bundle order",
            "body": f"tx {GOOD_TXID}",
        }]
        transfers = {GOOD_TXID: 25.0}
        to_send = fulfill.process_orders(orders, transfers, {}, {})
        body = to_send[0][3]
        for f in fulfill.BUNDLE_FILES:
            self.assertIn(f, body)

    def test_unrelated_subject_is_ignored(self):
        orders = [{"from": "x@example.com", "subject": "Hello", "body": "no order here"}]
        to_send = fulfill.process_orders(orders, {GOOD_TXID: 15.0}, {}, {})
        self.assertEqual(to_send, [])

    def test_successful_match_clears_earlier_pending_flag_from_same_sender(self):
        pending = {"Buyer <buyer@example.com>|Starter Kit order": {"from": "Buyer <buyer@example.com>", "subject": "Starter Kit order", "reason": "no txid in body"}}
        orders = [{
            "from": "Buyer <buyer@example.com>",
            "subject": "Starter Kit order",
            "body": f"forgot the hash before, here it is: {GOOD_TXID}",
        }]
        transfers = {GOOD_TXID: 15.0}
        to_send = fulfill.process_orders(orders, transfers, {}, pending)
        self.assertEqual(len(to_send), 1)
        self.assertEqual(pending, {})


class TestMainNotifications(unittest.TestCase):
    """main() does real I/O (mail, chain, smtp, telegram) — mock all of it and check
    only the decision of *when* notify_human gets called, which is the actual bug
    class worth guarding (silent sales / silently-stuck orders)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        fulfill.LOG_PATH = os.path.join(self.tmpdir, "fulfilled.json")
        fulfill.PENDING_PATH = os.path.join(self.tmpdir, "pending-manual-followup.json")
        self.notified = []
        self.patchers = [
            mock.patch.object(fulfill, "notify_human", side_effect=lambda t: self.notified.append(t)),
            mock.patch.object(fulfill, "send_mail"),
            mock.patch.object(fulfill, "recent_usdt_transfers", return_value={GOOD_TXID: 15.0}),
        ]
        for p in self.patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patchers])

    def test_new_sale_triggers_one_notification(self):
        with mock.patch.object(fulfill, "recent_mail", return_value=[{
            "from": "Buyer <buyer@example.com>", "subject": "Starter Kit order",
            "body": f"tx {GOOD_TXID}",
        }]):
            fulfill.main()
        self.assertEqual(len(self.notified), 1)
        self.assertIn("Sale!", self.notified[0])

    def test_new_pending_order_triggers_one_notification(self):
        with mock.patch.object(fulfill, "recent_mail", return_value=[{
            "from": "Buyer <buyer@example.com>", "subject": "Starter Kit order",
            "body": "no hash pasted",
        }]):
            fulfill.main()
        self.assertEqual(len(self.notified), 1)
        self.assertIn("manual reply", self.notified[0])

    def test_already_known_pending_order_does_not_renotify(self):
        order = {"from": "Buyer <buyer@example.com>", "subject": "Starter Kit order", "body": "no hash pasted"}
        with mock.patch.object(fulfill, "recent_mail", return_value=[order]):
            fulfill.main()
        self.assertEqual(len(self.notified), 1)  # first run: notified once
        with mock.patch.object(fulfill, "recent_mail", return_value=[order]):
            fulfill.main()
        self.assertEqual(len(self.notified), 1)  # second run, same stuck order: no new notification

    def test_earlier_sale_in_batch_is_persisted_even_if_a_later_send_raises(self):
        # Regression for a real bug: fulfilled.json used to be saved once at the
        # very end of main(), after the whole send loop. If order #2's send_mail
        # raised (e.g. a transient SMTP error), the exception skipped save_json()
        # entirely — so order #1, already emailed successfully moments earlier,
        # would be missing from fulfilled.json. The next cron run (10 min later)
        # would then re-match order #1 and email that buyer a duplicate delivery
        # for something they already received. Each send must persist immediately.
        self.patchers[2].stop()  # replace the transfers-only mock with two txids
        mock.patch.object(fulfill, "recent_usdt_transfers",
                           return_value={GOOD_TXID: 15.0, OTHER_TXID: 9.0}).start()
        orders = [
            {"from": "Buyer1 <buyer1@example.com>", "subject": "Starter Kit order", "body": f"tx {GOOD_TXID}"},
            {"from": "Buyer2 <buyer2@example.com>", "subject": "Prompt Pack order", "body": f"tx {OTHER_TXID}"},
        ]
        with mock.patch.object(fulfill, "recent_mail", return_value=orders), \
             mock.patch.object(fulfill, "send_mail", side_effect=[None, smtplib.SMTPException("transient")]):
            with self.assertRaises(smtplib.SMTPException):
                fulfill.main()
        log = fulfill.load_json(fulfill.LOG_PATH)
        self.assertIn(GOOD_TXID, log)  # order #1's send succeeded and must be persisted
        self.assertNotIn(OTHER_TXID, log)  # order #2's send never completed

    def test_transient_network_error_fetching_is_skipped_without_crashing(self):
        with mock.patch.object(fulfill, "recent_usdt_transfers",
                                side_effect=ConnectionResetError("Connection reset by peer")):
            result = fulfill.main()  # must not raise
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()

