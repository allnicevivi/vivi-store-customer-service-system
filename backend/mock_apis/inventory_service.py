"""
Mock Inventory Service — 模擬電商庫存查詢 API
生產環境應替換為真實 WMS/ERP API 呼叫
"""

_PRODUCTS = {
    "PROD-NKA90-US9": {
        "product_id": "PROD-NKA90-US9",
        "name": "Nike Air Max 90 US9",
        "category": "運動鞋",
        "price": 3200,
        "in_stock": True,
        "stock_qty": 15,
        "out_of_stock": False,
        "restock_date": None,
        "warehouse": "台北倉",
    },
    "PROD-NKA90-US10": {
        "product_id": "PROD-NKA90-US10",
        "name": "Nike Air Max 90 US10",
        "category": "運動鞋",
        "price": 3200,
        "in_stock": False,
        "stock_qty": 0,
        "out_of_stock": True,
        "restock_date": "2026-04-10",
        "restock_note": "預計 2026-04-10 到貨，數量有限",
        "warehouse": "台北倉",
    },
    "PROD-NKA90-US11": {
        "product_id": "PROD-NKA90-US11",
        "name": "Nike Air Max 90 US11",
        "category": "運動鞋",
        "price": 3200,
        "in_stock": True,
        "stock_qty": 3,
        "out_of_stock": False,
        "low_stock": True,
        "restock_date": None,
        "warehouse": "台北倉",
    },
    "PROD-SONY-WH1000XM5": {
        "product_id": "PROD-SONY-WH1000XM5",
        "name": "Sony WH-1000XM5 降噪耳機",
        "category": "電子產品",
        "price": 9900,
        "in_stock": False,
        "stock_qty": 0,
        "out_of_stock": True,
        "restock_date": "2026-04-01",
        "restock_note": "Sony 原廠補貨中，預計 4 月初到貨",
        "warehouse": "桃園倉",
    },
    "PROD-DYSON-V15": {
        "product_id": "PROD-DYSON-V15",
        "name": "Dyson V15 Detect 吸塵器",
        "category": "家電",
        "price": 19900,
        "in_stock": True,
        "stock_qty": 8,
        "out_of_stock": False,
        "restock_date": None,
        "warehouse": "台中倉",
    },
    "PROD-IPHONE16-CASE": {
        "product_id": "PROD-IPHONE16-CASE",
        "name": "iPhone 16 Pro MagSafe 保護殼",
        "category": "手機配件",
        "price": 890,
        "in_stock": True,
        "stock_qty": 50,
        "out_of_stock": False,
        "restock_date": None,
        "warehouse": "台北倉",
    },
    "PROD-SWITCH-OLED": {
        "product_id": "PROD-SWITCH-OLED",
        "name": "Nintendo Switch OLED 白色",
        "category": "遊戲主機",
        "price": 10800,
        "in_stock": False,
        "stock_qty": 0,
        "out_of_stock": True,
        "restock_date": "2026-03-28",
        "restock_note": "任天堂官方補貨，預計本月底到貨",
        "warehouse": "桃園倉",
    },
    "PROD-APPLEWATCH-S10": {
        "product_id": "PROD-APPLEWATCH-S10",
        "name": "Apple Watch Series 10 45mm 黑色",
        "category": "穿戴裝置",
        "price": 12900,
        "in_stock": True,
        "stock_qty": 5,
        "out_of_stock": False,
        "low_stock": True,
        "restock_date": None,
        "warehouse": "台北倉",
    },
    "PROD-NESPRESSO-VP": {
        "product_id": "PROD-NESPRESSO-VP",
        "name": "Nespresso Vertuo Pop 咖啡機",
        "category": "廚房家電",
        "price": 5990,
        "in_stock": True,
        "stock_qty": 20,
        "out_of_stock": False,
        "restock_date": None,
        "warehouse": "台中倉",
    },
    "PROD-KINDLE-PW11": {
        "product_id": "PROD-KINDLE-PW11",
        "name": "Kindle Paperwhite 第11代 8GB",
        "category": "電子書閱讀器",
        "price": 4590,
        "in_stock": False,
        "stock_qty": 0,
        "out_of_stock": True,
        "restock_date": "2026-04-15",
        "restock_note": "Amazon 全球缺貨中，預計 4 月中旬到貨",
        "warehouse": "桃園倉",
    },
}

# 商品名稱關鍵字搜尋索引
_NAME_INDEX = {v["name"].lower(): k for k, v in _PRODUCTS.items()}


def _find_product(product_id: str) -> dict | None:
    """依 ID 或名稱關鍵字搜尋商品"""
    pid = product_id.upper().strip()
    if pid in _PRODUCTS:
        return _PRODUCTS[pid]
    # 嘗試名稱關鍵字搜尋
    query = product_id.lower().strip()
    for name, pid_key in _NAME_INDEX.items():
        if query in name or any(q in name for q in query.split()):
            return _PRODUCTS[pid_key]
    return None


def get_inventory(product_id: str) -> dict:
    """查詢商品庫存狀態"""
    product = _find_product(product_id)
    if not product:
        return {
            "found": False,
            "product_id": product_id,
            "message": f"找不到商品 {product_id}，請確認商品編號或名稱。",
        }
    return {"found": True, **product}


def get_product_info(product_id: str) -> dict:
    """查詢商品詳細資訊（含庫存）"""
    return get_inventory(product_id)
