# awslambda-orders

A serverless orders API on AWS: one Python Lambda function backed by
DynamoDB and fronted by API Gateway, deployed with the AWS SAM CLI.
Create an order, fetch it, list orders with pagination, and move an order
through its lifecycle — with the status transitions enforced in code.

No real AWS account is needed to develop or test this project: the test
suite runs fully offline against a hand-rolled fake DynamoDB table, and
`boto3` is only imported lazily at runtime inside Lambda.

## API design

Base URL after deploy: `https://<api-id>.execute-api.<region>.amazonaws.com/Prod`

| Method | Path | Description |
| ------ | ---- | ----------- |
| `POST` | `/orders` | Create an order |
| `GET` | `/orders/{order_id}` | Get one order |
| `GET` | `/orders` | List orders (paginated, optional `customer_id` filter) |
| `PATCH` | `/orders/{order_id}` | Update an order's status |

### Create an order — `POST /orders`

Request body:

```json
{
  "customer_id": "cust-123",
  "items": [
    { "sku": "WIDGET-A", "quantity": 2, "unit_price_cents": 1500 },
    { "sku": "GADGET-B", "quantity": 1, "unit_price_cents": 999 }
  ],
  "notes": "Leave at the door"
}
```

Response `201`:

```json
{
  "order_id": "9f2c3a1e-…",
  "customer_id": "cust-123",
  "items": [ { "sku": "WIDGET-A", "quantity": 2, "unit_price_cents": 1500 } ],
  "total_cents": 3999,
  "status": "PENDING",
  "notes": "Leave at the door",
  "created_at": "2026-11-09T05:30:00Z",
  "updated_at": "2026-11-09T05:30:00Z"
}
```

`total_cents` is always computed server-side from the items — a client
cannot set its own total or status. Validation rejects a missing/blank
`customer_id`, an empty `items` list, a missing `sku`, a non-integer or
non-positive `quantity`, and a negative `unit_price_cents` (all `400`).

### Get an order — `GET /orders/{order_id}`

Returns the order (`200`) or `404` when the id does not exist.

### List orders — `GET /orders`

Query parameters:

- `limit` — page size, 1–100 (default 20).
- `next_token` — opaque token from a previous page's response.
- `customer_id` — only return orders for this customer.

Response `200`:

```json
{
  "orders": [ { "...": "order" } ],
  "count": 2,
  "next_token": "eyJvcmRlcl9pZCI6IC…"
}
```

`next_token` is `null` on the last page. Invalid `limit` or `next_token`
values return `400`.

### Update status — `PATCH /orders/{order_id}`

```json
{ "status": "CONFIRMED" }
```

The order lifecycle is a small state machine:

```
PENDING ──► CONFIRMED ──► SHIPPED ──► DELIVERED
   │            │
   └────► CANCELLED ◄───┘
```

- Legal moves return the updated order (`200`).
- An illegal move (e.g. `PENDING → SHIPPED`, anything out of
  `DELIVERED` or `CANCELLED`) returns `409 Conflict`.
- An unknown status returns `400`; a missing order returns `404`.

### Errors

All errors are JSON: `{ "error": "human-readable message" }`, with the
HTTP status `400` (bad input), `404` (no such route / order), or `409`
(illegal status transition).

## Architecture

```
                ┌──────────────┐
  client ─────► │ API Gateway  │ ──► Lambda (src/handler.py)
                └──────────────┘        │ boto3
                                        ▼
                                  DynamoDB table
                                  (PK: order_id, on-demand)
```

- `src/handler.py` — the Lambda handler: routing, validation, the
  status state machine, and the DynamoDB calls. `boto3` is imported
  lazily so the module loads without the AWS SDK; the table name comes
  from the `ORDERS_TABLE` environment variable (set by the SAM template).
- `template.yaml` — the SAM template: the Lambda function, the API
  Gateway REST API with the four routes, and the on-demand DynamoDB
  table (least-privilege `DynamoDBCrudPolicy` on that table only).
- `tests/` — the offline test suite (see below).

## Setup

Requires Python 3.12+ (only the standard library for tests). For a real
deploy you also need the [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html),
an AWS account, and credentials configured (`aws configure`).

```bash
git clone https://github.com/872000/awslambda-orders.git
cd awslambda-orders
```

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

31 tests, fully offline — no AWS credentials, no network, no `boto3`
or `moto` installed. The suite drives `src/handler.py` through the same
API Gateway proxy events Lambda receives, against a hand-rolled fake
DynamoDB table (`tests/fake_dynamodb.py`) injected via the handler's
`TABLE_OVERRIDE` seam. It covers:

- create-order validation (bad JSON, missing fields, bad quantities/prices)
- server-side total computation and ignored client-supplied `status`/`total`
- get: found and missing (`404`)
- list: empty, all orders, `customer_id` filter, page-by-page pagination
  with `limit`/`next_token`, and invalid `limit`/`next_token` (`400`)
- the full `PENDING → CONFIRMED → SHIPPED → DELIVERED` lifecycle,
  cancellation from `PENDING`/`CONFIRMED`, illegal transitions (`409`),
  unknown statuses (`400`), and updates to missing orders (`404`)

## Deploying to AWS

```bash
sam build
sam deploy --guided   # first time: answers the stack name / region prompts
sam deploy            # later deploys reuse the saved answers
```

`sam deploy --guided` prints the API base URL (also in the stack outputs
as `ApiUrl`) once the stack is up. Verify it live:

```bash
API=$(aws cloudformation describe-stacks --stack-name awslambda-orders \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text)

curl -X POST "$API" -H 'Content-Type: application/json' -d \
  '{"customer_id":"cust-123","items":[{"sku":"WIDGET-A","quantity":2,"unit_price_cents":1500}]}'
```

Tear down everything (function, API, table) with:

```bash
sam delete
```

Note: the DynamoDB table uses on-demand billing, so an idle deployment
costs nothing; the table holds real order data and is deleted with the stack.

## License

MIT — see [LICENSE](LICENSE).
