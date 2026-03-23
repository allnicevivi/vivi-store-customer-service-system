"""
統一 logging 設定工具
所有服務只需 from shared.log_utils import get_logger 即可取得設定好的 logger
"""
import logging
import os


_LOG_FORMAT = "[%(asctime)s] - [%(filename)s] - [%(funcName)s():%(lineno)d %(levelname)s] %(message)s"

_DEFAULT_NOISY_LOGGERS: dict[str, int] = {
    # HTTP clients
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    # Chroma client internals + telemetry (can be very chatty / noisy)
    "chromadb": logging.WARNING,
    "chromadb.config": logging.WARNING,
    "chromadb.telemetry": logging.WARNING,
    "chromadb.telemetry.product.posthog": logging.CRITICAL,
    "posthog": logging.CRITICAL,
    # Google GenAI SDK logs can be verbose at INFO
    "google": logging.WARNING,
    "google_genai": logging.WARNING,
    "google_genai.models": logging.WARNING,
    # pymongo internals (topology monitoring, connection pool DEBUG logs)
    "pymongo": logging.WARNING,
    "pymongo.topology": logging.WARNING,
    "pymongo.connection": logging.WARNING,
    "pymongo.serverMonitor": logging.WARNING,
}


def setup_logging(level: int | None = None) -> None:
    """設定 root logger（冪等，多次呼叫不重複加 handler）"""
    if logging.root.handlers:
        return
    log_level = level or getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format=_LOG_FORMAT,
    )

    # Reduce noisy dependency logs by default.
    # Set QUIET_THIRD_PARTY_LOGS=0 to disable this behavior.
    quiet = os.getenv("QUIET_THIRD_PARTY_LOGS", "1").strip().lower() not in {"0", "false", "no"}
    if quiet:
        for logger_name, min_level in _DEFAULT_NOISY_LOGGERS.items():
            logging.getLogger(logger_name).setLevel(min_level)


def get_logger(name: str) -> logging.Logger:
    """取得 logger，並確保 root logger 已設定"""
    setup_logging()
    return logging.getLogger(name)
