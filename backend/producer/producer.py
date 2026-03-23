"""
CLI Producer — 模擬客戶輸入
用法：python producer.py "我的訂單什麼時候到？"
      python producer.py  （互動模式）
"""
import sys
import time

from shared.schema import QueryMessage
from shared.kafka_utils import get_producer, produce_message, create_topics
from shared.log_utils import get_logger

logger = get_logger(__name__)

INCOMING_TOPIC = "queries.incoming"


def send_query(query: str, user_id: str = "demo_user", session_id: str = None):
    import uuid as _uuid
    sid = session_id or f"SES-{_uuid.uuid4()}"
    msg = QueryMessage(user_query=query, user_id=user_id, session_id=sid)
    producer = get_producer()
    produce_message(producer, INCOMING_TOPIC, msg.to_json(), key=msg.query_id)
    producer.flush()
    logger.info(f"Sent query_id={msg.query_id} | session_id={sid} | query='{query}'")
    return msg.query_id, sid


def main():
    # 等待 Kafka 就緒
    time.sleep(3)
    create_topics()

    if len(sys.argv) > 1:
        # 命令列模式
        query = " ".join(sys.argv[1:])
        qid, sid = send_query(query)
        print(f"Query sent. ID: {qid} | Session: {sid}")
    else:
        # 互動模式（同一 session）
        import uuid as _uuid
        session_id = f"SES-{_uuid.uuid4()}"
        print("=== ViviStore 電商客服系統 ===")
        print(f"Session ID: {session_id}")
        print("輸入問題後按 Enter，輸入 'q' 離開\n")
        while True:
            try:
                query = input("您好，歡迎使用 ViviStore 客服，請問有什麼需要協助的？\n> ").strip()
                if query.lower() in ("q", "quit", "exit", ""):
                    print("感謝您的使用，再見！")
                    break
                qid, _ = send_query(query, session_id=session_id)
                print(f"  [已收到，Query ID: {qid}]\n")
            except (KeyboardInterrupt, EOFError):
                print("\n感謝您的使用，再見！")
                break


if __name__ == "__main__":
    main()
