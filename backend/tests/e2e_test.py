"""
端對端驗證腳本
用法：python e2e_test.py "你的問題"
      python e2e_test.py          (跑預設測試題)
"""
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

from confluent_kafka import Producer
from pymongo import MongoClient
from dotenv import load_dotenv
load_dotenv()

KAFKA_BOOTSTRAP = "localhost:9094"
MONGO_URI = "mongodb://localhost:27017"
MONGO_DB = os.getenv("MONGODB_DB", "ecommerce")
TOPIC = "queries.incoming"

DEFAULT_QUERIES = [
    "我的訂單什麼時候到？",
    "我想申請退貨",
    "你們有哪些付款方式？",
]


def send(query: str, user_id: str = "e2e_test") -> str:
    query_id = str(uuid.uuid4())
    msg = {
        "user_query": query,
        "user_id": user_id,
        "query_id": query_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "intent": None, "emotion_score": 0.0, "routed_to": None,
        "retry_count": 0, "token_usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "latency_ms": 0.0, "response": None, "error": None,
        "routing_reason": None, "stage": None, "origin_topic": None,
        "tool_calls": [], "escalated": False, "emotion": "",
    }
    p = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP, "socket.timeout.ms": 5000})
    p.produce(TOPIC, key=query_id, value=json.dumps(msg, ensure_ascii=False).encode())
    p.flush(timeout=5)
    return query_id


def wait(query_id: str, timeout: int = 60):
    col = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)[MONGO_DB]["responses"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        doc = col.find_one({"query_id": query_id})
        if doc:
            return doc
        time.sleep(0.8)
    return None


def run(query: str):
    print(f"\n{'─'*60}")
    print(f"  問題：{query}")
    t0 = time.time()
    qid = send(query)
    print(f"  query_id: {qid}")
    print(f"  等待回覆（最多 60s）...", end="", flush=True)
    doc = wait(qid)
    elapsed = time.time() - t0
    if doc is None:
        print(f"\n  ✗ 逾時（{elapsed:.1f}s），未收到回覆")
        return False
    print(f" 完成（{elapsed:.1f}s）")
    print(f"  路由：{doc.get('routed_to', '?')}")
    print(f"  升級人工：{doc.get('escalated', False)}")
    print(f"  回覆：\n  {(doc.get('response') or '（無）').replace(chr(10), chr(10)+'  ')}")
    return True


if __name__ == "__main__":
    queries = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_QUERIES
    ok = sum(run(q) for q in queries)
    print(f"\n{'─'*60}")
    print(f"  結果：{ok}/{len(queries)} 通過\n")
