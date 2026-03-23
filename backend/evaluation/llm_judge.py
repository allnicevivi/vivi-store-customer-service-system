"""
evaluation/llm_judge.py — LLM-as-Judge 評分器

使用獨立的 Gemini instance（與生成答案的分開）對回覆品質做結構化評分。
四個維度各 1–5 分：
  - correctness   正確性：答案是否與知識庫吻合？
  - completeness  完整性：是否涵蓋客戶問題的所有面向？
  - groundedness  根植性：是否基於提供的知識，而非憑空捏造？
  - actionability 可執行性：客戶是否清楚知道下一步要做什麼？

overall = round(mean of four dimensions)
passed  = overall >= 3
"""

import json
import logging
import os
import sys
from dotenv import load_dotenv
load_dotenv()

# ── PYTHONPATH 修正（本地直接執行時使用）───────────────────────────────────
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from llm.gemini_client import GeminiClient, LLMError

logger = logging.getLogger(__name__)

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "correctness":   {"type": "integer"},
        "completeness":  {"type": "integer"},
        "groundedness":  {"type": "integer"},
        "actionability": {"type": "integer"},
        "overall":       {"type": "integer"},
        "critique":      {"type": "string"},
    },
    "required": ["correctness", "completeness", "groundedness", "actionability", "overall", "critique"],
}

_JUDGE_PROMPT_TEMPLATE = """你是電商客服系統的品質評審員。請根據以下資訊，評估客服回覆的品質。

## 客戶問題
{query}

## 可用知識庫內容（客服回覆應基於此）
{retrieved_context}

## 客服回覆
{response}

## 評分標準（各維度 1–5 分）

**1. 正確性（correctness）**
- 5：完全正確，所有資訊都與知識庫吻合
- 4：大致正確，有小細節不完整但沒有錯誤
- 3：部分正確，混有不確定或含糊的說法
- 2：有明顯錯誤或與知識庫矛盾
- 1：完全錯誤或嚴重誤導客戶

**2. 完整性（completeness）**
- 5：完整回答客戶問題的所有面向
- 4：涵蓋主要問題，少數次要細節未提及
- 3：回答了部分問題，有明顯遺漏
- 2：只回答了問題的一小部分
- 1：完全沒有回答客戶的實際問題

**3. 根植性（groundedness）**
- 5：完全基於提供的知識庫，無憑空捏造
- 4：主要基於知識庫，有小部分合理推論
- 3：部分內容無法從知識庫驗證
- 2：有明顯超出知識庫的捏造內容
- 1：幾乎都是知識庫以外的捏造

**4. 可執行性（actionability）**
- 5：客戶清楚知道下一步，有具體步驟或聯繫方式
- 4：有明確指引，但可再具體一些
- 3：有方向但不夠具體
- 2：模糊，客戶仍不知道該怎麼做
- 1：完全沒有任何可執行的指引

**overall**: 四個維度的平均值（四捨五入取整數）

請提供簡短的 critique（50字以內，繁體中文），說明主要優缺點。

只回傳 JSON，不要其他文字。"""


class LLMJudge:
    """LLM-as-Judge 評分器，使用獨立 Gemini instance。"""

    def __init__(self, api_key: str | None = None):
        # None = 未指定，從環境變數讀取；"" = 明確表示無 API key
        effective_key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "")
        self._gemini = GeminiClient(api_key=effective_key)

    @property
    def available(self) -> bool:
        return self._gemini.has_key

    def evaluate(
        self,
        query: str,
        response: str,
        retrieved_context: str = "",
        query_id: str = "",
    ) -> dict:
        """
        評估客服回覆品質。

        Args:
            query:             客戶原始問題
            response:          客服（Resolution Agent）的回覆
            retrieved_context: Agent 使用的知識庫內容（可選，空字串代表無知識庫支援）
            query_id:          用於 LLM metrics logging

        Returns:
            {
                "correctness":   int 1–5,
                "completeness":  int 1–5,
                "groundedness":  int 1–5,
                "actionability": int 1–5,
                "overall":       int 1–5,
                "critique":      str,
                "passed":        bool,  # overall >= 3
                "error":         str,   # 非空代表評估失敗，分數不可信
            }
        """
        if not self.available:
            return self._fallback_evaluate(query, response)

        context_section = retrieved_context.strip() if retrieved_context else "（無知識庫內容，請根據一般電商客服標準評分）"
        prompt = _JUDGE_PROMPT_TEMPLATE.format(
            query=query,
            retrieved_context=context_section,
            response=response,
        )

        try:
            resp = self._gemini.generate(
                prompt,
                response_schema=_JUDGE_SCHEMA,
                query_id=query_id,
            )
            text = resp.text.strip().strip("```json").strip("```").strip()
            result = json.loads(text)
            return self._validate_and_finalize(result)
        except json.JSONDecodeError as e:
            logger.warning(f"LLM judge JSON parse error: {e}")
            return self._error_result(f"JSON parse error: {e}")
        except LLMError as e:
            logger.warning(f"LLM judge API error: {e}")
            return self._error_result(f"LLM error: {e}")
        except Exception as e:
            logger.error(f"LLM judge unexpected error: {e}", exc_info=True)
            return self._error_result(f"Unexpected error: {e}")

    def _validate_and_finalize(self, raw: dict) -> dict:
        """確保分數在 1–5 範圍內，補全 passed 欄位。"""
        def clamp(v) -> int:
            try:
                return max(1, min(5, int(v)))
            except (TypeError, ValueError):
                return 3  # 預設中間值

        result = {
            "correctness":   clamp(raw.get("correctness", 3)),
            "completeness":  clamp(raw.get("completeness", 3)),
            "groundedness":  clamp(raw.get("groundedness", 3)),
            "actionability": clamp(raw.get("actionability", 3)),
            "critique":      str(raw.get("critique", "")),
            "error":         "",
        }
        dims = [result["correctness"], result["completeness"],
                result["groundedness"], result["actionability"]]
        result["overall"] = round(sum(dims) / len(dims))
        result["passed"] = result["overall"] >= 3
        return result

    def _fallback_evaluate(self, query: str, response: str) -> dict:
        """無 API Key 時使用 evaluate_answer() 的規則式評分作為 fallback。"""
        try:
            from resolution_agent.resolution_agent import evaluate_answer
            rule_based = evaluate_answer(query, response)
            score_1_to_5 = round(rule_based["score"] * 4 + 1)  # 0–1 → 1–5
            score_1_to_5 = max(1, min(5, score_1_to_5))
            return {
                "correctness":   score_1_to_5,
                "completeness":  score_1_to_5,
                "groundedness":  score_1_to_5,
                "actionability": score_1_to_5,
                "overall":       score_1_to_5,
                "critique":      f"規則式評分 fallback（無 API Key）: {', '.join(rule_based.get('reasons', []))}",
                "passed":        score_1_to_5 >= 3,
                "error":         "no_api_key_fallback",
            }
        except Exception as e:
            return self._error_result(f"fallback also failed: {e}")

    def _error_result(self, msg: str) -> dict:
        return {
            "correctness":   0,
            "completeness":  0,
            "groundedness":  0,
            "actionability": 0,
            "overall":       0,
            "critique":      "",
            "passed":        False,
            "error":         msg,
        }


# ── 模組層級單例 ─────────────────────────────────────────────────────────────
_judge = LLMJudge()


def evaluate(
    query: str,
    response: str,
    retrieved_context: str = "",
    query_id: str = "",
) -> dict:
    """模組層級便捷函式，使用全域 LLMJudge 實例。"""
    return _judge.evaluate(
        query=query,
        response=response,
        retrieved_context=retrieved_context,
        query_id=query_id,
    )
