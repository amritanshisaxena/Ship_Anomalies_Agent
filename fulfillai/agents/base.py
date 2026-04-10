from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from openai import AsyncOpenAI
from sqlalchemy.orm import Session

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

_api_key = os.getenv("OPENAI_API_KEY")
if not _api_key:
    raise RuntimeError(
        "OPENAI_API_KEY not found. Make sure fulfillai/.env contains: OPENAI_API_KEY=sk-..."
    )

client = AsyncOpenAI(api_key=_api_key)

# Broadcast queue for SSE activity feed — routes/activity.py registers listeners
activity_listeners: List[asyncio.Queue] = []


async def _call_openai_with_retry(
    messages: list,
    stream: bool = False,
    max_retries: int = 3,
    **kwargs,
):
    delay = 1.0
    last_exc = None
    for attempt in range(max_retries):
        try:
            response = await client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                stream=stream,
                **kwargs,
            )
            return response
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)
                delay *= 2
    raise last_exc


def log_agent_action(
    db: Session,
    agent_name: str,
    action_type: str,
    entity_type: str,
    entity_id: int,
    input_summary: str,
    output_summary: str,
    details: Optional[dict] = None,
    severity: str = "info",
    step_number: Optional[int] = None,
) -> int:
    from models import AgentAction

    action = AgentAction(
        agent_name=agent_name,
        action_type=action_type,
        step_number=step_number,
        entity_type=entity_type,
        entity_id=entity_id,
        input_summary=input_summary,
        output_summary=output_summary,
        details=details,
        severity=severity,
    )
    db.add(action)
    db.flush()

    # Broadcast to SSE listeners
    event_data = {
        "id": action.id,
        "agent_name": agent_name,
        "action_type": action_type,
        "step_number": step_number,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "input_summary": input_summary,
        "output_summary": output_summary,
        "severity": severity,
        "created_at": action.created_at.isoformat() if action.created_at else None,
    }
    for q in activity_listeners:
        try:
            q.put_nowait(event_data)
        except asyncio.QueueFull:
            pass

    return action.id
