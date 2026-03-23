"""
llm/gemini_client.py — Gemini API 存取層

所有需要呼叫 Gemini 的服務都透過此類別，避免重複：
- API Key / Model 讀取（由 .env 控制）
- genai.Client 初始化與快取
- 文字生成（支援 Function Calling）
- Embedding（SDK，單筆與批量）
- 錯誤分類與重試（rate limit / timeout 可重試；auth / invalid 致命）
- LLM metrics 累計（call count, error rate, latency, tokens）
"""
import logging
import os
from time import monotonic, sleep

from google import genai
from google.genai import types

from .base import (
    LLMError, LLMRateLimitError, LLMTimeoutError,
    LLMAuthError, LLMInvalidRequestError,
    LLMMetrics, LLMClientBase, _DEFAULT_METRICS,
)

# re-export（讓既有 from llm.gemini_client import LLMError 繼續有效）
__all__ = [
    "GeminiClient",
    "LLMError", "LLMRateLimitError", "LLMTimeoutError",
    "LLMAuthError", "LLMInvalidRequestError",
    "LLMMetrics",
]

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")

# Client 層最多重試 2 次（delay after attempt 0 = 1s, after attempt 1 = 3s）
_RETRY_DELAYS = (1.0, 3.0)


# ── GeminiClient ───────────────────────────────────────────────────────────

class GeminiClient(LLMClientBase):
    """
    Gemini API 統一存取層。

    用法（模組層級單例）：
        from gemini_client import GeminiClient
        gemini = GeminiClient()

    方法：
        gemini.generate(contents, tools=None)   → GenerateContentResponse
        gemini.embed(text, task_type=...)        → list[float]
        gemini.embed_batch(texts, task_type=...) → list[list[float]]

    LLM 錯誤會被包裝成 LLMError 子類別：
        LLMRateLimitError  — 可重試（已在 client 層嘗試 2 次）
        LLMTimeoutError    — 可重試
        LLMAuthError       — 致命（不應再重試）
        LLMInvalidRequestError — 致命
    """

    def __init__(
        self,
        api_key: str = GEMINI_API_KEY,
        model: str = GEMINI_MODEL,
        embed_model: str = GEMINI_EMBED_MODEL,
        metrics: LLMMetrics = _DEFAULT_METRICS,
    ):
        self.api_key = api_key
        self.model = model
        self.embed_model = embed_model
        self.metrics = metrics
        self._client: genai.Client | None = None

    # ── 基礎屬性 ──────────────────────────────────────────────────────────

    @property
    def has_key(self) -> bool:
        """是否有 API Key（可用於 if not gemini.has_key: fallback）"""
        return bool(self.api_key)

    def _get_client(self) -> genai.Client:
        if self._client is None:
            if not self.api_key:
                raise LLMAuthError("GEMINI_API_KEY 未設定，無法建立 Gemini client")
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    # ── 錯誤分類 ──────────────────────────────────────────────────────────

    def _wrap_sdk_error(self, exc: Exception) -> LLMError:
        """
        將 SDK 拋出的原始例外轉換為對應的 LLMError 子類別。

        優先使用 google.api_core.exceptions（GCP 共用層）；
        備選：比對 type(exc).__name__ 字串（避免 private module 依賴）；
        最後防線：LLMError。
        """
        # 優先嘗試 google.api_core（較穩定的公開 API）
        try:
            from google.api_core import exceptions as gac_exc

            if isinstance(exc, gac_exc.ResourceExhausted):
                return LLMRateLimitError(f"Rate limit: {exc}", cause=exc)
            if isinstance(exc, (gac_exc.DeadlineExceeded, gac_exc.ServiceUnavailable)):
                return LLMTimeoutError(f"Timeout/unavailable: {exc}", cause=exc)
            if isinstance(exc, (gac_exc.Unauthenticated, gac_exc.PermissionDenied)):
                return LLMAuthError(f"Auth error: {exc}", cause=exc)
            if isinstance(exc, (gac_exc.InvalidArgument, gac_exc.BadRequest)):
                return LLMInvalidRequestError(f"Invalid request: {exc}", cause=exc)
        except ImportError:
            pass

        # 備選：type name 字串匹配
        type_name = type(exc).__name__
        msg = str(exc)
        type_name_lower = type_name.lower()

        if "resourceexhausted" in type_name_lower or "ratelimit" in type_name_lower or "429" in msg:
            return LLMRateLimitError(f"Rate limit: {exc}", cause=exc)
        if any(k in type_name_lower for k in ("deadline", "timeout", "unavailable")) or "503" in msg or "504" in msg:
            return LLMTimeoutError(f"Timeout/unavailable: {exc}", cause=exc)
        if any(k in type_name_lower for k in ("unauthenticated", "permissiondenied", "auth")) or "401" in msg or "403" in msg:
            return LLMAuthError(f"Auth error: {exc}", cause=exc)
        if any(k in type_name_lower for k in ("invalidargument", "badrequest")) or "400" in msg or "422" in msg:
            return LLMInvalidRequestError(f"Invalid request: {exc}", cause=exc)

        # 最後防線
        return LLMError(f"LLM error ({type_name}): {exc}", cause=exc)

    # ── 重試包裝 ──────────────────────────────────────────────────────────

    def _call_with_retry(self, fn, call_label: str, content_preview: str, query_id: str = ""):
        """
        執行 fn()，自動包裝錯誤、記錄 metrics、重試可恢復錯誤。

        - LLMRateLimitError / LLMTimeoutError：最多重試 2 次（delay 1s, 3s）
        - LLMAuthError / LLMInvalidRequestError：直接 raise，不重試
        - 重試耗盡後 raise 最後一個 wrapped error
        """
        last_exc: LLMError | None = None
        delays = list(_RETRY_DELAYS)

        for attempt, delay_after in enumerate(delays + [None]):
            t0 = monotonic()
            try:
                result = fn()
                latency_ms = (monotonic() - t0) * 1000

                # 從 usage_metadata 取 token 用量
                usage = getattr(result, "usage_metadata", None)
                prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
                completion_tokens = getattr(usage, "candidates_token_count", 0) or 0

                self.metrics.record_call(latency_ms, prompt_tokens, completion_tokens,
                                         call_type=call_label, attempt=attempt, model=self.model,
                                         query_id=query_id)
                logger.info(
                    "LLM call ok | %s | attempt=%d latency=%.0fms prompt_tok=%d comp_tok=%d | %s",
                    call_label, attempt, latency_ms, prompt_tokens, completion_tokens, content_preview,
                )
                return result

            except LLMError:
                # 已包裝過（e.g. LLMAuthError from _get_client）→ 直接 raise
                raise
            except Exception as raw_exc:
                latency_ms = (monotonic() - t0) * 1000
                wrapped = self._wrap_sdk_error(raw_exc)
                self.metrics.record_error(type(wrapped).__name__, call_type=call_label)

                if isinstance(wrapped, (LLMRateLimitError, LLMTimeoutError)) and delay_after is not None:
                    logger.warning(
                        "LLM retryable | %s | attempt=%d %s, retry in %.1fs | %s",
                        call_label, attempt, type(wrapped).__name__, delay_after, content_preview,
                    )
                    last_exc = wrapped
                    sleep(delay_after)
                    continue

                logger.error(
                    "LLM call failed | %s | attempt=%d %s | %s",
                    call_label, attempt, type(wrapped).__name__, content_preview,
                    exc_info=True,
                )
                raise wrapped from raw_exc

        # 重試耗盡
        assert last_exc is not None
        logger.error(
            "LLM call failed after retries | %s | %s",
            call_label, content_preview,
        )
        raise last_exc

    # ── 文字生成 ──────────────────────────────────────────────────────────

    def generate(
        self,
        contents,
        tools: list | None = None,
        response_schema: dict | None = None,
        system_instruction: str | None = None,
        query_id: str = "",
    ):
        """
        呼叫 generate_content，支援一般生成、Function Calling 與 Structured Output。

        Args:
            contents:           str（簡單 prompt）或 list[types.Content]（對話歷史）
            tools:              Gemini TOOLS list，傳入時啟用 Function Calling
            response_schema:    JSON Schema dict，傳入時強制 application/json 輸出
            system_instruction: 系統提示字串，透過 GenerateContentConfig 正確傳遞

        Returns:
            GenerateContentResponse

        Raises:
            LLMRateLimitError  — 429，已重試 2 次
            LLMTimeoutError    — timeout / 5xx，已重試 2 次
            LLMAuthError       — 無 API Key 或認證失敗
            LLMInvalidRequestError — 400 / 422
            LLMError           — 其他未分類錯誤
        """
        client = self._get_client()
        kwargs: dict = {"model": self.model, "contents": contents}
        config_kwargs: dict = {}
        if tools:
            config_kwargs["tools"] = tools
        if response_schema:
            config_kwargs["response_mime_type"] = "application/json"
            config_kwargs["response_schema"] = response_schema
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        if config_kwargs:
            kwargs["config"] = types.GenerateContentConfig(**config_kwargs)

        preview = (
            contents[:80] if isinstance(contents, str)
            else f"[{len(contents)} messages]"
        )

        return self._call_with_retry(
            fn=lambda: client.models.generate_content(**kwargs),  # noqa: B023 (kwargs bound at call time)
            call_label="generate",
            content_preview=str(preview),
            query_id=query_id,
        )

    # ── Embedding ─────────────────────────────────────────────────────────

    def embed(self, text: str, task_type: str = "RETRIEVAL_QUERY") -> list[float]:
        """
        單筆 embedding。

        Args:
            text:      要 embed 的文字
            task_type: "RETRIEVAL_QUERY" | "RETRIEVAL_DOCUMENT" | "SEMANTIC_SIMILARITY"

        Returns:
            list[float] — 向量值
        """
        return self.embed_batch([text], task_type=task_type)[0]

    def embed_batch(
        self,
        texts: list[str],
        task_type: str = "SEMANTIC_SIMILARITY",
    ) -> list[list[float]]:
        """
        批量 embedding。

        Args:
            texts:     要 embed 的字串列表
            task_type: "SEMANTIC_SIMILARITY" | "RETRIEVAL_DOCUMENT" | "RETRIEVAL_QUERY"

        Returns:
            list[list[float]] — 每筆文字對應的向量

        Raises:
            LLMRateLimitError / LLMTimeoutError / LLMAuthError / LLMInvalidRequestError / LLMError
        """
        client = self._get_client()
        preview = f"{len(texts)} texts, task={task_type}"

        result = self._call_with_retry(
            fn=lambda: client.models.embed_content(
                model=self.embed_model,
                contents=texts,
                config=types.EmbedContentConfig(task_type=task_type, output_dimensionality=768),
            ),
            call_label="embed_batch",
            content_preview=preview,
        )
        return [e.values for e in result.embeddings]
