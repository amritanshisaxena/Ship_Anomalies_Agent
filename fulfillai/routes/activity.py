"""Activity feed — SSE real-time stream + history."""

import asyncio
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from agents.base import activity_listeners
from database import get_db

router = APIRouter(prefix="/api/activity", tags=["activity"])


@router.get("/feed")
async def activity_feed():
    queue = asyncio.Queue(maxsize=100)
    activity_listeners.append(queue)

    async def generate():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if queue in activity_listeners:
                activity_listeners.remove(queue)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.get("/history")
def activity_history(
    page: int = 1,
    per_page: int = 100,
    order_id: int = None,
    action_type: str = None,
    db: Session = Depends(get_db),
):
    from models import AgentAction

    query = db.query(AgentAction)
    if order_id:
        query = query.filter(AgentAction.entity_type == "order", AgentAction.entity_id == order_id)
    if action_type:
        query = query.filter(AgentAction.action_type == action_type)

    total = query.count()
    actions = (
        query.order_by(AgentAction.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return {
        "actions": [
            {
                "id": a.id,
                "agent_name": a.agent_name,
                "action_type": a.action_type,
                "step_number": a.step_number,
                "entity_type": a.entity_type,
                "entity_id": a.entity_id,
                "input_summary": a.input_summary,
                "output_summary": a.output_summary,
                "details": a.details,
                "severity": a.severity,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in actions
        ],
        "total": total,
        "page": page,
    }
