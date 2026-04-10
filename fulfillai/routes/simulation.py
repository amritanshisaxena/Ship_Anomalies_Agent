"""Simulation routes — mock orders, advance queue, reset DB, disruption events."""

import random
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from agents.base import log_agent_action
from agents.fulfillment import advance_queue, process_order
from database import ENGINE, SessionLocal, get_db, init_db
from models import Base

router = APIRouter(prefix="/api/simulate", tags=["simulation"])


@router.post("/mock-orders")
def create_mock_orders(count: int = 5, brand_id: int = None, db: Session = Depends(get_db)):
    """Generate random orders for a brand and process them through the pipeline."""
    from models import Brand, Order, OrderItem, Product

    if brand_id:
        brands = db.query(Brand).filter(Brand.id == brand_id, Brand.status == "active").all()
    else:
        brands = db.query(Brand).filter(Brand.status == "active").all()

    if not brands:
        return {"error": "No active brands found. Onboard a brand first."}

    cities = [
        ("New York", "NY"), ("Los Angeles", "CA"), ("Chicago", "IL"),
        ("Houston", "TX"), ("Phoenix", "AZ"), ("Miami", "FL"),
        ("Seattle", "WA"), ("Denver", "CO"), ("Atlanta", "GA"),
        ("Boston", "MA"), ("Portland", "OR"), ("Dallas", "TX"),
        ("San Francisco", "CA"), ("Philadelphia", "PA"), ("Minneapolis", "MN"),
    ]
    names = [
        "Alex Johnson", "Sam Rivera", "Jordan Lee", "Taylor Smith",
        "Casey Brown", "Morgan Davis", "Riley Wilson", "Quinn Thomas",
        "Avery Martinez", "Blake Anderson", "Drew Campbell", "Sage Cooper",
    ]
    tiers = ["standard", "standard", "standard", "express", "express", "overnight"]

    created = []
    max_id = db.query(func.max(Order.id)).scalar() or 0

    for i in range(count):
        brand = random.choice(brands)
        products = db.query(Product).filter(Product.brand_id == brand.id).all()
        if not products:
            continue

        max_id += 1
        city, state = random.choice(cities)
        tier = random.choice(tiers)
        is_vip = random.random() < 0.15  # 15% chance VIP

        order = Order(
            id=max_id,
            brand_id=brand.id,
            order_number=f"ORD-{max_id + 1000}",
            status="pending",
            recipient_name=random.choice(names),
            recipient_city=city,
            recipient_state=state,
            shipping_tier=tier,
            is_vip=is_vip,
        )
        db.add(order)
        db.flush()

        selected = random.sample(products, min(random.randint(1, 3), len(products)))
        for product in selected:
            db.add(OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=random.randint(1, 4),
                unit_price=product.price,
            ))

        db.commit()

        # Process through pipeline
        pipeline_result = process_order(db, order.id)

        created.append({
            "order_id": order.id,
            "order_number": order.order_number,
            "brand": brand.name,
            "tier": tier,
            "vip": is_vip,
            "status": pipeline_result.get("final_status", "unknown"),
            "priority": pipeline_result.get("priority_score", 0),
        })

    return {"created": len(created), "orders": created}


@router.post("/advance-queue")
def advance(count: int = 5, db: Session = Depends(get_db)):
    """Advance top N orders through pick → pack → ship by priority."""
    results = advance_queue(db, count)
    return {"advanced": len(results), "details": results}


@router.post("/reset")
def reset_database():
    """Drop all tables and re-seed infrastructure."""
    from seed import seed_if_empty

    Base.metadata.drop_all(ENGINE)
    init_db()
    session = SessionLocal()
    try:
        seed_if_empty(session)
    finally:
        session.close()

    return {"status": "reset", "message": "Database reset — FCs and carriers seeded, everything else empty"}


@router.post("/disruption")
def simulate_disruption(payload: dict = Body(...), db: Session = Depends(get_db)):
    """
    Inject a delay event affecting shipments by FC, carrier, or region.
    Body: {scope: 'fc'|'carrier'|'region', target: str, event: str}
      - fc target: FC code (e.g. 'LAX') or id
      - carrier target: carrier name or id
      - region target: 'east'|'central'|'west' or US state code
      - event: free-form label used for the shipment event message
    """
    from models import Carrier, FulfillmentCenter, Order, Shipment, ShipmentEvent

    scope = (payload.get("scope") or "").lower()
    target = str(payload.get("target") or "").strip()
    event = (payload.get("event") or "Unknown disruption").strip()

    if scope not in ("fc", "carrier", "region") or not target:
        return {"error": "scope must be fc|carrier|region and target is required"}

    from agents.monitor import _STATE_REGION

    query = db.query(Shipment).filter(Shipment.status.notin_(["shipped", "delivered"]))
    scope_label = target

    if scope == "fc":
        fc = None
        if target.isdigit():
            fc = db.query(FulfillmentCenter).get(int(target))
        if not fc:
            fc = db.query(FulfillmentCenter).filter(
                func.lower(FulfillmentCenter.code) == target.lower()
            ).first()
        if not fc:
            return {"error": f"FC '{target}' not found"}
        scope_label = fc.code
        query = query.filter(Shipment.fulfillment_center_id == fc.id)

    elif scope == "carrier":
        carrier = None
        if target.isdigit():
            carrier = db.query(Carrier).get(int(target))
        if not carrier:
            carrier = db.query(Carrier).filter(
                func.lower(Carrier.name) == target.lower()
            ).first()
        if not carrier:
            return {"error": f"Carrier '{target}' not found"}
        scope_label = carrier.name
        query = query.filter(Shipment.carrier_id == carrier.id)

    elif scope == "region":
        region = target.lower()
        if len(target) == 2:
            region = _STATE_REGION.get(target.upper(), target.lower())
        matching_states = [s for s, r in _STATE_REGION.items() if r == region]
        if not matching_states:
            return {"error": f"Region '{target}' not recognized"}
        scope_label = f"{region} region"
        order_ids = [
            o.id for o in db.query(Order).filter(Order.recipient_state.in_(matching_states)).all()
        ]
        if not order_ids:
            return {"affected": 0, "message": f"No active orders destined for {scope_label}"}
        query = query.filter(Shipment.order_id.in_(order_ids))

    shipments = query.all()
    if not shipments:
        return {"affected": 0, "message": f"No active shipments match {scope}={scope_label}"}

    now = datetime.now(timezone.utc)
    past = now - timedelta(days=2)

    for s in shipments:
        s.estimated_delivery = past
        db.add(
            ShipmentEvent(
                shipment_id=s.id,
                status="delayed",
                message=f"Delayed — {event}",
                location=scope_label,
            )
        )

    log_agent_action(
        db,
        agent_name="simulation",
        action_type="disruption_injected",
        entity_type="simulation",
        entity_id=0,
        input_summary=f"scope={scope} target={scope_label} event={event}",
        output_summary=f"{len(shipments)} shipments marked delayed",
        details={"scope": scope, "target": scope_label, "event": event, "count": len(shipments)},
        severity="warning",
    )
    db.commit()

    return {
        "affected": len(shipments),
        "scope": scope,
        "target": scope_label,
        "event": event,
        "message": f"Injected disruption: {len(shipments)} shipments delayed ({scope_label} — {event})",
    }
