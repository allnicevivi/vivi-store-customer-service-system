"""
Session Store — 跨 Session 對話連續性
使用 MongoDB sessions collection 儲存對話歷史
"""
from datetime import datetime, timezone
from typing import Optional

from shared.mongo_client import get_collection
from shared.log_utils import get_logger

logger = get_logger(__name__)

_MAX_TURNS_PER_SESSION = 20


def load_recent_context(user_id: str, session_id: str, max_turns: int = 5) -> list:
    """
    載入當前 session 最近 N 輪對話
    Returns: list of {"role": "user"|"assistant", "content": "..."}
    """
    col = get_collection("sessions")
    if col is None:
        return []
    try:
        doc = col.find_one({"session_id": session_id, "user_id": user_id})
        if not doc:
            return []
        turns = doc.get("turns", [])[-max_turns:]
        history = []
        for t in turns:
            history.append({"role": "user", "content": t.get("user_query", "")})
            if t.get("response"):
                history.append({"role": "assistant", "content": t["response"]})
        return history
    except Exception as e:
        logger.warning(f"load_recent_context failed: {e}")
        return []


def load_cross_session_context(user_id: str, max_sessions: int = 3) -> list:
    """
    跨 session 查詢最近 N 個 session 的最後一輪對話
    用於客戶說「昨天/上次/之前那個問題」
    Returns: list of {"role": ..., "content": ..., "session_id": ..., "timestamp": ...}
    """
    col = get_collection("sessions")
    if col is None:
        return []
    try:
        docs = list(
            col.find({"user_id": user_id})
            .sort("last_updated", -1)
            .limit(max_sessions)
        )
        history = []
        for doc in docs:
            turns = doc.get("turns", [])
            if turns:
                last = turns[-1]
                history.append({
                    "role": "user",
                    "content": last.get("user_query", ""),
                    "session_id": doc.get("session_id"),
                    "timestamp": last.get("timestamp", ""),
                })
                if last.get("response"):
                    history.append({
                        "role": "assistant",
                        "content": last["response"],
                        "session_id": doc.get("session_id"),
                        "timestamp": last.get("timestamp", ""),
                    })
        return history
    except Exception as e:
        logger.warning(f"load_cross_session_context failed: {e}")
        return []


def append_turn(
    session_id: str,
    user_id: str,
    query_id: str,
    user_query: str,
    response: Optional[str],
    intent: Optional[str],
    routed_to: Optional[str],
):
    """
    寫入一輪對話到 sessions collection（upsert + $push + $slice）
    唯一寫入點：response_delivery.py 完成 delivery 後呼叫
    """
    col = get_collection("sessions")
    if col is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    turn = {
        "query_id": query_id,
        "user_query": user_query,
        "response": response or "",
        "intent": intent,
        "routed_to": routed_to,
        "timestamp": now,
    }
    try:
        col.update_one(
            {"session_id": session_id, "user_id": user_id},
            {
                "$setOnInsert": {
                    "session_id": session_id,
                    "user_id": user_id,
                    "created_at": now,
                },
                "$set": {
                    "last_updated": now,
                    "last_query_id": query_id,
                },
                "$push": {
                    "turns": {
                        "$each": [turn],
                        "$slice": -_MAX_TURNS_PER_SESSION,
                    }
                },
            },
            upsert=True,
        )
        logger.info(f"Session turn appended: session_id={session_id} query_id={query_id}")
    except Exception as e:
        logger.warning(f"append_turn failed: {e}")
