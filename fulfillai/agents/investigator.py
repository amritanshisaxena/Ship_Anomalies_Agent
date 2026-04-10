"""
AI Investigator — LLM call #1 of the anomaly workflow.

Given a detected anomaly, gathers:
  1. Internal pipeline facts (affected shipments, events, origin FC, destination)
  2. Real web search results from Tavily (weather, news, carrier status)

…and asks gpt-4o to diagnose the most likely root cause. The system prompt
strictly forbids inventing events not supported by the Tavily results. If
grounding sources are empty or irrelevant, the LLM must return confidence=low.

Every evidence bullet must cite either an internal fact (internal:...) or a
specific Tavily URL (source:https://...). The UI surfaces these citations so
ops can click through to verify the grounding.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from agents.base import _call_openai_with_retry, log_agent_action
from agents.tavily_client import build_grounding_queries, tavily_search


async def investigate_anomaly(db: Session, anomaly_id: int, ops_context: str | None = None) -> dict:
    """
    Run the full grounded investigation for a single anomaly.

    Updates the Anomaly row in place:
      - ai_grounding_queries / ai_grounding_sources from Tavily
      - ai_likely_cause / ai_detailed_reasoning / ai_evidence / ai_confidence
      - ai_recommended_action / ai_customer_impact / ai_sources_used
      - status transitions: investigating → diagnosed
    Returns the parsed diagnosis dict.
    Raises on API failure after retries — caller handles rollback.
    """
    from models import Anomaly, Carrier, FulfillmentCenter, Order, Shipment, ShipmentEvent

    anomaly = db.query(Anomaly).get(anomaly_id)
    if not anomaly:
        raise ValueError(f"Anomaly {anomaly_id} not found")

    anomaly.status = "investigating"
    if ops_context is not None:
        anomaly.ops_context = ops_context
    db.commit()

    # ── 1. Gather internal pipeline facts ────────────────────────────────
    fc = None
    carrier = None
    if anomaly.scope_type == "fc" and anomaly.scope_id:
        fc = db.query(FulfillmentCenter).get(anomaly.scope_id)
    if anomaly.scope_type == "carrier" and anomaly.scope_id:
        carrier = db.query(Carrier).get(anomaly.scope_id)

    order_ids = anomaly.affected_order_ids or []
    shipment_facts = []
    for oid in order_ids[:20]:  # cap so the prompt stays reasonable
        order = db.query(Order).get(oid)
        if not order:
            continue
        shipments = db.query(Shipment).filter(Shipment.order_id == oid).all()
        for s in shipments:
            events = (
                db.query(ShipmentEvent)
                .filter(ShipmentEvent.shipment_id == s.id)
                .order_by(ShipmentEvent.occurred_at)
                .all()
            )
            ship_fc = db.query(FulfillmentCenter).get(s.fulfillment_center_id) if s.fulfillment_center_id else None
            ship_carrier = db.query(Carrier).get(s.carrier_id) if s.carrier_id else None
            shipment_facts.append(
                {
                    "order_number": order.order_number,
                    "destination": f"{order.recipient_city}, {order.recipient_state}",
                    "shipping_tier": order.shipping_tier,
                    "status": s.status,
                    "origin_fc": ship_fc.code if ship_fc else None,
                    "origin_fc_city": ship_fc.city if ship_fc else None,
                    "origin_fc_state": ship_fc.state if ship_fc else None,
                    "carrier": ship_carrier.name if ship_carrier else None,
                    "carrier_service": s.carrier_service,
                    "tracking": s.tracking_number,
                    "estimated_delivery": s.estimated_delivery.isoformat() if s.estimated_delivery else None,
                    "order_created_at": order.created_at.isoformat() if order.created_at else None,
                    "events": [
                        {
                            "status": e.status,
                            "message": e.message,
                            "location": e.location,
                            "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
                        }
                        for e in events
                    ],
                }
            )

    # ── 2. Tavily grounding ──────────────────────────────────────────────
    queries = build_grounding_queries(anomaly, fc=fc, carrier=carrier)
    grounding_results: list[dict] = []
    for q in queries:
        for hit in await tavily_search(q, max_results=5):
            grounding_results.append({"query": q, **hit})

    anomaly.ai_grounding_queries = queries
    anomaly.ai_grounding_sources = grounding_results
    db.commit()

    # ── 3. Build LLM prompt ──────────────────────────────────────────────
    system_prompt = """You are a senior fulfillment operations analyst investigating a detected anomaly in a 3PL order pipeline.

STRICT RULES — follow exactly:
1. Base your diagnosis ONLY on (a) the internal pipeline facts and (b) the web search results provided below. Do NOT invent weather events, news events, natural disasters, strikes, outages, or carrier issues that are not supported by the search results.
2. Every item in the "evidence" array MUST cite its source. Use the prefix "internal:" for pipeline-data observations (e.g. "internal: 5 shipments all originate from FC-LAX") or "source:<url>" for search-result claims (e.g. "source:https://example.com/news-article").
3. If the web search results do NOT clearly support a specific external cause (empty, irrelevant, or inconclusive), you MUST set confidence="low" and include the phrase "insufficient external evidence" in detailed_reasoning. In that case, still propose the most likely INTERNAL cause (FC overload, inventory imbalance, carrier service quality, process issue) based only on the pipeline data — but label confidence low.
4. "sources_used" must list the exact URLs from the search results you actually cited. Empty list is valid if you cited nothing external.
5. Never fabricate a URL. Every URL in sources_used must appear verbatim in the provided search results.
6. If the ops-provided context contradicts the search results, prefer the search results but mention the discrepancy.

Respond ONLY with valid JSON matching this schema:
{
  "likely_cause": "one concise sentence naming the most likely root cause",
  "detailed_reasoning": "2-4 sentence paragraph walking through your logic and what the search results did or did not show",
  "evidence": [
    {"bullet": "specific observation", "source": "internal:..." or "source:https://..."}
  ],
  "confidence": "low" | "medium" | "high",
  "recommended_action": "specific actionable next step for the operations team",
  "customer_impact": "brief description of how customers are affected and what they should be told",
  "sources_used": ["https://...", ...]
}
"""

    grounding_text = (
        json.dumps(grounding_results, indent=2)
        if grounding_results
        else "(no results — no external grounding available; you MUST set confidence=low)"
    )

    user_content = f"""ANOMALY
Scope: {anomaly.scope_label} ({anomaly.anomaly_type})
Severity: {anomaly.severity}
Affected orders: {anomaly.affected_count}
Detection summary: {anomaly.detection_summary}
Detection details: {json.dumps(anomaly.detection_details or {}, default=str)}

INTERNAL PIPELINE FACTS (affected shipments):
{json.dumps(shipment_facts, indent=2, default=str)}

WEB SEARCH RESULTS (from Tavily — your only source of external information):
{grounding_text}

OPS-PROVIDED CONTEXT (a human note, may be empty):
{ops_context or anomaly.ops_context or "(none)"}
"""

    response = await _call_openai_with_retry(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        stream=False,
        response_format={"type": "json_object"},
        temperature=0.2,
    )

    raw = response.choices[0].message.content
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        anomaly.status = "detected"
        db.commit()
        raise RuntimeError(f"Investigator returned invalid JSON: {exc}") from exc

    # ── 4. Normalize + validate sources ──────────────────────────────────
    grounding_urls = {g.get("url") for g in grounding_results if g.get("url")}
    raw_sources_used = parsed.get("sources_used") or []
    if isinstance(raw_sources_used, list):
        sources_used = [u for u in raw_sources_used if u in grounding_urls]
    else:
        sources_used = []

    confidence = (parsed.get("confidence") or "low").lower()
    if confidence not in ("low", "medium", "high"):
        confidence = "low"
    if not grounding_results and confidence != "low":
        # Hard guardrail — if there's nothing to ground on, force low confidence
        confidence = "low"

    anomaly.ai_likely_cause = parsed.get("likely_cause") or "Unknown cause"
    anomaly.ai_detailed_reasoning = parsed.get("detailed_reasoning") or ""
    anomaly.ai_evidence = parsed.get("evidence") or []
    anomaly.ai_confidence = confidence
    anomaly.ai_recommended_action = parsed.get("recommended_action") or ""
    anomaly.ai_customer_impact = parsed.get("customer_impact") or ""
    anomaly.ai_sources_used = sources_used
    anomaly.ai_investigated_at = datetime.now(timezone.utc)
    anomaly.status = "diagnosed"
    db.commit()

    log_agent_action(
        db,
        agent_name="investigator",
        action_type="ai_diagnosis",
        entity_type="anomaly",
        entity_id=anomaly_id,
        input_summary=(
            f"{anomaly.scope_label}: {anomaly.affected_count} orders, "
            f"{len(grounding_results)} grounding sources from {len(queries)} queries"
        ),
        output_summary=f"{anomaly.ai_likely_cause} (confidence: {confidence})",
        details={
            "likely_cause": anomaly.ai_likely_cause,
            "confidence": confidence,
            "evidence_count": len(anomaly.ai_evidence or []),
            "sources_used": sources_used,
        },
        severity="warning" if confidence == "low" else "info",
    )
    db.commit()

    return {
        "likely_cause": anomaly.ai_likely_cause,
        "detailed_reasoning": anomaly.ai_detailed_reasoning,
        "evidence": anomaly.ai_evidence,
        "confidence": confidence,
        "recommended_action": anomaly.ai_recommended_action,
        "customer_impact": anomaly.ai_customer_impact,
        "sources_used": sources_used,
    }
