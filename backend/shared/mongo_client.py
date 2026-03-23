"""
MongoDB client helper — graceful fallback if MongoDB not available.

使用方式：
    from mongo_client import get_collection
    col = get_collection("responses")
    if col is not None:
        col.insert_one({...})

MongoDB 不可用時 get_collection 回傳 None，呼叫方應跳過寫入並只記 log。
"""
import os

from shared.log_utils import get_logger

logger = get_logger(__name__)

_client = None


def get_db():
    """回傳 MongoDB database，不可用時回傳 None（不拋例外）。"""
    global _client
    if _client is None:
        try:
            from pymongo import MongoClient
            uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
            _client = MongoClient(
                uri,
                serverSelectionTimeoutMS=3000,
                maxPoolSize=200,
                minPoolSize=10,
                connectTimeoutMS=5000,
                socketTimeoutMS=30000,
            )
            _client.admin.command("ping")
            db_name = os.getenv("MONGODB_DB", "ecommerce")
            logger.info(f"MongoDB connected: {uri}/{db_name}")
        except Exception as e:
            logger.warning(f"MongoDB not available: {e}  (writes will be skipped)")
            return None
    try:
        db_name = os.getenv("MONGODB_DB", "ecommerce")
        return _client[db_name]
    except Exception as e:
        logger.warning(f"MongoDB error: {e}")
        return None


def get_collection(collection_name: str):
    """回傳指定 collection，MongoDB 不可用時回傳 None。"""
    db = get_db()
    if db is None:
        return None
    return db[collection_name]


def ensure_indexes() -> None:
    """在服務啟動時一次性建立所有 collection indexes（idempotent）。

    MongoDB 不可用時靜默回傳，不拋例外。
    MongoDB create_index 本身是 idempotent，重複呼叫不會重建已存在的 index。
    """
    db = get_db()
    if db is None:
        logger.warning("ensure_indexes: MongoDB not available, skipping")
        return

    _specs = [
        # (collection,         keys,                                       extra)
        ("responses",          [("created_at", -1)],                       {}),
        ("sessions",           [("user_id", 1), ("last_updated", -1)],     {}),
        ("sessions",           [("session_id", 1), ("user_id", 1)],        {"unique": True}),
        ("cost_records",       [("recorded_at", 1)],                       {}),
        ("handoff_tickets",    [("status", 1), ("priority", 1)],           {}),
        ("analytics_events",   [("escalated", 1), ("timestamp", -1)],      {}),
        ("llm_metrics",        [("recorded_at", 1)],                       {}),
    ]

    for coll, keys, extra in _specs:
        try:
            db[coll].create_index(keys, background=True, **extra)
            logger.debug(f"Index ensured: {coll} {keys}")
        except Exception as e:
            logger.warning(f"ensure_indexes failed for {coll} {keys}: {e}")
