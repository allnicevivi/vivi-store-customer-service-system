"""
測試 consumers/cost_tracker.py
涵蓋：calculate_cost, handle_message
"""
from unittest.mock import MagicMock, patch

with patch.dict("sys.modules", {"shared.chroma_init": MagicMock(), "shared.mongo_client": MagicMock()}):
    import consumers.cost_tracker as ct


class TestCalculateCost:
    def test_zero_tokens(self):
        assert ct.calculate_cost(0, 0) == 0.0

    def test_prompt_tokens_only(self):
        cost = ct.calculate_cost(1_000_000, 0)
        print(f"\n  1M prompt tokens → ${cost}  (expected $0.075)")
        assert abs(cost - 0.075) < 1e-9

    def test_completion_tokens_only(self):
        cost = ct.calculate_cost(0, 1_000_000)
        print(f"\n  1M completion tokens → ${cost}  (expected $0.30)")
        assert abs(cost - 0.30) < 1e-9

    def test_both_tokens(self):
        cost = ct.calculate_cost(1_000_000, 1_000_000)
        assert abs(cost - 0.375) < 1e-9

    def test_small_usage(self):
        cost = ct.calculate_cost(1000, 500)
        expected = 1000 / 1_000_000 * 0.075 + 500 / 1_000_000 * 0.30
        assert abs(cost - expected) < 1e-12


class TestHandleMessage:
    def test_writes_to_mongodb(self, mongo_col):
        from shared.schema import QueryMessage, TokenUsage
        msg = QueryMessage(user_query="test", routed_to="queries.agent")
        msg.token_usage = TokenUsage(100, 50)
        msg.latency_ms = 80.0
        msg.intent = "order_status"
        msg.session_id = "sess-123"

        with patch.object(ct, "get_collection", return_value=mongo_col):
            ct.handle_message(msg.to_json())

        assert mongo_col.insert_one.called
        doc = mongo_col.insert_one.call_args[0][0]
        print(f"\n  MongoDB doc keys: {list(doc.keys())}")
        assert doc["query_id"] == msg.query_id
        assert doc["token_usage"]["prompt"] == 100
        assert doc["token_usage"]["completion"] == 50
        assert doc["token_usage"]["total"] == 150
        assert "estimated_cost_usd" in doc
        assert "recorded_at" in doc
        assert doc["routed_to"] == "queries.agent"
        assert doc["latency_ms"] == 80.0
        assert doc["intent"] == "order_status"
        assert doc["session_id"] == "sess-123"

    def test_graceful_degradation_no_mongo(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="test")
        with patch.object(ct, "get_collection", return_value=None):
            ct.handle_message(msg.to_json())  # 不應拋例外

    def test_cost_written_to_doc(self, mongo_col):
        from shared.schema import QueryMessage, TokenUsage
        msg = QueryMessage(user_query="test")
        msg.token_usage = TokenUsage(prompt_tokens=1_000_000, completion_tokens=0)

        with patch.object(ct, "get_collection", return_value=mongo_col):
            ct.handle_message(msg.to_json())

        doc = mongo_col.insert_one.call_args[0][0]
        assert abs(doc["estimated_cost_usd"] - 0.075) < 1e-6

    def test_mongodb_exception_not_propagated(self, mongo_col):
        mongo_col.insert_one.side_effect = Exception("timeout")
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="test")
        with patch.object(ct, "get_collection", return_value=mongo_col):
            ct.handle_message(msg.to_json())  # 不應拋例外
