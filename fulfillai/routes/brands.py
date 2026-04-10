"""Brand management — onboard brands, add products, distribute inventory."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional

from database import get_db

router = APIRouter(prefix="/api/brands", tags=["brands"])


# ── Pydantic models ──────────────────────────────────────────────────────────

class BrandCreate(BaseModel):
    name: str
    platform: str = "other"
    store_url: str = ""


class ProductCreate(BaseModel):
    name: str
    sku: str
    category: str = "general"
    weight_oz: float = 16
    price: float = 0


class InventorySet(BaseModel):
    product_id: int
    fulfillment_center_id: int
    onhand_qty: int = 0
    fulfillable_qty: int = 0


class BulkInventory(BaseModel):
    items: List[InventorySet]


# ── Brand CRUD ───────────────────────────────────────────────────────────────

@router.get("")
def list_brands(db: Session = Depends(get_db)):
    from models import Brand, Product

    brands = db.query(Brand).order_by(Brand.created_at.desc()).all()
    result = []
    for b in brands:
        product_count = db.query(Product).filter(Product.brand_id == b.id).count()
        result.append({
            "id": b.id,
            "name": b.name,
            "platform": b.platform,
            "store_url": b.store_url,
            "status": b.status,
            "product_count": product_count,
            "created_at": b.created_at.isoformat() if b.created_at else None,
        })
    return {"brands": result}


@router.post("")
def create_brand(req: BrandCreate, db: Session = Depends(get_db)):
    from models import Brand

    brand = Brand(name=req.name, platform=req.platform, store_url=req.store_url, status="onboarding")
    db.add(brand)
    db.commit()
    db.refresh(brand)
    return {"id": brand.id, "name": brand.name, "status": brand.status}


@router.post("/{brand_id}/activate")
def activate_brand(brand_id: int, db: Session = Depends(get_db)):
    from models import Brand

    brand = db.query(Brand).get(brand_id)
    if not brand:
        return {"error": "Brand not found"}
    brand.status = "active"
    db.commit()
    return {"id": brand.id, "status": "active"}


@router.get("/{brand_id}")
def get_brand(brand_id: int, db: Session = Depends(get_db)):
    from models import Brand, FulfillmentCenter, Inventory, Product

    brand = db.query(Brand).get(brand_id)
    if not brand:
        return {"error": "Brand not found"}

    products = db.query(Product).filter(Product.brand_id == brand_id).all()
    product_list = []
    for p in products:
        inv_rows = db.query(Inventory).filter(Inventory.product_id == p.id).all()
        inv_data = []
        total_stock = 0
        for inv in inv_rows:
            fc = db.query(FulfillmentCenter).get(inv.fulfillment_center_id)
            total_stock += inv.fulfillable_qty
            inv_data.append({
                "fc_id": inv.fulfillment_center_id,
                "fc_code": fc.code if fc else "",
                "onhand": inv.onhand_qty,
                "fulfillable": inv.fulfillable_qty,
                "reserved": inv.reserved_qty,
            })
        product_list.append({
            "id": p.id,
            "sku": p.sku,
            "name": p.name,
            "category": p.category,
            "weight_oz": p.weight_oz,
            "price": p.price,
            "total_stock": total_stock,
            "inventory": inv_data,
        })

    return {
        "id": brand.id,
        "name": brand.name,
        "platform": brand.platform,
        "store_url": brand.store_url,
        "status": brand.status,
        "products": product_list,
    }


# ── Product CRUD ─────────────────────────────────────────────────────────────

@router.post("/{brand_id}/products")
def add_product(brand_id: int, req: ProductCreate, db: Session = Depends(get_db)):
    from models import Brand, FulfillmentCenter, Inventory, Product

    brand = db.query(Brand).get(brand_id)
    if not brand:
        return {"error": "Brand not found"}

    product = Product(
        brand_id=brand_id,
        name=req.name,
        sku=req.sku,
        category=req.category,
        weight_oz=req.weight_oz,
        price=req.price,
    )
    db.add(product)
    db.flush()

    # Create empty inventory slots at all FCs
    fcs = db.query(FulfillmentCenter).all()
    for fc in fcs:
        db.add(Inventory(product_id=product.id, fulfillment_center_id=fc.id))

    db.commit()
    db.refresh(product)
    return {"id": product.id, "sku": product.sku, "name": product.name}


@router.delete("/{brand_id}/products/{product_id}")
def remove_product(brand_id: int, product_id: int, db: Session = Depends(get_db)):
    from models import Product

    product = db.query(Product).filter(Product.id == product_id, Product.brand_id == brand_id).first()
    if not product:
        return {"error": "Product not found"}
    db.delete(product)
    db.commit()
    return {"deleted": True}


# ── Inventory ────────────────────────────────────────────────────────────────

@router.post("/{brand_id}/inventory")
def set_inventory(brand_id: int, req: BulkInventory, db: Session = Depends(get_db)):
    from models import Inventory, Product

    updated = []
    for item in req.items:
        # Verify product belongs to brand
        product = db.query(Product).filter(Product.id == item.product_id, Product.brand_id == brand_id).first()
        if not product:
            continue

        inv = db.query(Inventory).filter(
            Inventory.product_id == item.product_id,
            Inventory.fulfillment_center_id == item.fulfillment_center_id,
        ).first()

        if inv:
            inv.onhand_qty = item.onhand_qty
            inv.fulfillable_qty = item.fulfillable_qty
        else:
            inv = Inventory(
                product_id=item.product_id,
                fulfillment_center_id=item.fulfillment_center_id,
                onhand_qty=item.onhand_qty,
                fulfillable_qty=item.fulfillable_qty,
            )
            db.add(inv)

        updated.append({"product_id": item.product_id, "fc_id": item.fulfillment_center_id})

    db.commit()
    return {"updated": len(updated)}
