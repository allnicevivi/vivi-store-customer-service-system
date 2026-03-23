"""
Telemetry helper — emit TelemetryEvent to events.telemetry topic.

每個 Agent / Consumer 呼叫 emit_telemetry() 發送輕量 telemetry event，
與 QueryMessage pipeline 完全解耦。
"""
from shared.schema import TelemetryEvent
from shared.kafka_utils import get_producer, produce_message
from shared.log_utils import get_logger

logger = get_logger(__name__)

TELEMETRY_TOPIC = "events.telemetry"


def emit_telemetry(event_type: str, source: str, query_id: str, payload: dict):
    """
    發送 TelemetryEvent 到 events.telemetry topic。
    任何例外都靜默吃掉，避免 telemetry 失敗影響主流程。
    """
    try:
        event = TelemetryEvent(
            event_type=event_type,
            source=source,
            query_id=query_id,
            payload=payload,
        )
        producer = get_producer()
        produce_message(producer, TELEMETRY_TOPIC, event.to_json(), key=query_id)
        producer.flush()
        logger.debug(f"[telemetry] {event_type} query_id={query_id}")
    except Exception as e:
        logger.warning(f"[telemetry] emit failed ({event_type}): {e}")
