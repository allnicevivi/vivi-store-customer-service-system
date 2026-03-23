"""
Kafka Producer / Consumer 工廠
封裝 confluent-kafka，提供統一的建立方式
"""
import os
from pathlib import Path
from typing import Callable, Optional
from confluent_kafka import Producer, Consumer, KafkaException, KafkaError
from confluent_kafka.admin import AdminClient, NewTopic
from shared.log_utils import get_logger

logger = get_logger(__name__)

_ROOT = Path(__file__).resolve().parent.parent

# Load local .env for "python xxx.py" dev runs.
# In Docker, env is provided by docker-compose; override=False avoids clobbering it.
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(dotenv_path=_ROOT / ".env", override=False)
except Exception:
    pass


def _in_docker() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def get_bootstrap_servers() -> str:
    """
    Resolve Kafka bootstrap servers for both Docker and local dev.

    Priority:
    1) KAFKA_BOOTSTRAP_SERVERS env var (optionally from .env)
    2) Docker default: kafka:9092
    3) Local default: localhost:9094 (matches docker-compose advertised listener)
    """
    v = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
    if v:
        return v
    return "kafka:9092" if _in_docker() else "localhost:9094"

# Topic 定義
TOPICS = {
    "queries.incoming":      {"num_partitions": 10, "replication_factor": 1},  # 3 → 10，支援最多 10 個並行 router-agent
    "queries.agent":         {"num_partitions": 5,  "replication_factor": 1},  # 2 → 5，Router → Resolution Agent
    "queries.human":         {"num_partitions": 3,  "replication_factor": 1},  # 2 → 3
    "queries.retry":         {"num_partitions": 2,  "replication_factor": 1},  # 1 → 2
    "queries.dlq":           {"num_partitions": 1,  "replication_factor": 1},
    "responses.completed":   {"num_partitions": 6,  "replication_factor": 1},  # 3 → 6（3 consumer groups × 2）
    "events.telemetry":      {"num_partitions": 3,  "replication_factor": 1},  # 2 → 3
}


def create_topics(bootstrap_servers: str | None = None):
    """建立所有 Topic（冪等，已存在不報錯）"""
    bootstrap_servers = bootstrap_servers or get_bootstrap_servers()
    admin = AdminClient({"bootstrap.servers": bootstrap_servers})
    new_topics = [
        NewTopic(name, num_partitions=cfg["num_partitions"],
                 replication_factor=cfg["replication_factor"])
        for name, cfg in TOPICS.items()
    ]
    fs = admin.create_topics(new_topics)
    for topic, f in fs.items():
        try:
            f.result()
            logger.info(f"Topic created: {topic}")
        except Exception as e:
            if "already exists" in str(e).lower():
                logger.debug(f"Topic already exists: {topic}")
            else:
                logger.warning(f"Failed to create topic {topic}: {e}")


def get_producer(extra_config: dict = None) -> Producer:
    config = {
        "bootstrap.servers": get_bootstrap_servers(),
        "acks": "all",
        "retries": 3,
        "retry.backoff.ms": 500,
    }
    if extra_config:
        config.update(extra_config)
    return Producer(config)


def get_consumer(group_id: str, topics: list[str], extra_config: dict = None) -> Consumer:
    config = {
        "bootstrap.servers": get_bootstrap_servers(),
        "group.id": group_id,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
        "session.timeout.ms": 30000,
        "max.poll.interval.ms": 300000,
    }
    if extra_config:
        config.update(extra_config)
    consumer = Consumer(config)
    consumer.subscribe(topics)
    return consumer


def delivery_report(err, msg):
    """Producer delivery callback"""
    if err:
        logger.error(f"Delivery failed for {msg.topic()}: {err}")
    else:
        logger.debug(f"Delivered to {msg.topic()}[{msg.partition()}] @ offset {msg.offset()}")


def produce_message(producer: Producer, topic: str, value: str, key: str = None):
    """發送訊息到指定 topic"""
    producer.produce(
        topic=topic,
        value=value.encode("utf-8"),
        key=key.encode("utf-8") if key else None,
        callback=delivery_report,
    )
    producer.poll(0)


_handler_failure_counts: dict[tuple, int] = {}
_MAX_HANDLER_FAILURES = 3


def consume_loop(
    consumer: Consumer,
    handler: Callable[[str], None],
    poll_timeout: float = 1.0,
    max_messages: Optional[int] = None,
):
    """
    標準消費迴圈：
    - 呼叫 handler(raw_value_str) 處理每筆訊息
    - handler 成功才 commit offset（避免靜默丟失訊息）
    - handler 連續失敗 _MAX_HANDLER_FAILURES 次後強制 commit（poison message 保護）
    - max_messages 用於測試（None = 永遠執行）
    """
    count = 0
    try:
        while True:
            msg = consumer.poll(poll_timeout)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                raise KafkaException(msg.error())

            msg_key = (msg.topic(), msg.partition(), msg.offset())
            try:
                raw = msg.value().decode("utf-8")
                handler(raw)
                consumer.commit(asynchronous=True)  # 只有成功才 commit
                _handler_failure_counts.pop(msg_key, None)
                count += 1
                if max_messages and count >= max_messages:
                    break
            except Exception as e:
                fail_count = _handler_failure_counts.get(msg_key, 0) + 1
                _handler_failure_counts[msg_key] = fail_count
                logger.error(
                    f"Handler error (attempt {fail_count}/{_MAX_HANDLER_FAILURES}): {e}",
                    exc_info=True,
                )
                if fail_count >= _MAX_HANDLER_FAILURES:
                    logger.error(
                        f"Poison message at {msg_key}, force-committing to avoid infinite loop"
                    )
                    consumer.commit(asynchronous=True)
                    _handler_failure_counts.pop(msg_key, None)
                    count += 1
                    if max_messages and count >= max_messages:
                        break
                # fail_count < MAX: 不 commit，下次 poll 重新處理

    except KeyboardInterrupt:
        logger.info("Consumer interrupted by user")
    finally:
        consumer.close()
