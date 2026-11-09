"""Hand-rolled fake DynamoDB table for offline tests.

Implements only the DynamoDB operations the handler uses — put_item,
get_item, update_item and scan — with just enough expression support for
the handler's exact ConditionExpression / UpdateExpression / FilterExpression
strings. No AWS SDK, credentials, or network access required.
"""

import copy


class ConditionalCheckFailedException(Exception):
    """Stand-in for boto3's ConditionalCheckFailedException."""


class FakeDynamoDBTable:
    def __init__(self):
        self._items = {}

    # -- writes ----------------------------------------------------------

    def put_item(self, Item, ConditionExpression=None, **kwargs):
        order_id = Item["order_id"]
        if (
            ConditionExpression == "attribute_not_exists(order_id)"
            and order_id in self._items
        ):
            raise ConditionalCheckFailedException(
                "The conditional request failed"
            )
        self._items[order_id] = copy.deepcopy(Item)
        return {}

    def update_item(
        self,
        Key,
        UpdateExpression,
        ConditionExpression=None,
        ExpressionAttributeNames=None,
        ExpressionAttributeValues=None,
        ReturnValues=None,
        **kwargs
    ):
        order_id = Key["order_id"]
        if (
            ConditionExpression == "attribute_exists(order_id)"
            and order_id not in self._items
        ):
            raise ConditionalCheckFailedException(
                "The conditional request failed"
            )
        item = self._items[order_id]
        # The handler only ever sends:
        #   SET #status = :status, updated_at = :updated_at
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        assert UpdateExpression.startswith("SET "), "unsupported expression"
        for assignment in UpdateExpression[4:].split(","):
            lhs, _, rhs = assignment.strip().partition("=")
            lhs = lhs.strip()
            rhs = rhs.strip()
            attr = names.get(lhs, lhs)
            item[attr] = copy.deepcopy(values[rhs])
        if ReturnValues == "ALL_NEW":
            return {"Attributes": copy.deepcopy(item)}
        return {}

    # -- reads -----------------------------------------------------------

    def get_item(self, Key, **kwargs):
        item = self._items.get(Key["order_id"])
        if item is None:
            return {}
        return {"Item": copy.deepcopy(item)}

    def scan(
        self,
        Limit=None,
        ExclusiveStartKey=None,
        FilterExpression=None,
        ExpressionAttributeValues=None,
        **kwargs
    ):
        items = sorted(self._items.values(), key=lambda o: o["created_at"])
        if FilterExpression == "customer_id = :cid":
            wanted = (ExpressionAttributeValues or {}).get(":cid")
            items = [o for o in items if o.get("customer_id") == wanted]

        start = 0
        if ExclusiveStartKey is not None:
            ids = [o["order_id"] for o in items]
            if ExclusiveStartKey["order_id"] in ids:
                start = ids.index(ExclusiveStartKey["order_id"]) + 1

        page = items[start:]
        last_key = None
        if Limit is not None and len(page) > Limit:
            page = page[:Limit]
            last_key = {"order_id": page[-1]["order_id"]}

        result = {"Items": copy.deepcopy(page), "Count": len(page)}
        if last_key is not None:
            result["LastEvaluatedKey"] = last_key
        return result
