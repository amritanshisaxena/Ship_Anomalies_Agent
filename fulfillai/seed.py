"""Seed infrastructure data only — FCs and carriers. Brands/products/orders start empty."""

from sqlalchemy.orm import Session

from models import Carrier, FulfillmentCenter


def seed_if_empty(session: Session):
    if session.query(FulfillmentCenter).first():
        return

    # ── Fulfillment Centers ──────────────────────────────────────────────────
    session.add_all([
        FulfillmentCenter(id=1, name="NYC Fulfillment Center", code="NYC-FC", city="New York", state="NY", region="east"),
        FulfillmentCenter(id=2, name="Dallas Fulfillment Center", code="DAL-FC", city="Dallas", state="TX", region="central"),
        FulfillmentCenter(id=3, name="Chicago Fulfillment Center", code="CHI-FC", city="Chicago", state="IL", region="central"),
        FulfillmentCenter(id=4, name="LA Fulfillment Center", code="LA-FC", city="Los Angeles", state="CA", region="west"),
    ])

    # ── Carriers with service tiers ──────────────────────────────────────────
    # cost_per_lb is base rate; actual cost factors in distance multiplier
    session.add_all([
        Carrier(id=1, name="UPS", code="ups", services=[
            {"name": "Ground", "speed_days": 5, "cost_per_lb": 0.50},
            {"name": "2-Day Air", "speed_days": 2, "cost_per_lb": 1.20},
            {"name": "Next Day Air", "speed_days": 1, "cost_per_lb": 2.50},
        ]),
        Carrier(id=2, name="FedEx", code="fedex", services=[
            {"name": "Ground", "speed_days": 5, "cost_per_lb": 0.55},
            {"name": "Express Saver", "speed_days": 3, "cost_per_lb": 1.00},
            {"name": "Priority Overnight", "speed_days": 1, "cost_per_lb": 2.80},
        ]),
        Carrier(id=3, name="USPS", code="usps", services=[
            {"name": "First-Class", "speed_days": 5, "cost_per_lb": 0.35},
            {"name": "Priority Mail", "speed_days": 3, "cost_per_lb": 0.75},
            {"name": "Priority Express", "speed_days": 2, "cost_per_lb": 1.60},
        ]),
    ])

    session.commit()
