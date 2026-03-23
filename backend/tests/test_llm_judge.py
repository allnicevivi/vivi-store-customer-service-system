"""
tests/test_llm_judge.py — LLM Judge 單元測試（不需要外部服務）

測試覆蓋：
- 正常評分路徑（mock Gemini 回傳有效 JSON）
- JSON parse error 處理
- LLM API 錯誤處理
- 分數夾值（clamp 1–5）
- passed 欄位邏輯
- 無 API Key 時的 fallback
- 模組層級 evaluate() 便捷函式
"""

import json
from unittest.mock import MagicMock, patch

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def judge():
    """建立有效的 LLMJudge（mock GeminiClient）"""
    from evaluation.llm_judge import LLMJudge
    j = LLMJudge(api_key="fake-key-for-test")
    j._gemini = MagicMock()
    j._gemini.has_key = True
    return j


def _make_gemini_response(json_dict: dict):
    """建立 mock Gemini response，text 屬性為 JSON 字串。"""
    resp = MagicMock()
    resp.text = json.dumps(json_dict, ensure_ascii=False)
    resp.usage_metadata = None
    return resp


# ── 正常路徑 ──────────────────────────────────────────────────────────────────

def test_evaluate_happy_path(judge):
    """正常評分：四個維度均有效，overall 應為平均值（四捨五入）。"""
    judge._gemini.generate.return_value = _make_gemini_response({
        "correctness":   5,
        "completeness":  4,
        "groundedness":  5,
        "actionability": 4,
        "overall":       5,
        "critique":      "回答準確且有具體步驟",
    })

    result = judge.evaluate(
        query="退貨流程是什麼？",
        response="退貨請至會員中心提交申請，7個工作天內退款。",
        retrieved_context="退貨需在7天鑑賞期內申請...",
    )

    assert result["error"] == ""
    assert result["correctness"] == 5
    assert result["completeness"] == 4
    assert result["groundedness"] == 5
    assert result["actionability"] == 4
    assert result["overall"] == round((5 + 4 + 5 + 4) / 4)
    assert result["passed"] is True
    assert "回答準確" in result["critique"]


def test_evaluate_low_score_not_passed(judge):
    """低分評分：overall < 3 時 passed 應為 False。"""
    judge._gemini.generate.return_value = _make_gemini_response({
        "correctness":   1,
        "completeness":  2,
        "groundedness":  1,
        "actionability": 2,
        "overall":       2,
        "critique":      "答非所問",
    })

    result = judge.evaluate(
        query="怎麼退款？",
        response="感謝您的支持！",
    )

    assert result["overall"] <= 2
    assert result["passed"] is False


def test_evaluate_boundary_score_exactly_3_passes(judge):
    """邊界：overall = 3 時 passed 應為 True。"""
    judge._gemini.generate.return_value = _make_gemini_response({
        "correctness":   3,
        "completeness":  3,
        "groundedness":  3,
        "actionability": 3,
        "overall":       3,
        "critique":      "尚可",
    })

    result = judge.evaluate(query="有問題", response="請聯繫客服")
    assert result["overall"] == 3
    assert result["passed"] is True


# ── 分數夾值 ────────────────────────────────────────────────────────────────

def test_score_clamped_above_5(judge):
    """分數超過 5 應被夾回 5。"""
    judge._gemini.generate.return_value = _make_gemini_response({
        "correctness":   10,
        "completeness":  6,
        "groundedness":  5,
        "actionability": 5,
        "overall":       5,
        "critique":      "滿分",
    })

    result = judge.evaluate(query="q", response="r")
    assert result["correctness"] == 5
    assert result["completeness"] == 5


def test_score_clamped_below_1(judge):
    """分數低於 1 應被夾回 1。"""
    judge._gemini.generate.return_value = _make_gemini_response({
        "correctness":   0,
        "completeness":  -1,
        "groundedness":  1,
        "actionability": 1,
        "overall":       1,
        "critique":      "很差",
    })

    result = judge.evaluate(query="q", response="r")
    assert result["correctness"] == 1
    assert result["completeness"] == 1


def test_score_with_invalid_type_defaults_to_3(judge):
    """無法解析的分數值應 fallback 為 3。"""
    judge._gemini.generate.return_value = _make_gemini_response({
        "correctness":   "five",
        "completeness":  None,
        "groundedness":  3,
        "actionability": 3,
        "overall":       3,
        "critique":      "解析問題",
    })

    result = judge.evaluate(query="q", response="r")
    assert result["correctness"] == 3
    assert result["completeness"] == 3


# ── 錯誤處理 ────────────────────────────────────────────────────────────────

def test_json_parse_error_returns_error_result(judge):
    """Gemini 回傳非 JSON 時，error 欄位應有內容，分數應為 0。"""
    bad_response = MagicMock()
    bad_response.text = "這不是 JSON 格式的回覆"
    judge._gemini.generate.return_value = bad_response

    result = judge.evaluate(query="q", response="r")

    assert result["error"] != ""
    assert result["overall"] == 0
    assert result["passed"] is False


def test_llm_api_error_returns_error_result(judge):
    """Gemini API 拋出 LLMError 時應回傳 error result。"""
    from llm.gemini_client import LLMRateLimitError
    judge._gemini.generate.side_effect = LLMRateLimitError("Rate limited", cause=None)

    result = judge.evaluate(query="q", response="r")

    assert "LLM error" in result["error"] or "Rate" in result["error"]
    assert result["overall"] == 0
    assert result["passed"] is False


def test_unexpected_exception_returns_error_result(judge):
    """非預期例外（如網路斷線）應回傳 error result 而非 raise。"""
    judge._gemini.generate.side_effect = RuntimeError("Unexpected failure")

    result = judge.evaluate(query="q", response="r")

    assert result["error"] != ""
    assert result["passed"] is False


# ── 無 API Key 的 fallback ────────────────────────────────────────────────────

def test_fallback_when_no_api_key():
    """無 API Key 時應使用規則式評分 fallback，不 raise。"""
    from evaluation.llm_judge import LLMJudge
    j = LLMJudge(api_key="")  # 明確傳空字串 = 強制無 key

    result = j.evaluate(
        query="退貨流程",
        response="請至會員中心申請退貨，步驟如下：1. 登入 2. 選擇訂單 3. 申請退貨。"
    )

    # fallback 不應 raise，應回傳結果（但 error 欄位標記 no_api_key_fallback）
    assert "error" in result
    assert "overall" in result


# ── overall 計算驗證 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("dims,expected_overall", [
    ([5, 5, 5, 5], 5),
    ([1, 1, 1, 1], 1),
    ([4, 3, 4, 3], 4),  # mean=3.5 → round to 4
    ([3, 3, 3, 4], 3),  # mean=3.25 → round to 3
])
def test_overall_computed_from_dims(judge, dims, expected_overall):
    """overall 應為四個維度的平均值（四捨五入），忽略 LLM 回傳的 overall。"""
    judge._gemini.generate.return_value = _make_gemini_response({
        "correctness":   dims[0],
        "completeness":  dims[1],
        "groundedness":  dims[2],
        "actionability": dims[3],
        "overall":       99,  # 故意給錯誤值，應被重新計算
        "critique":      "test",
    })

    result = judge.evaluate(query="q", response="r")
    assert result["overall"] == expected_overall


# ── 模組層級 evaluate() 函式 ──────────────────────────────────────────────────

def test_module_level_evaluate_function():
    """模組層級的 evaluate() 便捷函式應可正常呼叫（不需 API key，用 mock）。"""
    import evaluation.llm_judge as judge_module

    original_judge = judge_module._judge
    mock_judge = MagicMock()
    mock_judge.evaluate.return_value = {
        "correctness": 4, "completeness": 4, "groundedness": 4,
        "actionability": 4, "overall": 4, "critique": "ok",
        "passed": True, "error": "",
    }
    judge_module._judge = mock_judge

    try:
        result = judge_module.evaluate(query="test q", response="test r")
        assert result["passed"] is True
        mock_judge.evaluate.assert_called_once_with(
            query="test q", response="test r", retrieved_context="", query_id=""
        )
    finally:
        judge_module._judge = original_judge


# ── LLMJudge.available 屬性 ──────────────────────────────────────────────────

def test_available_true_with_api_key():
    from evaluation.llm_judge import LLMJudge
    j = LLMJudge(api_key="test-key")
    assert j.available is True


def test_available_false_without_api_key(monkeypatch):
    """明確傳空字串 api_key="" 時，即使環境變數有值也應視為無 key。"""
    from evaluation.llm_judge import LLMJudge
    # 傳空字串 = 明確指定無 key（不從環境變數讀取）
    j = LLMJudge(api_key="")
    assert j.available is False
