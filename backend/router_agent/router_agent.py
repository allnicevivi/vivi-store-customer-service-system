"""
Router Agent — 核心路由 Agent（重構版）

設計原則：
1. 單次 LLM structured output（不用 Function Calling while loop）
   - 一次輸出：emotion_score、intent、needs_clarification、route

2. 情緒為 metadata（emotion_score 隨 message 傳遞給 Resolution Agent）
   - Resolution Agent 根據 emotion_score 調整回應語氣

3. needs_clarification = true → 直接回覆澄清問題，不進 Resolution Agent
   - 澄清問題送到 responses.completed（走正常回覆路徑）
   - 用戶帶 session_id 回覆後，下一輪才進 Resolution Agent

4. 路由只有兩種：
   - queries.agent：Resolution Agent 處理（含高情緒查詢）
   - queries.human：真正需要人工（帳號安全、法律糾紛等）
"""
import json
import os
import time

from shared.schema import QueryMessage, emotion_label
from shared.kafka_utils import get_producer, get_consumer, produce_message, consume_loop, create_topics
from shared.redis_semantic_cache import (
    lookup as cache_lookup, store as cache_store,
    increment_llm_failure, get_llm_failure_count, clear_llm_failure,
)
from shared.log_utils import get_logger
from shared.telemetry import emit_telemetry
from llm.gemini_client import GeminiClient, LLMRateLimitError, LLMTimeoutError, LLMAuthError, LLMError

logger = get_logger(__name__)

_gemini = GeminiClient()

# ── Fallback keyword classifier ──────────────────────────────────────────────
def _keyword_classify_query(query: str) -> dict:
    """關鍵字分類 fallback（無 API key 或 LLM 失敗時使用）。"""
    negative_keywords = [
        "騙人", "詐騙", "垃圾", "爛透了", "氣死", "氣炸", "投訴", "告你",
        "受夠了", "太過分", "不負責任", "沒良心", "黑心", "廢物", "無能",
        "怒", "憤怒", "失望", "崩潰", "絕望", "無語", "傻眼",
    ]
    mild_keywords = [
        "不滿", "問題", "為什麼", "怎麼回事", "搞什麼", "一直", "還沒",
        "等很久", "遲遲", "怎麼這麼", "難道", "竟然",
    ]
    emotion_score = 0.0
    query_lower = query.lower()
    for kw in negative_keywords:
        if kw in query_lower:
            emotion_score += 0.3
    for kw in mild_keywords:
        if kw in query_lower:
            emotion_score += 0.1
    emotion_score = min(emotion_score, 1.0)

    intent_keywords = {
        "order_inquiry":     ["訂單", "查詢", "訂購", "出貨", "出貨了嗎", "什麼時候到", "包裹"],
        "return_refund":     ["退貨", "退款", "換貨", "退", "換", "鑑賞期", "七天", "7天"],
        "logistics":         ["物流", "配送", "快遞", "運送", "遺失", "損壞", "沒收到"],
        "payment_issue":     ["付款", "付費", "付不成功", "刷卡", "繳費", "發票", "信用卡"],
        "account_issue":     ["密碼", "帳號", "登入", "會員", "註冊", "忘記", "積分"],
        "general_complaint": ["投訴", "申訴", "客訴", "不滿意", "要求賠償"],
    }
    best_intent = "other"
    best_confidence = 0.35
    for intent, keywords in intent_keywords.items():
        matches = sum(1 for kw in keywords if kw in query)
        if matches > 0:
            confidence = min(0.6 + matches * 0.15, 0.98)
            if confidence > best_confidence:
                best_confidence = confidence
                best_intent = intent

    return {
        "emotion_score": round(emotion_score, 2),
        "intent": best_intent,
        "needs_clarification": False,  # 關鍵字模式不生成澄清問題
        "clarification_question": "",
        "route": "agent",
        "route_reason": f"關鍵字分類：intent={best_intent}，信心度={round(best_confidence, 2)}",
    }


# ── LLM structured output schema ────────────────────────────────────────────
_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "emotion_score":         {"type": "number"},
        "intent":                {"type": "string"},
        "needs_clarification":   {"type": "boolean"},
        "clarification_question": {"type": "string"},
        "route":                 {"type": "string"},
        "route_reason":          {"type": "string"},
    },
    "required": ["emotion_score", "intent", "needs_clarification", "route", "route_reason"],
}

_CLASSIFY_PROMPT_TEMPLATE = """你是電商客服智能工單路由系統。分析客戶問題，一次輸出所有分類結果。

## 意圖定義
- order_inquiry：訂單狀態、出貨時間、取消或修改訂單
- return_refund：退貨、換貨、退款、鑑賞期
- logistics：物流配送問題、包裹遺失、損壞、改地址
- payment_issue：付款失敗、付款方式、發票
- account_issue：帳號、密碼、會員、積分
- general_complaint：對服務或態度的投訴
- other：無法歸類

## 情緒評分（emotion_score 0.0–1.0）
- 0.9-1.0：極度憤怒，謾罵、威脅提告
- 0.7-0.9：明顯激動，強烈抱怨、語氣失控
- 0.5-0.7：中度不滿，輕微抱怨但仍理性
- 0.3-0.5：輕微不耐，委婉不滿
- 0.0-0.3：語氣平和，中性詢問

## 路由規則
- route = "agent"：意圖明確或可以嘗試回答，交給 AI 處理（即使情緒高也路由到 agent，由 AI 以同理語氣回應）
- route = "human"：需要真人介入的情況，例如：帳號被盜、疑似詐騙、法律糾紛、客戶明確要求真人

## 澄清問題
- needs_clarification = true：問題極度模糊，完全無法判斷任何意圖（如只說「我要幫助」「有問題」「怎麼辦」）
- 一般情況即使意圖不完全確定，也設 false，由 AI Agent 在對話中詢問細節
- 若 needs_clarification = true，必須提供 clarification_question（友善地詢問更多資訊）{history_section}

<user_input>
{query}
</user_input>

只回傳 JSON，不要其他文字。"""


# ── Parse failure tracking / negative cache ──────────────────────────────────
_MAX_JSON_FAILURES = 3


def _llm_classify(query: str, conversation_history: list = None, query_id: str = "") -> tuple[dict, int, int]:
    """
    單次 LLM 呼叫，取得完整分類結果。

    Returns:
        (classification_dict, prompt_tokens, completion_tokens)
    """
    history_section = ""
    if conversation_history:
        recent = conversation_history[-2:]
        history_section = "\n\n近期對話紀錄：\n" + "\n".join(
            f"  [{t['role']}]: {t['content']}" for t in recent
        )

    prompt = _CLASSIFY_PROMPT_TEMPLATE.format(
        query=query,
        history_section=history_section,
    )
    resp = _gemini.generate(prompt, response_schema=_CLASSIFY_SCHEMA, query_id=query_id)
    text = resp.text.strip().strip("```json").strip("```").strip()
    logger.info(f"\n[Router LLM classify]\n{text}\n")

    prompt_tokens = 0
    completion_tokens = 0
    if resp.usage_metadata:
        prompt_tokens = resp.usage_metadata.prompt_token_count or 0
        completion_tokens = resp.usage_metadata.candidates_token_count or 0

    return json.loads(text), prompt_tokens, completion_tokens


# ── Core routing logic ───────────────────────────────────────────────────────
def run_router_agent(msg: QueryMessage) -> tuple[str, str]:
    """
    單次 LLM 分類路由。

    Returns:
        (target_topic, routing_reason)
    Side effects: sets msg.emotion_score, msg.intent, msg.emotion
    """
    emit_telemetry("routing.started", "router_agent", msg.query_id, {})

    if not _gemini.has_key:
        logger.warning("No GEMINI_API_KEY, using fallback routing")
        return _fallback_routing(msg)

    # ── Step 1: Redis semantic cache lookup ──
    embedding = None
    cached = {}
    try:
        embedding = _gemini.embed(msg.user_query, task_type="SEMANTIC_SIMILARITY")
        msg.token_usage.embedding_calls += 1
        hit = cache_lookup(msg.user_query, embedding)
        if hit:
            cached = hit
    except Exception as e:
        logger.warning(f"Redis cache lookup failed: {e}")

    # ── Step 2: LLM classify（快取命中則跳過）──
    classification = None
    prompt_tokens, completion_tokens = 0, 0
    from_cache = False

    if cached and "route" in cached:
        classification = cached
        from_cache = True
        logger.info(f"Router cache hit for query_id={msg.query_id}")
    else:
        # Negative cache check（Redis 跨重啟/多實例共享，TTL 1 小時）
        try:
            if get_llm_failure_count(msg.user_query) >= _MAX_JSON_FAILURES:
                logger.warning("Negative cache: skipping LLM for this query")
                return _fallback_routing(msg)
        except Exception as e:
            logger.warning(f"Redis negative cache check failed: {e}")

        for attempt in range(2):
            try:
                classification, prompt_tokens, completion_tokens = _llm_classify(
                    msg.user_query, msg.conversation_history, query_id=msg.query_id
                )
                try:
                    clear_llm_failure(msg.user_query)
                except Exception:
                    pass
                break
            except json.JSONDecodeError as e:
                logger.warning(f"[Router JSON parse error] attempt={attempt + 1}: {e}")
                if attempt == 1:
                    try:
                        count = increment_llm_failure(msg.user_query)
                    except Exception as redis_err:
                        logger.warning(f"Redis negative cache write failed: {redis_err}")
                        count = 1
                    logger.error(f"[Router JSON permanent fail] consecutive_failures={count}")
                    return _fallback_routing(msg)
            except Exception as e:
                logger.warning(f"LLM classify failed: {e}, falling back to keywords")
                return _fallback_routing(msg)

    if classification is None:
        return _fallback_routing(msg)

    # ── Step 3: Write cache（不快取 clarification_question，因為是 query-specific）──
    if not from_cache and embedding is not None and not os.getenv("EVAL_MODE"):
        try:
            cacheable = {k: v for k, v in classification.items() if k != "clarification_question"}
            cache_store(msg.user_query, embedding, cacheable)
        except Exception as e:
            logger.warning(f"Redis cache store failed: {e}")

    # ── Step 4: Set message metadata ──
    msg.emotion_score = float(classification.get("emotion_score", 0.0))
    msg.intent = classification.get("intent", "other")
    msg.emotion = emotion_label(msg.emotion_score)
    msg.add_tokens(prompt_tokens, completion_tokens)

    needs_clarification = classification.get("needs_clarification", False)
    route_reason = classification.get("route_reason", "LLM 路由決策")

    emit_telemetry("routing.decided", "router_agent", msg.query_id, {
        "intent": msg.intent,
        "emotion_score": msg.emotion_score,
        "source": "cache" if from_cache else "llm",
        "needs_clarification": needs_clarification,
        "route": classification.get("route"),
        "token_usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    })

    # ── Step 5: Determine target topic ──
    if needs_clarification:
        msg.response = classification.get(
            "clarification_question",
            "您好！請問您能描述得更詳細一些嗎？這樣我才能更好地協助您。"
        )
        return "responses.completed", "問題不明確，回覆澄清問題"

    route = classification.get("route", "agent")
    target_topic = "queries.agent" if route == "agent" else "queries.human"
    return target_topic, route_reason


def _fallback_routing(msg: QueryMessage) -> tuple[str, str]:
    """無 API Key 或 LLM 持續失敗時的降級路由。"""
    result = _keyword_classify_query(msg.user_query)
    msg.emotion_score = result["emotion_score"]
    msg.intent = result["intent"]
    msg.emotion = emotion_label(msg.emotion_score)

    if result.get("needs_clarification"):
        msg.response = "您好！請問您能描述得更詳細一些嗎？這樣我才能更好地協助您。"
        return "responses.completed", "問題不明確，回覆澄清問題"

    route = result.get("route", "agent")
    target_topic = "queries.agent" if route == "agent" else "queries.human"
    return target_topic, result["route_reason"]


# ── Kafka 消費迴圈 ─────────────────────────────────────────────────────────
_CROSS_SESSION_KEYWORDS = ["昨天", "上次", "之前", "剛才", "前幾天", "前次", "之前問"]


def handle_message(raw: str):
    start_time = time.time()
    msg = QueryMessage.from_json(raw)
    logger.info(f"Routing query_id={msg.query_id} | '{msg.user_query[:50]}'")

    # 載入對話歷史（若有 session_id）
    if msg.session_id and not msg.conversation_history:
        try:
            from shared.session_store import load_recent_context, load_cross_session_context
            if any(kw in msg.user_query for kw in _CROSS_SESSION_KEYWORDS):
                msg.conversation_history = load_cross_session_context(msg.user_id, max_sessions=3)
                logger.info(f"Loaded cross-session context: {len(msg.conversation_history)} turns")
            else:
                msg.conversation_history = load_recent_context(msg.user_id, msg.session_id, max_turns=5)
                logger.info(f"Loaded session context: {len(msg.conversation_history)} turns")
        except Exception as e:
            logger.warning(f"Session context load failed: {e}")

    try:
        target_topic, reason = run_router_agent(msg)
    except (LLMRateLimitError, LLMTimeoutError) as e:
        logger.warning(f"Router LLM transient error, falling back to keyword routing: {e}")
        target_topic, reason = _fallback_routing(msg)
    except (LLMAuthError, LLMError, Exception) as e:
        logger.error(f"Router agent fatal error: {e}", exc_info=True)
        msg.error = str(e)
        msg.stage = "router"
        target_topic = "queries.dlq"
        reason = f"Router 失敗，進入 DLQ: {e}"

    msg.routed_to = target_topic
    msg.routing_reason = reason
    msg.latency_ms = round((time.time() - start_time) * 1000, 2)

    producer = get_producer()
    produce_message(producer, target_topic, msg.to_json(), key=msg.query_id)
    producer.flush()
    logger.info(
        f"Routed query_id={msg.query_id} → {target_topic} "
        f"| reason='{reason}' | latency={msg.latency_ms}ms "
        f"| tokens={msg.token_usage.total_tokens}"
    )


def main():
    time.sleep(10)  # 等待 Kafka + ChromaDB 就緒
    create_topics()

    consumer = get_consumer("router-agent-group", ["queries.incoming"])
    logger.info("Router Agent started, listening on queries.incoming ...")
    consume_loop(consumer, handle_message)


if __name__ == "__main__":
    main()
