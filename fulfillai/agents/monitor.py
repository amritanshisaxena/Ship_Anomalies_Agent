"""
Deterministic anomaly monitor — scans the DB for stuck/delayed shipments and
clusters them by fulfillment center, carrier, or destination region.

No AI, no Tavily — pure SQL + Python. Called by the background loop in
agents/background.py every 60 seconds.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from agents.base import log_agent_action

# Region mapping reused from the fulfillment pipeline
_STATE_REGION = {
    "NY": "east", "NJ": "east", "MA": "east", "CT": "east", "PA": "east",
    "VA": "east", "MD": "east", "NC": "east", "SC": "east", "GA": "east",
    "FL": "east", "ME": "east", "NH": "east", "VT": "east", "RI": "east",
    "DE": "east", "DC": "east", "WV": "east",
    "TX": "central", "IL": "central", "OH": "central", "MI": "central",
    "IN": "central", "WI": "central", "MN": "central", "MO": "central",
    "IA": "central", "KS": "central", "NE": "central", "OK": "central",
    "AR": "central", "LA": "central", "MS": "central", "AL": "central",
    "TN": "central", "KY": "central", "CO": "central", "ND": "central",
    "SD": "central",
    "CA": "west", "WA": "west", "OR": "west", "NV": "west", "AZ": "west",
    "UT": "west", "ID": "west", "MT": "west", "WY": "west", "NM": "west",
    "HI": "west", "AK": "west",
}

# Clusters must contain at least this many stuck shipments to be worth investigating
CLUSTER_MIN = 3

# An order is considered "stuck" if it's been processing longer than this
STUCK_AFTER_HOURS = 4

# Statuses of Anomaly that block creating a new one for the same scope (dedup)
OPEN_STATUSES = {"detected", "investigating", "diagnosed", "drafting", "pending_review"}


def run_monitor_scan(db: Session) -> list:
    """Find stuck shipments, cluster them, create Anomaly rows. Returns new anomalies."""
    from models import Anomaly, Carrier, FulfillmentCenter, Order, Shipment

    now = datetime.now(timezone.utc)
    stuck_threshold = now - timedelta(hours=STUCK_AFTER_HOURS)

    # Find all stuck shipments in a single pass
    all_shipments = (
        db.query(Shipment)
        .filter(Shipment.status.notin_(["shipped", "delivered"]))
        .all()
    )

    stuck_shipments = []
    for s in all_shipments:
        # Past estimated delivery
        if s.estimated_delivery:
            eta = s.estimated_delivery
            if eta.tzinfo is None:
                eta = eta.replace(tzinfo=timezone.utc)
            if eta < now:
                stuck_shipments.append(s)
                continue
        # Stuck in a warehouse stage too long
        if s.status in ("queued", "picking", "packing"):
            order = db.query(Order).get(s.order_id)
            if order and order.created_at:
                created = order.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created < stuck_threshold:
                    stuck_shipments.append(s)

    if not stuck_shipments:
        return []

    # Cluster into groups
    fc_groups = defaultdict(list)       # fc_id -> [shipments]
    carrier_groups = defaultdict(list)  # carrier_id -> [shipments]
    region_groups = defaultdict(list)   # region -> [shipments]

    for s in stuck_shipments:
        if s.fulfillment_center_id:
            fc_groups[s.fulfillment_center_id].append(s)
        if s.carrier_id:
            carrier_groups[s.carrier_id].append(s)

        order = db.query(Order).get(s.order_id)
        if order and order.recipient_state:
            region = _STATE_REGION.get(order.recipient_state, "unknown")
            region_groups[region].append(s)

    # Track which shipments are already covered by a cluster so we don't
    # double-count them as single-order anomalies
    clustered_shipment_ids = set()
    new_anomalies = []

    # ── FC clusters ──
    for fc_id, ships in fc_groups.items():
        if len(ships) < CLUSTER_MIN:
            continue
        if _has_open_anomaly(db, "fc", fc_id):
            for s in ships:
                clustered_shipment_ids.add(s.id)
            continue
        fc = db.query(FulfillmentCenter).get(fc_id)
        scope_label = fc.code if fc else f"FC-{fc_id}"
        order_ids = sorted({s.order_id for s in ships})
        anomaly = _create_anomaly(
            db,
            anomaly_type="fc_issue",
            scope_type="fc",
            scope_id=fc_id,
            scope_label=scope_label,
            severity="high",
            affected_order_ids=order_ids,
            detection_summary=f"{len(order_ids)} stuck shipments originating from {scope_label}",
            detection_details={
                "fc_city": fc.city if fc else None,
                "fc_state": fc.state if fc else None,
                "fc_region": fc.region if fc else None,
                "shipment_ids": [s.id for s in ships],
            },
        )
        new_anomalies.append(anomaly)
        for s in ships:
            clustered_shipment_ids.add(s.id)

    # ── Carrier clusters ──
    for carrier_id, ships in carrier_groups.items():
        # Skip if all these shipments are already in an FC cluster
        remaining = [s for s in ships if s.id not in clustered_shipment_ids]
        if len(remaining) < CLUSTER_MIN:
            for s in ships:
                clustered_shipment_ids.add(s.id)
            continue
        if _has_open_anomaly(db, "carrier", carrier_id):
            for s in ships:
                clustered_shipment_ids.add(s.id)
            continue
        carrier = db.query(Carrier).get(carrier_id)
        scope_label = carrier.name if carrier else f"Carrier-{carrier_id}"
        order_ids = sorted({s.order_id for s in remaining})
        anomaly = _create_anomaly(
            db,
            anomaly_type="carrier_issue",
            scope_type="carrier",
            scope_id=carrier_id,
            scope_label=scope_label,
            severity="high",
            affected_order_ids=order_ids,
            detection_summary=f"{len(order_ids)} stuck shipments using carrier {scope_label}",
            detection_details={
                "carrier_name": carrier.name if carrier else None,
                "shipment_ids": [s.id for s in remaining],
            },
        )
        new_anomalies.append(anomaly)
        for s in ships:
            clustered_shipment_ids.add(s.id)

    # ── Region clusters ──
    for region, ships in region_groups.items():
        if region == "unknown":
            continue
        remaining = [s for s in ships if s.id not in clustered_shipment_ids]
        if len(remaining) < CLUSTER_MIN:
            continue
        if _has_open_anomaly(db, "region", None, scope_label=f"{region} region"):
            for s in ships:
                clustered_shipment_ids.add(s.id)
            continue
        order_ids = sorted({s.order_id for s in remaining})
        anomaly = _create_anomaly(
            db,
            anomaly_type="cluster_delay",
            scope_type="region",
            scope_id=None,
            scope_label=f"{region} region",
            severity="medium",
            affected_order_ids=order_ids,
            detection_summary=f"{len(order_ids)} stuck shipments destined for the {region} region",
            detection_details={
                "region": region,
                "shipment_ids": [s.id for s in remaining],
            },
        )
        new_anomalies.append(anomaly)
        for s in remaining:
            clustered_shipment_ids.add(s.id)

    # ── Individual stuck orders (not part of any cluster) ──
    for s in stuck_shipments:
        if s.id in clustered_shipment_ids:
            continue
        order = db.query(Order).get(s.order_id)
        if not order:
            continue
        if _has_open_anomaly(db, "order", order.id):
            continue
        anomaly = _create_anomaly(
            db,
            anomaly_type="single_stuck",
            scope_type="order",
            scope_id=order.id,
            scope_label=order.order_number,
            severity="medium",
            affected_order_ids=[order.id],
            detection_summary=f"Order {order.order_number} stuck (status: {s.status}, past ETA)",
            detection_details={
                "shipment_id": s.id,
                "shipment_status": s.status,
                "destination": f"{order.recipient_city}, {order.recipient_state}",
            },
        )
        new_anomalies.append(anomaly)

    if new_anomalies:
        db.commit()

    return new_anomalies


def _has_open_anomaly(
    db: Session,
    scope_type: str,
    scope_id: int | None,
    scope_label: str | None = None,
) -> bool:
    """Return True if an anomaly with this scope already exists in an open state."""
    from models import Anomaly

    query = db.query(Anomaly).filter(
        Anomaly.scope_type == scope_type,
        Anomaly.status.in_(OPEN_STATUSES),
    )
    if scope_id is not None:
        query = query.filter(Anomaly.scope_id == scope_id)
    if scope_label is not None:
        query = query.filter(Anomaly.scope_label == scope_label)
    return db.query(query.exists()).scalar()


def _create_anomaly(
    db: Session,
    *,
    anomaly_type: str,
    scope_type: str,
    scope_id: int | None,
    scope_label: str,
    severity: str,
    affected_order_ids: list,
    detection_summary: str,
    detection_details: dict,
):
    from models import Anomaly

    anomaly = Anomaly(
        anomaly_type=anomaly_type,
        scope_type=scope_type,
        scope_id=scope_id,
        scope_label=scope_label,
        severity=severity,
        affected_order_ids=affected_order_ids,
        affected_count=len(affected_order_ids),
        detection_summary=detection_summary,
        detection_details=detection_details,
        status="detected",
    )
    db.add(anomaly)
    db.flush()

    log_agent_action(
        db,
        agent_name="monitor",
        action_type="anomaly_detected",
        entity_type="anomaly",
        entity_id=anomaly.id,
        input_summary=f"Scan found {len(affected_order_ids)} stuck orders",
        output_summary=f"Detected {anomaly_type} — {scope_label} ({severity})",
        details={
            "scope_type": scope_type,
            "scope_label": scope_label,
            "affected_count": len(affected_order_ids),
            "summary": detection_summary,
        },
        severity="warning" if severity in ("high", "critical") else "info",
    )
    return anomaly
