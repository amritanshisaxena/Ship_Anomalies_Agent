"""Simulation routes — mock orders, force-advance queue (debug), reset DB."""

import random

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from agents.fulfillment import advance_queue, process_order
from database import ENGINE, SessionLocal, get_db, init_db
from models import Base

router = APIRouter(prefix="/api/simulate", tags=["simulation"])


@router.post("/mock-orders")
async def create_mock_orders(count: int = 5, brand_id: int = None, db: Session = Depends(get_db)):
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

        # Process through pipeline (async — runs narrator + proactive risk)
        pipeline_result = await process_order(db, order.id)

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
    """Force-advance top N orders through pick → pack → ship by priority.

    Debug/demo button only — normal order flow is handled by the auto-advance
    background loop in agents/background.py. This endpoint does NOT skip
    held orders, so ops can use it to verify hold behavior.
    """
    results = advance_queue(db, count, skip_holds=False)
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
