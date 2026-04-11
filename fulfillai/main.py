import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from agents.background import anomaly_monitor_loop, queue_advance_loop
from database import SessionLocal, init_db
from routes.activity import router as activity_router
from routes.anomalies import router as anomalies_router
from routes.brands import router as brands_router
from routes.explorer import router as explorer_router
from routes.fulfillment import router as fulfillment_router
from routes.orders import router as orders_router
from routes.simulation import router as simulation_router
from routes.storefront import router as storefront_router

app = FastAPI(title="FulfillAI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(brands_router)
app.include_router(storefront_router)
app.include_router(orders_router)
app.include_router(fulfillment_router)
app.include_router(explorer_router)
app.include_router(activity_router)
app.include_router(simulation_router)
app.include_router(anomalies_router)


@app.get("/")
async def serve_ops_ui():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/shop")
async def serve_shop_ui():
    return FileResponse(Path(__file__).parent / "shop.html")


@app.on_event("startup")
async def on_startup():
    from seed import seed_if_empty

    init_db()
    session = SessionLocal()
    try:
        seed_if_empty(session)
    finally:
        session.close()

    # Launch the autonomous anomaly monitor loop. It scans every 60s,
    # investigates with Tavily grounding, and drafts customer notifications
    # — all without human intervention. Ops only reviews.
    asyncio.create_task(anomaly_monitor_loop())

    # Launch the auto-advance loop. It moves orders through
    # queued → picking → packing → shipped every 30s, skipping any order
    # that is on hold (split shipment, backorder, proactive route risk).
    # Held orders resume only after ops approves the gating anomaly.
    asyncio.create_task(queue_advance_loop())

    print("FulfillAI running at http://localhost:8000")
    print("  Ops portal:      http://localhost:8000/")
    print("  Customer shop:   http://localhost:8000/shop")
