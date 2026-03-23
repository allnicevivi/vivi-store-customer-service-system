"""
Handoff Context Builder — 人工升級品質提升
為人工客服提供完整的升級 context，避免重新理解問題
"""
from shared.schema import QueryMessage

_INTENT_ACTIONS = {
    "order_inquiry":    "查訂單系統（OMS）確認最新狀態",
    "return_refund":    "啟動退換貨流程，確認鑑賞期與商品狀態",
    "logistics":        "聯繫物流廠商確認包裹位置",
    "payment_issue":    "查財務系統確認付款狀態，必要時聯繫銀行",
    "account_issue":    "協助帳號驗證與資料修改",
    "general_complaint": "傾聽客訴，提供補償方案（折扣/退款/換貨）",
    "other":            "深入了解需求，必要時轉交對應部門",
}


def _get_priority(msg: QueryMessage) -> str:
    """
    P1：emotion_score >= 0.8 或 retry_count >= 2
    P2：emotion_score >= 0.5 或 escalated=True
    P3：其他
    """
    if msg.emotion_score >= 0.8 or msg.retry_count >= 2:
        return "P1"
    if msg.emotion_score >= 0.5 or msg.escalated:
        return "P2"
    return "P3"


def build_handoff_context(msg: QueryMessage) -> dict:
    """
    建立完整 handoff 包，供人工客服快速了解情況
    """
    priority = _get_priority(msg)
    intent = msg.intent or "unknown"

    # 情緒摘要
    emotion_summary = "語氣平和"
    if msg.emotion_score >= 0.8:
        emotion_summary = f"極度憤怒 (score={msg.emotion_score:.2f})"
    elif msg.emotion_score >= 0.5:
        emotion_summary = f"明顯不滿 (score={msg.emotion_score:.2f})"
    elif msg.emotion_score >= 0.3:
        emotion_summary = f"輕微不耐 (score={msg.emotion_score:.2f})"

    # 已嘗試工具
    tried_tools = [t.get("tool") for t in (msg.tool_calls or []) if t.get("tool")]

    # 對話歷史最近 5 輪
    recent_history = (msg.conversation_history or [])[-10:]  # 5 輪 = 10 條（user+assistant）

    # 建議動作
    suggested_action = _INTENT_ACTIONS.get(intent, _INTENT_ACTIONS["other"])

    return {
        "priority": priority,
        "user_id": msg.user_id,
        "query_id": msg.query_id,
        "session_id": msg.session_id,
        "user_query": msg.user_query,
        "intent": intent,
        "emotion_summary": emotion_summary,
        "emotion_score": msg.emotion_score,
        "routing_reason": msg.routing_reason,
        "retry_count": msg.retry_count,
        "tried_tools": tried_tools,
        "conversation_history": recent_history,
        "suggested_action": suggested_action,
        "timestamp": msg.timestamp,
    }
