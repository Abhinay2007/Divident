"""Read-only public runtime configuration."""

from ..allocation.policy import load_allocation_policy
from ..config import DEFAULT_POLICY_PATH
from ..schemas import ConfigResponse, FeeParams


class ConfigService:
    @staticmethod
    def get(settings) -> ConfigResponse:
        policy_path = settings.allocation_policy_path
        fees = load_allocation_policy(policy_path if policy_path.exists() else DEFAULT_POLICY_PATH).fees
        return ConfigResponse(
            budget_min_cents=100,
            budget_max_cents=1_000_000,
            fee_params=FeeParams(
                order_fee_pct=float(fees.order_fee_pct) if fees.order_fee_pct is not None else None,
                order_fee_fixed_cents=fees.order_fee_fixed_cents,
                payout_fee_per_item_cents=fees.payout_fee_per_item_cents,
                buffer_cents=fees.buffer_cents,
                tolerance_cents=fees.tolerance_cents,
            ),
            paypal_env=settings.PAYPAL_ENV,
            sandbox=settings.PAYPAL_ENV == "sandbox",
            payments_mode=settings.PAYMENTS_MODE,
            llm_live_enabled=settings.LLM_LIVE_ENABLED and not settings.LLM_KILL_SWITCH,
            cold_start_seconds=0,
        )
