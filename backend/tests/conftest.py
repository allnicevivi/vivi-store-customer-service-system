"""
測試共用設定
- 全域 mock kafka_utils.Producer / Consumer，防止實際連線 kafka:9092
- 全域 mock mongo_client，防止實際連線 MongoDB
"""
import logging
import pytest
from unittest.mock import MagicMock, patch

# Load .env early so GEMINI_API_KEY / other env vars are set before any module-level
# GeminiClient() singletons are constructed.  This avoids ordering-dependent failures
# where gemini_client.py is imported (and GEMINI_API_KEY is read as "") before the
# first test file calls load_dotenv().
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except Exception:
    pass


@pytest.fixture(autouse=True)
def _block_real_kafka(monkeypatch):
    """
    防止任何測試意外建立真實 Kafka 連線（confluent_kafka 會嘗試連到 kafka:9092）。
    所有測試應自行 patch get_producer / get_consumer；此 fixture 做最後一道防線。
    """
    from shared import kafka_utils
    monkeypatch.setattr(kafka_utils, "Producer", MagicMock)
    monkeypatch.setattr(kafka_utils, "Consumer", MagicMock)


@pytest.fixture(autouse=True)
def _block_real_mongo(monkeypatch):
    """
    防止任何測試意外建立真實 MongoDB 連線。
    回傳 MagicMock collection，讓 insert_one / find 等呼叫不拋例外。
    """
    from shared import mongo_client
    mock_col = MagicMock()
    monkeypatch.setattr(mongo_client, "get_collection", lambda name: mock_col)
    monkeypatch.setattr(mongo_client, "_client", None)  # 重置 cached client
    return mock_col


@pytest.fixture(autouse=True)
def _quiet_noisy_third_party_logs():
    """
    Pytest is configured to show DEBUG logs globally.
    Keep our own DEBUG logs, but reduce noisy dependency logs (httpx/httpcore/chromadb...).
    """
    noisy = [
        "httpx",
        "httpcore",
        "chromadb",
        "chromadb.telemetry",
        "chromadb.telemetry.product.posthog",
        "chromadb.config",
        "posthog",
        "google",
        "google_genai",
        "google_genai.models",
    ]
    for name in noisy:
        level = logging.WARNING
        if name in {"posthog", "chromadb.telemetry.product.posthog"}:
            level = logging.CRITICAL
        logging.getLogger(name).setLevel(level)


# ── Integration test markers ────────────────────────────────────────────────

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: marks tests that require running external services "
        "(Kafka, ChromaDB, MongoDB). Run with: pytest -m integration"
    )


@pytest.fixture
def mongo_col(_block_real_mongo):
    """測試中取得 mock MongoDB collection，可斷言 insert_one 呼叫。"""
    return _block_real_mongo
