"""AWS Lambda handler for a serverless orders API.

Routes (fronted by API Gateway, REST design — see README):

    POST   /orders                 create an order
    GET    /orders/{order_id}      get one order
    GET    /orders                 list orders (paginated)
    PATCH  /orders/{order_id}      update an order's status

Persistence is a single DynamoDB table (name from the ORDERS_TABLE
environment variable, partition key ``order_id``). ``boto3`` is imported
lazily inside :func:`_table` so the module can be imported — and tested —
on a machine with no AWS SDK or credentials installed. Tests inject a
fake table by assigning :data:`TABLE_OVERRIDE`.
"""

import base64
import json
import os
import re
import uuid
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration / table access
# ---------------------------------------------------------------------------

TABLE_NAME = os.environ.get("ORDERS_TABLE", "orders")

# Test seam: tests assign a fake table object here. When None, a real
# boto3 DynamoDB table is created on first use (Lambda runtime only).
TABLE_OVERRIDE = None

# Order lifecycle. Keys are the current status, values are the statuses
# an order may legally move to from there.
ALLOWED_TRANSITIONS = {
    "PENDING": {"CONFIRMED", "CANCELLED"},
    "CONFIRMED": {"SHIPPED", "CANCELLED"},
    "SHIPPED": {"DELIVERED"},
    "DELIVERED": set(),
    "CANCELLED": set(),
}

VALID_STATUSES = frozenset(ALLOWED_TRANSITIONS)


def _table():
    """Return the DynamoDB table (real or test-injected)."""
    if TABLE_OVERRIDE is not None:
        return TABLE_OVERRIDE
    import boto3  # imported lazily: no boto3 needed for offline tests

    return boto3.resource("dynamodb").Table(TABLE_NAME)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def _error(status_code, message):
    return _response(status_code, {"error": message})


def _parse_body(event):
    """Parse a JSON request body; returns (data, error_response)."""
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    if not raw.strip():
        return None, _error(400, "Request body must be valid JSON")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, _error(400, "Request body must be valid JSON")
    if not isinstance(data, dict):
        return None, _error(400, "Request body must be a JSON object")
    return data, None


def _encode_next_token(last_key):
    if not last_key:
        return None
    return base64.urlsafe_b64encode(
        json.dumps(last_key).encode("utf-8")
    ).decode("ascii")


def _decode_next_token(token):
    """Decode a pagination token; returns (key, error_response)."""
    if not token:
        return None, None
    try:
        key = json.loads(base64.urlsafe_b64decode(token.encode("ascii")))
    except (ValueError, json.JSONDecodeError):
        return None, _error(400, "Invalid next_token")
    if not isinstance(key, dict) or "order_id" not in key:
        return None, _error(400, "Invalid next_token")
    return key, None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_create_order(data):
    """Validate a create-order payload.

    Returns (order_fields, error_response). ``order_fields`` is the
    validated, normalised payload including the server-computed total.
    """
    customer_id = data.get("customer_id")
    if not isinstance(customer_id, str) or not customer_id.strip():
        return None, _error(400, "customer_id is required and must be a string")

    items = data.get("items")
    if not isinstance(items, list) or not items:
        return None, _error(400, "items must be a non-empty list")

    clean_items = []
    total_cents = 0
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            return None, _error(400, f"items[{i}] must be an object")
        sku = item.get("sku")
        if not isinstance(sku, str) or not sku.strip():
            return None, _error(400, f"items[{i}].sku is required")
        quantity = item.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            return None, _error(400, f"items[{i}].quantity must be a positive integer")
        unit_price_cents = item.get("unit_price_cents")
        if (
            isinstance(unit_price_cents, bool)
            or not isinstance(unit_price_cents, int)
            or unit_price_cents < 0
        ):
            return None, _error(400, f"items[{i}].unit_price_cents must be a non-negative integer")
        clean_items.append(
            {
                "sku": sku.strip(),
                "quantity": quantity,
                "unit_price_cents": unit_price_cents,
            }
        )
        total_cents += quantity * unit_price_cents

    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        return None, _error(400, "notes must be a string")

    return {
        "customer_id": customer_id.strip(),
        "items": clean_items,
        "total_cents": total_cents,
        "notes": notes,
    }, None


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _create_order(event):
    data, err = _parse_body(event)
    if err is not None:
        return err
    fields, err = _validate_create_order(data)
    if err is not None:
        return err

    now = _now_iso()
    order = {
        "order_id": str(uuid.uuid4()),
        "customer_id": fields["customer_id"],
        "items": fields["items"],
        "total_cents": fields["total_cents"],
        "status": "PENDING",
        "created_at": now,
        "updated_at": now,
    }
    if fields["notes"] is not None:
        order["notes"] = fields["notes"]

    _table().put_item(
        Item=order,
        ConditionExpression="attribute_not_exists(order_id)",
    )
    return _response(201, order)


def _get_order(event):
    order_id = (event.get("pathParameters") or {}).get("order_id")
    result = _table().get_item(Key={"order_id": order_id})
    order = result.get("Item")
    if order is None:
        return _error(404, f"Order {order_id} not found")
    return _response(200, order)


def _list_orders(event):
    params = event.get("queryStringParameters") or {}

    limit = 20
    raw_limit = params.get("limit")
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError:
            return _error(400, "limit must be an integer")
        if limit < 1 or limit > 100:
            return _error(400, "limit must be between 1 and 100")

    start_key, err = _decode_next_token(params.get("next_token"))
    if err is not None:
        return err

    customer_id = params.get("customer_id")

    kwargs = {"Limit": limit}
    if start_key is not None:
        kwargs["ExclusiveStartKey"] = start_key
    if customer_id:
        # Filter server-side in DynamoDB; pagination still applies to the
        # scanned page, which is the documented DynamoDB behaviour.
        kwargs["FilterExpression"] = "customer_id = :cid"
        kwargs["ExpressionAttributeValues"] = {":cid": customer_id}

    result = _table().scan(**kwargs)
    orders = sorted(result.get("Items", []), key=lambda o: o["created_at"])
    return _response(
        200,
        {
            "orders": orders,
            "count": len(orders),
            "next_token": _encode_next_token(result.get("LastEvaluatedKey")),
        },
    )


def _update_order_status(event):
    order_id = (event.get("pathParameters") or {}).get("order_id")
    data, err = _parse_body(event)
    if err is not None:
        return err

    new_status = data.get("status")
    if not isinstance(new_status, str) or new_status not in VALID_STATUSES:
        return _error(
            400,
            "status must be one of: " + ", ".join(sorted(VALID_STATUSES)),
        )

    table = _table()
    current = table.get_item(Key={"order_id": order_id}).get("Item")
    if current is None:
        return _error(404, f"Order {order_id} not found")

    allowed = ALLOWED_TRANSITIONS[current["status"]]
    if new_status not in allowed:
        return _error(
            409,
            f"Cannot move order from {current['status']} to {new_status}",
        )

    now = _now_iso()
    updated = table.update_item(
        Key={"order_id": order_id},
        UpdateExpression="SET #status = :status, updated_at = :updated_at",
        ConditionExpression="attribute_exists(order_id)",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": new_status, ":updated_at": now},
        ReturnValues="ALL_NEW",
    )["Attributes"]
    return _response(200, updated)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_ROUTES = [
    ("POST", re.compile(r"^/orders$"), _create_order),
    ("GET", re.compile(r"^/orders/(?P<order_id>[^/]+)$"), _get_order),
    ("GET", re.compile(r"^/orders$"), _list_orders),
    ("PATCH", re.compile(r"^/orders/(?P<order_id>[^/]+)$"), _update_order_status),
]


def lambda_handler(event, context=None):
    """API Gateway proxy-integration entry point."""
    method = event.get("httpMethod", "")
    path = event.get("path", "") or ""

    for route_method, pattern, handler in _ROUTES:
        if method != route_method:
            continue
        match = pattern.match(path)
        if match:
            event = dict(event)
            params = dict(event.get("pathParameters") or {})
            params.update(match.groupdict())
            event["pathParameters"] = params
            return handler(event)

    return _error(404, f"No route for {method} {path}")
