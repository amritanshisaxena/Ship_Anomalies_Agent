"""
Decision Narrator Agent.

After the deterministic fulfillment pipeline has finalized an order, this
agent reads the AgentAction trace rows written by the pipeline and asks
GPT-4o to write ONE conversational sentence explaining *why* the order was
routed the way it was (which FC, which carrier, what the trade-off was).

This exists so the "Agent Decision Trace" panel in the ops portal is
actually AI-assisted rather than just labeled that way. The deterministic
engine stays in charge of the decision — the narrator only explains.

Fail-safe: on any exception the narrator returns a deterministic templated
string and never raises. A failed narration must not block a finalized order.
"""

from __future__ import annotations

import json
from typing import Tuple

from sqlalchemy.orm import Session

from agents.base import _call_openai_with_retry, log_agent_action


_SYSTEM_PROMPT = (
    "You are the Decision Narrator for a shipping operations system. "
    "You are shown the raw step-by-step trace from a deterministic routing "
    "engine and must write ONE conversational sentence (≤35 words) that "
    "explains WHY this order was routed the way it was. "
    "Reference the fulfillment center, the carrier, the destination, and the "
    "trade-off (cost saved, speed chosen, stock depth, etc). "
    "No jargon. No lists. No hedging. "
    'Respond ONLY as JSON: {"explanation": "..."}'
)


async def narrate_order_decisions(db: Session, order_id: int) -> Tuple[str, bool]:
    """
    Generate a one-sentence explanation for how `order_id` was routed.

    Returns (explanation_text, is_fallback). Never raises.
    """
    from models import AgentAction, Order, Shipment, FulfillmentCenter, Carrier

    order = db.query(Order).get(order_id)
    if not order:
        return ("Order not found.", True)

    # Deterministic facts we can always include in a fallback string
    shipments = db.query(Shipment).filter(Shipment.order_id == order_id).all()
    fc_codes: list[str] = []
    carriers: list[str] = []
    total_cost = 0.0
    speed_days: list[int] = []
    for s in shipments:
        if s.fulfillment_center_id:
            fc = db.query(FulfillmentCenter).get(s.fulfillment_center_id)
            if fc and fc.code and fc.code not in fc_codes:
                fc_codes.append(fc.code)
        if s.carrier_id:
            c = db.query(Carrier).get(s.carrier_id)
            if c and c.name and c.name not in carriers:
                carriers.append(c.name)
        if s.shipping_cost:
            total_cost += float(s.shipping_cost)

    fc_str = " + ".join(fc_codes) if fc_codes else "?"
    carrier_str = " + ".join(carriers) if carriers else "?"
    dest_str = f"{order.recipient_city or '?'}, {order.recipient_state or '?'}"

    def _fallback() -> str:
        return (
            f"Shipped from {fc_str} via {carrier_str} to {dest_str} "
            f"(${total_cost:.2f} total, {order.shipping_tier} tier)."
        )

    # Pull the agent trace — same ordering as routes/orders.py
    trace_rows = (
        db.query(AgentAction)
        .filter(
            AgentAction.entity_type == "order",
            AgentAction.entity_id == order_id,
            AgentAction.agent_name == "pipeline",
        )
        .order_by(AgentAction.step_number.asc(), AgentAction.id.asc())
        .all()
    )

    trace_lines = []
    for row in trace_rows:
        trace_lines.append(
            f"Step {row.step_number} {row.action_type}: {row.output_summary or ''}"
        )

    user_content = (
        f"Order: {order.order_number}\n"
        f"Destination: {dest_str}\n"
        f"Shipping tier: {order.shipping_tier}  VIP: {order.is_vip}\n"
        f"Selected FC(s): {fc_str}\n"
        f"Selected carrier(s): {carrier_str}\n"
        f"Total shipping cost: ${total_cost:.2f}\n"
        f"\nAgent trace:\n" + "\n".join(trace_lines)
    )

    explanation = None
    is_fallback = False

    try:
        response = await _call_openai_with_retry(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        raw = response.choices[0].message.content or ""
        parsed = json.loads(raw)
        explanation = (parsed.get("explanation") or "").strip()
        if not explanation:
            raise ValueError("empty explanation")
    except Exception as exc:
        print(f"[narrator] LLM failed for order {order_id}: {exc}")
        explanation = _fallback()
        is_fallback = True

    # Log a step #7 AgentAction so the existing trace UI picks it up for free
    try:
        log_agent_action(
            db=db,
            agent_name="narrator",
            action_type="decision_narration",
            entity_type="order",
            entity_id=order_id,
            input_summary=f"trace of {len(trace_rows)} step(s)",
            output_summary=explanation,
            details={"is_fallback": is_fallback},
            severity="info",
            step_number=7,
        )
    except Exception as exc:
        print(f"[narrator] failed to log action: {exc}")

    return (explanation, is_fallback)
