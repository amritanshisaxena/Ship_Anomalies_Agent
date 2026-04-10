"""
Fulfillment Pipeline Agent — the brain of FulfillAI.

Processes each order through 6 decision steps:
1. Availability Check
2. FC Selection
3. Shipping Cost Calculation
4. Priority Scoring
5. Edge Case Detection
6. Finalize (reserve inventory, create shipment, assign queue)
"""

import random
import string
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from agents.base import log_agent_action

# ── Region mapping for distance-based shipping cost ──────────────────────────

_STATE_REGION = {
    "NY": "east", "NJ": "east", "MA": "east", "CT": "east", "PA": "east",
    "VA": "east", "MD": "east", "NC": "east", "SC": "east", "GA": "east",
    "FL": "east", "ME": "east", "NH": "east", "VT": "east", "RI": "east",
    "DE": "east", "DC": "east", "WV": "east",
    "TX": "central", "IL": "central", "OH": "central", "MI": "central",
    "IN": "central", "WI": "central", "MN": "central", "MO": "central",
    "IA": "central", "KS": "central", "NE": "central", "OK": "central",
    "AR": "central", "LA": "central", "MS": "central", "AL": "central",
    "TN": "central", "KY": "central", "CO": "central", "ND": "central",
    "SD": "central",
    "CA": "west", "WA": "west", "OR": "west", "NV": "west", "AZ": "west",
    "UT": "west", "ID": "west", "MT": "west", "WY": "west", "NM": "west",
    "HI": "west", "AK": "west",
    "ON": "central", "QC": "east", "BC": "west", "AB": "west",
}

# Distance multiplier: how far apart are two regions
_DISTANCE_MULT = {
    ("east", "east"): 1.0,
    ("central", "central"): 1.0,
    ("west", "west"): 1.0,
    ("east", "central"): 1.5,
    ("central", "east"): 1.5,
    ("central", "west"): 1.5,
    ("west", "central"): 1.5,
    ("east", "west"): 2.5,
    ("west", "east"): 2.5,
}

# Speed requirements by tier
_TIER_MAX_DAYS = {"standard": 99, "express": 3, "overnight": 1}

# Fulfillment processing stages
QUEUE_STAGES = ["queued", "picking", "packing", "shipped"]


def process_order(db: Session, order_id: int) -> dict:
    """Run the full 6-step pipeline for an order. Returns a summary of all decisions."""
    from models import Order

    order = db.query(Order).get(order_id)
    if not order:
        return {"error": f"Order {order_id} not found"}
    if order.status != "pending":
        return {"error": f"Order {order.order_number} is not pending (status: {order.status})"}

    pipeline_result = {"order_id": order.id, "order_number": order.order_number, "steps": []}

    # ── Step 1: Availability Check ───────────────────────────────────────────
    step1 = _step1_availability(db, order)
    pipeline_result["steps"].append(step1)

    if step1.get("backorder"):
        order.status = "backorder"
        order.priority_score = -100
        db.commit()
        _log_step(db, 6, order, "finalize", f"Order {order.order_number} → backorder (out of stock)",
                  {"reason": "No FC can fulfill any items"})
        pipeline_result["final_status"] = "backorder"
        return pipeline_result

    # ── Step 2: FC Selection ─────────────────────────────────────────────────
    step2 = _step2_fc_selection(db, order, step1["availability"])
    pipeline_result["steps"].append(step2)

    # ── Step 3: Shipping Cost Calculation ────────────────────────────────────
    step3 = _step3_shipping(db, order, step2["selected_fcs"])
    pipeline_result["steps"].append(step3)

    # ── Step 4: Priority Scoring ─────────────────────────────────────────────
    step4 = _step4_priority(db, order)
    pipeline_result["steps"].append(step4)

    # ── Step 5: Edge Case Detection ──────────────────────────────────────────
    step5 = _step5_edge_cases(db, order, step2["selected_fcs"])
    pipeline_result["steps"].append(step5)

    # ── Step 6: Finalize ─────────────────────────────────────────────────────
    step6 = _step6_finalize(db, order, step2, step3)
    pipeline_result["steps"].append(step6)

    pipeline_result["final_status"] = order.status
    pipeline_result["priority_score"] = order.priority_score
    pipeline_result["queue_position"] = order.queue_position

    return pipeline_result


# ═════════════════════════════════════════════════════════════════════════════
# STEP IMPLEMENTATIONS
# ═════════════════════════════════════════════════════════════════════════════

def _step1_availability(db: Session, order) -> dict:
    from models import FulfillmentCenter, Inventory, OrderItem, Product

    items = db.query(OrderItem).filter(OrderItem.order_id == order.id).all()
    availability = {}  # product_id → {fc_id: qty_available}

    for item in items:
        product = db.query(Product).get(item.product_id)
        if not product:
            continue

        inv_rows = db.query(Inventory).filter(Inventory.product_id == product.id).all()
        fc_stock = {}
        for inv in inv_rows:
            if inv.fulfillable_qty > 0:
                fc = db.query(FulfillmentCenter).get(inv.fulfillment_center_id)
                fc_stock[inv.fulfillment_center_id] = {
                    "qty": inv.fulfillable_qty,
                    "fc_code": fc.code if fc else "?",
                }

        availability[product.id] = {
            "sku": product.sku,
            "name": product.name,
            "needed": item.quantity,
            "fc_stock": fc_stock,
            "total_available": sum(v["qty"] for v in fc_stock.values()),
        }

    # Check if any item is completely out of stock
    all_out = any(v["total_available"] < v["needed"] for v in availability.values())
    completely_out = any(v["total_available"] == 0 for v in availability.values())

    summary_parts = []
    for pid, info in availability.items():
        fc_list = ", ".join(f"{v['fc_code']}({v['qty']})" for v in info["fc_stock"].values())
        status = "OK" if info["total_available"] >= info["needed"] else "LOW" if info["total_available"] > 0 else "OUT"
        summary_parts.append(f"{info['sku']}: need {info['needed']}, available at [{fc_list}] — {status}")

    summary = "; ".join(summary_parts)
    _log_step(db, 1, order, "availability_check", summary, {
        "availability": {str(k): v for k, v in availability.items()},
        "backorder": completely_out,
    })

    return {
        "step": 1,
        "name": "Availability Check",
        "availability": availability,
        "backorder": completely_out,
        "summary": summary,
    }


def _step2_fc_selection(db: Session, order, availability: dict) -> dict:
    from models import FulfillmentCenter, OrderItem

    items = db.query(OrderItem).filter(OrderItem.order_id == order.id).all()
    item_map = {item.product_id: item.quantity for item in items}
    dest_region = _STATE_REGION.get(order.recipient_state, "central")

    fcs = db.query(FulfillmentCenter).all()
    fc_map = {fc.id: fc for fc in fcs}

    # Try to find a single FC that can fulfill everything
    single_fc_scores = []
    for fc in fcs:
        can_fulfill_all = True
        for pid, needed in item_map.items():
            avail = availability.get(pid, {}).get("fc_stock", {})
            fc_qty = avail.get(fc.id, {}).get("qty", 0)
            if fc_qty < needed:
                can_fulfill_all = False
                break

        if can_fulfill_all:
            dist_mult = _DISTANCE_MULT.get((fc.region, dest_region), 2.0)
            # Lower score = better (cost-like)
            cost_score = dist_mult * 10
            # For express/overnight, prefer closer FCs more aggressively
            if order.shipping_tier == "overnight":
                cost_score = dist_mult * 50  # heavily penalize distance
            elif order.shipping_tier == "express":
                cost_score = dist_mult * 25

            # Stock depth bonus (prefer FCs with more stock to avoid depletion)
            stock_depth = sum(
                availability.get(pid, {}).get("fc_stock", {}).get(fc.id, {}).get("qty", 0)
                for pid in item_map
            )
            cost_score -= stock_depth * 0.1  # slight preference for deeper stock

            single_fc_scores.append((fc.id, cost_score, fc.code))

    selected_fcs = []  # list of {fc_id, fc_code, items: [{product_id, qty}]}
    is_split = False

    if single_fc_scores:
        single_fc_scores.sort(key=lambda x: x[1])
        best_fc_id = single_fc_scores[0][0]
        best_fc = fc_map[best_fc_id]
        selected_fcs.append({
            "fc_id": best_fc_id,
            "fc_code": best_fc.code,
            "items": [{"product_id": pid, "qty": qty} for pid, qty in item_map.items()],
        })
        reason = f"Single FC: {best_fc.code} (region: {best_fc.region}, dest: {dest_region})"
        if len(single_fc_scores) > 1:
            alternatives = ", ".join(f"{s[2]}(cost:{s[1]:.1f})" for s in single_fc_scores[:4])
            reason += f". Scored {len(single_fc_scores)} FCs: {alternatives}"
    else:
        # Split shipment — assign each item to the FC with most stock
        is_split = True
        fc_assignments = {}  # fc_id → [items]
        for pid, needed in item_map.items():
            avail = availability.get(pid, {}).get("fc_stock", {})
            # Pick FC with most stock for this item
            best = max(avail.items(), key=lambda x: x[1]["qty"]) if avail else None
            if best:
                fc_id = best[0]
                if fc_id not in fc_assignments:
                    fc_assignments[fc_id] = {"fc_id": fc_id, "fc_code": fc_map[fc_id].code, "items": []}
                fc_assignments[fc_id]["items"].append({"product_id": pid, "qty": needed})

        selected_fcs = list(fc_assignments.values())
        reason = f"SPLIT SHIPMENT across {len(selected_fcs)} FCs — no single FC has all items"

    _log_step(db, 2, order, "fc_selection", reason, {
        "selected_fcs": selected_fcs,
        "is_split": is_split,
        "destination_region": dest_region,
    })

    return {
        "step": 2,
        "name": "FC Selection",
        "selected_fcs": selected_fcs,
        "is_split": is_split,
        "summary": reason,
    }


def _step3_shipping(db: Session, order, selected_fcs: list) -> dict:
    from models import Carrier, FulfillmentCenter, OrderItem, Product

    dest_region = _STATE_REGION.get(order.recipient_state, "central")
    max_days = _TIER_MAX_DAYS.get(order.shipping_tier, 99)

    items = db.query(OrderItem).filter(OrderItem.order_id == order.id).all()
    total_weight_oz = 0
    for item in items:
        product = db.query(Product).get(item.product_id)
        if product:
            total_weight_oz += (product.weight_oz or 16) * item.quantity
    total_weight_lb = total_weight_oz / 16

    carriers = db.query(Carrier).all()
    shipping_options = []  # per-FC shipping decisions

    for fc_assign in selected_fcs:
        fc = db.query(FulfillmentCenter).get(fc_assign["fc_id"])
        fc_region = fc.region if fc else "central"
        dist_mult = _DISTANCE_MULT.get((fc_region, dest_region), 2.0)

        best_option = None
        all_options = []

        for carrier in carriers:
            for service in (carrier.services or []):
                speed = service.get("speed_days", 5)
                if speed > max_days:
                    continue  # too slow for this tier
                cost = service.get("cost_per_lb", 1.0) * total_weight_lb * dist_mult
                option = {
                    "carrier_id": carrier.id,
                    "carrier": carrier.name,
                    "service": service["name"],
                    "speed_days": speed,
                    "cost": round(cost, 2),
                }
                all_options.append(option)

                # Selection logic: standard → cheapest; express/overnight → fastest then cheapest
                if best_option is None:
                    best_option = option
                elif order.shipping_tier == "standard":
                    if cost < best_option["cost"]:
                        best_option = option
                else:
                    if speed < best_option["speed_days"] or (speed == best_option["speed_days"] and cost < best_option["cost"]):
                        best_option = option

        if not best_option and all_options:
            best_option = min(all_options, key=lambda x: x["cost"])
        elif not best_option:
            best_option = {"carrier_id": 1, "carrier": "UPS", "service": "Ground", "speed_days": 5, "cost": round(total_weight_lb * 0.5 * dist_mult, 2)}

        shipping_options.append({
            "fc_id": fc_assign["fc_id"],
            "fc_code": fc_assign.get("fc_code", "?"),
            "selected": best_option,
            "alternatives_count": len(all_options),
            "all_options": sorted(all_options, key=lambda x: x["cost"]),
        })

    total_cost = sum(opt["selected"]["cost"] for opt in shipping_options)
    summary_parts = []
    for opt in shipping_options:
        s = opt["selected"]
        summary_parts.append(
            f"{opt['fc_code']} → {s['carrier']} {s['service']} "
            f"(${s['cost']:.2f}, {s['speed_days']}d) "
            f"[compared {opt['alternatives_count']} options]"
        )
    summary = f"Total: ${total_cost:.2f} for {total_weight_lb:.1f}lb. " + "; ".join(summary_parts)

    _log_step(db, 3, order, "shipping_calc", summary, {
        "shipping_tier": order.shipping_tier,
        "total_weight_lb": round(total_weight_lb, 2),
        "total_cost": total_cost,
        "options": shipping_options,
    })

    return {
        "step": 3,
        "name": "Shipping Cost",
        "shipping_options": shipping_options,
        "total_cost": total_cost,
        "summary": summary,
    }


def _step4_priority(db: Session, order) -> dict:
    score = 100

    bonuses = []

    if order.is_vip:
        score += 50
        bonuses.append("VIP +50")

    if order.shipping_tier == "overnight":
        score += 40
        bonuses.append("Overnight +40")
    elif order.shipping_tier == "express":
        score += 20
        bonuses.append("Express +20")

    # Age bonus: +1 per hour since creation
    if order.created_at:
        # SQLite strips tzinfo, so after a commit-expire-reload cycle,
        # order.created_at comes back naive. Normalize before subtracting
        # an aware datetime, otherwise Python raises TypeError and the
        # whole pipeline aborts.
        created = order.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
        age_bonus = int(min(age_hours, 48))  # cap at 48
        score += age_bonus
        if age_bonus > 0:
            bonuses.append(f"Age +{age_bonus} ({age_hours:.1f}hr)")

    order.priority_score = score

    summary = f"Priority score: {score} (base 100" + (", " + ", ".join(bonuses) if bonuses else "") + ")"

    _log_step(db, 4, order, "priority", summary, {
        "score": score,
        "bonuses": bonuses,
        "shipping_tier": order.shipping_tier,
        "is_vip": order.is_vip,
    })

    return {
        "step": 4,
        "name": "Priority Scoring",
        "score": score,
        "bonuses": bonuses,
        "summary": summary,
    }


def _step5_edge_cases(db: Session, order, selected_fcs: list) -> dict:
    from models import Inventory, Order, OrderItem, Product

    alerts = []

    items = db.query(OrderItem).filter(OrderItem.order_id == order.id).all()

    for item in items:
        product = db.query(Product).get(item.product_id)
        if not product:
            continue

        for fc_assign in selected_fcs:
            inv = db.query(Inventory).filter(
                Inventory.product_id == product.id,
                Inventory.fulfillment_center_id == fc_assign["fc_id"],
            ).first()

            if inv:
                remaining = inv.fulfillable_qty - item.quantity
                if remaining < 5 and remaining >= 0:
                    alerts.append({
                        "type": "low_stock",
                        "severity": "warning",
                        "message": f"LOW STOCK: {product.sku} at {fc_assign.get('fc_code', '?')} will drop to {remaining} units after this order",
                        "recommendation": "Consider rebalancing inventory from other FCs",
                    })

        # Check for imbalance across FCs
        all_inv = db.query(Inventory).filter(Inventory.product_id == product.id).all()
        if len(all_inv) > 1:
            qtys = [(inv.fulfillable_qty, inv.fulfillment_center_id) for inv in all_inv]
            max_qty = max(q[0] for q in qtys)
            min_qty = min(q[0] for q in qtys)
            if max_qty > 0 and min_qty == 0:
                alerts.append({
                    "type": "rebalance",
                    "severity": "info",
                    "message": f"IMBALANCE: {product.sku} has {max_qty} units at one FC but 0 at another",
                    "recommendation": f"Transfer stock to balance across fulfillment centers",
                })

    # Check for batch opportunity
    for fc_assign in selected_fcs:
        same_dest = db.query(Order).filter(
            Order.status.in_(["queued", "pending"]),
            Order.recipient_state == order.recipient_state,
            Order.id != order.id,
        ).count()
        if same_dest >= 2:
            alerts.append({
                "type": "batch",
                "severity": "info",
                "message": f"BATCH: {same_dest + 1} orders going to {order.recipient_state} — potential batch optimization",
                "recommendation": "Consider grouping for warehouse pick efficiency",
            })

    # Check split shipment
    if len(selected_fcs) > 1:
        alerts.append({
            "type": "split_shipment",
            "severity": "warning",
            "message": f"SPLIT: Order requires {len(selected_fcs)} separate shipments from different FCs",
            "recommendation": "Higher shipping cost — consider inventory consolidation",
        })

    summary = f"{len(alerts)} alert(s)" if alerts else "No edge cases detected"
    if alerts:
        summary += ": " + "; ".join(a["message"] for a in alerts)

    severity = "warning" if any(a["severity"] == "warning" for a in alerts) else "info"

    _log_step(db, 5, order, "edge_case", summary, {"alerts": alerts}, severity=severity)

    return {
        "step": 5,
        "name": "Edge Case Detection",
        "alerts": alerts,
        "summary": summary,
    }


def _step6_finalize(db: Session, order, step2: dict, step3: dict) -> dict:
    from models import Inventory, Order, OrderItem, Shipment, ShipmentEvent

    items = db.query(OrderItem).filter(OrderItem.order_id == order.id).all()
    item_map = {item.product_id: item.quantity for item in items}

    shipment_ids = []

    for i, fc_assign in enumerate(step2["selected_fcs"]):
        shipping = step3["shipping_options"][i] if i < len(step3["shipping_options"]) else step3["shipping_options"][0]
        selected = shipping["selected"]

        # Reserve inventory
        for item_info in fc_assign["items"]:
            inv = db.query(Inventory).filter(
                Inventory.product_id == item_info["product_id"],
                Inventory.fulfillment_center_id == fc_assign["fc_id"],
            ).first()
            if inv:
                inv.fulfillable_qty -= item_info["qty"]
                inv.reserved_qty += item_info["qty"]

        # Create shipment
        est_delivery = datetime.now(timezone.utc) + timedelta(days=selected["speed_days"])
        tracking = _generate_tracking(selected.get("carrier", "ups"))

        shipment = Shipment(
            order_id=order.id,
            fulfillment_center_id=fc_assign["fc_id"],
            carrier_id=selected.get("carrier_id"),
            status="queued",
            carrier_service=selected["service"],
            shipping_cost=selected["cost"],
            tracking_number=tracking,
            estimated_delivery=est_delivery,
        )
        db.add(shipment)
        db.flush()
        shipment_ids.append(shipment.id)

        db.add(ShipmentEvent(
            shipment_id=shipment.id,
            status="queued",
            message=f"Order processed — assigned to {fc_assign.get('fc_code', '?')}, {selected['carrier']} {selected['service']}",
            location=fc_assign.get("fc_code", ""),
        ))

    # Assign queue position
    max_pos = db.query(func.max(Order.queue_position)).scalar() or 0
    order.queue_position = max_pos + 1
    order.status = "queued"
    db.commit()

    summary = (
        f"FINALIZED: {order.order_number} → queued at position #{order.queue_position}, "
        f"priority {order.priority_score}, "
        f"{len(shipment_ids)} shipment(s)"
    )

    _log_step(db, 6, order, "finalize", summary, {
        "shipment_ids": shipment_ids,
        "queue_position": order.queue_position,
        "priority_score": order.priority_score,
    })

    return {
        "step": 6,
        "name": "Finalize",
        "queue_position": order.queue_position,
        "shipment_ids": shipment_ids,
        "summary": summary,
    }


# ═════════════════════════════════════════════════════════════════════════════
# WAREHOUSE QUEUE ADVANCEMENT
# ═════════════════════════════════════════════════════════════════════════════

def advance_queue(db: Session, count: int = 5) -> list:
    """Advance the top N queued orders by priority through pick → pack → ship."""
    from models import Order, Shipment, ShipmentEvent

    results = []

    # Get orders in queue, sorted by priority (highest first)
    queued_orders = (
        db.query(Order)
        .filter(Order.status.in_(["queued", "picking", "packing"]))
        .order_by(Order.priority_score.desc())
        .limit(count)
        .all()
    )

    transitions = {
        "queued": "picking",
        "picking": "packing",
        "packing": "shipped",
    }

    for order in queued_orders:
        next_status = transitions.get(order.status)
        if not next_status:
            continue

        prev_status = order.status
        order.status = next_status

        # Update shipments too
        shipments = db.query(Shipment).filter(Shipment.order_id == order.id).all()
        for s in shipments:
            if s.status == prev_status or s.status in transitions:
                s.status = next_status
                db.add(ShipmentEvent(
                    shipment_id=s.id,
                    status=next_status,
                    message=f"Advanced: {prev_status} → {next_status}",
                    location=s.fulfillment_center.code if s.fulfillment_center else "",
                ))

                # If shipped, release reserved inventory
                if next_status == "shipped":
                    for item in order.items:
                        from models import Inventory
                        inv = db.query(Inventory).filter(
                            Inventory.product_id == item.product_id,
                            Inventory.fulfillment_center_id == s.fulfillment_center_id,
                        ).first()
                        if inv:
                            inv.reserved_qty = max(0, inv.reserved_qty - item.quantity)
                            inv.onhand_qty = max(0, inv.onhand_qty - item.quantity)

        results.append({
            "order_number": order.order_number,
            "priority_score": order.priority_score,
            "previous": prev_status,
            "new": next_status,
        })

        _log_step(db, 0, order, "queue_advance",
                  f"{order.order_number}: {prev_status} → {next_status} (priority: {order.priority_score})",
                  {"previous": prev_status, "new": next_status})

    db.commit()
    return results


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _log_step(db: Session, step_number: int, order, action_type: str, summary: str,
              details: dict, severity: str = "info"):
    log_agent_action(
        db,
        agent_name="pipeline",
        action_type=action_type,
        step_number=step_number,
        entity_type="order",
        entity_id=order.id,
        input_summary=f"Order {order.order_number} ({order.shipping_tier}" + (", VIP" if order.is_vip else "") + f") → {order.recipient_city}, {order.recipient_state}",
        output_summary=summary,
        details=details,
        severity=severity,
    )


def _generate_tracking(carrier_name: str) -> str:
    carrier_name = carrier_name.lower()
    if "ups" in carrier_name:
        return "1Z" + "".join(random.choices(string.ascii_uppercase + string.digits, k=16))
    elif "fedex" in carrier_name:
        return "".join(random.choices(string.digits, k=12))
    else:
        return "9400" + "".join(random.choices(string.digits, k=18))
