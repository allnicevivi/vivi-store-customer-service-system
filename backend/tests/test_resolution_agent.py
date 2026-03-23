"""
測試 resolution_agent/resolution_agent.py
涵蓋：evaluate_answer, _fallback_answer, _parse_resolution,
       search_faq (tool), get_policy_info, escalate_to_human,
       run_resolution_agent (no API key)
"""
import json
import pytest
from unittest.mock import MagicMock, patch

with patch.dict("sys.modules", {"shared.chroma_init": MagicMock()}):
    import resolution_agent.resolution_agent as res


def _log(label: str, value):
    if isinstance(value, dict):
        print(f"\n  {label}: {json.dumps(value, ensure_ascii=False)}")
    else:
        print(f"\n  {label}: {repr(value)}")


# ── evaluate_answer（內部工具，非 Gemini tool）────────────────────────────

class TestEvaluateAnswer:
    def test_high_quality_answer(self):
        query  = "退貨需要什麼流程"
        answer = (
            "退貨需要什麼流程很簡單，請按照以下步驟操作：\n"
            "1. 登入我的訂單申請退貨\n2. 填寫退貨原因\n3. 寄回商品\n"
            "請撥打 02-XXXX-XXXX 客服專線確認。\n"
            "您可以登入會員中心申請，步驟如下..."
        )
        result = res.evaluate_answer(query, answer)
        _log("QUERY ", query)
        _log("OUTPUT", result)
        assert result["score"] > 0.8
        assert result["is_acceptable"] is True
        assert result["recommendation"] == "publish"

    def test_low_quality_short_answer(self):
        query  = "退貨需要什麼流程"
        answer = "不知道。"
        result = res.evaluate_answer(query, answer)
        _log("QUERY ", query)
        _log("OUTPUT", result)
        assert result["score"] < 0.8
        assert result["is_acceptable"] is False
        assert result["recommendation"] == "retry_search"

    def test_negative_indicators_reduce_score(self):
        query    = "付款失敗怎麼辦"
        bad_ans  = "無法回答您的問題，請聯繫客服。"
        good_ans = "付款失敗可能是信用卡額度不足，可以登入會員中心更換付款方式。"
        r_bad  = res.evaluate_answer(query, bad_ans)
        r_good = res.evaluate_answer(query, good_ans)
        print(f"\n  BAD score={r_bad['score']}  GOOD score={r_good['score']}")
        assert r_bad["score"] < r_good["score"]

    def test_score_capped_at_1(self):
        query  = "退貨"
        answer = "退貨步驟如下：請撥打 02-XXXX-XXXX 申請，需要登入系統，可以線上申請。" * 5
        result = res.evaluate_answer(query, answer)
        assert result["score"] <= 1.0


# ── _fallback_answer ─────────────────────────────────────────────────────────

class TestFallbackAnswer:
    def test_includes_context_and_hotline(self):
        query   = "退貨問題"
        context = "電商退貨相關資訊"
        answer  = res._fallback_answer(query, context)
        assert "電商退貨相關資訊" in answer
        assert "02-XXXX-XXXX" in answer

    def test_context_truncated_to_500(self):
        long_ctx = "A" * 1000
        answer   = res._fallback_answer("query", long_ctx)
        assert "A" * 500 in answer
        assert "A" * 600 not in answer


# ── _parse_resolution ────────────────────────────────────────────────────────

class TestParseResolution:
    def _run(self, text):
        answer, action = res._parse_resolution(text)
        print(f"\n  answer={repr(answer[:60])}  action={repr(action)}")
        return answer, action

    def test_standard_format(self):
        answer, action = self._run(
            "ANSWER: 根據知識庫，您需要以下文件。\nACTION: publish"
        )
        assert answer == "根據知識庫，您需要以下文件。"
        assert action == "publish"

    def test_escalate_action(self):
        _, action = self._run("ANSWER: 無法確定。\nACTION: escalate_to_human")
        assert action == "escalate_to_human"

    def test_action_human_keyword(self):
        _, action = self._run("ANSWER: 轉交。\nACTION: escalate human")
        assert action == "escalate_to_human"

    def test_no_answer_tag_uses_full_text(self):
        text = "這是一段沒有標準格式的回覆內容，直接作為答案。"
        answer, action = self._run(text)
        assert answer == text.strip()
        assert action == "publish"

    def test_no_answer_tag_strips_action_marker(self):
        text = "退貨請在7天內申請。\nACTION: publish"
        answer, action = self._run(text)
        assert "ACTION" not in answer
        assert answer == "退貨請在7天內申請。"
        assert action == "publish"

    def test_multiline_answer(self):
        text = (
            "ANSWER: 以下是回答：\n"
            "第一步：申請表格\n第二步：提交文件\n"
            "ACTION: publish"
        )
        answer, action = self._run(text)
        assert "第一步" in answer and "第二步" in answer


# ── search_faq tool ───────────────────────────────────────────────────────────

class TestSearchFaqTool:
    def test_returns_structured_result(self):
        mock_hits = [
            {"question": "退貨問題", "document": "說明...", "score": 0.85},
            {"question": "物流問題", "document": "說明...", "score": 0.75},
        ]
        with patch.object(res, "chroma_search_faq", return_value=mock_hits):
            result = res.search_faq("退貨", n_results=2)
        _log("OUTPUT", {k: v for k, v in result.items() if k != "results"})
        assert result["count"] == 2
        assert result["best_score"] == 0.85

    def test_n_results_clipped_to_5(self):
        with patch.object(res, "chroma_search_faq", return_value=[]) as mock_sf:
            res.search_faq("query", n_results=10)
            actual_n = mock_sf.call_args.kwargs.get(
                "n_results", mock_sf.call_args.args[1] if len(mock_sf.call_args.args) > 1 else None
            )
        print(f"\n  n_results=10 clipped to: {actual_n}")
        assert actual_n <= 5

    def test_empty_results(self):
        with patch.object(res, "chroma_search_faq", return_value=[]):
            result = res.search_faq("不存在的問題")
        assert result["count"] == 0
        assert result["best_score"] == 0.0


# ── get_policy_info tool ──────────────────────────────────────────────────────

class TestGetPolicyInfo:
    def test_returns_structured_result(self):
        mock_hits = [{"question": "鑑賞期規定", "document": "7天鑑賞期...", "score": 0.78}]
        with patch.object(res, "chroma_search_policy", return_value=mock_hits):
            result = res.get_policy_info("鑑賞期是幾天")
        _log("OUTPUT", {k: v for k, v in result.items() if k != "results"})
        assert result["count"] == 1
        assert result["best_score"] == 0.78
        assert "note" in result  # 示範環境說明

    def test_empty_results(self):
        with patch.object(res, "chroma_search_policy", return_value=[]):
            result = res.get_policy_info("不存在條款")
        assert result["count"] == 0
        assert result["best_score"] == 0.0


# ── escalate_to_human tool ────────────────────────────────────────────────────

class TestEscalateToHuman:
    def test_returns_escalated_true(self):
        result = res.escalate_to_human("問題超出知識庫範圍")
        _log("OUTPUT", result)
        assert result["escalated"] is True
        assert "reason" in result
        assert len(result["message"]) > 0

    def test_reason_preserved(self):
        reason = "需要查看我目前的訂單"
        result = res.escalate_to_human(reason)
        assert result["reason"] == reason


# ── execute_tool dispatcher ───────────────────────────────────────────────────

class TestExecuteTool:
    def test_dispatches_search_faq(self):
        with patch.object(res, "search_faq", return_value={"results": [], "count": 0, "best_score": 0.0}) as mock_sf:
            res.execute_tool("search_faq", {"query": "測試", "n_results": 2})
            mock_sf.assert_called_once_with("測試", 2)

    def test_dispatches_get_policy_info(self):
        with patch.object(res, "get_policy_info", return_value={"results": [], "count": 0, "best_score": 0.0, "note": ""}) as mock_pi:
            res.execute_tool("get_policy_info", {"query": "保障範圍"})
            mock_pi.assert_called_once_with("保障範圍")

    def test_dispatches_escalate_to_human(self):
        with patch.object(res, "escalate_to_human", return_value={"escalated": True, "reason": "x", "message": ""}) as mock_es:
            res.execute_tool("escalate_to_human", {"reason": "超出範圍"})
            mock_es.assert_called_once_with("超出範圍")

    def test_unknown_tool_returns_error(self):
        result_str = res.execute_tool("nonexistent_tool", {})
        result = json.loads(result_str)
        assert "error" in result


# ── run_resolution_agent (no API key) ────────────────────────────────────────

class TestRunResolutionAgentNoApiKey:
    def test_fallback_mode_returns_completed(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="退貨流程是什麼？")
        mock_hits = [{"question": "退貨流程", "document": "需要登入我的訂單申請", "score": 0.80}]

        with patch.object(res._gemini, "api_key", ""):
            with patch.object(res, "chroma_search_faq", return_value=mock_hits):
                topic, answer = res.run_resolution_agent(msg)

        print(f"\n  topic={repr(topic)}  answer_len={len(answer) if answer else 0}")
        assert topic == "responses.completed"
        assert answer is not None and len(answer) > 0

    def test_fallback_mode_sets_tool_calls_empty(self):
        """降級模式不使用 Gemini tools，tool_calls 應為空"""
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="付款失敗問題")
        with patch.object(res._gemini, "api_key", ""):
            with patch.object(res, "chroma_search_faq", return_value=[]):
                res.run_resolution_agent(msg)
        # fallback mode 不設 tool_calls（msg 未被修改）
        assert msg.tool_calls == []


# ── run_resolution_agent (with API key) ──────────────────────────────────────

class TestRunResolutionAgentWithApiKey:

    def _mk_response(self, *, tool_name: str | None = None, args: dict | None = None,
                     text: str | None = None, prompt_tokens: int = 3, completion_tokens: int = 2):
        """Build a minimal fake Gemini GenerateContentResponse."""
        part = MagicMock()
        if tool_name:
            fc = MagicMock()
            fc.name = tool_name
            fc.args = args or {}
            part.function_call = fc
            part.text = None
        else:
            part.function_call = None
            part.text = text or ""

        content = MagicMock()
        content.parts = [part]

        candidate = MagicMock()
        candidate.content = content

        usage = MagicMock()
        usage.prompt_token_count = prompt_tokens
        usage.candidates_token_count = completion_tokens

        resp = MagicMock()
        resp.candidates = [candidate]
        resp.usage_metadata = usage
        return resp

    def test_search_faq_then_answer(self):
        """正常路徑：搜尋 FAQ → 輸出最終 ANSWER"""
        from shared.schema import QueryMessage

        user_query = "退貨流程是什麼"
        msg = QueryMessage(user_query=user_query)
        calls: list[tuple[str, dict]] = []

        def _fake_execute_tool(name: str, args: dict) -> str:
            calls.append((name, args))
            if name == "search_faq":
                return json.dumps({
                    "count": 1,
                    "best_score": 0.88,
                    "results": [{"question": "退貨流程", "document": "登入我的訂單申請退貨", "score": 0.88}],
                }, ensure_ascii=False)
            return json.dumps({"error": "unexpected"}, ensure_ascii=False)

        responses = [
            self._mk_response(tool_name="search_faq", args={"query": user_query, "n_results": 3}, prompt_tokens=5, completion_tokens=2),
            self._mk_response(text="ANSWER: 登入我的訂單即可申請退貨，步驟如下。\nACTION: publish", prompt_tokens=5, completion_tokens=3),
        ]

        with patch.object(res._gemini, "api_key", "test-key"):
            with patch.object(res._gemini, "generate", side_effect=responses):
                with patch.object(res, "execute_tool", side_effect=_fake_execute_tool):
                    topic, answer = res.run_resolution_agent(msg)

        _log("topic", topic)
        _log("answer", answer)
        assert topic == "responses.completed"
        assert answer is not None and len(answer) > 0
        assert msg.tool_calls == [{"tool": "search_faq", "args": {"query": user_query, "n_results": 3}}]
        assert msg.token_usage.total_tokens > 0

    def test_escalate_via_tool_call(self):
        """Agent 直接呼叫 escalate_to_human 工具 → 路由至 queries.human"""
        from shared.schema import QueryMessage

        msg = QueryMessage(user_query="我要投訴你們公司")
        calls: list[tuple[str, dict]] = []

        def _fake_execute_tool(name: str, args: dict) -> str:
            calls.append((name, args))
            return json.dumps({"escalated": True, "reason": args.get("reason", ""), "message": "已升級"}, ensure_ascii=False)

        responses = [
            self._mk_response(tool_name="escalate_to_human", args={"reason": "問題超出知識庫範圍"}, prompt_tokens=4, completion_tokens=2),
        ]

        with patch.object(res._gemini, "api_key", "test-key"):
            with patch.object(res._gemini, "generate", side_effect=responses):
                with patch.object(res, "execute_tool", side_effect=_fake_execute_tool):
                    topic, answer = res.run_resolution_agent(msg)

        _log("topic", topic)
        assert topic == "queries.human"
        assert answer is None
        assert msg.escalated is True
        assert any(c["tool"] == "escalate_to_human" for c in msg.tool_calls)

    def test_escalate_via_text_action(self):
        """Agent 搜尋後在文字中輸出 ACTION: escalate_to_human → 路由至 queries.human"""
        from shared.schema import QueryMessage

        user_query = "有法律糾紛需要處理"
        msg = QueryMessage(user_query=user_query)

        def _fake_execute_tool(name: str, args: dict) -> str:
            return json.dumps({"count": 0, "best_score": 0.0, "results": []}, ensure_ascii=False)

        responses = [
            self._mk_response(tool_name="search_faq", args={"query": user_query, "n_results": 3}),
            self._mk_response(text="ANSWER: 此問題超出範圍。\nACTION: escalate_to_human"),
        ]

        with patch.object(res._gemini, "api_key", "test-key"):
            with patch.object(res._gemini, "generate", side_effect=responses):
                with patch.object(res, "execute_tool", side_effect=_fake_execute_tool):
                    topic, answer = res.run_resolution_agent(msg)

        _log("topic", topic)
        assert topic == "queries.human"
        assert answer is None
        assert msg.escalated is True

    def test_multiple_tools_before_answer(self):
        """Agent 依序呼叫 search_faq + get_policy_info 後輸出最終答案"""
        from shared.schema import QueryMessage

        user_query = "鑑賞期退貨政策是什麼"
        msg = QueryMessage(user_query=user_query)
        calls: list[tuple[str, dict]] = []

        def _fake_execute_tool(name: str, args: dict) -> str:
            calls.append((name, args))
            if name == "search_faq":
                return json.dumps({"count": 1, "best_score": 0.7, "results": [{"document": "退貨說明"}]}, ensure_ascii=False)
            if name == "get_policy_info":
                return json.dumps({"count": 1, "best_score": 0.9, "results": [{"document": "7天鑑賞期"}], "note": "demo"}, ensure_ascii=False)
            return json.dumps({"error": "unexpected"}, ensure_ascii=False)

        responses = [
            self._mk_response(tool_name="search_faq", args={"query": user_query, "n_results": 3}),
            self._mk_response(tool_name="get_policy_info", args={"query": user_query}),
            self._mk_response(text="ANSWER: 商品享有7天鑑賞期，可申請退貨。\nACTION: publish"),
        ]

        with patch.object(res._gemini, "api_key", "test-key"):
            with patch.object(res._gemini, "generate", side_effect=responses):
                with patch.object(res, "execute_tool", side_effect=_fake_execute_tool):
                    topic, answer = res.run_resolution_agent(msg)

        _log("topic", topic)
        _log("tool_calls", msg.tool_calls)
        assert topic == "responses.completed"
        assert len(msg.tool_calls) == 2

    def test_max_iterations_exceeded(self):
        """Agent 持續呼叫工具超過 max_iterations(12) → 路由至 queries.human"""
        from shared.schema import QueryMessage

        msg = QueryMessage(user_query="無限循環問題")

        def _fake_execute_tool(name: str, args: dict) -> str:
            return json.dumps({"count": 0, "best_score": 0.0, "results": []}, ensure_ascii=False)

        # 提供 12 個 tool-call 回應，耗盡迴圈上限
        responses = [
            self._mk_response(tool_name="search_faq", args={"query": "無限循環問題", "n_results": 3})
            for _ in range(12)
        ]

        with patch.object(res._gemini, "api_key", "test-key"):
            with patch.object(res._gemini, "generate", side_effect=responses):
                with patch.object(res, "execute_tool", side_effect=_fake_execute_tool):
                    topic, answer = res.run_resolution_agent(msg)

        _log("topic", topic)
        assert topic == "queries.human"
        assert answer is None
