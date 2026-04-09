ORDERS = [
    {
        "id": 1041,
        "order_number": "ORD-1041",
        "status": "Exception",
        "created_date": "2025-04-09T08:00:00Z",
        "recipient": {
            "name": "Sarah Chen",
            "address": {"city": "New York", "state": "NY", "country": "US"},
        },
        "products": [
            {"sku": "SHOE-BLK-10", "name": "Black Sneaker Size 10", "quantity": 5}
        ],
        "shipments": [
            {
                "id": 2041,
                "status": "Exception",
                "location": {"id": 1, "name": "NYC Fulfillment Center"},
                "status_details": [
                    {
                        "type": "OutOfStock",
                        "message": "0 units available at NYC FC",
                    }
                ],
                "tracking": None,
            }
        ],
    },
    {
        "id": 1042,
        "order_number": "ORD-1042",
        "status": "Exception",
        "created_date": "2025-04-09T08:15:00Z",
        "recipient": {
            "name": "Marcus Webb",
            "address": {"city": "Austin", "state": "TX", "country": "US"},
        },
        "products": [
            {"sku": "GIFT-SET-HOLIDAY", "name": "Holiday Gift Set", "quantity": 1}
        ],
        "shipments": [
            {
                "id": 2042,
                "status": "Exception",
                "location": {"id": 2, "name": "Dallas Fulfillment Center"},
                "status_details": [
                    {
                        "type": "SystemError",
                        "message": "Bundle GIFT-SET-HOLIDAY not configured",
                    }
                ],
                "tracking": None,
            }
        ],
    },
    {
        "id": 1043,
        "order_number": "ORD-1043",
        "status": "OnHold",
        "created_date": "2025-04-09T08:30:00Z",
        "recipient": {
            "name": "Pierre Dubois",
            "address": {"city": "Toronto", "state": "ON", "country": "CA"},
        },
        "products": [
            {"sku": "WOOL-COAT-M", "name": "Wool Coat Medium", "quantity": 2}
        ],
        "shipments": [
            {
                "id": 2043,
                "status": "OnHold",
                "location": {"id": 3, "name": "Chicago Fulfillment Center"},
                "status_details": [
                    {
                        "type": "OnHold",
                        "message": "Missing tariff code for international shipment to Canada",
                    }
                ],
                "tracking": None,
            }
        ],
    },
    {
        "id": 1044,
        "order_number": "ORD-1044",
        "status": "Processing",
        "created_date": "2025-04-09T08:45:00Z",
        "recipient": {
            "name": "Emma Rodriguez",
            "address": {"city": "Los Angeles", "state": "CA", "country": "US"},
        },
        "products": [
            {
                "sku": "LIGHT-ROAST",
                "name": "Light Roast Coffee 500g",
                "quantity": 3,
            }
        ],
        "shipments": [
            {
                "id": 2044,
                "status": "Processing",
                "location": {"id": 4, "name": "LA Fulfillment Center"},
                "status_details": [],
                "tracking": None,
            }
        ],
    },
]

INVENTORY = [
    {
        "id": 101,
        "sku": "SHOE-BLK-10",
        "name": "Black Sneaker Size 10",
        "total_fulfillable_quantity": 47,
        "locations": [
            {
                "fulfillment_center": {"id": 1, "name": "NYC Fulfillment Center"},
                "fulfillable_quantity": 0,
                "onhand_quantity": 0,
            },
            {
                "fulfillment_center": {"id": 5, "name": "LA Fulfillment Center"},
                "fulfillable_quantity": 47,
                "onhand_quantity": 52,
            },
        ],
    },
    {
        "id": 102,
        "sku": "GIFT-SET-HOLIDAY",
        "name": "Holiday Gift Set",
        "total_fulfillable_quantity": 0,
        "locations": [],
    },
    {
        "id": 103,
        "sku": "WOOL-COAT-M",
        "name": "Wool Coat Medium",
        "total_fulfillable_quantity": 30,
        "locations": [
            {
                "fulfillment_center": {"id": 3, "name": "Chicago Fulfillment Center"},
                "fulfillable_quantity": 30,
                "onhand_quantity": 30,
            }
        ],
    },
    {
        "id": 104,
        "sku": "LIGHT-ROAST",
        "name": "Light Roast Coffee 500g",
        "total_fulfillable_quantity": 200,
        "locations": [
            {
                "fulfillment_center": {"id": 4, "name": "LA Fulfillment Center"},
                "fulfillable_quantity": 200,
                "onhand_quantity": 210,
            }
        ],
    },
]
