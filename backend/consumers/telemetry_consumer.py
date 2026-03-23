"""
Telemetry Consumer — 取代 audit_logger，提供完整的可觀測性
消費 events.telemetry，寫入 MongoDB: telemetry_events

職責：
- 儲存每個 stage 的 telemetry event（含 per-tool 計時）
- 以 query_id 串聯同一查詢的所有 events，重建完整 timeline
- 取代 audit_logger.py（audit_logs collection 廢棄）
"""
import time
from datetime import datetime, timezone

from shared.schema import TelemetryEvent
from shared.kafka_utils import get_consumer, consume_loop, create_topics
from shared.mongo_client import get_collection, ensure_indexes
from shared.log_utils import get_logger

logger = get_logger(__name__)


def handle_message(raw: str):
    event = TelemetryEvent.from_json(raw)
    logger.info(
        f"Telemetry event_type={event.event_type} "
        f"query_id={event.query_id} source={event.source}"
    )

    col = get_collection("telemetry_events")
    if col is not None:
        try:
            col.insert_one({
                "event_id": event.event_id,
                "event_type": event.event_type,
                "query_id": event.query_id,
                "timestamp": event.timestamp,
                "source": event.source,
                "payload": event.payload,
                "received_at": datetime.now(timezone.utc).isoformat(),
            })
            logger.debug(f"Written to MongoDB telemetry_events: event_id={event.event_id}")
        except Exception as e:
            logger.warning(f"MongoDB write failed: {e}")
    else:
        # MongoDB 不可用：只記 log（graceful degradation）
        logger.info(
            f"[TELEMETRY] event_type={event.event_type} | query_id={event.query_id} "
            f"| source={event.source} | payload={event.payload}"
        )


def main():
    time.sleep(10)
    create_topics()
    ensure_indexes()

    consumer = get_consumer("telemetry-consumer-group", ["events.telemetry"])
    logger.info("Telemetry Consumer started, listening on events.telemetry ...")
    consume_loop(consumer, handle_message)


if __name__ == "__main__":
    main()
