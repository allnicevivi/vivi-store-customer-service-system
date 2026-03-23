"""
Retry Consumer — 指數退避重試機制
消費 queries.retry，等待後重發回原始失敗 topic（由 origin_topic 決定）
無 origin_topic 時 fallback 到 queries.incoming（向下相容）

指數退避：delay = base_delay * (2 ^ retry_count)
- retry 1: 5s
- retry 2: 10s
- retry 3: 20s
- retry > MAX_RETRIES: 轉 DLQ
"""
import time

from shared.schema import QueryMessage
from shared.kafka_utils import get_producer, get_consumer, produce_message, consume_loop, create_topics
from shared.log_utils import get_logger
from shared.telemetry import emit_telemetry

logger = get_logger(__name__)

MAX_RETRIES = 3
BASE_DELAY_SECONDS = 5


def handle_message(raw: str):
    msg = QueryMessage.from_json(raw)
    retry_count = msg.retry_count

    if retry_count > MAX_RETRIES:
        # 超過重試上限 → DLQ
        logger.warning(
            f"Query {msg.query_id} exceeded max retries ({MAX_RETRIES}), "
            f"sending to DLQ"
        )
        producer = get_producer()
        produce_message(producer, "queries.dlq", msg.to_json(), key=msg.query_id)
        producer.flush()
        return

    # 計算退避時間
    delay = BASE_DELAY_SECONDS * (2 ** (retry_count - 1))
    logger.info(
        f"Retry {retry_count}/{MAX_RETRIES} for query_id={msg.query_id} "
        f"| waiting {delay}s | error='{msg.error}'"
    )
    emit_telemetry("message.retrying", "retry_consumer", msg.query_id, {
        "retry_count": retry_count,
        "backoff_ms": delay * 1000,
    })
    time.sleep(delay)

    # 清除錯誤資訊，保留 origin_topic / intent / emotion 等路由資訊
    msg.error = None
    msg.response = None

    # 直接回到原始失敗 topic，無 origin_topic 時 fallback 到 queries.incoming
    target = msg.origin_topic or "queries.incoming"
    producer = get_producer()
    produce_message(producer, target, msg.to_json(), key=msg.query_id)
    producer.flush()
    logger.info(f"Re-queued query_id={msg.query_id} → {target}")


def main():
    time.sleep(10)
    create_topics()

    consumer = get_consumer("retry-consumer-group", ["queries.retry"])
    logger.info("Retry Consumer started, listening on queries.retry ...")
    consume_loop(consumer, handle_message)


if __name__ == "__main__":
    main()
