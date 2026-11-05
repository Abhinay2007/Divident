"""Offline validation tests for the PayPal Sandbox spike CLI."""

import unittest

from scripts.spike import SpikeError, payout_items


class PayoutItemsValidationTests(unittest.TestCase):
    recipients = ["one@example.test", "two@example.test"]

    def items(self, amount: str, ids: list[str] | None = None) -> list[dict]:
        return payout_items(self.recipients, [amount, "0.25"], ids)

    def test_rejects_non_finite_amounts(self) -> None:
        for amount in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(amount=amount), self.assertRaises(SpikeError):
                self.items(amount)

    def test_rejects_zero_and_negative_amounts(self) -> None:
        for amount in ("0", "-0.01", "-1"):
            with self.subTest(amount=amount), self.assertRaises(SpikeError):
                self.items(amount)

    def test_rejects_more_than_two_decimal_places(self) -> None:
        with self.assertRaises(SpikeError):
            self.items("0.001")

    def test_accepts_valid_positive_usd_amounts(self) -> None:
        items = self.items("12.34", ["first-item", "second-item"])
        self.assertEqual(items[0]["amount"], {"value": "12.34", "currency": "USD"})
        self.assertEqual(items[1]["amount"], {"value": "0.25", "currency": "USD"})

    def test_sender_item_id_length_boundary(self) -> None:
        accepted = payout_items(self.recipients, ["0.10", "0.20"], ["x" * 63, "valid"])
        self.assertEqual(len(accepted[0]["sender_item_id"]), 63)
        with self.assertRaisesRegex(SpikeError, "1-63"):
            payout_items(self.recipients, ["0.10", "0.20"], ["x" * 64, "valid"])


if __name__ == "__main__":
    unittest.main()
