"""Storefront routes — brand catalog and checkout."""

from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db

router = APIRouter(prefix="/api/storefront", tags=["storefront"])


class CartItem(BaseModel):
    product_id: int
    quantity: int = 1


class CheckoutRequest(BaseModel):
    brand_id: int
    items: List[CartItem]
    recipient_name: str
    recipient_city: str
    recipient_state: str
    shipping_tier: str = "standard"  # standard / express / overnight
    is_vip: bool = False


@router.get("/catalog/{brand_id}")
def get_catalog(brand_id: int, db: Session = Depends(get_db)):
    """Get brand's product catalog with total available stock."""
    from models import Brand, Inventory, Product

    brand = db.query(Brand).get(brand_id)
    if not brand:
        return {"error": "Brand not found"}
    if brand.status != "active":
        return {"error": f"Brand is not active (status: {brand.status})"}

    products = db.query(Product).filter(Product.brand_id == brand_id).all()
    catalog = []
    for p in products:
        total_stock = (
            db.query(func.sum(Inventory.fulfillable_qty))
            .filter(Inventory.product_id == p.id)
            .scalar() or 0
        )
        catalog.append({
            "id": p.id,
            "sku": p.sku,
            "name": p.name,
            "category": p.category,
            "weight_oz": p.weight_oz,
            "price": p.price,
            "in_stock": total_stock > 0,
            "stock": total_stock,
        })

    return {
        "brand": {"id": brand.id, "name": brand.name, "platform": brand.platform},
        "products": catalog,
    }


@router.post("/checkout")
async def checkout(req: CheckoutRequest, db: Session = Depends(get_db)):
    """Place an order and immediately run it through the agent pipeline."""
    from models import Brand, Order, OrderItem, Product
    from agents.fulfillment import process_order

    brand = db.query(Brand).get(req.brand_id)
    if not brand or brand.status != "active":
        return {"error": "Brand not active"}

    # Generate order number
    max_id = db.query(func.max(Order.id)).scalar() or 0
    order_number = f"ORD-{max_id + 1001}"

    order = Order(
        brand_id=req.brand_id,
        order_number=order_number,
        status="pending",
        recipient_name=req.recipient_name,
        recipient_city=req.recipient_city,
        recipient_state=req.recipient_state,
        shipping_tier=req.shipping_tier,
        is_vip=req.is_vip,
    )
    db.add(order)
    db.flush()

    for cart_item in req.items:
        product = db.query(Product).get(cart_item.product_id)
        if product:
            db.add(OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=cart_item.quantity,
                unit_price=product.price,
            ))

    db.commit()

    # Run the agent pipeline immediately
    pipeline_result = await process_order(db, order.id)

    return {
        "order_id": order.id,
        "order_number": order_number,
        "pipeline": pipeline_result,
    }
