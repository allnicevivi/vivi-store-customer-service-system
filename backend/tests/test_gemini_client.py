"""
Unit tests for llm/gemini_client.py

測試重點：
1. 例外分類：各種 SDK 例外 → 正確的 LLMError 子類別
2. 重試行為：可重試錯誤重試 2 次後 raise；致命錯誤立即 raise
3. LLMMetrics：record_call / record_error / to_dict 正確累計
4. generate() / embed_batch() 正確包裝呼叫並傳回結果
5. 無 API Key → LLMAuthError
"""
import pytest
from unittest.mock import MagicMock, patch, call
from time import monotonic

from llm.gemini_client import (
    GeminiClient,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMAuthError,
    LLMInvalidRequestError,
    LLMMetrics,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_client(api_key="test-key", model="gemini-test", embed_model="embed-test"):
    metrics = LLMMetrics()
    client = GeminiClient(api_key=api_key, model=model, embed_model=embed_model, metrics=metrics)
    return client, metrics


def _fake_response(prompt_tokens=10, completion_tokens=20):
    resp = MagicMock()
    resp.usage_metadata.prompt_token_count = prompt_tokens
    resp.usage_metadata.candidates_token_count = completion_tokens
    return resp


# ── LLMMetrics ───────────────────────────────────────────────────────────────

class TestLLMMetrics:
    def test_initial_state(self):
        m = LLMMetrics()
        d = m.to_dict()
        assert d["total_calls"] == 0
        assert d["total_errors"] == 0
        assert d["error_rate"] == 0.0
        assert d["avg_latency_ms"] == 0.0
        assert d["total_tokens"] == 0

    def test_record_call(self):
        m = LLMMetrics()
        m.record_call(latency_ms=100.0, prompt_tokens=5, completion_tokens=15)
        d = m.to_dict()
        assert d["total_calls"] == 1
        assert d["avg_latency_ms"] == 100.0
        assert d["total_prompt_tokens"] == 5
        assert d["total_completion_tokens"] == 15
        assert d["total_tokens"] == 20

    def test_record_error(self):
        m = LLMMetrics()
        m.record_error("LLMRateLimitError")
        m.record_error("LLMRateLimitError")
        m.record_error("LLMTimeoutError")
        d = m.to_dict()
        assert d["total_errors"] == 3
        assert d["error_counts"]["LLMRateLimitError"] == 2
        assert d["error_counts"]["LLMTimeoutError"] == 1

    def test_error_rate(self):
        m = LLMMetrics()
        m.record_call(50.0)
        m.record_call(50.0)
        m.record_error("LLMError")
        d = m.to_dict()
        assert d["error_rate"] == 0.5  # 1 error / 2 calls

    def test_async_queue_receives_events(self):
        m = LLMMetrics()
        m.record_call(latency_ms=200.0, prompt_tokens=10, completion_tokens=20,
                      call_type="generate", attempt=0)
        event = m._event_queue.get_nowait()
        assert event["event"] == "call"
        assert event["call_type"] == "generate"
        assert event["latency_ms"] == 200.0
        assert event["prompt_tokens"] == 10
        assert event["completion_tokens"] == 20
        assert event["attempt"] == 0
        assert "recorded_at" in event

    def test_async_queue_receives_error_events(self):
        m = LLMMetrics()
        m.record_error("LLMRateLimitError", call_type="generate")
        event = m._event_queue.get_nowait()
        assert event["event"] == "error"
        assert event["error_type"] == "LLMRateLimitError"
        assert event["call_type"] == "generate"
        assert "recorded_at" in event

    def test_flush_to_mongo_calls_insert_many(self):
        import queue as _queue
        m = LLMMetrics()
        # Pre-populate queue directly
        m._event_queue.put_nowait({"event": "call", "latency_ms": 100.0})
        m._event_queue.put_nowait({"event": "error", "error_type": "LLMTimeoutError"})

        mock_col = MagicMock()
        with patch("shared.mongo_client.get_collection", return_value=mock_col):
            m._flush_to_mongo()

        mock_col.insert_many.assert_called_once()
        docs = mock_col.insert_many.call_args[0][0]
        assert len(docs) == 2

    def test_queue_drop_when_full(self):
        m = LLMMetrics(max_queue_size=2)
        # Drain the background thread queue by filling it directly
        m._event_queue.put_nowait({"event": "call"})
        m._event_queue.put_nowait({"event": "call"})
        # Now queue is at max; record_call should drop
        m.record_call(latency_ms=50.0)
        assert m._queue_dropped >= 1


# ── _wrap_sdk_error ───────────────────────────────────────────────────────────

class TestWrapSdkError:
    def setup_method(self):
        self.client, _ = _make_client()

    def _wrap(self, exc):
        return self.client._wrap_sdk_error(exc)

    def test_wraps_unknown_as_llm_error(self):
        exc = ValueError("something went wrong")
        wrapped = self._wrap(exc)
        assert isinstance(wrapped, LLMError)
        assert wrapped.cause is exc

    def test_rate_limit_by_type_name(self):
        class ResourceExhausted(Exception): pass
        wrapped = self._wrap(ResourceExhausted("quota exceeded"))
        assert isinstance(wrapped, LLMRateLimitError)

    def test_timeout_by_type_name(self):
        class DeadlineExceeded(Exception): pass
        wrapped = self._wrap(DeadlineExceeded("timed out"))
        assert isinstance(wrapped, LLMTimeoutError)

    def test_auth_by_type_name(self):
        class Unauthenticated(Exception): pass
        wrapped = self._wrap(Unauthenticated("401 invalid key"))
        assert isinstance(wrapped, LLMAuthError)

    def test_invalid_request_by_type_name(self):
        class InvalidArgument(Exception): pass
        wrapped = self._wrap(InvalidArgument("bad input"))
        assert isinstance(wrapped, LLMInvalidRequestError)

    def test_rate_limit_by_message_429(self):
        exc = Exception("HTTP Error 429: too many requests")
        wrapped = self._wrap(exc)
        assert isinstance(wrapped, LLMRateLimitError)

    def test_auth_by_message_401(self):
        exc = Exception("HTTP Error 401 Unauthorized")
        wrapped = self._wrap(exc)
        assert isinstance(wrapped, LLMAuthError)

    def test_timeout_by_message_503(self):
        exc = Exception("HTTP Error 503 Service Unavailable")
        wrapped = self._wrap(exc)
        assert isinstance(wrapped, LLMTimeoutError)

    def test_google_api_core_resource_exhausted(self):
        """Test with actual google.api_core exceptions if available."""
        try:
            from google.api_core.exceptions import ResourceExhausted
            exc = ResourceExhausted("quota exceeded")
            wrapped = self._wrap(exc)
            assert isinstance(wrapped, LLMRateLimitError)
        except ImportError:
            pytest.skip("google.api_core not available")

    def test_google_api_core_unauthenticated(self):
        try:
            from google.api_core.exceptions import Unauthenticated
            exc = Unauthenticated("invalid key")
            wrapped = self._wrap(exc)
            assert isinstance(wrapped, LLMAuthError)
        except ImportError:
            pytest.skip("google.api_core not available")


# ── _call_with_retry ─────────────────────────────────────────────────────────

class TestCallWithRetry:
    def setup_method(self):
        self.client, self.metrics = _make_client()

    def test_success_no_retry(self):
        resp = _fake_response(10, 20)
        result = self.client._call_with_retry(lambda: resp, "test", "preview")
        assert result is resp
        assert self.metrics.to_dict()["total_calls"] == 1

    def test_retryable_error_then_success(self):
        """第一次 rate limit，第二次成功"""
        resp = _fake_response()
        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                class ResourceExhausted(Exception): pass
                raise ResourceExhausted("429")
            return resp

        with patch("llm.gemini_client.sleep") as mock_sleep:
            result = self.client._call_with_retry(fn, "test", "preview")

        assert result is resp
        assert call_count == 2
        mock_sleep.assert_called_once_with(1.0)  # first retry delay

    def test_retryable_error_exhausted(self):
        """兩次 rate limit → raise LLMRateLimitError"""
        class ResourceExhausted(Exception): pass

        def fn():
            raise ResourceExhausted("429")

        with patch("llm.gemini_client.sleep"):
            with pytest.raises(LLMRateLimitError):
                self.client._call_with_retry(fn, "test", "preview")

        assert self.metrics.to_dict()["total_errors"] == 3  # attempt 0, 1, 2

    def test_fatal_error_no_retry(self):
        """LLMAuthError 直接 raise，不重試"""
        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            class Unauthenticated(Exception): pass
            raise Unauthenticated("401")

        with patch("llm.gemini_client.sleep") as mock_sleep:
            with pytest.raises(LLMAuthError):
                self.client._call_with_retry(fn, "test", "preview")

        assert call_count == 1
        mock_sleep.assert_not_called()

    def test_already_wrapped_llm_error_reraises(self):
        """若 fn() 已拋出 LLMAuthError（e.g. from _get_client），直接 re-raise"""
        auth_err = LLMAuthError("no key")

        with pytest.raises(LLMAuthError) as exc_info:
            self.client._call_with_retry(lambda: (_ for _ in ()).throw(auth_err), "test", "preview")

        assert exc_info.value is auth_err


# ── generate() ────────────────────────────────────────────────────────────────

class TestGenerate:
    def test_no_api_key_raises_llm_auth_error(self):
        client = GeminiClient(api_key="", metrics=LLMMetrics())
        with pytest.raises(LLMAuthError):
            client.generate("hello")

    def test_generate_success(self):
        client, metrics = _make_client()
        mock_sdk_client = MagicMock()
        resp = _fake_response(5, 10)
        mock_sdk_client.models.generate_content.return_value = resp
        client._client = mock_sdk_client

        with patch("llm.gemini_client.sleep"):
            result = client.generate("test prompt")

        assert result is resp
        assert metrics.to_dict()["total_calls"] == 1
        mock_sdk_client.models.generate_content.assert_called_once()

    def test_generate_with_tools(self):
        client, _ = _make_client()
        mock_sdk_client = MagicMock()
        resp = _fake_response()
        mock_sdk_client.models.generate_content.return_value = resp
        client._client = mock_sdk_client

        tools = [MagicMock()]
        client.generate("prompt", tools=tools)

        call_kwargs = mock_sdk_client.models.generate_content.call_args[1]
        assert "config" in call_kwargs

    def test_generate_rate_limit_propagates(self):
        client, _ = _make_client()
        mock_sdk_client = MagicMock()

        class ResourceExhausted(Exception): pass
        mock_sdk_client.models.generate_content.side_effect = ResourceExhausted("429")
        client._client = mock_sdk_client

        with patch("llm.gemini_client.sleep"):
            with pytest.raises(LLMRateLimitError):
                client.generate("prompt")


# ── embed_batch() ─────────────────────────────────────────────────────────────

class TestEmbedBatch:
    def test_no_api_key_raises_llm_auth_error(self):
        client = GeminiClient(api_key="", metrics=LLMMetrics())
        with pytest.raises(LLMAuthError):
            client.embed_batch(["text"])

    def test_embed_batch_success(self):
        client, metrics = _make_client()
        mock_sdk_client = MagicMock()

        # embed_content returns an object with .embeddings list
        emb1 = MagicMock()
        emb1.values = [0.1, 0.2, 0.3]
        emb2 = MagicMock()
        emb2.values = [0.4, 0.5, 0.6]
        mock_result = MagicMock()
        mock_result.embeddings = [emb1, emb2]
        # embed_content result has no usage_metadata (or it's None)
        mock_result.usage_metadata = None
        mock_sdk_client.models.embed_content.return_value = mock_result
        client._client = mock_sdk_client

        result = client.embed_batch(["hello", "world"])
        assert result == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        assert metrics.to_dict()["total_calls"] == 1

    def test_embed_delegates_to_embed_batch(self):
        client, _ = _make_client()
        with patch.object(client, "embed_batch", return_value=[[0.1, 0.2]]) as mock_eb:
            result = client.embed("hello", task_type="RETRIEVAL_QUERY")
        mock_eb.assert_called_once_with(["hello"], task_type="RETRIEVAL_QUERY")
        assert result == [0.1, 0.2]
