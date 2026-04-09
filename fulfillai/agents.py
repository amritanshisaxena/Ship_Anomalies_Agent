import asyncio
import json
import os
import re
from pathlib import Path
from typing import AsyncGenerator

from dotenv import load_dotenv
from openai import AsyncOpenAI

# Load .env from the same directory as this file, regardless of cwd
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

_api_key = os.getenv("OPENAI_API_KEY")
if not _api_key:
    raise RuntimeError(
        "OPENAI_API_KEY not found. Make sure fulfillai/.env contains: OPENAI_API_KEY=sk-..."
    )

client = AsyncOpenAI(api_key=_api_key)

AGENT1_SYSTEM = """You are FulfillAI, an intelligent onboarding assistant for a fulfillment platform.
Your job is to collect setup information from a brand in a friendly, conversational way.
Ask one question at a time. Be concise. Acknowledge each answer briefly before moving on.
Track which step you're on: Store Info → Products → Inventory → Shipping → Go Live.

Steps to collect:
1. Store Info — brand name, platform (Shopify/WooCommerce/Amazon/Other), store URL
2. Products — how many SKUs, main product category, average order size
3. Inventory — which warehouses to use (East Coast / West Coast / Central), estimated monthly units
4. Shipping — preferred carriers (UPS/FedEx/USPS), shipping speed priority (cost vs speed)
5. Go Live — summarize everything collected, ask for confirmation

When all information is collected and confirmed, respond with exactly this trigger phrase at the start:
ONBOARDING_COMPLETE: followed by a JSON summary of everything collected.

Example: ONBOARDING_COMPLETE: {"brand_name": "...", "platform": "...", ...}"""

AGENT2_SYSTEM = """You are an intelligent ops agent for a fulfillment platform.
You will receive an order in Exception or OnHold status plus inventory data.
Diagnose the root cause and recommend a specific concrete action.

Severity rules:
HIGH = revenue blocked, customer impacted right now
MEDIUM = needs action today but not immediately critical
LOW = informational, can be addressed in normal workflow

Respond ONLY in this exact JSON format with no other text:
{
  "diagnosis": "1-2 sentence plain English explanation of what went wrong",
  "severity": "HIGH or MEDIUM or LOW",
  "severity_reason": "one sentence explaining why this severity",
  "recommended_action": "specific concrete action e.g. Transfer 5 units from LA FC to NYC FC",
  "merchant_message": "2-3 sentence professional message to send the brand"
}"""


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


async def stream_onboarding_chat(
    message: str, history: list
) -> AsyncGenerator[str, None]:
    messages = [{"role": "system", "content": AGENT1_SYSTEM}]
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})

    response = await _call_openai_with_retry(messages, stream=True)

    full_text = ""
    async for chunk in response:
        delta = chunk.choices[0].delta.content or ""
        if delta:
            full_text += delta
            yield delta

    # After full stream, emit special event if complete
    if full_text.startswith("ONBOARDING_COMPLETE:"):
        yield "\n\n__ONBOARDING_COMPLETE__"


async def analyze_order(order: dict, inventory: list) -> dict:
    order_json = json.dumps(order, indent=2)
    inventory_json = json.dumps(inventory, indent=2)

    messages = [
        {"role": "system", "content": AGENT2_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Order:\n{order_json}\n\nInventory Data:\n{inventory_json}"
            ),
        },
    ]

    response = await _call_openai_with_retry(messages, stream=False)
    raw = response.choices[0].message.content.strip()

    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "diagnosis": "Unable to parse AI response.",
            "severity": "MEDIUM",
            "severity_reason": "Parse error — manual review required.",
            "recommended_action": "Review order manually.",
            "merchant_message": "We are investigating an issue with your order and will update you shortly.",
        }
