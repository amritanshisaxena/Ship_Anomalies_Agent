"""Order routes — list orders, get detail with full agent trace."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.get("/stats")
def order_stats(db: Session = Depends(get_db)):
    from models import Order

    orders = db.query(Order).all()
    counts = {}
    for o in orders:
        counts[o.status] = counts.get(o.status, 0) + 1
    return {"total": len(orders), "by_status": counts}


@router.get("")
def list_orders(status: str = None, brand_id: int = None, db: Session = Depends(get_db)):
    from models import Brand, Notification, Order, OrderItem, Product, Shipment

    query = db.query(Order)
    if status:
        query = query.filter(Order.status == status)
    if brand_id:
        query = query.filter(Order.brand_id == brand_id)

    orders = query.order_by(Order.created_at.desc()).all()
    result = []
    for o in orders:
        brand = db.query(Brand).get(o.brand_id)
        items = db.query(OrderItem).filter(OrderItem.order_id == o.id).all()
        shipments = db.query(Shipment).filter(Shipment.order_id == o.id).all()

        item_list = []
        for item in items:
            product = db.query(Product).get(item.product_id)
            item_list.append({
                "product_id": item.product_id,
                "sku": product.sku if product else "",
                "name": product.name if product else "",
                "quantity": item.quantity,
                "unit_price": item.unit_price,
            })

        shipment_list = []
        for s in shipments:
            shipment_list.append({
                "id": s.id,
                "status": s.status,
                "fc_id": s.fulfillment_center_id,
                "carrier_id": s.carrier_id,
                "carrier_service": s.carrier_service,
                "shipping_cost": s.shipping_cost,
                "tracking_number": s.tracking_number,
            })

        sent_notifs = (
            db.query(Notification)
            .filter(Notification.order_id == o.id, Notification.status == "sent")
            .order_by(Notification.sent_at.desc())
            .all()
        )
        notif_list = [
            {
                "id": n.id,
                "anomaly_id": n.anomaly_id,
                "subject": n.subject,
                "body": n.body,
                "sent_at": n.sent_at.isoformat() if n.sent_at else None,
            }
            for n in sent_notifs
        ]

        result.append({
            "id": o.id,
            "order_number": o.order_number,
            "brand_id": o.brand_id,
            "brand_name": brand.name if brand else "",
            "status": o.status,
            "shipping_tier": o.shipping_tier,
            "is_vip": o.is_vip,
            "priority_score": o.priority_score,
            "queue_position": o.queue_position,
            "recipient_name": o.recipient_name,
            "recipient_city": o.recipient_city,
            "recipient_state": o.recipient_state,
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "items": item_list,
            "shipments": shipment_list,
            "total": sum(i.quantity * (i.unit_price or 0) for i in items),
            "notifications": notif_list,
            "on_hold": bool(o.on_hold),
            "hold_reason": o.hold_reason,
        })

    return {"orders": result}


@router.get("/{order_id}")
def get_order(order_id: int, db: Session = Depends(get_db)):
    """Get order detail with full agent pipeline trace."""
    from models import (
        AgentAction,
        Brand,
        Carrier,
        FulfillmentCenter,
        Notification,
        Order,
        OrderItem,
        Product,
        Shipment,
        ShipmentEvent,
    )

    order = db.query(Order).get(order_id)
    if not order:
        return {"error": "Order not found"}

    brand = db.query(Brand).get(order.brand_id)
    items = db.query(OrderItem).filter(OrderItem.order_id == order.id).all()
    shipments = db.query(Shipment).filter(Shipment.order_id == order.id).all()

    # Get agent pipeline trace for this order
    agent_trace = (
        db.query(AgentAction)
        .filter(AgentAction.entity_type == "order", AgentAction.entity_id == order.id)
        .order_by(AgentAction.step_number, AgentAction.created_at)
        .all()
    )

    item_list = []
    for item in items:
        product = db.query(Product).get(item.product_id)
        item_list.append({
            "product_id": item.product_id,
            "sku": product.sku if product else "",
            "name": product.name if product else "",
            "quantity": item.quantity,
            "unit_price": item.unit_price,
        })

    shipment_list = []
    for s in shipments:
        fc = db.query(FulfillmentCenter).get(s.fulfillment_center_id) if s.fulfillment_center_id else None
        carrier = db.query(Carrier).get(s.carrier_id) if s.carrier_id else None
        events = db.query(ShipmentEvent).filter(ShipmentEvent.shipment_id == s.id).order_by(ShipmentEvent.occurred_at).all()
        shipment_list.append({
            "id": s.id,
            "status": s.status,
            "fc_code": fc.code if fc else None,
            "carrier": carrier.name if carrier else None,
            "carrier_service": s.carrier_service,
            "shipping_cost": s.shipping_cost,
            "tracking_number": s.tracking_number,
            "estimated_delivery": s.estimated_delivery.isoformat() if s.estimated_delivery else None,
            "events": [
                {"status": e.status, "message": e.message, "location": e.location,
                 "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None}
                for e in events
            ],
        })

    trace_list = [
        {
            "id": a.id,
            "step_number": a.step_number,
            "action_type": a.action_type,
            "input_summary": a.input_summary,
            "output_summary": a.output_summary,
            "details": a.details,
            "severity": a.severity,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in agent_trace
    ]

    # Sent anomaly notifications for this order — the customer portal renders
    # these as prominent banners on the My Orders page.
    sent_notifs = (
        db.query(Notification)
        .filter(Notification.order_id == order.id, Notification.status == "sent")
        .order_by(Notification.sent_at.desc())
        .all()
    )
    notifications_list = [
        {
            "id": n.id,
            "anomaly_id": n.anomaly_id,
            "subject": n.subject,
            "body": n.body,
            "sent_at": n.sent_at.isoformat() if n.sent_at else None,
        }
        for n in sent_notifs
    ]

    return {
        "id": order.id,
        "order_number": order.order_number,
        "brand": brand.name if brand else None,
        "brand_id": order.brand_id,
        "status": order.status,
        "shipping_tier": order.shipping_tier,
        "is_vip": order.is_vip,
        "priority_score": order.priority_score,
        "queue_position": order.queue_position,
        "recipient_name": order.recipient_name,
        "recipient_city": order.recipient_city,
        "recipient_state": order.recipient_state,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "items": item_list,
        "shipments": shipment_list,
        "total": sum(i.quantity * (i.unit_price or 0) for i in items),
        "agent_trace": trace_list,
        "notifications": notifications_list,
        "narrator_explanation": order.narrator_explanation,
        "narrator_is_fallback": bool(order.narrator_is_fallback),
        "on_hold": bool(order.on_hold),
        "hold_reason": order.hold_reason,
        "hold_anomaly_id": order.hold_anomaly_id,
    }
