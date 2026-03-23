"""
Mock Order Service — 模擬電商訂單查詢 API
生產環境應替換為真實 ERP/OMS API 呼叫
"""
from datetime import datetime, timedelta, timezone

_ORDERS = {
    "ORD-2026-001234": {
        "order_id": "ORD-2026-001234",
        "customer_id": "C001",
        "status": "shipped",
        "status_label": "已出貨",
        "created_at": "2026-03-15T10:00:00Z",
        "shipped_at": "2026-03-16T14:30:00Z",
        "estimated_delivery": "2026-03-19T18:00:00Z",
        "tracking_number": "TW-9876543210",
        "items": [
            {"name": "Nike Air Max 90 US9", "qty": 1, "price": 3200},
            {"name": "運動襪三入組", "qty": 2, "price": 480},
        ],
        "total": 4160,
        "payment_status": "paid",
        "shipping_address": "台北市信義區松仁路100號",
    },
    "ORD-2026-001235": {
        "order_id": "ORD-2026-001235",
        "customer_id": "C002",
        "status": "delivered",
        "status_label": "已送達",
        "created_at": "2026-03-10T09:00:00Z",
        "shipped_at": "2026-03-11T11:00:00Z",
        "delivered_at": "2026-03-13T15:45:00Z",
        "tracking_number": "TW-9876543211",
        "items": [
            {"name": "iPhone 16 Pro 保護殼", "qty": 1, "price": 890},
        ],
        "total": 890,
        "payment_status": "paid",
        "shipping_address": "台中市西屯區台灣大道三段200號",
    },
    "ORD-2026-001236": {
        "order_id": "ORD-2026-001236",
        "customer_id": "C003",
        "status": "pending",
        "status_label": "處理中",
        "created_at": "2026-03-18T16:00:00Z",
        "estimated_ship_at": "2026-03-20T12:00:00Z",
        "estimated_delivery": "2026-03-22T18:00:00Z",
        "items": [
            {"name": "Sony WH-1000XM5 耳機", "qty": 1, "price": 9900},
        ],
        "total": 9900,
        "payment_status": "paid",
        "shipping_address": "高雄市前鎮區中山二路300號",
    },
    "ORD-2026-001237": {
        "order_id": "ORD-2026-001237",
        "customer_id": "C001",
        "status": "cancelled",
        "status_label": "已取消",
        "created_at": "2026-03-14T08:00:00Z",
        "cancelled_at": "2026-03-14T09:30:00Z",
        "cancel_reason": "客戶主動取消",
        "items": [
            {"name": "Uniqlo 連帽外套 L", "qty": 1, "price": 1290},
        ],
        "total": 1290,
        "payment_status": "refunded",
        "refund_amount": 1290,
        "shipping_address": "台北市信義區松仁路100號",
    },
    "ORD-2026-001238": {
        "order_id": "ORD-2026-001238",
        "customer_id": "C004",
        "status": "return_processing",
        "status_label": "退貨處理中",
        "created_at": "2026-03-05T14:00:00Z",
        "shipped_at": "2026-03-06T10:00:00Z",
        "delivered_at": "2026-03-08T16:00:00Z",
        "return_requested_at": "2026-03-12T10:00:00Z",
        "tracking_number": "TW-9876543212",
        "items": [
            {"name": "ZARA 洋裝 S", "qty": 1, "price": 1590},
        ],
        "total": 1590,
        "payment_status": "return_pending",
        "shipping_address": "新北市板橋區文化路一段100號",
    },
    "ORD-2026-001239": {
        "order_id": "ORD-2026-001239",
        "customer_id": "C005",
        "status": "shipped",
        "status_label": "已出貨",
        "created_at": "2026-03-17T11:00:00Z",
        "shipped_at": "2026-03-18T09:00:00Z",
        "estimated_delivery": "2026-03-21T18:00:00Z",
        "tracking_number": "TW-9876543213",
        "items": [
            {"name": "Nintendo Switch OLED 白色", "qty": 1, "price": 10800},
            {"name": "Mario Kart 8 Deluxe", "qty": 1, "price": 1680},
        ],
        "total": 12480,
        "payment_status": "paid",
        "shipping_address": "桃園市中壢區中央西路200號",
    },
    "ORD-2026-001240": {
        "order_id": "ORD-2026-001240",
        "customer_id": "C006",
        "status": "pending_payment",
        "status_label": "待付款",
        "created_at": "2026-03-19T20:00:00Z",
        "payment_deadline": "2026-03-20T20:00:00Z",
        "items": [
            {"name": "Dyson V15 吸塵器", "qty": 1, "price": 19900},
        ],
        "total": 19900,
        "payment_status": "pending",
        "shipping_address": "台北市大安區敦化南路一段50號",
    },
    "ORD-2026-001241": {
        "order_id": "ORD-2026-001241",
        "customer_id": "C007",
        "status": "delivered",
        "status_label": "已送達",
        "created_at": "2026-03-01T10:00:00Z",
        "shipped_at": "2026-03-02T14:00:00Z",
        "delivered_at": "2026-03-04T11:30:00Z",
        "tracking_number": "TW-9876543214",
        "items": [
            {"name": "IKEA 書桌椅組合", "qty": 1, "price": 5990},
        ],
        "total": 5990,
        "payment_status": "paid",
        "shipping_address": "台南市東區崇學路100號",
    },
    "ORD-2026-001242": {
        "order_id": "ORD-2026-001242",
        "customer_id": "C008",
        "status": "processing",
        "status_label": "備貨中",
        "created_at": "2026-03-19T08:00:00Z",
        "estimated_ship_at": "2026-03-21T14:00:00Z",
        "estimated_delivery": "2026-03-24T18:00:00Z",
        "items": [
            {"name": "Adidas 運動鞋 US8", "qty": 1, "price": 3500},
            {"name": "Adidas 運動短褲 M", "qty": 2, "price": 1200},
        ],
        "total": 5900,
        "payment_status": "paid",
        "shipping_address": "嘉義市東區垂楊路200號",
    },
    "ORD-2026-001243": {
        "order_id": "ORD-2026-001243",
        "customer_id": "C009",
        "status": "shipped",
        "status_label": "已出貨",
        "created_at": "2026-03-16T13:00:00Z",
        "shipped_at": "2026-03-17T10:00:00Z",
        "estimated_delivery": "2026-03-20T18:00:00Z",
        "tracking_number": "TW-9876543215",
        "items": [
            {"name": "Apple Watch Series 10 黑色", "qty": 1, "price": 12900},
        ],
        "total": 12900,
        "payment_status": "paid",
        "shipping_address": "台北市松山區民生東路三段100號",
    },
    "ORD-2026-001244": {
        "order_id": "ORD-2026-001244",
        "customer_id": "C010",
        "status": "delivered",
        "status_label": "已送達",
        "created_at": "2026-02-28T09:00:00Z",
        "shipped_at": "2026-03-01T11:00:00Z",
        "delivered_at": "2026-03-03T14:00:00Z",
        "tracking_number": "TW-9876543216",
        "items": [
            {"name": "咖啡機 Nespresso Vertuo Pop", "qty": 1, "price": 5990},
        ],
        "total": 5990,
        "payment_status": "paid",
        "shipping_address": "新竹市東區光復路一段100號",
    },
    "ORD-2026-001245": {
        "order_id": "ORD-2026-001245",
        "customer_id": "C001",
        "status": "shipped",
        "status_label": "已出貨",
        "created_at": "2026-03-18T10:00:00Z",
        "shipped_at": "2026-03-19T08:00:00Z",
        "estimated_delivery": "2026-03-22T18:00:00Z",
        "tracking_number": "TW-9876543217",
        "items": [
            {"name": "無線充電板 20W", "qty": 1, "price": 890},
        ],
        "total": 890,
        "payment_status": "paid",
        "shipping_address": "台北市信義區松仁路100號",
    },
    "ORD-2026-001246": {
        "order_id": "ORD-2026-001246",
        "customer_id": "C011",
        "status": "cancelled",
        "status_label": "已取消",
        "created_at": "2026-03-17T15:00:00Z",
        "cancelled_at": "2026-03-17T16:00:00Z",
        "cancel_reason": "商品缺貨，系統自動取消",
        "items": [
            {"name": "Nike Air Max 90 US10", "qty": 1, "price": 3200},
        ],
        "total": 3200,
        "payment_status": "refunded",
        "refund_amount": 3200,
        "shipping_address": "台北市中山區中山北路二段50號",
    },
    "ORD-2026-001247": {
        "order_id": "ORD-2026-001247",
        "customer_id": "C012",
        "status": "shipped",
        "status_label": "已出貨",
        "created_at": "2026-03-15T16:00:00Z",
        "shipped_at": "2026-03-16T10:00:00Z",
        "estimated_delivery": "2026-03-20T18:00:00Z",
        "tracking_number": "TW-9876543218",
        "items": [
            {"name": "Samsung 65吋 QLED 電視", "qty": 1, "price": 45000},
        ],
        "total": 45000,
        "payment_status": "paid",
        "shipping_address": "台北市內湖區瑞光路100號",
    },
    "ORD-2026-001248": {
        "order_id": "ORD-2026-001248",
        "customer_id": "C013",
        "status": "processing",
        "status_label": "備貨中",
        "created_at": "2026-03-20T07:00:00Z",
        "estimated_ship_at": "2026-03-21T16:00:00Z",
        "estimated_delivery": "2026-03-25T18:00:00Z",
        "items": [
            {"name": "Kindle Paperwhite 11代", "qty": 1, "price": 4590},
        ],
        "total": 4590,
        "payment_status": "paid",
        "shipping_address": "台北市大安區和平東路一段200號",
    },
}


def get_order_status(order_id: str) -> dict:
    """查詢訂單狀態與詳細資訊"""
    order = _ORDERS.get(order_id.upper().strip())
    if not order:
        return {
            "found": False,
            "order_id": order_id,
            "message": f"找不到訂單 {order_id}，請確認訂單編號是否正確。",
        }
    return {"found": True, **order}


def get_order_items(order_id: str) -> dict:
    """查詢訂單商品清單"""
    order = _ORDERS.get(order_id.upper().strip())
    if not order:
        return {"found": False, "order_id": order_id}
    return {
        "found": True,
        "order_id": order_id,
        "items": order.get("items", []),
        "total": order.get("total", 0),
    }


def cancel_order(order_id: str) -> dict:
    """申請取消訂單（僅 pending/processing/pending_payment 狀態可取消）"""
    order = _ORDERS.get(order_id.upper().strip())
    if not order:
        return {"success": False, "message": f"找不到訂單 {order_id}"}

    cancellable_statuses = {"pending", "processing", "pending_payment"}
    if order["status"] not in cancellable_statuses:
        status_label = order.get("status_label", order["status"])
        return {
            "success": False,
            "order_id": order_id,
            "current_status": status_label,
            "message": (
                f"訂單目前狀態為「{status_label}」，無法取消。"
                "已出貨或已送達的訂單請申請退貨退款。"
            ),
        }

    return {
        "success": True,
        "order_id": order_id,
        "message": (
            f"訂單 {order_id} 取消申請已受理。"
            "款項將在 3-5 個工作天內退回原支付帳戶。"
        ),
        "refund_amount": order.get("total", 0),
    }
