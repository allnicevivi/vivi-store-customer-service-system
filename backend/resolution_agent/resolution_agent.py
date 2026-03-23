"""
Resolution Agent — 複雜問題解析 Agent（ReAct pattern）

Agent 決策流程（Thought → Action → Observation → Answer）：
1. Thought：分析問題，決定下一步工具
2. Action：呼叫 search_faq() 或 get_policy_info()
3. Observation：解析工具回傳結果
4. 重複直到能給出 Answer，或判斷需升級 → 呼叫 escalate_to_human()

5. 成功 → 發布 responses.completed
   API 失敗 → 退回 queries.retry
   retry_count ≥ 3 → queries.dlq

Why it's a real Agent：
- 採用 ReAct pattern，能推理選擇工具、動態決定是否升級人工
- 不是固定 pipeline，而是目標導向推理
"""
import json
import os
import re
import time

from google.genai import types

from shared.schema import QueryMessage, emotion_label
from shared.kafka_utils import get_producer, get_consumer, produce_message, consume_loop, create_topics
from shared.chroma_init import search_faq as chroma_search_faq, search_policy as chroma_search_policy
from shared.log_utils import get_logger
from shared.telemetry import emit_telemetry
from llm.gemini_client import GeminiClient, LLMRateLimitError, LLMTimeoutError, LLMAuthError, LLMInvalidRequestError
from mock_apis.order_service import get_order_status as _get_order_status
from mock_apis.inventory_service import get_inventory as _get_inventory
from mock_apis.logistics_service import track_shipment as _track_shipment

logger = get_logger(__name__)

_gemini = GeminiClient()
MAX_AGENT_RETRIES = 3

INTENT_HINTS = {
    "order_inquiry":     "客戶詢問訂單，優先呼叫 get_order_status（需訂單號碼）；無號碼時先詢問。",
    "return_refund":     "客戶詢問退換貨，優先呼叫 get_policy_info 查退換貨政策，再參考 search_faq。",
    "logistics":         "客戶詢問物流，優先呼叫 track_logistics（需追蹤碼）；無號碼時先詢問。",
    "payment_issue":     "客戶詢問付款問題，優先呼叫 search_faq 查付款相關 FAQ。",
    "account_issue":     "客戶詢問帳號問題，先 search_faq；若涉及帳號安全（被盜、異常登入）立即 escalate_to_human。",
    "general_complaint": "客戶有投訴情緒，先以同理語氣回應，再 search_faq 找解決方案。",
}

# ── Gemini Tool 定義 ────────────────────────────────────────────────────────
TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="search_faq",
                description=(
                    "在電商常見問題知識庫中做語義搜尋，回傳最相關的 FAQ 條目。"
                    "適用於一般性電商問題，如退換貨流程、付款方式、物流查詢等。"
                    "建議先用客戶原始問題搜尋，如果相關性不高，"
                    "可改用更具體的關鍵字重新搜尋。"
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "query": types.Schema(
                            type=types.Type.STRING,
                            description="搜尋查詢字串",
                        ),
                        "n_results": types.Schema(
                            type=types.Type.INTEGER,
                            description="回傳結果數量（1-5，默認 3）",
                        ),
                    },
                    required=["query"],
                ),
            ),
            types.FunctionDeclaration(
                name="get_policy_info",
                description=(
                    "查詢電商平台服務條款與政策的詳細資訊。"
                    "適用於鑑賞期規定、退換貨條件、商品保固、隱私政策、爭議處理等問題。"
                    "生產環境會按商品類別查詢政策資料庫；"
                    "示範環境透過語義搜尋知識庫中的條款說明。"
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "query": types.Schema(
                            type=types.Type.STRING,
                            description="電商條款相關查詢（例如：鑑賞期規定、退換貨條件、商品保固範圍）",
                        ),
                    },
                    required=["query"],
                ),
            ),
            types.FunctionDeclaration(
                name="escalate_to_human",
                description=(
                    "將問題升級給人工客服處理。"
                    "當問題超出知識庫範圍、需要查看特定客戶資料、"
                    "或多次搜尋仍無法給出可靠回答時使用。"
                    "呼叫此工具後，問題將轉入人工工單系統。"
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "reason": types.Schema(
                            type=types.Type.STRING,
                            description="升級人工的原因說明",
                        ),
                    },
                    required=["reason"],
                ),
            ),
            types.FunctionDeclaration(
                name="get_order_status",
                description=(
                    "查詢客戶訂單的即時狀態、預計到貨時間、訂單內容。"
                    "當客戶詢問訂單進度、出貨狀態、預計送達時間時使用。"
                    "需要客戶提供訂單編號（格式：ORD-YYYY-XXXXXX）。"
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "order_id": types.Schema(
                            type=types.Type.STRING,
                            description="訂單編號，格式如 ORD-2026-001234",
                        ),
                    },
                    required=["order_id"],
                ),
            ),
            types.FunctionDeclaration(
                name="get_inventory_info",
                description=(
                    "查詢商品的庫存狀態與補貨時間。"
                    "當客戶詢問商品是否有貨、何時補貨、缺貨原因時使用。"
                    "可使用商品名稱或商品編號（格式：PROD-XXXXX）查詢。"
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "product_id": types.Schema(
                            type=types.Type.STRING,
                            description="商品編號或商品名稱關鍵字",
                        ),
                    },
                    required=["product_id"],
                ),
            ),
            types.FunctionDeclaration(
                name="track_logistics",
                description=(
                    "追蹤包裹物流狀態，確認是否可以攔截改地址。"
                    "當客戶詢問包裹在哪裡、想更改收件地址、要攔截包裹時使用。"
                    "需要物流追蹤編號（格式：TW-XXXXXXXXXX）。"
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "tracking_number": types.Schema(
                            type=types.Type.STRING,
                            description="物流追蹤編號，格式如 TW-9876543210",
                        ),
                    },
                    required=["tracking_number"],
                ),
            ),
        ]
    )
]


# ── Tool 執行函式 ──────────────────────────────────────────────────────────
def search_faq(query: str, n_results: int = 3) -> dict:
    """呼叫 ChromaDB 做 FAQ 語義搜尋"""
    n_results = max(1, min(5, int(n_results)))
    hits = chroma_search_faq(query, n_results=n_results, threshold=0.0)
    return {
        "results": hits,
        "count": len(hits),
        "best_score": hits[0]["score"] if hits else 0.0,
    }


def get_policy_info(query: str) -> dict:
    """
    查詢電商服務條款資訊。
    搜尋 ecommerce_policy collection（ecommerce_policy.md 中的條款內容）。
    生產環境：按商品類別查詢政策資料庫（此處模擬）。
    """
    hits = chroma_search_policy(query, n_results=3, threshold=0.0)
    return {
        "results": hits,
        "count": len(hits),
        "best_score": hits[0]["score"] if hits else 0.0,
        "note": "示範環境：語義搜尋 ecommerce_policy.md 條款知識庫；生產環境應按商品類別查詢政策 DB",
    }


def escalate_to_human(reason: str) -> dict:
    """標記升級人工（工具呼叫本身回傳確認；實際路由在 run_resolution_agent 中處理）"""
    logger.info(f"Agent escalating to human: {reason}")
    return {
        "escalated": True,
        "reason": reason,
        "message": "問題已升級至人工客服，將盡快安排專人處理。",
    }


def generate_answer(query: str, context_documents: str) -> dict:
    """
    使用 Gemini 根據文件生成回答（fallback 模式使用）。
    """
    if not _gemini.has_key:
        return {"answer": _fallback_answer(query, context_documents), "generated_by": "fallback"}

    prompt = f"""你是 ViviStore 購物平台的客服專員，請根據以下知識庫文件，用繁體中文回答客戶問題。

客戶問題：{query}

相關知識庫文件：
{context_documents}

回答要求：
- 直接回答問題，不要重複問題
- 語氣親切、專業
- 如果有具體步驟，用數字列點
- 如有不確定的部分，建議撥打客服專線確認
- 回答長度適中（100-300字）"""

    response = _gemini.generate(prompt, query_id="")
    answer = response.text.strip()
    return {
        "answer": answer,
        "generated_by": "gemini",
        "prompt_tokens": response.usage_metadata.prompt_token_count if response.usage_metadata else 0,
        "completion_tokens": response.usage_metadata.candidates_token_count if response.usage_metadata else 0,
    }


def evaluate_answer(query: str, answer: str) -> dict:
    """
    評估回答品質（fallback 模式的自我評估，不作為 Gemini tool）。
    """
    score = 0.0
    reasons = []

    raw_tokens = query.replace("？", "").replace("?", "").split()
    query_keywords: set[str] = set()
    for token in raw_tokens:
        if len(token) <= 2:
            query_keywords.add(token)
        else:
            for i in range(len(token) - 1):
                query_keywords.add(token[i:i + 2])
    answer_lower = answer.lower()
    keyword_hits = sum(1 for kw in query_keywords if kw in answer_lower)
    relevance = min(keyword_hits / max(len(query_keywords), 1) * 0.4, 0.4)
    score += relevance
    if relevance > 0.2:
        reasons.append("關鍵字覆蓋率良好")

    if len(answer) > 50:
        score += 0.2
        reasons.append("回答長度足夠")
    if len(answer) > 75:
        score += 0.1
        reasons.append("回答詳細")

    actionable_indicators = ["步驟", "方式", "可以", "請", "需要", "電話", "0800", "登入", "申請"]
    action_hits = sum(1 for ind in actionable_indicators if ind in answer)
    actionability = min(action_hits * 0.1, 0.3)
    score += actionability
    if actionability > 0.1:
        reasons.append("包含可執行指引")

    negative_indicators = ["無法回答", "不知道", "不清楚", "抱歉，我", "沒有相關"]
    if any(ind in answer for ind in negative_indicators):
        score *= 0.5
        reasons.append("回答包含不確定表述，扣分")

    score = round(min(score, 1.0), 3)
    return {
        "score": score,
        "is_acceptable": score > 0.8,
        "reasons": reasons,
        "recommendation": "publish" if score > 0.8 else "retry_search",
    }


def _fallback_answer(query: str, context_documents: str) -> str:
    """無 API Key 時的降級回答"""
    return (
        f"感謝您的詢問。根據我們的知識庫，以下是相關資訊：\n\n"
        f"{context_documents[:500]}\n\n"
        f"如需進一步協助，請撥打客服專線 02-XXXX-XXXX 或緊急專線 0800-SHOP-NOW（24小時）。"
    )


def get_order_status(order_id: str) -> dict:
    """查詢訂單狀態（Mock API）"""
    return _get_order_status(order_id)


def get_inventory_info(product_id: str) -> dict:
    """查詢商品庫存（Mock API）"""
    return _get_inventory(product_id)


def track_logistics(tracking_number: str) -> dict:
    """追蹤物流狀態（Mock API）"""
    return _track_shipment(tracking_number)



def execute_tool(tool_name: str, tool_args: dict) -> str:
    """分派 tool call 到對應函式，回傳 JSON string"""
    if tool_name == "search_faq":
        result = search_faq(
            tool_args["query"],
            tool_args.get("n_results", 3),
        )
    elif tool_name == "get_policy_info":
        result = get_policy_info(tool_args["query"])
    elif tool_name == "escalate_to_human":
        result = escalate_to_human(tool_args["reason"])
    elif tool_name == "get_order_status":
        result = get_order_status(tool_args["order_id"])
    elif tool_name == "get_inventory_info":
        result = get_inventory_info(tool_args["product_id"])
    elif tool_name == "track_logistics":
        result = track_logistics(tool_args["tracking_number"])
    else:
        result = {"error": f"Unknown tool: {tool_name}"}
    logger.info(f"Tool {tool_name} result: {json.dumps(result, ensure_ascii=False)[:200]}")
    return json.dumps(result, ensure_ascii=False)


# ── Agent 主邏輯（ReAct pattern） ──────────────────────────────────────────
def run_resolution_agent(msg: QueryMessage) -> tuple[str, str | None]:
    """
    ReAct Agent（Thought → Action → Observation → Answer）

    Returns:
        (target_topic, final_answer_or_None)
    """
    emit_telemetry("resolution.started", "resolution_agent", msg.query_id, {})

    if not _gemini.has_key:
        # 降級模式：直接搜尋 + 生成
        docs = search_faq(msg.user_query, n_results=3)
        context = json.dumps(docs["results"], ensure_ascii=False)
        result = generate_answer(msg.user_query, context)
        return "responses.completed", result["answer"]

    system_prompt = """你是電商客服智能工單系統的 Resolution Agent，採用 ReAct 模式。

工具說明：
- search_faq(query)：搜尋常見問題知識庫（訂單、退換貨、物流、付款等問題）
- get_policy_info(query)：查詢電商服務條款與政策（鑑賞期、退換貨條件、保固等）
- escalate_to_human(reason)：升級人工（問題超出知識庫範圍時）
- get_order_status(order_id)：查詢訂單即時狀態、預計到貨、訂單內容（需提供訂單號碼）
- get_inventory_info(product_id)：查詢商品庫存、補貨時間
- track_logistics(tracking_number)：追蹤包裹物流、確認是否可攔截改地址

工作流程（ReAct）：
1. Thought：思考客戶問題，決定需要什麼資訊
2. Action：呼叫適合的工具
3. Observation：分析工具結果，判斷是否足夠回答
4. 重複 1-3 直到資訊充足
5. 給出最終回答，或無法回答時呼叫 escalate_to_human

推理範例：
- 客戶問「我的訂單到哪了」→ 請客戶提供訂單號碼 → 呼叫 get_order_status
- 客戶問「我想攔截包裹改地址」→ 請客戶提供追蹤碼 → 呼叫 track_logistics 確認 can_intercept → 說明費用規則
- 客戶問「商品什麼時候補貨」→ 呼叫 get_inventory_info → 說明補貨時間或建議改買其他商品

最終回答格式（無 tool call 時輸出）：
ANSWER: <完整的客戶回答，繁體中文，語氣親切專業>

若需升級人工，先呼叫 escalate_to_human 工具，系統會自動處理後續路由。"""

    # 注入對話歷史（最後 3 輪）
    if msg.conversation_history:
        recent = msg.conversation_history[-3:]
        history_text = "\n".join(
            f"  [{t['role']}]: {t['content']}"
            for t in recent
        )
        system_prompt += f"\n\n近期對話紀錄（最後 {len(recent)} 輪）：\n{history_text}"

    # 注入 intent hint（Router 已分類，避免 LLM 重複推理）
    if msg.intent and msg.intent in INTENT_HINTS:
        system_prompt += (
            f"\n\n## 本次工單意圖（Router 已分類）\n"
            f"意圖：{msg.intent}\n"
            f"建議：{INTENT_HINTS[msg.intent]}"
        )

    messages = [
        types.Content(role="user", parts=[
            types.Part(text=f"{system_prompt}\n\n<user_input>\n{msg.user_query}\n</user_input>")
        ])
    ]

    max_iterations = 12
    iteration = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    tool_call_log = []  # 記錄 tool calls 供 Output Layer 使用

    while iteration < max_iterations:
        iteration += 1
        logger.debug(f"Resolution agent iteration {iteration}")

        response = _gemini.generate(
            messages,
            tools=TOOLS,
            query_id=msg.query_id,
        )

        if response.usage_metadata:
            total_prompt_tokens += response.usage_metadata.prompt_token_count or 0
            total_completion_tokens += response.usage_metadata.candidates_token_count or 0

        if not response.candidates:
            logger.warning(f"Empty candidates for query_id={msg.query_id}, iteration {iteration}")
            break

        candidate = response.candidates[0]
        content = candidate.content
        messages.append(content)

        # 檢查 tool calls
        tool_calls = [p for p in content.parts if p.function_call is not None and p.function_call.name]
        if tool_calls:
            tool_results = []
            for part in tool_calls:
                fc = part.function_call
                args = dict(fc.args)
                logger.info(f"Resolution calling tool: {fc.name}({list(args.keys())})")

                # 記錄 tool call
                tool_call_log.append({"tool": fc.name, "args": args})

                # escalate_to_human：直接路由，不繼續迴圈
                if fc.name == "escalate_to_human":
                    msg.escalated = True
                    msg.tool_calls = tool_call_log
                    msg.add_tokens(total_prompt_tokens, total_completion_tokens)
                    return "queries.human", None

                tool_start = time.time()
                result_str = execute_tool(fc.name, args)
                tool_duration_ms = round((time.time() - tool_start) * 1000, 2)
                if fc.name in ("search_faq", "get_policy_info"):
                    msg.token_usage.embedding_calls += 1
                emit_telemetry("tool.called", "resolution_agent", msg.query_id, {
                    "tool_name": fc.name,
                    "duration_ms": tool_duration_ms,
                    "success": True,
                })
                tool_results.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            name=fc.name,
                            response=json.loads(result_str),
                        )
                    )
                )
            messages.append(types.Content(role="user", parts=tool_results))
            # 限制 messages 歷史長度，避免 token 成本隨迭代二次成長
            # 保留第 1 筆（用戶查詢）+ 最新 12 筆往返
            if len(messages) > 13:
                messages = [messages[0]] + messages[-12:]
            continue

        # 無 tool call → 最終回答
        text = "".join(p.text for p in content.parts if p.text is not None)
        logger.info(f"Resolution agent raw output ({len(text)} chars): {text}")

        answer, action = _parse_resolution(text)
        msg.tool_calls = tool_call_log
        msg.add_tokens(total_prompt_tokens, total_completion_tokens)

        if action == "escalate_to_human":
            msg.escalated = True
            return "queries.human", None

        if not answer:
            logger.warning(
                f"Resolution agent produced empty answer for query_id={msg.query_id}, "
                f"raw_text={text[:200]!r}"
            )
            msg.escalated = True
            return "queries.human", None

        emit_telemetry("resolution.completed", "resolution_agent", msg.query_id, {
            "retry_count": msg.retry_count,
            "token_usage": {
                "prompt_tokens": total_prompt_tokens,
                "completion_tokens": total_completion_tokens,
            },
        })
        return "responses.completed", answer

    logger.warning(f"Resolution agent exceeded iterations for query_id={msg.query_id}")
    msg.tool_calls = tool_call_log
    msg.add_tokens(total_prompt_tokens, total_completion_tokens)
    return "queries.human", None


_REACT_LINE_RE = re.compile(
    r'^\s*(thought|action|observation):', re.IGNORECASE
)


def _parse_resolution(text: str) -> tuple[str, str]:
    """解析 ANSWER / ACTION（雙模式）

    - 結構化模式（有 ANSWER: 標籤）：
        ANSWER: 標籤後開始收集，遇到 ACTION: 停止；
        ANSWER: 之前的 Thought/Action/prose 全部忽略。
    - 自由模式（無 ANSWER: 標籤）：
        保留所有非 ReAct 關鍵字行（大小寫不敏感過濾）。
    """
    action = "publish"
    answer_parts = []

    lines = text.split("\n")

    # 先掃一遍：判斷是否有結構化 ANSWER: 標籤
    has_answer_tag = any(
        line.startswith("ANSWER:") or re.match(r'^Action:\s*ANSWER:', line)
        for line in lines
    )

    in_answer = False  # 結構化模式：是否已進入 ANSWER: 區段

    for line in lines:
        # ACTION: 標記（大小寫不敏感，優先捕獲）
        if re.match(r'^\s*ACTION:\s*', line, re.IGNORECASE):
            val = re.sub(r'^\s*ACTION:\s*', '', line, flags=re.IGNORECASE).strip()
            if "escalate" in val or "human" in val:
                action = "escalate_to_human"
            in_answer = False  # ACTION: 後停止收集
            continue

        if has_answer_tag:
            # 結構化模式：遇到 ANSWER: 才開始收集
            m = re.match(r'^(?:Action:\s*)?ANSWER:\s*(.*)', line)
            if m:
                in_answer = True
                content = m.group(1).strip()
                if content:
                    answer_parts.append(content)
            elif in_answer and not _REACT_LINE_RE.match(line):
                # ANSWER: 之後的延續行（排除 ReAct 關鍵字行）
                answer_parts.append(line)
            # ANSWER: 之前 或 ReAct 關鍵字行 → 忽略
        else:
            # 自由模式：過濾所有 ReAct 關鍵字行（大小寫不敏感）
            if _REACT_LINE_RE.match(line):
                continue
            answer_parts.append(line)

    answer = "\n".join(answer_parts).strip()
    return answer, action


# ── Kafka 消費迴圈 ─────────────────────────────────────────────────────────
def handle_message(raw: str):
    start_time = time.time()
    msg = QueryMessage.from_json(raw)
    logger.info(f"Resolving query_id={msg.query_id} | '{msg.user_query[:50]}'")

    try:
        target_topic, answer = run_resolution_agent(msg)
    except (LLMRateLimitError, LLMTimeoutError) as e:
        # 可重試 → queries.retry（由 retry_consumer 執行退避）
        logger.warning(f"Resolution agent transient LLM error: {e}")
        msg.origin_topic = "queries.agent"
        msg.stage = "resolution"
        msg.retry_count += 1
        msg.error = str(e)
        target_topic = "queries.dlq" if msg.retry_count > MAX_AGENT_RETRIES else "queries.retry"
        answer = None
    except (LLMAuthError, LLMInvalidRequestError) as e:
        # 致命 → 直接 DLQ，不計入 retry_count
        logger.error(f"Resolution agent fatal LLM error: {e}", exc_info=True)
        msg.origin_topic = "queries.agent"
        msg.stage = "resolution"
        msg.error = str(e)
        target_topic = "queries.dlq"
        answer = None
    except Exception as e:
        # 未知錯誤 → 保持原有行為（retry_count++，retry or DLQ）
        logger.error(f"Resolution agent error: {e}", exc_info=True)
        msg.origin_topic = "queries.agent"
        msg.stage = "resolution"
        msg.retry_count += 1
        msg.error = str(e)
        target_topic = "queries.dlq" if msg.retry_count > MAX_AGENT_RETRIES else "queries.retry"
        answer = None

    if answer:
        msg.response = answer
    elif target_topic == "responses.completed":
        # 防護：answer 為空卻要 publish → 改為 escalate to human
        logger.warning(f"Empty answer on responses.completed, escalating query_id={msg.query_id}")
        target_topic = "queries.human"
        msg.escalated = True

    msg.latency_ms = round((time.time() - start_time) * 1000, 2)
    msg.routed_to = target_topic

    # emotion label（供 Output Layer 使用）
    if not msg.emotion:
        msg.emotion = emotion_label(msg.emotion_score)

    producer = get_producer()
    produce_message(producer, target_topic, msg.to_json(), key=msg.query_id)
    producer.flush()
    logger.info(
        f"Resolved query_id={msg.query_id} → {target_topic} "
        f"| latency={msg.latency_ms}ms | tokens={msg.token_usage.total_tokens} "
        f"| tools={[t['tool'] for t in msg.tool_calls]}"
    )


def main():
    time.sleep(12)  # 等待 Kafka + ChromaDB 就緒
    create_topics()

    consumer = get_consumer("resolution-agent-group", ["queries.agent"])
    logger.info("Resolution Agent started, listening on queries.agent ...")
    consume_loop(consumer, handle_message)


if __name__ == "__main__":
    main()
