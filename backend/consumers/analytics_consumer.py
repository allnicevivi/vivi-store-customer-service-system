"""
Analytics Consumer — FAQ Gap 分析
消費 responses.completed，寫入 MongoDB analytics_events
供 /api/analytics/* 端點查詢
"""
import time
from datetime import datetime, timezone

from shared.schema import QueryMessage
from shared.kafka_utils import get_consumer, consume_loop, create_topics
from shared.mongo_client import get_collection, ensure_indexes
from shared.log_utils import get_logger

logger = get_logger(__name__)


def handle_message(raw: str):
    msg = QueryMessage.from_json(raw)

    event = {
        "query_id": msg.query_id,
        "user_id": msg.user_id,
        "user_query": msg.user_query,
        "intent": msg.intent,
        "routed_to": msg.routed_to,
        "escalated": msg.escalated,
        "was_unresolved": msg.routed_to in ("queries.human", "queries.dlq"),
        "tool_calls": [t.get("tool") for t in (msg.tool_calls or []) if t.get("tool")],
        "emotion_score": msg.emotion_score,
        "retry_count": msg.retry_count,
        "latency_ms": msg.latency_ms,
        "timestamp": msg.timestamp,
        "recorded_at": datetime.now(timezone.utc).isoformat()
    }

    col = get_collection("analytics_events")
    if col is not None:
        try:
            col.insert_one(event)
            logger.info(
                f"Analytics event written: query_id={msg.query_id} "
                f"intent={msg.intent} routed_to={msg.routed_to} escalated={msg.escalated}"
            )
        except Exception as e:
            logger.warning(f"MongoDB analytics_events write failed: {e}")


def main():
    time.sleep(10)
    create_topics()
    ensure_indexes()

    consumer = get_consumer("analytics-consumer-group", ["responses.completed"])
    logger.info("Analytics Consumer started, listening on responses.completed ...")
    consume_loop(consumer, handle_message)


if __name__ == "__main__":
    main()
