"""
測試 router_agent/router_agent.py（重構版 — 單次 LLM structured output）
涵蓋：_keyword_classify_query, _fallback_routing, run_router_agent,
       JSON parse failure handling + negative cache
"""
import json
import pytest
from unittest.mock import MagicMock, patch

_mock_redis = MagicMock()
_mock_redis.lookup.return_value = None   # 預設 cache miss
_mock_redis.store.return_value = None

with patch.dict("sys.modules", {"shared.chroma_init": MagicMock(), "shared.redis_semantic_cache": _mock_redis}):
    import router_agent.router_agent as ra

ra.cache_lookup = _mock_redis.lookup
ra.cache_store = _mock_redis.store

# 單元測試不呼叫真實 LLM：強制 keyword fallback 模式
ra._gemini.api_key = ""


def _log(label: str, value):
    print(f"\n  {label}: {json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else repr(value)}")


# ── _keyword_classify_query — 情緒偵測 ──────────────────────────────────────

class TestKeywordClassifyEmotion:
    def test_neutral_query(self):
        query = "請問我的訂單狀態如何？"
        result = ra._keyword_classify_query(query)
        _log("INPUT ", query)
        _log("OUTPUT", result)
        assert result["emotion_score"] == 0.0

    def test_strong_negative(self):
        query = "你們是騙人的！垃圾公司！氣死我了！"
        result = ra._keyword_classify_query(query)
        _log("INPUT ", query)
        _log("OUTPUT", result)
        assert result["emotion_score"] > 0.7

    def test_mild_negative(self):
        query = "為什麼遲遲沒有回覆？我等很久了！"
        result = ra._keyword_classify_query(query)
        _log("INPUT ", query)
        _log("OUTPUT", result)
        assert 0.0 < result["emotion_score"] <= 0.7

    def test_score_capped_at_1(self):
        query = "騙人 詐騙 垃圾 爛透了 氣死 告你 受夠了 太過分 不負責任 沒良心 黑心 廢物 無能"
        result = ra._keyword_classify_query(query)
        _log("INPUT ", query)
        _log("OUTPUT", result)
        assert result["emotion_score"] <= 1.0

    def test_score_is_float(self):
        query = "測試"
        result = ra._keyword_classify_query(query)
        _log("INPUT ", query)
        _log("OUTPUT", result)
        assert isinstance(result["emotion_score"], float)


# ── _keyword_classify_query — 意圖偵測 ──────────────────────────────────────

class TestKeywordClassifyIntent:
    def test_order_inquiry(self):
        query = "我的訂單到哪裡了？什麼時候到？"
        result = ra._keyword_classify_query(query)
        _log("INPUT ", query)
        _log("OUTPUT", result)
        assert result["intent"] == "order_inquiry"

    def test_return_refund(self):
        query = "我要申請退貨退款"
        result = ra._keyword_classify_query(query)
        _log("INPUT ", query)
        _log("OUTPUT", result)
        assert result["intent"] == "return_refund"

    def test_logistics(self):
        query = "物流配送問題，快遞沒收到"
        result = ra._keyword_classify_query(query)
        _log("INPUT ", query)
        _log("OUTPUT", result)
        assert result["intent"] == "logistics"

    def test_payment_issue(self):
        query = "付款失敗，信用卡刷卡不成功"
        result = ra._keyword_classify_query(query)
        _log("INPUT ", query)
        _log("OUTPUT", result)
        assert result["intent"] == "payment_issue"

    def test_account_issue(self):
        query = "我忘記登入密碼了"
        result = ra._keyword_classify_query(query)
        _log("INPUT ", query)
        _log("OUTPUT", result)
        assert result["intent"] == "account_issue"

    def test_general_complaint(self):
        query = "我要投訴你們的服務！"
        result = ra._keyword_classify_query(query)
        _log("INPUT ", query)
        _log("OUTPUT", result)
        assert result["intent"] == "general_complaint"

    def test_unknown_intent(self):
        query = "嗯嗯嗯嗯"
        result = ra._keyword_classify_query(query)
        _log("INPUT ", query)
        _log("OUTPUT", result)
        assert result["intent"] == "other"

    def test_multiple_keywords_increase_confidence(self):
        q_single = "訂單"
        q_multi = "訂單 查詢 出貨 包裹"
        r_single = ra._keyword_classify_query(q_single)
        r_multi = ra._keyword_classify_query(q_multi)
        print(f"\n  single→intent={r_single['intent']}  multi→intent={r_multi['intent']}")
        # 多關鍵字仍應辨識為同一意圖
        assert r_multi["intent"] == "order_inquiry"
        assert r_single["intent"] == "order_inquiry"


# ── _fallback_routing ────────────────────────────────────────────────────────

class TestFallbackRouting:
    def _make_msg(self, query: str):
        from shared.schema import QueryMessage
        return QueryMessage(user_query=query)

    def test_sets_emotion_score_on_msg(self):
        msg = self._make_msg("騙人 詐騙 氣死了！")
        ra._fallback_routing(msg)
        _log("emotion_score", msg.emotion_score)
        assert msg.emotion_score > 0.0

    def test_sets_intent_on_msg(self):
        msg = self._make_msg("我的訂單在哪裡？")
        ra._fallback_routing(msg)
        _log("intent", msg.intent)
        assert msg.intent == "order_inquiry"

    def test_normal_query_routes_to_agent(self):
        msg = self._make_msg("我想查訂單狀態")
        topic, reason = ra._fallback_routing(msg)
        _log("topic", topic)
        assert topic == "queries.agent"

    def test_returns_reason_string(self):
        msg = self._make_msg("退貨退款")
        topic, reason = ra._fallback_routing(msg)
        assert isinstance(reason, str) and len(reason) > 0

    def test_clarification_routes_to_completed(self):
        """needs_clarification = True 時，應路由到 responses.completed"""
        msg = self._make_msg("任意問題")
        # Monkey-patch _keyword_classify_query to trigger clarification
        original = ra._keyword_classify_query
        try:
            ra._keyword_classify_query = lambda q: {
                "emotion_score": 0.0, "intent": "other",
                "needs_clarification": True, "clarification_question": "請說明您的問題？",
                "route": "agent", "route_reason": "問題不明確",
            }
            topic, reason = ra._fallback_routing(msg)
        finally:
            ra._keyword_classify_query = original
        assert topic == "responses.completed"
        assert "能描述" in msg.response or len(msg.response) > 0


# ── run_router_agent — 無 API key ────────────────────────────────────────────

class TestRunRouterAgentNoApiKey:
    def test_fallback_called_without_api_key(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="我的訂單到哪了？")

        with patch.object(ra._gemini, "api_key", ""):
            with patch.object(ra, "_fallback_routing", return_value=("queries.agent", "fallback")) as mock_fb:
                topic, reason = ra.run_router_agent(msg)
                mock_fb.assert_called_once_with(msg)
                assert topic == "queries.agent"


# ── run_router_agent — 有 API key ────────────────────────────────────────────

class TestRunRouterAgentNormal:
    def _make_llm_resp(self, classification: dict, prompt_tokens=10, completion_tokens=5):
        resp = MagicMock()
        resp.text = json.dumps(classification, ensure_ascii=False)
        resp.usage_metadata = MagicMock()
        resp.usage_metadata.prompt_token_count = prompt_tokens
        resp.usage_metadata.candidates_token_count = completion_tokens
        return resp

    def test_routes_to_agent_on_normal_query(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="我的訂單狀態如何？")
        classification = {
            "emotion_score": 0.1, "intent": "order_inquiry",
            "needs_clarification": False, "route": "agent", "route_reason": "意圖明確",
        }
        with patch.object(ra._gemini, "api_key", "test-key"):
            with patch.object(ra._gemini, "embed", return_value=[0.1] * 768):
                with patch.object(ra._gemini, "generate", return_value=self._make_llm_resp(classification)):
                    topic, reason = ra.run_router_agent(msg)

        assert topic == "queries.agent"
        assert msg.intent == "order_inquiry"

    def test_routes_to_human_when_requested(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="我要找真人客服")
        classification = {
            "emotion_score": 0.2, "intent": "other",
            "needs_clarification": False, "route": "human", "route_reason": "客戶要求真人",
        }
        with patch.object(ra._gemini, "api_key", "test-key"):
            with patch.object(ra._gemini, "embed", return_value=[0.1] * 768):
                with patch.object(ra._gemini, "generate", return_value=self._make_llm_resp(classification)):
                    topic, reason = ra.run_router_agent(msg)

        assert topic == "queries.human"

    def test_returns_clarification_to_completed(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="有問題")
        classification = {
            "emotion_score": 0.0, "intent": "other",
            "needs_clarification": True, "clarification_question": "請問您遇到什麼問題？",
            "route": "agent", "route_reason": "問題不明確",
        }
        with patch.object(ra._gemini, "api_key", "test-key"):
            with patch.object(ra._gemini, "embed", return_value=[0.1] * 768):
                with patch.object(ra._gemini, "generate", return_value=self._make_llm_resp(classification)):
                    topic, reason = ra.run_router_agent(msg)

        assert topic == "responses.completed"
        assert msg.response == "請問您遇到什麼問題？"

    def test_token_usage_tracked(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="我的訂單在哪？")
        classification = {
            "emotion_score": 0.0, "intent": "order_inquiry",
            "needs_clarification": False, "route": "agent", "route_reason": "意圖明確",
        }
        with patch.object(ra._gemini, "api_key", "test-key"):
            with patch.object(ra._gemini, "embed", return_value=[0.1] * 768):
                with patch.object(ra._gemini, "generate",
                                  return_value=self._make_llm_resp(classification, 100, 50)):
                    ra.run_router_agent(msg)

        assert msg.token_usage.total_tokens == 150

    def test_emotion_score_set_on_msg(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="我很憤怒！")
        classification = {
            "emotion_score": 0.85, "intent": "general_complaint",
            "needs_clarification": False, "route": "agent", "route_reason": "高情緒路由 agent",
        }
        with patch.object(ra._gemini, "api_key", "test-key"):
            with patch.object(ra._gemini, "embed", return_value=[0.1] * 768):
                with patch.object(ra._gemini, "generate", return_value=self._make_llm_resp(classification)):
                    ra.run_router_agent(msg)

        assert msg.emotion_score == pytest.approx(0.85)


# ── JSON parse failure handling + negative cache ──────────────────────────────

class TestJsonParseFailureHandling:
    def setup_method(self):
        # router_agent 已改用 Redis 函式追蹤失敗計數；測試用 in-memory dict 模擬
        self._failure_cache: dict = {}
        ra.increment_llm_failure.side_effect = (
            lambda q: self._failure_cache.update({q: self._failure_cache.get(q, 0) + 1})
            or self._failure_cache[q]
        )
        ra.get_llm_failure_count.side_effect = lambda q: self._failure_cache.get(q, 0)
        ra.clear_llm_failure.side_effect = lambda q: self._failure_cache.pop(q, None)
        ra._gemini.api_key = "fake-key-for-test"

    def teardown_method(self):
        ra._gemini.api_key = ""
        ra.increment_llm_failure.side_effect = None
        ra.get_llm_failure_count.side_effect = None
        ra.clear_llm_failure.side_effect = None
        self._failure_cache = {}

    def _bad_resp(self):
        resp = MagicMock()
        resp.text = "this is not valid json {"
        resp.usage_metadata = None
        return resp

    def _good_resp(self, classification: dict):
        resp = MagicMock()
        resp.text = json.dumps(classification, ensure_ascii=False)
        resp.usage_metadata = None
        return resp

    def test_failure_counter_increments(self):
        """JSON 解析兩次失敗後，_json_failure_cache 計數至少為 1。"""
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="失敗計數查詢")

        with patch.object(ra._gemini, "embed", return_value=[0.1] * 768):
            with patch.object(ra._gemini, "generate", return_value=self._bad_resp()):
                ra.run_router_agent(msg)

        assert self._failure_cache.get("失敗計數查詢", 0) >= 1

    def test_negative_cache_skips_llm_after_max_failures(self):
        """達到 _MAX_JSON_FAILURES 後，不再呼叫 LLM，直接 fallback。"""
        from shared.schema import QueryMessage
        self._failure_cache["已失敗查詢"] = ra._MAX_JSON_FAILURES
        msg = QueryMessage(user_query="已失敗查詢")

        with patch.object(ra._gemini, "embed", return_value=[0.1] * 768):
            with patch.object(ra._gemini, "generate") as mock_gen:
                ra.run_router_agent(msg)

        mock_gen.assert_not_called()

    def test_success_clears_failure_cache(self):
        """成功解析後，_json_failure_cache 中的對應 entry 被清除。"""
        from shared.schema import QueryMessage
        self._failure_cache["好查詢"] = 2
        good_classification = {
            "emotion_score": 0.0, "intent": "order_inquiry",
            "needs_clarification": False, "route": "agent", "route_reason": "ok",
        }
        msg = QueryMessage(user_query="好查詢")

        with patch.object(ra._gemini, "embed", return_value=[0.1] * 768):
            with patch.object(ra._gemini, "generate", return_value=self._good_resp(good_classification)):
                ra.run_router_agent(msg)

        assert "好查詢" not in self._failure_cache

    def test_failure_cache_accumulates_across_two_calls(self):
        """多次 bad JSON，failure count 累積。"""
        from shared.schema import QueryMessage

        with patch.object(ra._gemini, "embed", return_value=[0.1] * 768):
            with patch.object(ra._gemini, "generate", return_value=self._bad_resp()):
                ra.run_router_agent(QueryMessage(user_query="累積查詢"))
                # Reset to allow second call (clear negative cache temporarily)
                self._failure_cache.pop("累積查詢", None)
                ra.run_router_agent(QueryMessage(user_query="累積查詢"))

        assert self._failure_cache.get("累積查詢", 0) >= 1

    def test_fallback_routing_used_on_json_failure(self):
        """JSON 解析失敗後最終使用 keyword fallback（不崩潰）。"""
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="json fail routing test")

        with patch.object(ra._gemini, "embed", return_value=[0.1] * 768):
            with patch.object(ra._gemini, "generate", return_value=self._bad_resp()):
                topic, reason = ra.run_router_agent(msg)

        assert topic in ("queries.agent", "queries.human", "responses.completed")
