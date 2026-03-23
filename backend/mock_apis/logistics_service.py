"""
Mock Logistics Service — 模擬物流追蹤 API
生產環境應替換為真實物流 API（黑貓、新竹、7-11 物流等）
"""

_SHIPMENTS = {
    "TW-9876543210": {
        "tracking_number": "TW-9876543210",
        "carrier": "黑貓宅急便",
        "order_id": "ORD-2026-001234",
        "status": "in_transit",
        "status_label": "配送中",
        "origin": "台北轉運中心",
        "destination": "台北市信義區",
        "can_intercept": True,
        "intercept_fee": 100,
        "intercept_note": "攔截改地址費用 NT$100，僅限配送中且未進入最後一哩段",
        "events": [
            {"time": "2026-03-16 14:30", "location": "台北轉運中心", "description": "包裹已交付物流，等待轉運"},
            {"time": "2026-03-17 08:00", "location": "台北轉運中心", "description": "包裹已完成分撿，轉運中"},
            {"time": "2026-03-17 18:00", "location": "信義配送站", "description": "包裹已到達配送站，排入明日配送"},
        ],
        "estimated_delivery": "2026-03-19 18:00",
    },
    "TW-9876543211": {
        "tracking_number": "TW-9876543211",
        "carrier": "新竹物流",
        "order_id": "ORD-2026-001235",
        "status": "delivered",
        "status_label": "已送達",
        "origin": "台中轉運中心",
        "destination": "台中市西屯區",
        "can_intercept": False,
        "events": [
            {"time": "2026-03-11 11:00", "location": "台中轉運中心", "description": "包裹已交付物流"},
            {"time": "2026-03-12 09:00", "location": "西屯配送站", "description": "包裹已到達配送站"},
            {"time": "2026-03-13 15:45", "location": "台中市西屯區", "description": "包裹已送達，由本人簽收"},
        ],
        "delivered_at": "2026-03-13 15:45",
        "signed_by": "本人",
    },
    "TW-9876543212": {
        "tracking_number": "TW-9876543212",
        "carrier": "黑貓宅急便",
        "order_id": "ORD-2026-001238",
        "status": "return_in_transit",
        "status_label": "退貨運送中",
        "origin": "新北市板橋區",
        "destination": "桃園退貨倉",
        "can_intercept": False,
        "events": [
            {"time": "2026-03-12 14:00", "location": "新北市板橋區", "description": "退貨包裹已取件"},
            {"time": "2026-03-13 10:00", "location": "板橋轉運中心", "description": "退貨包裹轉運中"},
        ],
    },
    "TW-9876543213": {
        "tracking_number": "TW-9876543213",
        "carrier": "7-11 物流",
        "order_id": "ORD-2026-001239",
        "status": "out_for_delivery",
        "status_label": "最後一哩配送中",
        "origin": "桃園配送站",
        "destination": "桃園市中壢區",
        "can_intercept": False,
        "intercept_note": "包裹已進入最後一哩段，無法攔截或改地址",
        "events": [
            {"time": "2026-03-18 09:00", "location": "桃園轉運中心", "description": "包裹已交付物流"},
            {"time": "2026-03-19 07:00", "location": "中壢配送站", "description": "包裹已到達配送站"},
            {"time": "2026-03-19 08:30", "location": "配送中", "description": "司機已出發，預計今日送達"},
        ],
        "estimated_delivery": "2026-03-19 18:00",
    },
    "TW-9876543214": {
        "tracking_number": "TW-9876543214",
        "carrier": "新竹物流",
        "order_id": "ORD-2026-001241",
        "status": "delivered",
        "status_label": "已送達",
        "origin": "台南轉運中心",
        "destination": "台南市東區",
        "can_intercept": False,
        "events": [
            {"time": "2026-03-02 14:00", "location": "台南轉運中心", "description": "包裹已交付物流"},
            {"time": "2026-03-03 09:00", "location": "東區配送站", "description": "包裹已到達配送站"},
            {"time": "2026-03-04 11:30", "location": "台南市東區", "description": "包裹已送達，由管理員代收"},
        ],
        "delivered_at": "2026-03-04 11:30",
        "signed_by": "管理員代收",
    },
    "TW-9876543215": {
        "tracking_number": "TW-9876543215",
        "carrier": "黑貓宅急便",
        "order_id": "ORD-2026-001243",
        "status": "in_transit",
        "status_label": "配送中",
        "origin": "台北轉運中心",
        "destination": "台北市松山區",
        "can_intercept": True,
        "intercept_fee": 100,
        "intercept_note": "攔截改地址費用 NT$100，需在今日 17:00 前申請",
        "events": [
            {"time": "2026-03-17 10:00", "location": "台北轉運中心", "description": "包裹已交付物流"},
            {"time": "2026-03-17 22:00", "location": "松山分撿中心", "description": "包裹分撿完成"},
            {"time": "2026-03-18 08:00", "location": "松山配送站", "description": "包裹已到達配送站"},
        ],
        "estimated_delivery": "2026-03-20 18:00",
    },
    "TW-9876543216": {
        "tracking_number": "TW-9876543216",
        "carrier": "新竹物流",
        "order_id": "ORD-2026-001244",
        "status": "delivered",
        "status_label": "已送達",
        "origin": "新竹配送站",
        "destination": "新竹市東區",
        "can_intercept": False,
        "events": [
            {"time": "2026-03-01 11:00", "location": "新竹轉運中心", "description": "包裹已交付物流"},
            {"time": "2026-03-02 09:00", "location": "新竹配送站", "description": "包裹已到達配送站"},
            {"time": "2026-03-03 14:00", "location": "新竹市東區", "description": "包裹已送達，本人簽收"},
        ],
        "delivered_at": "2026-03-03 14:00",
        "signed_by": "本人",
    },
    "TW-9876543217": {
        "tracking_number": "TW-9876543217",
        "carrier": "黑貓宅急便",
        "order_id": "ORD-2026-001245",
        "status": "arrived_at_station",
        "status_label": "已到達配送站",
        "origin": "台北轉運中心",
        "destination": "台北市信義區",
        "can_intercept": True,
        "intercept_fee": 100,
        "intercept_note": "包裹尚在配送站，攔截費用 NT$100",
        "events": [
            {"time": "2026-03-19 08:00", "location": "台北轉運中心", "description": "包裹已交付物流"},
            {"time": "2026-03-19 14:00", "location": "信義配送站", "description": "包裹已到達配送站，排入明日配送"},
        ],
        "estimated_delivery": "2026-03-20 18:00",
    },
    "TW-9876543218": {
        "tracking_number": "TW-9876543218",
        "carrier": "新竹物流（大型貨運）",
        "order_id": "ORD-2026-001247",
        "status": "in_transit",
        "status_label": "配送中",
        "origin": "台北倉庫",
        "destination": "台北市內湖區",
        "can_intercept": True,
        "intercept_fee": 500,
        "intercept_note": "大型貨物攔截改地址費用 NT$500，需提前 24 小時申請",
        "events": [
            {"time": "2026-03-16 10:00", "location": "台北倉庫", "description": "大型商品已完成包裝出庫"},
            {"time": "2026-03-17 14:00", "location": "大型貨運轉運中心", "description": "轉運中"},
        ],
        "estimated_delivery": "2026-03-20 18:00",
    },
}


def track_shipment(tracking_number: str) -> dict:
    """追蹤包裹物流狀態"""
    tn = tracking_number.upper().strip()
    shipment = _SHIPMENTS.get(tn)
    if not shipment:
        return {
            "found": False,
            "tracking_number": tracking_number,
            "message": f"找不到物流單號 {tracking_number}，請確認追蹤編號是否正確。",
        }
    return {"found": True, **shipment}
