import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

from agents import analyze_order, stream_onboarding_chat
from mock_data import INVENTORY, ORDERS

app = FastAPI(title="FulfillAI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

LOG_FILE = Path(__file__).parent / "agent_log.json"


def _append_log(entry: dict):
    logs = []
    if LOG_FILE.exists():
        try:
            logs = json.loads(LOG_FILE.read_text())
        except Exception:
            logs = []
    logs.append(entry)
    LOG_FILE.write_text(json.dumps(logs, indent=2))


# ── Pydantic models ──────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    history: list = []


class ApproveRequest(BaseModel):
    order_id: int
    action: str


class RejectRequest(BaseModel):
    order_id: int
    reason: str


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_ui():
    return FileResponse(Path(__file__).parent / "index.html")


@app.post("/api/chat")
async def chat(req: ChatRequest):
    async def generate():
        async for token in stream_onboarding_chat(req.message, req.history):
            if token == "\n\n__ONBOARDING_COMPLETE__":
                yield "event: onboarding_complete\ndata: {}\n\n"
            else:
                payload = json.dumps({"token": token})
                yield f"data: {payload}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/analyze")
async def analyze():
    inv_by_sku = {item["sku"]: item for item in INVENTORY}

    async def generate():
        for order in ORDERS:
            status = order["status"]
            if status in ("Exception", "OnHold"):
                # Collect relevant inventory for this order's SKUs
                relevant_inv = []
                for product in order["products"]:
                    sku = product["sku"]
                    if sku in inv_by_sku:
                        relevant_inv.append(inv_by_sku[sku])

                # Stream "analyzing" placeholder first
                placeholder = json.dumps(
                    {"order_id": order["id"], "analyzing": True, "order": order}
                )
                yield f"data: {placeholder}\n\n"

                try:
                    result = await analyze_order(order, relevant_inv)
                except Exception as exc:
                    result = {
                        "diagnosis": f"Analysis failed: {exc}",
                        "severity": "MEDIUM",
                        "severity_reason": "Error during AI analysis.",
                        "recommended_action": "Review manually.",
                        "merchant_message": "We are reviewing your order.",
                    }

                payload = json.dumps(
                    {
                        "order_id": order["id"],
                        "analyzing": False,
                        "order": order,
                        "analysis": result,
                    }
                )
                yield f"data: {payload}\n\n"

            else:
                # Healthy order — stream directly, no AI call
                payload = json.dumps(
                    {
                        "order_id": order["id"],
                        "analyzing": False,
                        "order": order,
                        "analysis": None,
                    }
                )
                yield f"data: {payload}\n\n"

        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/approve")
async def approve(req: ApproveRequest):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "order_id": req.order_id,
        "action": req.action,
        "decision": "approved",
    }
    _append_log(entry)
    return {"status": "approved"}


@app.post("/api/reject")
async def reject(req: RejectRequest):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "order_id": req.order_id,
        "reason": req.reason,
        "decision": "rejected",
    }
    _append_log(entry)
    return {"status": "rejected"}


# ── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    print("FulfillAI running at http://localhost:8000")
