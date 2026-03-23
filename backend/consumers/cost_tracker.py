"""
Cost Tracker — 成本追蹤 Consumer
消費 responses.completed，寫 MongoDB: cost_records

職責（普通 Consumer，非 Agent）：
1. 寫入 MongoDB: cost_records（含 routed_to, latency_ms, intent, session_id）
"""
import time
from datetime import datetime, timezone

from shared.schema import QueryMessage
from shared.kafka_utils import get_consumer, consume_loop, create_topics
from shared.mongo_client import get_collection, ensure_indexes
from shared.log_utils import get_logger

logger = get_logger(__name__)

# Gemini 2.0 Flash 定價（USD per 1M tokens，2025 Q1）
PROMPT_TOKEN_COST_PER_1M = 0.075
COMPLETION_TOKEN_COST_PER_1M = 0.30
# Gemini text-embedding 估算（~$0.025/1M chars，每次呼叫約 500 chars）
EMBEDDING_COST_PER_CALL = 0.000025


def calculate_cost(prompt_tokens: int, completion_tokens: int, embedding_calls: int = 0) -> float:
    return (
        prompt_tokens / 1_000_000 * PROMPT_TOKEN_COST_PER_1M
        + completion_tokens / 1_000_000 * COMPLETION_TOKEN_COST_PER_1M
        + embedding_calls * EMBEDDING_COST_PER_CALL
    )


def handle_message(raw: str):
    msg = QueryMessage.from_json(raw)
    embedding_calls = msg.token_usage.embedding_calls
    cost = calculate_cost(
        msg.token_usage.prompt_tokens,
        msg.token_usage.completion_tokens,
        embedding_calls,
    )

    logger.info(
        f"Tracked query_id={msg.query_id} | "
        f"tokens=({msg.token_usage.prompt_tokens}p+{msg.token_usage.completion_tokens}c) | "
        f"embedding_calls={embedding_calls} | "
        f"cost=${cost:.6f} | latency={msg.latency_ms}ms"
    )

    # 寫入 MongoDB: cost_records
    col = get_collection("cost_records")
    if col is not None:
        try:
            col.insert_one({
                "query_id": msg.query_id,
                "session_id": msg.session_id,
                "token_usage": {
                    "prompt": msg.token_usage.prompt_tokens,
                    "completion": msg.token_usage.completion_tokens,
                    "embedding_calls": embedding_calls,
                    "total": msg.token_usage.total_tokens,
                },
                "estimated_cost_usd": round(cost, 8),
                "model": "gemini-2.0-flash",
                "routed_to": msg.routed_to,
                "latency_ms": msg.latency_ms,
                "intent": msg.intent,
                "recorded_at": datetime.now(timezone.utc),
            })
        except Exception as e:
            logger.warning(f"MongoDB write failed: {e}")


def main():
    time.sleep(10)
    create_topics()
    ensure_indexes()

    consumer = get_consumer("cost-tracker-group", ["responses.completed"])
    logger.info("Cost Tracker started, listening on responses.completed ...")
    consume_loop(consumer, handle_message)


if __name__ == "__main__":
    main()
