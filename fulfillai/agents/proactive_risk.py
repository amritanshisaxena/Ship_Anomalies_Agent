"""
Proactive Route-Risk Agent.

Runs at queue time, right after the deterministic fulfillment pipeline has
selected an origin FC and carrier for a new order. For each order it performs
three Tavily web searches (destination / carrier / origin FC), feeds the
results to GPT-4o with strict anti-hallucination rules, and — if the model
decides there is a meaningful risk — creates an Anomaly row flagged as
proactive_route_risk, puts the order on hold, and kicks off the existing
notifier to draft a personalized customer delay notification.

Fail-safe: any Tavily or LLM failure returns {"has_risk": False} so the order
is NOT held. A phantom anomaly is worse than a missed one.

Tavily results are cached per (destination / carrier / origin) for 2 hours via
agents/tavily_client.get_or_fetch_tavily, so a burst of orders to the same
route does not hammer the Tavily API.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from agents.base import _call_openai_with_retry, log_agent_action
from agents.tavily_client import get_or_fetch_tavily


_SYSTEM_PROMPT = """You are a fulfillment risk analyst assessing a freshly-routed order for deliverability risk BEFORE it ships.

STRICT RULES:
1. Base your assessment ONLY on the Tavily web search results provided. Do NOT invent weather, strikes, outages, disasters, or carrier issues that are not supported by the search results.
2. If the search results are empty, irrelevant, or do not clearly indicate a problem affecting THIS route, set has_risk=false. Being cautious is correct — a false alarm is worse than a miss.
3. If has_risk=true, every item in "evidence" must cite its source URL from the search results. "sources_used" must contain only URLs that appear verbatim in the provided results.
4. confidence must be "low", "medium", or "high". Only medium or high will trigger a hold on the order.
5. severity is "low", "medium", or "high".

Respond ONLY with valid JSON matching this schema:
{
  "has_risk": true | false,
  "severity": "low" | "medium" | "high",
  "likely_cause": "one concise sentence",
  "detailed_reasoning": "2-3 sentence paragraph citing specific search results",
  "evidence": [{"bullet": "...", "source": "source:https://..."}],
  "confidence": "low" | "medium" | "high",
  "recommended_action": "specific next step for ops",
  "customer_impact": "brief description for customer notification",
  "sources_used": ["https://...", ...]
}
"""


def _slug(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_") or "unknown"


async def assess_route_risk(db: Session, order_id: int) -> dict:
    """
    Assess proactive route risk for a freshly finalized order.

    Returns a dict: {"has_risk": bool, "anomaly_id": int | None, "fallback": bool}
    Never raises.
    """
    from models import Anomaly, Carrier, FulfillmentCenter, Order, Shipment
    from agents.notifier import draft_notifications_for_anomaly

    order = db.query(Order).get(order_id)
    if not order:
        return {"has_risk": False, "anomaly_id": None, "fallback": True}

    shipments = db.query(Shipment).filter(Shipment.order_id == order_id).all()
    if not shipments:
        return {"has_risk": False, "anomaly_id": None, "fallback": True}

    # Collect the first FC and first carrier — typical orders have one
    # shipment; split orders have more, and we grade the riskiest pairing by
    # just looking at the first (good enough for a demo).
    origin_fcs = []
    carriers = []
    for s in shipments:
        if s.fulfillment_center_id:
            fc = db.query(FulfillmentCenter).get(s.fulfillment_center_id)
            if fc and fc not in origin_fcs:
                origin_fcs.append(fc)
        if s.carrier_id:
            c = db.query(Carrier).get(s.carrier_id)
            if c and c not in carriers:
                carriers.append(c)

    if not origin_fcs or not carriers:
        return {"has_risk": False, "anomaly_id": None, "fallback": True}

    fc = origin_fcs[0]
    carrier = carriers[0]

    dest_city = order.recipient_city or ""
    dest_state = order.recipient_state or ""
    origin_city = fc.city or ""
    origin_state = fc.state or ""
    carrier_name = carrier.name or ""

    month_year = datetime.utcnow().strftime("%B %Y")

    dest_key = f"proactive:dest:{_slug(dest_state)}:{_slug(dest_city)}"
    carrier_key = f"proactive:carrier:{_slug(carrier_name)}"
    origin_key = f"proactive:origin:{_slug(fc.code or '')}"

    dest_query = f"severe weather OR wildfire OR flood OR storm {dest_city} {dest_state} {month_year}"
    carrier_query = f"{carrier_name} delivery delays OR strike OR outage OR disruption {month_year}"
    origin_query = f"airport OR highway closure OR severe weather {origin_city} {origin_state} {month_year}"

    # ── 1. Tavily grounding (cached) ─────────────────────────────────────
    try:
        dest_results = await get_or_fetch_tavily(db, dest_key, dest_query, max_results=5)
        carrier_results = await get_or_fetch_tavily(db, carrier_key, carrier_query, max_results=5)
        origin_results = await get_or_fetch_tavily(db, origin_key, origin_query, max_results=5)
    except Exception as exc:
        print(f"[proactive] tavily failed for order {order_id}: {exc}")
        return {"has_risk": False, "anomaly_id": None, "fallback": True}

    grounding_results: list[dict] = []
    for tag, qs, rs in (
        ("destination", dest_query, dest_results),
        ("carrier", carrier_query, carrier_results),
        ("origin", origin_query, origin_results),
    ):
        for hit in rs or []:
            grounding_results.append({"query_tag": tag, "query": qs, **hit})

    # ── 2. LLM risk assessment ───────────────────────────────────────────
    grounding_text = (
        json.dumps(grounding_results, indent=2)
        if grounding_results
        else "(no results — set has_risk=false)"
    )

    user_content = f"""ORDER
Order: {order.order_number}
Destination: {dest_city}, {dest_state}
Origin FC: {fc.code} ({origin_city}, {origin_state})
Carrier: {carrier_name}
Shipping tier: {order.shipping_tier}

WEB SEARCH RESULTS (Tavily — your only source of external information):
{grounding_text}
"""

    parsed: dict | None = None
    try:
        response = await _call_openai_with_retry(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            stream=False,
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        raw = response.choices[0].message.content or ""
        parsed = json.loads(raw)
    except Exception as exc:
        print(f"[proactive] LLM failed for order {order_id}: {exc}")
        try:
            log_agent_action(
                db=db,
                agent_name="proactive_risk",
                action_type="assessment_failed",
                entity_type="order",
                entity_id=order_id,
                input_summary=f"{len(grounding_results)} grounding sources",
                output_summary=f"LLM failed: {exc}",
                details=None,
                severity="warning",
            )
            db.commit()
        except Exception:
            db.rollback()
        return {"has_risk": False, "anomaly_id": None, "fallback": True}

    has_risk = bool(parsed.get("has_risk"))
    confidence = (parsed.get("confidence") or "low").lower()
    if confidence not in ("low", "medium", "high"):
        confidence = "low"

    # Hard guardrail: no grounding → no risk, period
    if not grounding_results:
        has_risk = False

    # Only medium/high confidence risks trigger a hold
    if not has_risk or confidence == "low":
        try:
            log_agent_action(
                db=db,
                agent_name="proactive_risk",
                action_type="no_risk",
                entity_type="order",
                entity_id=order_id,
                input_summary=(
                    f"dest={dest_city},{dest_state} carrier={carrier_name} "
                    f"origin={fc.code} grounding={len(grounding_results)}"
                ),
                output_summary=f"has_risk={has_risk} confidence={confidence} — no hold",
                details={"confidence": confidence},
                severity="info",
            )
            db.commit()
        except Exception:
            db.rollback()
        return {"has_risk": False, "anomaly_id": None, "fallback": False}

    # ── 3. Create the Anomaly + hold the order ──────────────────────────
    severity = (parsed.get("severity") or "medium").lower()
    if severity not in ("low", "medium", "high", "critical"):
        severity = "medium"

    grounding_urls = {g.get("url") for g in grounding_results if g.get("url")}
    raw_sources_used = parsed.get("sources_used") or []
    if isinstance(raw_sources_used, list):
        sources_used = [u for u in raw_sources_used if u in grounding_urls]
    else:
        sources_used = []

    anomaly = Anomaly(
        anomaly_type="proactive_route_risk",
        scope_type="order",
        scope_id=order.id,
        scope_label=order.order_number,
        severity=severity,
        affected_order_ids=[order.id],
        affected_count=1,
        # IMPORTANT: set to "diagnosed" so the notifier guard at
        # agents/notifier.py:53 lets draft_notifications_for_anomaly run.
        status="diagnosed",
        detection_summary=(
            f"Proactive risk detected on route {fc.code} → {dest_city},{dest_state} via {carrier_name}"
        ),
        detection_details={
            "destination": f"{dest_city}, {dest_state}",
            "origin_fc": fc.code,
            "carrier": carrier_name,
        },
        ai_grounding_queries=[dest_query, carrier_query, origin_query],
        ai_grounding_sources=grounding_results,
        ai_likely_cause=parsed.get("likely_cause") or "Potential route disruption",
        ai_detailed_reasoning=parsed.get("detailed_reasoning") or "",
        ai_evidence=parsed.get("evidence") or [],
        ai_confidence=confidence,
        ai_recommended_action=parsed.get("recommended_action") or "",
        ai_customer_impact=parsed.get("customer_impact") or "",
        ai_sources_used=sources_used,
        ai_investigated_at=datetime.now(timezone.utc),
    )
    db.add(anomaly)
    db.flush()

    order.on_hold = True
    order.hold_reason = "proactive_route_risk"
    order.hold_anomaly_id = anomaly.id
    db.commit()

    try:
        log_agent_action(
            db=db,
            agent_name="proactive_risk",
            action_type="risk_detected",
            entity_type="order",
            entity_id=order_id,
            input_summary=(
                f"dest={dest_city},{dest_state} carrier={carrier_name} origin={fc.code}"
            ),
            output_summary=f"{anomaly.ai_likely_cause} (confidence={confidence}, severity={severity})",
            details={
                "anomaly_id": anomaly.id,
                "confidence": confidence,
                "severity": severity,
                "sources_used": sources_used,
            },
            severity="warning",
        )
        db.commit()
    except Exception:
        db.rollback()

    # ── 4. Draft personalized customer notification ─────────────────────
    try:
        await draft_notifications_for_anomaly(db, anomaly.id)
    except Exception as exc:
        print(f"[proactive] notifier failed for anomaly {anomaly.id}: {exc}")
        # Notifier failures are tolerated — the anomaly still surfaces in
        # the command center, ops can re-investigate which will re-draft.

    return {"has_risk": True, "anomaly_id": anomaly.id, "fallback": False}
