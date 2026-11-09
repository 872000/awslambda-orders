"""Offline tests for the orders Lambda handler.

Run from the repo root with:  python3 -m unittest discover -s tests -v

Everything runs against FakeDynamoDBTable — no boto3, no AWS account,
no credentials, no network.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import handler  # noqa: E402
from fake_dynamodb import FakeDynamoDBTable  # noqa: E402


def event(method, path, body=None, path_params=None, query=None):
    return {
        "httpMethod": method,
        "path": path,
        "pathParameters": path_params,
        "queryStringParameters": query,
        "body": None if body is None else json.dumps(body),
        "isBase64Encoded": False,
    }


def valid_order(customer_id="cust-1"):
    return {
        "customer_id": customer_id,
        "items": [
            {"sku": "WIDGET-A", "quantity": 2, "unit_price_cents": 1500},
            {"sku": "GADGET-B", "quantity": 1, "unit_price_cents": 999},
        ],
        "notes": "Leave at the door",
    }


class HandlerTestCase(unittest.TestCase):
    def setUp(self):
        self.table = FakeDynamoDBTable()
        handler.TABLE_OVERRIDE = self.table

    def tearDown(self):
        handler.TABLE_OVERRIDE = None

    # -- helpers ---------------------------------------------------------

    def call(self, *args, **kwargs):
        response = handler.lambda_handler(event(*args, **kwargs))
        body = json.loads(response["body"])
        return response["statusCode"], body

    def create(self, payload=None, customer_id="cust-1"):
        status, body = self.call(
            "POST", "/orders", body=payload or valid_order(customer_id)
        )
        self.assertEqual(status, 201, body)
        return body

    # -- create ----------------------------------------------------------

    def test_create_order_success(self):
        status, body = self.call("POST", "/orders", body=valid_order())
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "PENDING")
        self.assertEqual(body["customer_id"], "cust-1")
        # Total is computed server-side: 2*1500 + 1*999 = 3999.
        self.assertEqual(body["total_cents"], 3999)
        self.assertEqual(body["notes"], "Leave at the door")
        self.assertTrue(body["order_id"])
        self.assertEqual(body["created_at"], body["updated_at"])
        # Persisted: a GET round-trips the same order.
        _, fetched = self.call(
            "GET", f"/orders/{body['order_id']}",
            path_params={"order_id": body["order_id"]},
        )
        self.assertEqual(fetched, body)

    def test_create_ignores_client_supplied_total_and_status(self):
        payload = valid_order()
        payload["total_cents"] = 1
        payload["status"] = "DELIVERED"
        _, body = self.call("POST", "/orders", body=payload)
        self.assertEqual(body["total_cents"], 3999)
        self.assertEqual(body["status"], "PENDING")

    def test_create_rejects_invalid_json(self):
        response = handler.lambda_handler(
            {
                "httpMethod": "POST",
                "path": "/orders",
                "body": "{not json",
                "isBase64Encoded": False,
            }
        )
        self.assertEqual(response["statusCode"], 400)

    def test_create_rejects_empty_body(self):
        status, body = self.call("POST", "/orders", body=None)
        self.assertEqual(status, 400)
        self.assertIn("JSON", body["error"])

    def test_create_rejects_missing_customer_id(self):
        payload = valid_order()
        del payload["customer_id"]
        status, body = self.call("POST", "/orders", body=payload)
        self.assertEqual(status, 400)
        self.assertIn("customer_id", body["error"])

    def test_create_rejects_blank_customer_id(self):
        payload = valid_order()
        payload["customer_id"] = "   "
        status, _ = self.call("POST", "/orders", body=payload)
        self.assertEqual(status, 400)

    def test_create_rejects_empty_items(self):
        payload = valid_order()
        payload["items"] = []
        status, body = self.call("POST", "/orders", body=payload)
        self.assertEqual(status, 400)
        self.assertIn("items", body["error"])

    def test_create_rejects_missing_items(self):
        payload = valid_order()
        del payload["items"]
        status, _ = self.call("POST", "/orders", body=payload)
        self.assertEqual(status, 400)

    def test_create_rejects_zero_quantity(self):
        payload = valid_order()
        payload["items"][0]["quantity"] = 0
        status, body = self.call("POST", "/orders", body=payload)
        self.assertEqual(status, 400)
        self.assertIn("quantity", body["error"])

    def test_create_rejects_fractional_quantity(self):
        payload = valid_order()
        payload["items"][0]["quantity"] = 1.5
        status, _ = self.call("POST", "/orders", body=payload)
        self.assertEqual(status, 400)

    def test_create_rejects_negative_price(self):
        payload = valid_order()
        payload["items"][0]["unit_price_cents"] = -5
        status, body = self.call("POST", "/orders", body=payload)
        self.assertEqual(status, 400)
        self.assertIn("unit_price_cents", body["error"])

    def test_create_rejects_missing_sku(self):
        payload = valid_order()
        del payload["items"][0]["sku"]
        status, body = self.call("POST", "/orders", body=payload)
        self.assertEqual(status, 400)
        self.assertIn("sku", body["error"])

    def test_create_rejects_non_string_notes(self):
        payload = valid_order()
        payload["notes"] = 42
        status, _ = self.call("POST", "/orders", body=payload)
        self.assertEqual(status, 400)

    def test_create_allows_free_item(self):
        payload = valid_order()
        payload["items"] = [{"sku": "FREE", "quantity": 1, "unit_price_cents": 0}]
        status, body = self.call("POST", "/orders", body=payload)
        self.assertEqual(status, 201)
        self.assertEqual(body["total_cents"], 0)

    # -- get -------------------------------------------------------------

    def test_get_missing_order_returns_404(self):
        status, body = self.call(
            "GET", "/orders/nope", path_params={"order_id": "nope"}
        )
        self.assertEqual(status, 404)
        self.assertIn("nope", body["error"])

    # -- list ------------------------------------------------------------

    def test_list_empty(self):
        status, body = self.call("GET", "/orders")
        self.assertEqual(status, 200)
        self.assertEqual(body["orders"], [])
        self.assertEqual(body["count"], 0)
        self.assertIsNone(body["next_token"])

    def test_list_returns_all_orders(self):
        self.create(customer_id="cust-1")
        self.create(customer_id="cust-2")
        status, body = self.call("GET", "/orders")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 2)
        self.assertIsNone(body["next_token"])

    def test_list_paginates(self):
        created = [self.create() for _ in range(5)]
        seen = []
        token = None
        pages = 0
        while True:
            query = {"limit": "2"}
            if token:
                query["next_token"] = token
            status, body = self.call("GET", "/orders", query=query)
            self.assertEqual(status, 200)
            self.assertLessEqual(len(body["orders"]), 2)
            seen.extend(o["order_id"] for o in body["orders"])
            pages += 1
            token = body["next_token"]
            if token is None:
                break
            self.assertLess(pages, 10, "pagination looped too long")
        self.assertEqual(pages, 3)  # 5 orders at 2 per page
        self.assertEqual(sorted(seen), sorted(o["order_id"] for o in created))

    def test_list_rejects_bad_limit(self):
        for bad in ("0", "101", "-3", "abc"):
            status, body = self.call("GET", "/orders", query={"limit": bad})
            self.assertEqual(status, 400, bad)
            self.assertIn("limit", body["error"])

    def test_list_rejects_bad_next_token(self):
        status, body = self.call(
            "GET", "/orders", query={"next_token": "not-a-token!!!"}
        )
        self.assertEqual(status, 400)
        self.assertIn("next_token", body["error"])

    def test_list_filters_by_customer(self):
        self.create(customer_id="cust-1")
        self.create(customer_id="cust-2")
        self.create(customer_id="cust-1")
        status, body = self.call(
            "GET", "/orders", query={"customer_id": "cust-1"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 2)
        self.assertTrue(
            all(o["customer_id"] == "cust-1" for o in body["orders"])
        )

    # -- status updates ---------------------------------------------------

    def _set_status(self, order_id, status):
        return self.call(
            "PATCH",
            f"/orders/{order_id}",
            body={"status": status},
            path_params={"order_id": order_id},
        )

    def test_full_status_lifecycle(self):
        order = self.create()
        order_id = order["order_id"]
        for next_status in ("CONFIRMED", "SHIPPED", "DELIVERED"):
            status, body = self._set_status(order_id, next_status)
            self.assertEqual(status, 200, body)
            self.assertEqual(body["status"], next_status)
            self.assertEqual(body["order_id"], order_id)

    def test_cancel_from_pending(self):
        order = self.create()
        status, body = self._set_status(order["order_id"], "CANCELLED")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "CANCELLED")

    def test_cancel_from_confirmed(self):
        order = self.create()
        self._set_status(order["order_id"], "CONFIRMED")
        status, body = self._set_status(order["order_id"], "CANCELLED")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "CANCELLED")

    def test_rejects_skipped_transition(self):
        order = self.create()
        status, body = self._set_status(order["order_id"], "SHIPPED")
        self.assertEqual(status, 409)
        self.assertIn("PENDING", body["error"])
        self.assertIn("SHIPPED", body["error"])

    def test_rejects_transition_from_delivered(self):
        order = self.create()
        for next_status in ("CONFIRMED", "SHIPPED", "DELIVERED"):
            self._set_status(order["order_id"], next_status)
        status, _ = self._set_status(order["order_id"], "CANCELLED")
        self.assertEqual(status, 409)

    def test_rejects_transition_from_cancelled(self):
        order = self.create()
        self._set_status(order["order_id"], "CANCELLED")
        status, _ = self._set_status(order["order_id"], "CONFIRMED")
        self.assertEqual(status, 409)

    def test_update_missing_order_returns_404(self):
        status, body = self._set_status("nope", "CONFIRMED")
        self.assertEqual(status, 404)

    def test_update_rejects_unknown_status(self):
        order = self.create()
        status, body = self._set_status(order["order_id"], "FLYING")
        self.assertEqual(status, 400)
        self.assertIn("status", body["error"])

    def test_update_rejects_missing_status(self):
        order = self.create()
        status, _ = self.call(
            "PATCH",
            f"/orders/{order['order_id']}",
            body={},
            path_params={"order_id": order["order_id"]},
        )
        self.assertEqual(status, 400)

    # -- routing ----------------------------------------------------------

    def test_unknown_route_returns_404(self):
        status, _ = self.call("DELETE", "/orders")
        self.assertEqual(status, 404)
        status, _ = self.call("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
