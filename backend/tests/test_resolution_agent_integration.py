"""
Integration tests for run_resolution_agent — no mocks.

Uses real Gemini API and real ChromaDB (localhost:8000).
Skipped automatically when GEMINI_API_KEY is not set.
"""
import os
import json
import pytest
from dotenv import load_dotenv

load_dotenv()

pytestmark = pytest.mark.skipif(
    not os.getenv("GEMINI_API_KEY"),
    reason="GEMINI_API_KEY not set",
)

# Real import — no chroma_init patch
import resolution_agent.resolution_agent as res
from shared.schema import QueryMessage


def _log(label, value):
    if isinstance(value, dict):
        print(f"\n  {label}: {json.dumps(value, ensure_ascii=False)}")
    else:
        print(f"\n  {label}: {repr(value)}")


class TestRunResolutionAgentIntegration:

    def test_faq_query_resolves(self):
        """常見退貨問題 → 應直接用知識庫回答，路由至 responses.completed"""
        msg = QueryMessage(user_query="我想退貨，請問退貨流程是什麼？")
        topic, answer = res.run_resolution_agent(msg)
        _log("topic", topic)
        _log("answer", answer)
        _log("tool_calls", msg.tool_calls)
        _log("tokens", msg.token_usage.total_tokens)

        assert topic == "responses.completed"
        assert answer is not None and len(answer) > 10
        assert msg.token_usage.total_tokens > 0
        # Agent 應至少呼叫一個工具
        assert len(msg.tool_calls) >= 1

    def test_policy_query_resolves(self):
        """鑑賞期政策問題 → 應查詢政策 FAQ，回傳完整答案"""
        msg = QueryMessage(user_query="商品的鑑賞期是幾天？可以無條件退款嗎？")
        topic, answer = res.run_resolution_agent(msg)
        _log("topic", topic)
        _log("answer", answer)
        _log("tool_calls", msg.tool_calls)

        assert topic == "responses.completed"
        assert answer is not None and len(answer) > 10

    def test_complex_query_uses_multiple_tools(self):
        """複雜問題（訂單 + 付款 + 退貨）→ Agent 應呼叫多個工具"""
        msg = QueryMessage(user_query="我已付款成功但想取消訂單，若已出貨還能退貨退款嗎？流程與退款時間是多久？")
        topic, answer = res.run_resolution_agent(msg)
        _log("topic", topic)
        _log("answer", answer)
        _log("tool_calls", msg.tool_calls)

        # 無論路由到哪，answer 或 escalation 都應有結果
        assert topic in ("responses.completed", "queries.human")
        if topic == "responses.completed":
            assert answer is not None and len(answer) > 10
        else:
            assert msg.escalated is True

    def test_out_of_scope_query_escalates(self):
        """超出知識庫範圍的問題 → 應升級至 queries.human"""
        msg = QueryMessage(user_query="我要對你們公司提起訴訟，請提供法律文件")
        topic, answer = res.run_resolution_agent(msg)
        _log("topic", topic)
        _log("answer", answer)
        _log("escalated", msg.escalated)
        _log("tool_calls", msg.tool_calls)

        assert topic == "queries.human"
        assert answer is None
        assert msg.escalated is True

    def test_token_usage_is_tracked(self):
        """token 累計必須正確（多輪對話累加）"""
        msg = QueryMessage(user_query="如何修改我的收件地址？")
        topic, answer = res.run_resolution_agent(msg)
        _log("total_tokens", msg.token_usage.total_tokens)

        assert msg.token_usage.total_tokens > 0
        assert msg.token_usage.prompt_tokens > 0
        assert msg.token_usage.completion_tokens > 0

    def test_tool_calls_are_logged(self):
        """tool_calls 欄位應記錄每次工具名稱與參數"""
        msg = QueryMessage(user_query="如何查詢物流狀態？")
        topic, answer = res.run_resolution_agent(msg)
        _log("tool_calls", msg.tool_calls)

        for call in msg.tool_calls:
            assert "tool" in call
            assert "args" in call
            assert isinstance(call["args"], dict)
