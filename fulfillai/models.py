from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _utcnow():
    return datetime.now(timezone.utc)


class Brand(Base):
    __tablename__ = "brands"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    platform = Column(String)
    store_url = Column(String)
    status = Column(String, default="onboarding")  # onboarding / active / paused
    created_at = Column(DateTime, default=_utcnow)

    products = relationship("Product", back_populates="brand", cascade="all, delete-orphan")
    orders = relationship("Order", back_populates="brand")


class FulfillmentCenter(Base):
    __tablename__ = "fulfillment_centers"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    code = Column(String, nullable=False, unique=True)
    city = Column(String)
    state = Column(String)
    region = Column(String)  # east / central / west

    inventory = relationship("Inventory", back_populates="fulfillment_center")
    shipments = relationship("Shipment", back_populates="fulfillment_center")


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    brand_id = Column(Integer, ForeignKey("brands.id"), nullable=False)
    sku = Column(String, nullable=False)
    name = Column(String, nullable=False)
    category = Column(String)
    weight_oz = Column(Float, default=16)
    price = Column(Float, default=0)

    brand = relationship("Brand", back_populates="products")
    inventory = relationship("Inventory", back_populates="product", cascade="all, delete-orphan")
    order_items = relationship("OrderItem", back_populates="product")


class Inventory(Base):
    __tablename__ = "inventory"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    fulfillment_center_id = Column(
        Integer, ForeignKey("fulfillment_centers.id"), nullable=False
    )
    onhand_qty = Column(Integer, default=0)
    fulfillable_qty = Column(Integer, default=0)
    reserved_qty = Column(Integer, default=0)

    product = relationship("Product", back_populates="inventory")
    fulfillment_center = relationship("FulfillmentCenter", back_populates="inventory")


class Carrier(Base):
    __tablename__ = "carriers"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    code = Column(String, nullable=False, unique=True)
    services = Column(JSON)  # list of {name, speed_days, cost_per_oz_per_mile}

    shipments = relationship("Shipment", back_populates="carrier")


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    brand_id = Column(Integer, ForeignKey("brands.id"), nullable=False)
    order_number = Column(String, nullable=False, unique=True)
    status = Column(String, default="pending")
    # pending → queued → picking → packing → shipped → in_transit → delivered
    # also: backorder, exception

    recipient_name = Column(String)
    recipient_city = Column(String)
    recipient_state = Column(String)

    shipping_tier = Column(String, default="standard")  # standard / express / overnight
    is_vip = Column(Boolean, default=False)
    priority_score = Column(Integer, default=0)
    queue_position = Column(Integer)

    created_at = Column(DateTime, default=_utcnow)

    brand = relationship("Brand", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    shipments = relationship("Shipment", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, default=1)
    unit_price = Column(Float)

    order = relationship("Order", back_populates="items")
    product = relationship("Product", back_populates="order_items")


class Shipment(Base):
    __tablename__ = "shipments"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    fulfillment_center_id = Column(Integer, ForeignKey("fulfillment_centers.id"))
    carrier_id = Column(Integer, ForeignKey("carriers.id"))
    status = Column(String, default="pending")
    tracking_number = Column(String)
    carrier_service = Column(String)
    shipping_cost = Column(Float)
    estimated_delivery = Column(DateTime)
    actual_delivery = Column(DateTime)

    order = relationship("Order", back_populates="shipments")
    fulfillment_center = relationship("FulfillmentCenter", back_populates="shipments")
    carrier = relationship("Carrier", back_populates="shipments")
    events = relationship(
        "ShipmentEvent", back_populates="shipment", order_by="ShipmentEvent.occurred_at"
    )


class ShipmentEvent(Base):
    __tablename__ = "shipment_events"

    id = Column(Integer, primary_key=True)
    shipment_id = Column(Integer, ForeignKey("shipments.id"), nullable=False)
    status = Column(String, nullable=False)
    message = Column(Text)
    location = Column(String)
    occurred_at = Column(DateTime, default=_utcnow)

    shipment = relationship("Shipment", back_populates="events")


class AgentAction(Base):
    __tablename__ = "agent_actions"

    id = Column(Integer, primary_key=True)
    agent_name = Column(String, nullable=False)
    action_type = Column(String, nullable=False)  # availability_check, fc_selection, shipping_calc, priority, edge_case, finalize
    step_number = Column(Integer)  # 1-6 within a pipeline run
    entity_type = Column(String)
    entity_id = Column(Integer)
    input_summary = Column(Text)
    output_summary = Column(Text)
    details = Column(JSON)
    severity = Column(String, default="info")  # info / warning / HIGH / MEDIUM / LOW
    created_at = Column(DateTime, default=_utcnow)


class Anomaly(Base):
    __tablename__ = "anomalies"

    id = Column(Integer, primary_key=True)
    anomaly_type = Column(String)       # 'cluster_delay', 'single_stuck', 'carrier_issue', 'fc_issue'
    scope_type = Column(String)         # 'fc', 'carrier', 'region', 'order'
    scope_id = Column(Integer, nullable=True)
    scope_label = Column(String)        # 'FC-LAX', 'FedEx', 'west region', 'ORD-1042'
    severity = Column(String, default="medium")  # low | medium | high | critical
    detected_at = Column(DateTime, default=_utcnow)
    affected_order_ids = Column(JSON)   # list[int]
    affected_count = Column(Integer, default=0)
    status = Column(String, default="detected")
    # detected → investigating → diagnosed → drafting → pending_review
    # pending_review → resolved | rejected
    # re-investigate loops back to investigating

    detection_summary = Column(Text)
    detection_details = Column(JSON)
    ops_context = Column(Text, nullable=True)

    # Tavily grounding
    ai_grounding_queries = Column(JSON, nullable=True)
    ai_grounding_sources = Column(JSON, nullable=True)

    # LLM diagnosis output
    ai_likely_cause = Column(Text, nullable=True)
    ai_detailed_reasoning = Column(Text, nullable=True)
    ai_evidence = Column(JSON, nullable=True)  # list[{bullet, source}]
    ai_confidence = Column(String, nullable=True)  # low | medium | high
    ai_recommended_action = Column(Text, nullable=True)
    ai_customer_impact = Column(Text, nullable=True)
    ai_sources_used = Column(JSON, nullable=True)
    ai_investigated_at = Column(DateTime, nullable=True)

    reviewed_at = Column(DateTime, nullable=True)
    review_action = Column(String, nullable=True)  # approved | rejected

    notifications = relationship("Notification", back_populates="anomaly", cascade="all, delete-orphan")


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    anomaly_id = Column(Integer, ForeignKey("anomalies.id"), nullable=False)
    recipient_name = Column(String)
    subject = Column(String)
    body = Column(Text)
    status = Column(String, default="draft")  # draft → approved → sent | rejected
    is_fallback = Column(Boolean, default=False)  # true if LLM call failed and we used canned text
    generated_at = Column(DateTime, default=_utcnow)
    approved_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)

    anomaly = relationship("Anomaly", back_populates="notifications")
