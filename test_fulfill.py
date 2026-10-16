#!/usr/bin/env python3
"""Offline tests for fulfill.py's decision logic. No network/mail/token needed —
process_orders() is pure (takes orders/transfers/log/pending dicts, returns what
to send) so the actual send_mail/IMAP side effects never need to run in tests."""
import os
import sys
import unittest

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

    def test_wrong_amount_is_skipped(self):
        orders = [{
            "from": "Buyer <buyer@example.com>",
            "subject": "Starter Kit order",
            "body": f"tx {GOOD_TXID}",
        }]
        transfers = {GOOD_TXID: 3.0}  # way below the $15 Starter Kit price
        to_send = fulfill.process_orders(orders, transfers, {}, {})
        self.assertEqual(to_send, [])

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


if __name__ == "__main__":
    unittest.main()
