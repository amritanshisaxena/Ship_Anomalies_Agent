"""
Autonomous background monitor loop.

Started at FastAPI app startup. Every SCAN_INTERVAL_SECONDS it:
  1. Runs run_monitor_scan() to detect stuck/clustered shipments
  2. For each new anomaly, calls investigate_anomaly() (Tavily + LLM #1)
  3. Then calls draft_notifications_for_anomaly() (LLM #2 per order)

When ops opens the Command Center, anomalies are already in
'pending_review' with a full diagnosis and draft notifications ready.

The loop also exposes run_one_cycle() for manual triggering via
POST /api/anomalies/scan-now so demos don't wait up to 60 seconds.
"""

from __future__ import annotations

import asyncio
import traceback

from agents.fulfillment import advance_queue_skip_holds
from agents.investigator import investigate_anomaly
from agents.monitor import run_monitor_scan
from agents.notifier import draft_notifications_for_anomaly
from database import SessionLocal

SCAN_INTERVAL_SECONDS = 60
STARTUP_DELAY_SECONDS = 5

# How often the auto-advance loop moves orders through queued → picking →
# packing → shipped. Held orders are skipped; they wait for ops to approve
# or reject them in the Command Center.
QUEUE_ADVANCE_INTERVAL_SECONDS = 30
QUEUE_ADVANCE_BATCH_SIZE = 10


async def run_one_cycle() -> dict:
    """
    Run a single monitor → investigate → draft cycle. Returns a summary dict.
    Safe to call from a route handler (uses its own DB session).
    """
    summary = {"scanned": True, "new_anomalies": 0, "processed": [], "errors": []}
    db = SessionLocal()
    try:
        new_anomalies = run_monitor_scan(db)
        summary["new_anomalies"] = len(new_anomalies)

        for anomaly in new_anomalies:
            anomaly_id = anomaly.id
            scope = anomaly.scope_label
            try:
                await investigate_anomaly(db, anomaly_id)
                await draft_notifications_for_anomaly(db, anomaly_id)
                summary["processed"].append({"id": anomaly_id, "scope": scope, "status": "pending_review"})
            except Exception as exc:
                err = f"{scope}: {exc}"
                print(f"[monitor-loop] failed to process anomaly {anomaly_id}: {err}")
                traceback.print_exc()
                summary["errors"].append({"id": anomaly_id, "scope": scope, "error": str(exc)})
                # Leave anomaly in whatever partial state it reached; the next
                # cycle will NOT pick it up (OPEN_STATUSES dedup). Ops can
                # trigger /re-investigate manually if needed.
    finally:
        db.close()
    return summary


async def anomaly_monitor_loop():
    """Forever-loop — started as an asyncio.create_task at app startup."""
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    print(f"[monitor-loop] started, scanning every {SCAN_INTERVAL_SECONDS}s")
    while True:
        try:
            result = await run_one_cycle()
            if result["new_anomalies"]:
                print(
                    f"[monitor-loop] cycle: {result['new_anomalies']} new anomalies, "
                    f"{len(result['processed'])} processed, {len(result['errors'])} errors"
                )
        except Exception as exc:
            print(f"[monitor-loop] scan error: {exc}")
            traceback.print_exc()
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


async def queue_advance_loop():
    """Forever-loop that auto-advances orders through the warehouse stages.

    Held orders (on_hold=True) are skipped. They only resume after ops
    approves the gating anomaly in the Command Center.
    """
    await asyncio.sleep(STARTUP_DELAY_SECONDS + 3)
    print(
        f"[advance-loop] started, advancing {QUEUE_ADVANCE_BATCH_SIZE} orders "
        f"every {QUEUE_ADVANCE_INTERVAL_SECONDS}s (holds skipped)"
    )
    while True:
        try:
            db = SessionLocal()
            try:
                moved = advance_queue_skip_holds(db, count=QUEUE_ADVANCE_BATCH_SIZE)
                if moved:
                    print(f"[advance-loop] moved {len(moved)} order(s)")
            finally:
                db.close()
        except Exception as exc:
            print(f"[advance-loop] error: {exc}")
            traceback.print_exc()
        await asyncio.sleep(QUEUE_ADVANCE_INTERVAL_SECONDS)
