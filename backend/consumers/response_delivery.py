"""
Response Delivery — 回覆交付 Consumer
消費 responses.completed，顯示最終回覆給客戶，並寫 MongoDB: responses

職責（普通 Consumer，非 Agent）：
1. 顯示最終回覆（模擬渠道交付：LINE / Email / App Push）
2. 寫入 MongoDB: responses（供 Simple UI 查詢對話紀錄）
"""
import json
import os
import time
from datetime import datetime, timezone

import redis

from shared.schema import QueryMessage
from shared.kafka_utils import get_consumer, consume_loop, create_topics
from shared.mongo_client import get_collection, ensure_indexes
from shared.log_utils import get_logger

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

_redis_pool = redis.ConnectionPool(
    host=REDIS_HOST, port=REDIS_PORT,
    decode_responses=True, max_connections=20
)

logger = get_logger(__name__)


def _get_redis() -> redis.Redis:
    return redis.Redis(connection_pool=_redis_pool)


def print_response(msg: QueryMessage):
    """模擬將回覆傳送給客戶（生產環境可透過 Email / App Push / SMS）"""
    print(
        f"\n{'─'*60}\n"
        f"  [客服回覆] Query ID: {msg.query_id}\n"
        f"  客戶 ID：{msg.user_id}\n"
        f"  處理管道：{msg.routed_to or '未知'}\n"
        f"  路由原因：{msg.routing_reason or '-'}\n"
        f"  {'─'*37}\n"
        f"  {(msg.response or '（無回覆）').replace(chr(10), chr(10) + '  ')}\n"
        f"{'─'*60}\n",
        flush=True,
    )


def handle_message(raw: str):
    msg = QueryMessage.from_json(raw)
    logger.info(
        f"Delivering query_id={msg.query_id} | "
        f"route={msg.routed_to} | latency={msg.latency_ms}ms"
    )

    # 顯示回覆
    print_response(msg)

    # 寫入 MongoDB: responses
    payload = {
        "query_id": msg.query_id,
        "session_id": msg.session_id,
        "user_id": msg.user_id,
        "user_query": msg.user_query,
        "response": msg.response or "",
        "escalated": msg.escalated,
        "routed_to": msg.routed_to,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    col = get_collection("responses")
    if col is not None:
        try:
            col.insert_one(payload.copy())
            logger.info(f"Written to MongoDB responses: query_id={msg.query_id}")
        except Exception as e:
            logger.warning(f"MongoDB write failed: {e}")

    # Pub/Sub 推送給等待中的 SSE 連線
    try:
        r = _get_redis()
        channel = f"response:{msg.query_id}"
        r.publish(channel, json.dumps({
            "response":       payload["response"],
            "routed_to":      payload["routed_to"],
            "escalated":      payload["escalated"],
            "intent":         msg.intent,
            "emotion":        msg.emotion,
            "emotion_score":  msg.emotion_score,
            "routing_reason": msg.routing_reason,
            "tool_calls":     msg.tool_calls,
            "latency_ms":     msg.latency_ms,
        }, ensure_ascii=False))
        logger.info(f"Published to Redis channel: {channel}")
    except Exception as e:
        logger.warning(f"Redis publish failed: {e}")

    # 寫入 session（對話連續性）
    if msg.session_id:
        try:
            from shared.session_store import append_turn
            append_turn(
                session_id=msg.session_id,
                user_id=msg.user_id,
                query_id=msg.query_id,
                user_query=msg.user_query,
                response=msg.response,
                intent=msg.intent,
                routed_to=msg.routed_to,
            )
        except Exception as e:
            logger.warning(f"Session append_turn failed: {e}")


def main():
    time.sleep(10)
    create_topics()
    ensure_indexes()

    consumer = get_consumer("response-delivery-group", ["responses.completed"])
    logger.info("Response Delivery started, listening on responses.completed ...")
    consume_loop(consumer, handle_message)


if __name__ == "__main__":
    main()
