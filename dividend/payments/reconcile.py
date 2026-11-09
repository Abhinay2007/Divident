"""Translate documented PayPal payout statuses to internal statuses."""


def map_item_status(raw_status: str) -> str:
    return {
        "PENDING": "pending", "ONHOLD": "pending", "SUCCESS": "success",
        "UNCLAIMED": "unclaimed", "RETURNED": "returned", "REFUNDED": "returned",
        "REVERSED": "returned", "FAILED": "failed", "DENIED": "failed",
        "BLOCKED": "failed",
    }.get(raw_status.upper() if type(raw_status) is str else "", "needs_attention")


def map_batch_status(raw_status: str) -> str:
    return {"PENDING": "pending", "PROCESSING": "pending", "ONHOLD": "pending", "SUCCESS": "success",
            "FAILED": "failed", "DENIED": "failed", "CANCELED": "failed"}.get(
                raw_status.upper() if type(raw_status) is str else "", "needs_attention")
