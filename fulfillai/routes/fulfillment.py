"""Fulfillment routes — process orders through the pipeline, advance queue."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from agents.fulfillment import advance_queue, process_order
from database import get_db

router = APIRouter(prefix="/api/fulfillment", tags=["fulfillment"])


@router.post("/process/{order_id}")
def process(order_id: int, db: Session = Depends(get_db)):
    """Run the full 6-step agent pipeline on a pending order."""
    return process_order(db, order_id)


@router.post("/advance-queue")
def advance(count: int = 5, db: Session = Depends(get_db)):
    """Advance top N orders in the queue by priority (pick/pack/ship)."""
    results = advance_queue(db, count)
    return {"advanced": len(results), "details": results}


@router.get("/centers")
def list_centers(db: Session = Depends(get_db)):
    from models import FulfillmentCenter, Inventory, Product

    fcs = db.query(FulfillmentCenter).all()
    result = []
    for fc in fcs:
        inv_rows = db.query(Inventory).filter(Inventory.fulfillment_center_id == fc.id).all()
        total_onhand = sum(r.onhand_qty for r in inv_rows)
        total_fulfillable = sum(r.fulfillable_qty for r in inv_rows)

        products = []
        for inv in inv_rows:
            product = db.query(Product).get(inv.product_id)
            if product and (inv.onhand_qty > 0 or inv.fulfillable_qty > 0 or inv.reserved_qty > 0):
                products.append({
                    "sku": product.sku,
                    "name": product.name,
                    "onhand": inv.onhand_qty,
                    "fulfillable": inv.fulfillable_qty,
                    "reserved": inv.reserved_qty,
                })

        result.append({
            "id": fc.id,
            "name": fc.name,
            "code": fc.code,
            "city": fc.city,
            "state": fc.state,
            "region": fc.region,
            "total_onhand": total_onhand,
            "total_fulfillable": total_fulfillable,
            "products": products,
        })
    return {"centers": result}


@router.get("/queue")
def get_queue(db: Session = Depends(get_db)):
    """Get all orders currently in the processing queue, sorted by priority."""
    from models import Order

    orders = (
        db.query(Order)
        .filter(Order.status.in_(["queued", "picking", "packing"]))
        .order_by(Order.priority_score.desc())
        .all()
    )
    return {
        "queue": [
            {
                "id": o.id,
                "order_number": o.order_number,
                "status": o.status,
                "priority_score": o.priority_score,
                "queue_position": o.queue_position,
                "shipping_tier": o.shipping_tier,
                "is_vip": o.is_vip,
                "recipient_city": o.recipient_city,
                "recipient_state": o.recipient_state,
            }
            for o in orders
        ]
    }
