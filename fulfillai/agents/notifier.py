"""
AI Notifier — LLM call #2 of the anomaly workflow.

Once an anomaly has an approved (or just-generated) diagnosis, this module
drafts one personalized customer notification per affected order. Each call
runs in parallel via asyncio.gather so many drafts come back quickly.

Every notification is saved as status='draft' — nothing is sent to customers
until a human approves in the ops Command Center.
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy.orm import Session

from agents.base import _call_openai_with_retry, log_agent_action

SUPPORT_EMAIL = "support@fulfillai.com"

_SYSTEM_PROMPT = f"""You are drafting a personalized delivery-delay notification to a customer on behalf of a fulfillment company called FulfillAI.

Tone: empathetic, honest, brief, human. Write like a real support professional, not a templated bot.

Rules:
- Acknowledge the specific delay and explain the reason in plain, customer-friendly language.
- Do NOT use internal jargon: no "FC", "fulfillment center code", "carrier ID", "pipeline", "queue position".
- Do NOT promise a specific new delivery date unless one is explicitly provided.
- Apologize sincerely and offer to help via {SUPPORT_EMAIL}.
- 3-4 short paragraphs maximum.
- Use the customer's actual first name in the greeting.
- Reference the items they actually ordered naturally (not as a formal list).
- Do not invent facts beyond what's provided.

Respond ONLY with valid JSON: {{"subject": "...", "body": "..."}}
The body should use real line breaks between paragraphs.
"""


async def draft_notifications_for_anomaly(db: Session, anomaly_id: int) -> list:
    """
    Draft a personalized notification per affected order.
    Transitions Anomaly: diagnosed → drafting → pending_review.
    Returns the list of created Notification rows.
    """
    from models import Anomaly, Notification, Order, OrderItem, Product

    anomaly = db.query(Anomaly).get(anomaly_id)
    if not anomaly:
        raise ValueError(f"Anomaly {anomaly_id} not found")
    if anomaly.status not in ("diagnosed",):
        print(f"[notifier] anomaly {anomaly_id} is in status {anomaly.status}, skipping draft")
        return []

    anomaly.status = "drafting"
    db.commit()

    # Remove any previous drafts (re-investigate case) so we don't duplicate
    existing = (
        db.query(Notification)
        .filter(Notification.anomaly_id == anomaly_id, Notification.status == "draft")
        .all()
    )
    for n in existing:
        db.delete(n)
    db.flush()

    order_ids = anomaly.affected_order_ids or []
    order_contexts = []
    for oid in order_ids:
        order = db.query(Order).get(oid)
        if not order:
            continue
        items = db.query(OrderItem).filter(OrderItem.order_id == oid).all()
        item_descriptions = []
        for it in items:
            product = db.query(Product).get(it.product_id)
            if product:
                qty = it.quantity
                item_descriptions.append(
                    f"{qty} × {product.name}" if qty > 1 else product.name
                )
        order_contexts.append(
            {
                "order_id": order.id,
                "order_number": order.order_number,
                "recipient_name": order.recipient_name or "Customer",
                "items_summary": ", ".join(item_descriptions) or "your order",
                "destination": f"{order.recipient_city}, {order.recipient_state}",
                "shipping_tier": order.shipping_tier,
            }
        )

    if not order_contexts:
        anomaly.status = "pending_review"
        db.commit()
        return []

    # ── Parallel LLM calls ───────────────────────────────────────────────
    likely_cause = anomaly.ai_likely_cause or "an unexpected delay in our fulfillment network"
    reasoning = anomaly.ai_detailed_reasoning or ""
    customer_impact = anomaly.ai_customer_impact or ""

    results = await asyncio.gather(
        *(_draft_one(ctx, likely_cause, reasoning, customer_impact) for ctx in order_contexts)
    )

    created = []
    personalized = 0
    fallback = 0
    for ctx, result in zip(order_contexts, results):
        notif = Notification(
            order_id=ctx["order_id"],
            anomaly_id=anomaly_id,
            recipient_name=ctx["recipient_name"],
            subject=result["subject"],
            body=result["body"],
            status="draft",
            is_fallback=not result["ok"],
        )
        db.add(notif)
        created.append(notif)
        if result["ok"]:
            personalized += 1
        else:
            fallback += 1

    anomaly.status = "pending_review"
    db.commit()

    log_agent_action(
        db,
        agent_name="notifier",
        action_type="drafts_ready",
        entity_type="anomaly",
        entity_id=anomaly_id,
        input_summary=f"{len(order_contexts)} affected orders",
        output_summary=f"{personalized} personalized, {fallback} fallback — ready for human review",
        details={"personalized": personalized, "fallback": fallback},
        severity="info",
    )
    db.commit()

    return created


async def _draft_one(ctx: dict, likely_cause: str, reasoning: str, customer_impact: str) -> dict:
    """Call the LLM for one order. Falls back to a canned message on failure."""
    user_content = f"""Customer: {ctx['recipient_name']}
Order number: {ctx['order_number']}
Items ordered: {ctx['items_summary']}
Destination: {ctx['destination']}
Shipping tier: {ctx['shipping_tier']}

Delay reason (from operations investigation): {likely_cause}
Details: {reasoning}
Customer impact note: {customer_impact}
"""

    try:
        response = await _call_openai_with_retry(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            stream=False,
            response_format={"type": "json_object"},
            temperature=0.7,
        )
        parsed = json.loads(response.choices[0].message.content)
        subject = (parsed.get("subject") or "").strip()
        body = (parsed.get("body") or "").strip()
        if not subject or not body:
            raise ValueError("LLM returned empty subject or body")
        return {"subject": subject, "body": body, "ok": True}
    except Exception as exc:
        print(f"[notifier] fallback for order {ctx['order_number']}: {exc}")
        first_name = ctx["recipient_name"].split()[0] if ctx["recipient_name"] else "there"
        subject = f"Update on your order {ctx['order_number']}"
        body = (
            f"Hi {first_name},\n\n"
            f"We wanted to let you know that your order {ctx['order_number']} is experiencing an unexpected delay. "
            f"We're actively working to get it moving again as quickly as possible.\n\n"
            f"If you have any questions or need help, please reach out to us at {SUPPORT_EMAIL} "
            f"and we'll do everything we can to make this right.\n\n"
            f"We sincerely apologize for the inconvenience.\n\n"
            f"— The FulfillAI team"
        )
        return {"subject": subject, "body": body, "ok": False}
