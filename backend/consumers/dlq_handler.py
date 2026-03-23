"""
DLQ Handler — 死信佇列處理
消費 queries.dlq，記錄失敗原因，寫 MongoDB: failed_events，模擬告警通知

職責（普通 Consumer，非 Agent）：
1. 寫入 MongoDB: failed_events（落地持久化）
2. 寫入本地 JSONL 備援檔（MongoDB 不可用時的 fallback）
3. 印出警告 log（生產環境可接 PagerDuty / Slack）
4. 發布錯誤回覆到 responses.completed，讓用戶端 SSE 即時收到失敗通知
"""
import time
import json
from datetime import datetime, timezone

from shared.schema import QueryMessage
from shared.kafka_utils import get_consumer, get_producer, produce_message, consume_loop, create_topics
from shared.mongo_client import get_collection, ensure_indexes
from shared.log_utils import get_logger
from shared.telemetry import emit_telemetry

logger = get_logger(__name__)

DLQ_LOG_FILE = "/tmp/dlq_records.jsonl"


def handle_message(raw: str):
    msg = QueryMessage.from_json(raw)

    record = {
        "dlq_received_at": datetime.now(timezone.utc).isoformat(),
        "query_id": msg.query_id,
        "user_id": msg.user_id,
        "user_query": msg.user_query,
        "retry_count": msg.retry_count,
        "error": msg.error,
        "routed_to": msg.routed_to,
        "timestamp": msg.timestamp,
    }

    # 寫入 MongoDB: failed_events
    col = get_collection("failed_events")
    if col is not None:
        try:
            col.insert_one({**record})
            logger.info(f"DLQ record written to MongoDB: query_id={msg.query_id}")
        except Exception as e:
            logger.warning(f"MongoDB write failed: {e}")

    # 寫入本地 JSONL（MongoDB 不可用時的備援）
    try:
        with open(DLQ_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"DLQ file write failed: {e}")

    emit_telemetry("message.dead_lettered", "dlq_handler", msg.query_id, {
        "error": msg.error,
        "stage": msg.stage,
        "retry_count": msg.retry_count,
    })

    # 模擬告警通知
    logger.error(
        f"\n{'!'*60}\n"
        f"  [DLQ ALERT] 訊息進入死信佇列\n"
        f"  Query ID：{msg.query_id}\n"
        f"  客戶 ID：{msg.user_id}\n"
        f"  問題：{msg.user_query[:80]}\n"
        f"  失敗原因：{msg.error or '未知'}\n"
        f"  重試次數：{msg.retry_count}\n"
        f"  [ACTION] 需人工介入處理 — 請查看 MongoDB: failed_events\n"
        f"{'!'*60}"
    )
    # 生產環境：在此呼叫 Slack/PagerDuty API 發送告警

    # 發送錯誤回覆給用戶，避免用戶端 SSE 乾等 60 秒逾時
    msg.response = "很抱歉，您的問題目前無法處理，請稍後再試或聯繫客服人員。"
    msg.routed_to = "dlq"
    try:
        producer = get_producer()
        produce_message(producer, "responses.completed", msg.to_json(), key=msg.query_id)
        producer.flush()
        logger.info(f"Error response published to responses.completed: query_id={msg.query_id}")
    except Exception as e:
        logger.warning(f"Failed to publish DLQ error response: {e}")


def main():
    time.sleep(10)
    create_topics()
    ensure_indexes()

    consumer = get_consumer("dlq-handler-group", ["queries.dlq"])
    logger.info("DLQ Handler started, listening on queries.dlq ...")
    consume_loop(consumer, handle_message)


if __name__ == "__main__":
    main()
