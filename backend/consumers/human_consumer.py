"""
Human Consumer — 模擬轉人工處理（建立工單）
消費 queries.human，印 log 模擬工單系統，並回覆客戶說明已轉人工
"""
import time
import uuid

from shared.schema import QueryMessage, emotion_label
from shared.kafka_utils import get_producer, get_consumer, produce_message, consume_loop, create_topics
from shared.mongo_client import get_collection, ensure_indexes
from shared.log_utils import get_logger
from shared.telemetry import emit_telemetry
from shared.handoff_context import build_handoff_context

logger = get_logger(__name__)


def create_ticket(msg: QueryMessage) -> str:
    """建立客服工單，含完整 handoff context 寫入 MongoDB"""
    ticket_id = f"TKT-{uuid.uuid4().hex[:8].upper()}"
    ctx = build_handoff_context(msg)

    logger.info(
        f"\n{'='*60}\n"
        f"  [工單系統] 新工單建立\n"
        f"  工單號碼：{ticket_id}\n"
        f"  優先級：{ctx['priority']}\n"
        f"  客戶 ID：{msg.user_id}\n"
        f"  Query ID：{msg.query_id}\n"
        f"  問題內容：{msg.user_query}\n"
        f"  意圖：{ctx['intent']}\n"
        f"  情緒：{ctx['emotion_summary']}\n"
        f"  已嘗試工具：{ctx['tried_tools']}\n"
        f"  建議動作：{ctx['suggested_action']}\n"
        f"  路由原因：{msg.routing_reason or '手動轉人工'}\n"
        f"  Retry 次數：{msg.retry_count}\n"
        f"  時間戳記：{msg.timestamp}\n"
        f"  預計處理時間：1-2 個工作天\n"
        f"{'='*60}"
    )

    # 寫入 MongoDB handoff_tickets
    col = get_collection("handoff_tickets")
    if col is not None:
        try:
            from datetime import datetime, timezone
            ticket_doc = {
                "ticket_id": ticket_id,
                "status": "open",
                "created_at": datetime.now(timezone.utc).isoformat(),
                **ctx,
            }
            col.insert_one(ticket_doc)
            logger.info(f"Ticket {ticket_id} written to MongoDB (priority={ctx['priority']})")
        except Exception as e:
            logger.warning(f"MongoDB handoff_tickets write failed: {e}")

    return ticket_id


def handle_message(raw: str):
    start_time = time.time()
    msg = QueryMessage.from_json(raw)
    logger.info(f"Creating ticket for query_id={msg.query_id}")

    ticket_id = create_ticket(msg)

    msg.response = (
        f"您好！我們已為您建立服務工單（工單號碼：{ticket_id}）。\n\n"
        f"您的問題需要由專業客服人員為您處理，"
        f"我們將在 1-2 個工作天內主動與您聯繫。\n\n"
        f"如需緊急協助，請撥打 24 小時緊急服務專線：0800-SHOP-NOW。\n"
        f"感謝您的耐心等候！"
    )
    msg.emotion = emotion_label(msg.emotion_score)
    msg.latency_ms = round((time.time() - start_time) * 1000, 2)
    emit_telemetry("response.delivered", "human_consumer", msg.query_id, {
        "handler_type": "human_consumer",
        "latency_ms": msg.latency_ms,
    })

    producer = get_producer()
    produce_message(producer, "responses.completed", msg.to_json(), key=msg.query_id)
    producer.flush()
    logger.info(f"Ticket {ticket_id} created for query_id={msg.query_id}")


def main():
    time.sleep(10)
    create_topics()
    ensure_indexes()

    consumer = get_consumer("human-consumer-group", ["queries.human"])
    logger.info("Human Consumer started, listening on queries.human ...")
    consume_loop(consumer, handle_message)


if __name__ == "__main__":
    main()
