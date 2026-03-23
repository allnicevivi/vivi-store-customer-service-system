"""
Redis Stack semantic cache for router classification results.
Uses RediSearch Flat index for COSINE distance lookup on query embeddings.
"""
import os
import json
import hashlib

import numpy as np
import redis
from redis.commands.search.field import VectorField, TextField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query
from shared.log_utils import get_logger

logger = get_logger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
INDEX_NAME = "router_cache_flat_cosine_idx"
KEY_PREFIX = "router_cache:"
VECTOR_DIM = 768            # gemini-embedding-001 output dimension
CACHE_TTL = 60 * 60 * 24 * 7   # 7 days
COSINE_DIST_THRESHOLD = 0.08    # Cosine distance 門檻（= 1 - similarity，0.08 → similarity ≥ 0.92）


_pool = redis.ConnectionPool(
    host=REDIS_HOST, port=REDIS_PORT,
    decode_responses=False, max_connections=50
)

_INCR_WITH_TTL = None  # Lua script（lazy init）

def _get_client() -> redis.Redis:
    return redis.Redis(connection_pool=_pool)


def _get_incr_script(r: redis.Redis):
    """Lazy-load Lua script：原子 INCR + EXPIRE，避免 TOCTOU race condition。"""
    global _INCR_WITH_TTL
    if _INCR_WITH_TTL is None:
        _INCR_WITH_TTL = r.register_script(
            """
            local count = redis.call('INCR', KEYS[1])
            if count == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1])) end
            return count
            """
        )
    return _INCR_WITH_TTL


def _ensure_index(r: redis.Redis):
    """建立 Flat 向量索引（Cosine metric，若已存在則跳過）。"""
    try:
        r.ft(INDEX_NAME).info()
    except Exception:
        schema = [
            TextField("$.query", as_name="query"),
            VectorField(
                "$.embedding", "FLAT", {
                    "TYPE": "FLOAT32",
                    "DIM": VECTOR_DIM,
                    "DISTANCE_METRIC": "COSINE",
                },
                as_name="embedding"
            ),
        ]
        r.ft(INDEX_NAME).create_index(
            schema,
            definition=IndexDefinition(prefix=[KEY_PREFIX], index_type=IndexType.JSON),
        )
        logger.info(f"Created Redis Flat Cosine index: {INDEX_NAME}")


def _embedding_to_bytes(embedding: list) -> bytes:
    return np.array(embedding, dtype=np.float32).tobytes()


def lookup(query: str, embedding: list) -> dict | None:
    """
    向量搜尋最相似歷史查詢。
    回傳快取的分類結果 dict，或 None（未命中）。
    """
    r = _get_client()
    _ensure_index(r)

    q = (
        Query("*=>[KNN 1 @embedding $vec AS score]")
        .sort_by("score")
        .return_fields("score", "$.result")
        .dialect(2)
    )
    params = {"vec": _embedding_to_bytes(embedding)}
    results = r.ft(INDEX_NAME).search(q, query_params=params)
    logger.info(f'redis lookup: {results}')

    if not results.docs:
        return None

    doc = results.docs[0]
    cosine_distance = float(doc.score)          # Redis Cosine distance: 0=identical, 2=opposite

    if cosine_distance <= COSINE_DIST_THRESHOLD:
        logger.info(f"Redis cache hit (cosine_distance={cosine_distance:.4f})")
        result_json = getattr(doc, "$.result", None)
        if result_json is None:
            return None
        return json.loads(result_json)
    return None


def store(query: str, embedding: list, result: dict):
    """將查詢 embedding 與分類結果寫入 Redis，設定 TTL。"""
    r = _get_client()
    _ensure_index(r)

    doc_id = hashlib.md5(query.encode()).hexdigest()
    key = f"{KEY_PREFIX}{doc_id}"

    r.json().set(key, "$", {
        "query": query,
        "embedding": embedding,
        "result": result,
    })
    r.expire(key, CACHE_TTL)
    logger.info(f"Redis cache stored for query hash={doc_id}")


# ── LLM JSON parse failure negative cache ────────────────────────────────────
NEG_CACHE_PREFIX = "router_neg_cache:"
NEG_CACHE_TTL = 3600  # 1 小時後自動清除（避免永久封鎖）


def increment_llm_failure(query: str) -> int:
    """LLM 對此 query 解析失敗，計數 +1。回傳目前累計次數。"""
    r = _get_client()
    key = f"{NEG_CACHE_PREFIX}{hashlib.md5(query.encode()).hexdigest()}"
    script = _get_incr_script(r)
    return int(script(keys=[key], args=[NEG_CACHE_TTL]))


def get_llm_failure_count(query: str) -> int:
    """取得目前失敗次數（key 不存在時回傳 0）。"""
    r = _get_client()
    key = f"{NEG_CACHE_PREFIX}{hashlib.md5(query.encode()).hexdigest()}"
    val = r.get(key)
    return int(val) if val else 0


def clear_llm_failure(query: str):
    """解析成功後清除失敗計數。"""
    r = _get_client()
    key = f"{NEG_CACHE_PREFIX}{hashlib.md5(query.encode()).hexdigest()}"
    r.delete(key)
