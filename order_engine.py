"""Restart-safe evaluation and execution of persistent trade orders."""

import logging
from typing import Any

import dexscreener_client as dex
import executor
import storage

logger = logging.getLogger(__name__)


def create_default_exit_orders(position: dict) -> list[int]:
    """Attach the configured stop-loss and staged take-profit plan."""
    import config

    common = {
        "position_id": position["id"],
        "token_address": position["token_address"],
        "token_symbol": position.get("token_symbol", ""),
        "wallet_address": position.get("wallet_address", ""),
    }
    return [
        storage.create_trade_order(order_type="stop_loss", trigger_value=config.STOP_LOSS_PCT,
                                   amount_pct=100.0, **common),
        storage.create_trade_order(order_type="take_profit", trigger_value=config.DCA_STAGE1_PCT,
                                   amount_pct=config.DCA_SELL_EACH, **common),
        storage.create_trade_order(order_type="take_profit", trigger_value=config.DCA_STAGE2_PCT,
                                   amount_pct=config.DCA_SELL_EACH, **common),
    ]


def _current_price(token_address: str) -> float | None:
    pairs = dex.fetch_pair_details("solana", token_address)
    if not pairs:
        return None
    price = float(pairs[0].get("priceUsd", 0) or 0)
    return price if price > 0 else None


def _is_triggered(order: dict, position: dict | None) -> tuple[bool, dict[str, Any]]:
    kind = order["order_type"]
    if kind == "limit_buy":
        price = _current_price(order["token_address"])
        return price is not None and price <= order["trigger_value"], {"price_usd": price}
    if not position or position.get("status") != "open" or position.get("tx_status") not in {"confirmed", "finalized"}:
        return False, {}
    pnl = executor.check_position_pnl(position)
    if pnl is None:
        return False, {}
    pct = pnl["pnl_pct"]
    if kind == "stop_loss":
        return pct <= order["trigger_value"], pnl
    if kind == "take_profit":
        return pct >= order["trigger_value"], pnl
    return False, pnl


def process_orders_once() -> list[dict]:
    """Evaluate active orders once and return events for user notification."""
    events: list[dict] = []
    for order in storage.get_active_trade_orders():
        position = storage.get_position_by_id(order["position_id"]) if order.get("position_id") else None
        if order.get("position_id") and (not position or position.get("status") == "closed"):
            storage.cancel_trade_order(order["id"])
            continue
        triggered, market = _is_triggered(order, position)
        if not triggered or not storage.claim_trade_order(order["id"]):
            continue
        try:
            if order["order_type"] == "limit_buy":
                result = executor.buy_token(order["token_address"], order.get("amount_sol"))
            else:
                pct = float(order.get("amount_pct") or 100)
                if pct >= 100:
                    result = executor.sell_token(position["id"], position["token_address"], position["token_amount"])
                else:
                    result = executor.sell_partial(
                        position["id"], position["token_address"], position["token_amount"], pct
                    )
            if not result:
                storage.release_trade_order(order["id"], "execution failed")
                continue
            storage.complete_trade_order(order["id"], result.get("signature", ""))
            if order["order_type"] == "stop_loss" or float(order.get("amount_pct") or 100) >= 100:
                if order.get("position_id"):
                    storage.cancel_position_orders(order["position_id"], exclude_order_id=order["id"])
            events.append({"order": order, "result": result, "market": market})
        except Exception as exc:
            logger.exception("[ORDER] Execution failed for order #%s", order["id"])
            storage.release_trade_order(order["id"], str(exc))
    return events
