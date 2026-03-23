"""
測試 shared/schema.py
涵蓋：QueryMessage 建立、序列化/反序列化、TokenUsage
"""
import json
from shared.schema import QueryMessage, TokenUsage


def _log(label: str, value):
    if isinstance(value, dict):
        print(f"\n  {label}: {json.dumps(value, ensure_ascii=False)}")
    else:
        print(f"\n  {label}: {repr(value)}")


class TestTokenUsage:
    def test_total_tokens(self):
        t = TokenUsage(prompt_tokens=100, completion_tokens=50)
        print(f"\n  INPUT : prompt=100  completion=50")
        print(f"  OUTPUT: total={t.total_tokens}")
        assert t.total_tokens == 150

    def test_default_zero(self):
        t = TokenUsage()
        print(f"\n  INPUT : (default)")
        print(f"  OUTPUT: total={t.total_tokens}")
        assert t.total_tokens == 0


class TestQueryMessage:
    def test_default_fields(self):
        msg = QueryMessage(user_query="測試問題")
        print(f"\n  INPUT : user_query='測試問題'")
        print(f"  OUTPUT: query_id={repr(msg.query_id[:8])}...  user_id={repr(msg.user_id)}")
        assert msg.user_id == "anonymous"
        assert msg.query_id
        assert msg.timestamp
        assert msg.intent is None
        assert msg.emotion_score == 0.0
        assert msg.retry_count == 0
        assert msg.response is None

    def test_custom_fields(self):
        msg = QueryMessage(
            user_query="保費多少",
            user_id="C001",
            intent="premium_payment",
            emotion_score=0.3,
        )
        print(f"\n  INPUT : user_id='C001'  intent='premium_payment'  emotion_score=0.3")
        print(f"  OUTPUT: user_id={repr(msg.user_id)}  intent={repr(msg.intent)}  score={msg.emotion_score}")
        assert msg.user_id == "C001"
        assert msg.intent == "premium_payment"
        assert msg.emotion_score == 0.3

    def test_to_json_roundtrip(self):
        msg = QueryMessage(
            user_query="理賠文件",
            user_id="C002",
            intent="claim_document",
            emotion_score=0.1,
            retry_count=1,
            response="好的，需要以下文件...",
        )
        msg.token_usage = TokenUsage(prompt_tokens=200, completion_tokens=80)
        msg.latency_ms = 123.4

        json_str = msg.to_json()
        restored = QueryMessage.from_json(json_str)

        print(f"\n  ORIGINAL: user_query={repr(msg.user_query)}  tokens=({msg.token_usage.prompt_tokens},{msg.token_usage.completion_tokens})")
        print(f"  RESTORED: user_query={repr(restored.user_query)}  tokens=({restored.token_usage.prompt_tokens},{restored.token_usage.completion_tokens})")

        assert restored.user_query == msg.user_query
        assert restored.user_id == msg.user_id
        assert restored.intent == msg.intent
        assert restored.emotion_score == msg.emotion_score
        assert restored.retry_count == msg.retry_count
        assert restored.response == msg.response
        assert restored.token_usage.prompt_tokens == 200
        assert restored.token_usage.completion_tokens == 80
        assert restored.latency_ms == 123.4

    def test_from_json_preserves_query_id(self):
        msg = QueryMessage(user_query="查詢保單")
        original_id = msg.query_id
        restored = QueryMessage.from_json(msg.to_json())
        print(f"\n  ORIGINAL query_id: {repr(original_id)}")
        print(f"  RESTORED query_id: {repr(restored.query_id)}")
        assert restored.query_id == original_id

    def test_add_tokens(self):
        msg = QueryMessage(user_query="test")
        msg.add_tokens(100, 50)
        print(f"\n  add_tokens(100, 50) → prompt={msg.token_usage.prompt_tokens}  completion={msg.token_usage.completion_tokens}")
        msg.add_tokens(20, 10)
        print(f"  add_tokens(20, 10)  → prompt={msg.token_usage.prompt_tokens}  completion={msg.token_usage.completion_tokens}  total={msg.token_usage.total_tokens}")
        assert msg.token_usage.prompt_tokens == 120
        assert msg.token_usage.completion_tokens == 60
        assert msg.token_usage.total_tokens == 180

    def test_optional_fields_none(self):
        msg = QueryMessage(user_query="test")
        data = json.loads(msg.to_json())
        print(f"\n  Optional fields: intent={data['intent']}  routed_to={data['routed_to']}  response={data['response']}")
        assert data["intent"] is None
        assert data["routed_to"] is None
        assert data["response"] is None
        assert data["error"] is None
        assert data["routing_reason"] is None
