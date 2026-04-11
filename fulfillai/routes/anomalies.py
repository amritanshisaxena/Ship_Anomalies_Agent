"""
Anomaly review routes — human-in-the-loop review of the autonomous AI workflow.

Ops does NOT trigger investigation or drafting — those run automatically in
the background loop (agents/background.py). Ops only reviews what's already
pre-baked and decides what to do:
  - approve a whole anomaly + send every drafted notification
  - reject the whole anomaly, nothing gets sent
  - re-investigate with human-provided context if the AI got it wrong
  - per-notification tweaks: edit, approve, reject a single draft

Also exposes POST /api/anomalies/scan-now for instant demo-triggered cycles.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from agents.background import run_one_cycle
from agents.base import log_agent_action
from agents.investigator import investigate_anomaly
from agents.notifier import draft_notifications_for_anomaly
from database import get_db

router = APIRouter(prefix="/api", tags=["anomalies"])


# ─────────────────────────────────────────────────────────────────────────
# Serialization helpers
# ─────────────────────────────────────────────────────────────────────────

def _serialize_notification(n) -> dict:
    return {
        "id": n.id,
        "order_id": n.order_id,
        "anomaly_id": n.anomaly_id,
        "recipient_name": n.recipient_name,
        "subject": n.subject,
        "body": n.body,
        "status": n.status,
        "is_fallback": bool(n.is_fallback),
        "generated_at": n.generated_at.isoformat() if n.generated_at else None,
        "approved_at": n.approved_at.isoformat() if n.approved_at else None,
        "sent_at": n.sent_at.isoformat() if n.sent_at else None,
    }


def _serialize_anomaly(a, include_detail: bool = False, db: Optional[Session] = None) -> dict:
    """Summary view by default; full detail (drafts, sources, orders) when asked."""
    from models import Notification, Order

    data = {
        "id": a.id,
        "anomaly_type": a.anomaly_type,
        "scope_type": a.scope_type,
        "scope_id": a.scope_id,
        "scope_label": a.scope_label,
        "severity": a.severity,
        "status": a.status,
        "detected_at": a.detected_at.isoformat() if a.detected_at else None,
        "affected_count": a.affected_count,
        "affected_order_ids": a.affected_order_ids or [],
        "detection_summary": a.detection_summary,
        "ai_likely_cause": a.ai_likely_cause,
        "ai_confidence": a.ai_confidence,
        "ai_investigated_at": a.ai_investigated_at.isoformat() if a.ai_investigated_at else None,
        "reviewed_at": a.reviewed_at.isoformat() if a.reviewed_at else None,
        "review_action": a.review_action,
    }

    if not include_detail:
        return data

    data.update(
        {
            "detection_details": a.detection_details or {},
            "ops_context": a.ops_context,
            "ai_detailed_reasoning": a.ai_detailed_reasoning,
            "ai_evidence": a.ai_evidence or [],
            "ai_recommended_action": a.ai_recommended_action,
            "ai_customer_impact": a.ai_customer_impact,
            "ai_sources_used": a.ai_sources_used or [],
            "ai_grounding_queries": a.ai_grounding_queries or [],
            "ai_grounding_sources": a.ai_grounding_sources or [],
        }
    )

    if db is not None:
        # Draft + approved + sent + rejected notifications for this anomaly
        notifs = (
            db.query(Notification)
            .filter(Notification.anomaly_id == a.id)
            .order_by(Notification.id)
            .all()
        )
        data["notifications"] = [_serialize_notification(n) for n in notifs]

        # Affected orders — lightweight summary
        order_ids = a.affected_order_ids or []
        affected = []
        for oid in order_ids:
            o = db.query(Order).get(oid)
            if not o:
                continue
            affected.append(
                {
                    "id": o.id,
                    "order_number": o.order_number,
                    "status": o.status,
                    "recipient_name": o.recipient_name,
                    "destination": f"{o.recipient_city}, {o.recipient_state}",
                    "shipping_tier": o.shipping_tier,
                }
            )
        data["affected_orders"] = affected

    return data


# ─────────────────────────────────────────────────────────────────────────
# Anomaly list + detail
# ─────────────────────────────────────────────────────────────────────────

@router.get("/anomalies")
def list_anomalies(status: Optional[str] = None, db: Session = Depends(get_db)):
    """
    List anomalies. Default filter: everything that ops can currently act on
    (pending_review). Pass ?status=all for everything, or a specific status.
    """
    from models import Anomaly

    query = db.query(Anomaly)
    if status is None or status == "pending_review":
        query = query.filter(Anomaly.status == "pending_review")
    elif status == "open":
        query = query.filter(
            Anomaly.status.in_(
                ["detected", "investigating", "diagnosed", "drafting", "pending_review"]
            )
        )
    elif status != "all":
        query = query.filter(Anomaly.status == status)

    rows = query.order_by(Anomaly.detected_at.desc()).all()

    # Also include a small counts map so the UI can render filter badges
    all_rows = db.query(Anomaly).all()
    counts = {"total": len(all_rows)}
    for a in all_rows:
        counts[a.status] = counts.get(a.status, 0) + 1

    return {
        "anomalies": [_serialize_anomaly(a) for a in rows],
        "counts": counts,
    }


@router.get("/anomalies/{anomaly_id}")
def get_anomaly(anomaly_id: int, db: Session = Depends(get_db)):
    """Full anomaly detail including grounding sources, drafts, and affected orders."""
    from models import Anomaly

    anomaly = db.query(Anomaly).get(anomaly_id)
    if not anomaly:
        return {"error": "Anomaly not found"}
    return _serialize_anomaly(anomaly, include_detail=True, db=db)


# ─────────────────────────────────────────────────────────────────────────
# Manual scan trigger (demo convenience)
# ─────────────────────────────────────────────────────────────────────────

@router.post("/anomalies/scan-now")
async def scan_now():
    """
    Manually run one full monitor → investigate → draft cycle immediately,
    instead of waiting up to 60 seconds for the background loop.
    """
    summary = await run_one_cycle()
    return summary


# ─────────────────────────────────────────────────────────────────────────
# Anomaly-level review actions
# ─────────────────────────────────────────────────────────────────────────

HOLD_ANOMALY_TYPES = ("proactive_route_risk", "split_shipment_review", "backorder_review")


@router.post("/anomalies/{anomaly_id}/approve")
def approve_anomaly(anomaly_id: int, db: Session = Depends(get_db)):
    """Approve every draft notification and mark them all sent. Anomaly → resolved.

    For hold-gating anomaly types (proactive_route_risk, split_shipment_review,
    backorder_review), also releases the affected orders so the auto-advance
    loop can pick them up on its next tick.
    """
    from models import Anomaly, Notification, Order

    anomaly = db.query(Anomaly).get(anomaly_id)
    if not anomaly:
        return {"error": "Anomaly not found"}
    if anomaly.status not in ("pending_review", "diagnosed"):
        return {"error": f"Cannot approve anomaly in status '{anomaly.status}'"}

    drafts = (
        db.query(Notification)
        .filter(Notification.anomaly_id == anomaly_id, Notification.status == "draft")
        .all()
    )

    now = datetime.now(timezone.utc)
    sent_count = 0
    for n in drafts:
        n.status = "sent"
        n.approved_at = now
        n.sent_at = now
        sent_count += 1

    anomaly.status = "resolved"
    anomaly.review_action = "approved"
    anomaly.reviewed_at = now

    # Release any held orders gated on this anomaly. Only releases orders
    # whose hold_anomaly_id actually points to us — never blindly clears
    # holds on unrelated orders.
    released_orders: list[str] = []
    if anomaly.anomaly_type in HOLD_ANOMALY_TYPES:
        for oid in anomaly.affected_order_ids or []:
            o = db.query(Order).get(oid)
            if o and o.hold_anomaly_id == anomaly.id:
                o.on_hold = False
                o.hold_reason = None
                o.hold_anomaly_id = None
                # Backorders never reach "queued" — they need manual reprocess.
                # Split and proactive-risk holds were parked at queued, so
                # clearing on_hold is enough for the advance loop to pick them up.
                released_orders.append(o.order_number)

    log_agent_action(
        db,
        agent_name="ops_review",
        action_type="anomaly_approved",
        entity_type="anomaly",
        entity_id=anomaly_id,
        input_summary=f"{anomaly.scope_label}: {len(drafts)} drafts",
        output_summary=(
            f"Approved — {sent_count} notifications sent"
            + (f", {len(released_orders)} order(s) released" if released_orders else "")
        ),
        details={
            "sent": sent_count,
            "scope": anomaly.scope_label,
            "released_orders": released_orders,
        },
        severity="info",
    )
    db.commit()

    return {
        "ok": True,
        "anomaly_id": anomaly_id,
        "status": "resolved",
        "sent": sent_count,
        "released_orders": released_orders,
    }


@router.post("/anomalies/{anomaly_id}/reject")
def reject_anomaly(anomaly_id: int, payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Reject the entire anomaly. Nothing is sent to customers.

    For hold-gating anomaly types, rejection is interpreted as "this is
    genuinely a problem, pull the order from the pipeline for manual
    handling" — the affected orders are flipped to 'exception' status and
    left on_hold=True so the auto-advance loop never touches them.
    """
    from models import Anomaly, Notification, Order

    anomaly = db.query(Anomaly).get(anomaly_id)
    if not anomaly:
        return {"error": "Anomaly not found"}
    if anomaly.status not in ("pending_review", "diagnosed"):
        return {"error": f"Cannot reject anomaly in status '{anomaly.status}'"}

    reason = (payload.get("reason") or "").strip()

    # Mark every pending draft as rejected so they don't linger
    drafts = (
        db.query(Notification)
        .filter(Notification.anomaly_id == anomaly_id, Notification.status == "draft")
        .all()
    )
    for n in drafts:
        n.status = "rejected"

    now = datetime.now(timezone.utc)
    anomaly.status = "rejected"
    anomaly.review_action = "rejected"
    anomaly.reviewed_at = now
    if reason:
        anomaly.ops_context = (anomaly.ops_context or "") + f"\n[Rejected] {reason}"

    # Flip gated orders to exception so ops can handle them offline.
    excepted_orders: list[str] = []
    if anomaly.anomaly_type in HOLD_ANOMALY_TYPES:
        for oid in anomaly.affected_order_ids or []:
            o = db.query(Order).get(oid)
            if o and o.hold_anomaly_id == anomaly.id:
                o.status = "exception"
                # Leave on_hold=True so the advance loop never picks it up
                excepted_orders.append(o.order_number)

    log_agent_action(
        db,
        agent_name="ops_review",
        action_type="anomaly_rejected",
        entity_type="anomaly",
        entity_id=anomaly_id,
        input_summary=f"{anomaly.scope_label}: {len(drafts)} drafts discarded",
        output_summary=(
            f"Rejected — no customer contact{' (' + reason + ')' if reason else ''}"
            + (f", {len(excepted_orders)} order(s) → exception" if excepted_orders else "")
        ),
        details={
            "reason": reason,
            "discarded": len(drafts),
            "excepted_orders": excepted_orders,
        },
        severity="warning",
    )
    db.commit()

    return {
        "ok": True,
        "anomaly_id": anomaly_id,
        "status": "rejected",
        "excepted_orders": excepted_orders,
    }


@router.post("/anomalies/{anomaly_id}/re-investigate")
async def re_investigate(anomaly_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    """
    Re-run the investigator (and notifier) with new ops-provided context.
    Typical use: ops thinks the AI's diagnosis is wrong and wants to nudge it
    with new info like "it's actually a carrier staffing shortage, not weather".
    """
    from models import Anomaly

    anomaly = db.query(Anomaly).get(anomaly_id)
    if not anomaly:
        return {"error": "Anomaly not found"}
    if anomaly.status in ("resolved",):
        return {"error": "Cannot re-investigate a resolved anomaly"}

    ops_context = (payload.get("ops_context") or "").strip()
    if not ops_context:
        return {"error": "ops_context is required"}

    log_agent_action(
        db,
        agent_name="ops_review",
        action_type="re_investigate_requested",
        entity_type="anomaly",
        entity_id=anomaly_id,
        input_summary=f"{anomaly.scope_label}: new context from ops",
        output_summary=ops_context[:160],
        details={"ops_context": ops_context},
        severity="info",
    )
    db.commit()

    # Investigator transitions investigating → diagnosed; notifier then
    # diagnosed → drafting → pending_review. Notifier also clears prior drafts.
    await investigate_anomaly(db, anomaly_id, ops_context=ops_context)
    await draft_notifications_for_anomaly(db, anomaly_id)

    anomaly = db.query(Anomaly).get(anomaly_id)
    return {
        "ok": True,
        "anomaly_id": anomaly_id,
        "status": anomaly.status,
        "likely_cause": anomaly.ai_likely_cause,
        "confidence": anomaly.ai_confidence,
    }


# ─────────────────────────────────────────────────────────────────────────
# Per-notification actions
# ─────────────────────────────────────────────────────────────────────────

@router.post("/notifications/{notif_id}/approve-send")
def approve_send_notification(notif_id: int, db: Session = Depends(get_db)):
    """Approve and send a single draft notification."""
    from models import Notification

    n = db.query(Notification).get(notif_id)
    if not n:
        return {"error": "Notification not found"}
    if n.status != "draft":
        return {"error": f"Cannot send notification in status '{n.status}'"}

    now = datetime.now(timezone.utc)
    n.status = "sent"
    n.approved_at = now
    n.sent_at = now

    log_agent_action(
        db,
        agent_name="ops_review",
        action_type="notification_sent",
        entity_type="notification",
        entity_id=notif_id,
        input_summary=f"Order notif for {n.recipient_name}",
        output_summary=f"Sent: {n.subject}",
        details={"anomaly_id": n.anomaly_id, "order_id": n.order_id},
        severity="info",
    )
    db.commit()

    return {"ok": True, "notification": _serialize_notification(n)}


@router.post("/notifications/{notif_id}/reject")
def reject_notification(notif_id: int, db: Session = Depends(get_db)):
    """Reject a single draft — it will not be sent."""
    from models import Notification

    n = db.query(Notification).get(notif_id)
    if not n:
        return {"error": "Notification not found"}
    if n.status != "draft":
        return {"error": f"Cannot reject notification in status '{n.status}'"}

    n.status = "rejected"

    log_agent_action(
        db,
        agent_name="ops_review",
        action_type="notification_rejected",
        entity_type="notification",
        entity_id=notif_id,
        input_summary=f"Draft for {n.recipient_name}",
        output_summary="Draft rejected — not sent",
        details={"anomaly_id": n.anomaly_id, "order_id": n.order_id},
        severity="warning",
    )
    db.commit()

    return {"ok": True, "notification": _serialize_notification(n)}


@router.patch("/notifications/{notif_id}")
def edit_notification(notif_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Edit a draft notification's subject and/or body before sending."""
    from models import Notification

    n = db.query(Notification).get(notif_id)
    if not n:
        return {"error": "Notification not found"}
    if n.status != "draft":
        return {"error": f"Cannot edit notification in status '{n.status}'"}

    subject = payload.get("subject")
    body = payload.get("body")
    if subject is not None:
        subject = str(subject).strip()
        if subject:
            n.subject = subject
    if body is not None:
        body = str(body).strip()
        if body:
            n.body = body
    # Once an ops person has edited a draft, it's no longer a raw LLM fallback
    if (subject or body) and n.is_fallback:
        n.is_fallback = False

    db.commit()
    return {"ok": True, "notification": _serialize_notification(n)}
