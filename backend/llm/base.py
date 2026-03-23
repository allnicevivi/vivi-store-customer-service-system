"""
llm/base.py — Provider 無關的 LLM 抽象層

包含：
- LLMError 例外階層（可重試 vs 致命）
- LLMMetrics：執行緒安全的 LLM 呼叫統計累計器，附 async MongoDB 持久化
- LLMClientBase：所有 LLM provider client 的抽象介面
"""
import logging
import os
import queue
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from time import sleep

logger = logging.getLogger(__name__)

_SERVICE_NAME = os.getenv("SERVICE", "unknown").split("/")[-1].replace(".py", "")


# ── 自定義例外階層 ─────────────────────────────────────────────────────────

class LLMError(Exception):
    """Base LLM error. 保留原始 cause。"""
    def __init__(self, message: str, cause: Exception | None = None):
        super().__init__(message)
        self.cause = cause


class LLMRateLimitError(LLMError):
    """429 rate limit — 可重試"""


class LLMTimeoutError(LLMError):
    """timeout / 5xx — 可重試"""


class LLMAuthError(LLMError):
    """401 / 403 / no API key — 致命"""


class LLMInvalidRequestError(LLMError):
    """400 / 422 bad request — 致命"""


# ── Metrics ────────────────────────────────────────────────────────────────

class LLMMetrics:
    """執行緒安全的 LLM 呼叫統計累計器，附 async MongoDB 持久化。"""

    def __init__(self, flush_interval_s: float = 30.0, max_queue_size: int = 1000):
        self._lock = threading.Lock()
        self._total_calls = 0
        self._total_errors = 0
        self._error_counts: dict[str, int] = {}
        self._total_latency_ms = 0.0
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._event_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._flush_interval = flush_interval_s
        self._max_queue_size = max_queue_size
        self._queue_dropped = 0
        t = threading.Thread(target=self._flush_loop, daemon=True)
        t.start()

    def record_call(
        self,
        latency_ms: float,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        call_type: str = "generate",
        attempt: int = 0,
        model: str = "",
        query_id: str = "",
    ) -> None:
        with self._lock:
            self._total_calls += 1
            self._total_latency_ms += latency_ms
            self._total_prompt_tokens += prompt_tokens
            self._total_completion_tokens += completion_tokens
            if self._total_calls % 100 == 0:
                self._log_snapshot()
        self._try_put({
            "event": "call",
            "service": _SERVICE_NAME,
            "call_type": call_type,
            "model": model,
            "latency_ms": round(latency_ms, 1),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "attempt": attempt,
            "query_id": query_id,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        })

    def record_error(self, error_type: str, call_type: str = "unknown", query_id: str = "") -> None:
        with self._lock:
            self._total_errors += 1
            self._error_counts[error_type] = self._error_counts.get(error_type, 0) + 1
        self._try_put({
            "event": "error",
            "service": _SERVICE_NAME,
            "call_type": call_type,
            "model": "unknown",
            "error_type": error_type,
            "query_id": query_id,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        })

    def to_dict(self) -> dict:
        with self._lock:
            calls = self._total_calls
            errors = self._total_errors
            return {
                "total_calls": calls,
                "total_errors": errors,
                "error_rate": round(errors / calls, 4) if calls else 0.0,
                "avg_latency_ms": round(self._total_latency_ms / calls, 1) if calls else 0.0,
                "total_prompt_tokens": self._total_prompt_tokens,
                "total_completion_tokens": self._total_completion_tokens,
                "total_tokens": self._total_prompt_tokens + self._total_completion_tokens,
                "error_counts": dict(self._error_counts),
            }

    def _try_put(self, event: dict) -> None:
        """Lock-free put；若佇列超過 max_queue_size 則丟棄並計數。"""
        if self._queue_dropped > 0 or self._event_queue.qsize() >= self._max_queue_size:
            self._queue_dropped += 1
            return
        self._event_queue.put_nowait(event)

    def _flush_loop(self) -> None:
        while True:
            sleep(self._flush_interval)
            self._flush_to_mongo()

    def _flush_to_mongo(self) -> None:
        if os.getenv("EVAL_MODE"):
            return
        events = []
        try:
            while True:
                events.append(self._event_queue.get_nowait())
        except queue.Empty:
            pass

        if not events:
            return

        try:
            from shared.mongo_client import get_collection
            col = get_collection("llm_metrics")
            if col is not None:
                col.insert_many(events, ordered=False)
                logger.debug("LLM metrics flushed %d events to MongoDB", len(events))
        except Exception as e:
            logger.warning("LLM metrics flush failed (events dropped): %s", e)

        if self._queue_dropped:
            logger.warning("LLM metrics: dropped %d events (queue full)", self._queue_dropped)
            self._queue_dropped = 0

    def _log_snapshot(self) -> None:
        """每 100 次呼叫記錄一次快照（在 lock 內呼叫）。"""
        calls = self._total_calls
        errors = self._total_errors
        avg_lat = round(self._total_latency_ms / calls, 1) if calls else 0.0
        total_tok = self._total_prompt_tokens + self._total_completion_tokens
        logger.info(
            "LLM metrics snapshot | calls=%d error_rate=%.2f%% avg_latency=%.1fms total_tokens=%d",
            calls,
            errors / calls * 100 if calls else 0.0,
            avg_lat,
            total_tok,
        )


_DEFAULT_METRICS = LLMMetrics()


# ── LLMClientBase ──────────────────────────────────────────────────────────

class LLMClientBase(ABC):
    """所有 LLM provider client 的抽象介面。"""

    @property
    @abstractmethod
    def has_key(self) -> bool:
        """是否有 API Key（可用於 if not client.has_key: fallback）"""

    @abstractmethod
    def generate(self, contents, tools=None, query_id: str = ""):
        """呼叫文字生成 API，支援 Function Calling。"""

    @abstractmethod
    def embed(self, text: str, task_type: str = "RETRIEVAL_QUERY") -> list[float]:
        """單筆 embedding。"""

    @abstractmethod
    def embed_batch(self, texts: list[str], task_type: str = "SEMANTIC_SIMILARITY") -> list[list[float]]:
        """批量 embedding。"""
