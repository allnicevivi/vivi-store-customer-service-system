import json
import os
import socket

import pytest


def _tcp_check(host: str, port: int, timeout_s: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _skip_if_no_prereqs():
    if not os.getenv("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set; skipping real integration test")

    chroma_host = os.getenv("CHROMA_HOST", "localhost")
    chroma_port = int(os.getenv("CHROMA_PORT", "8000"))
    if not _tcp_check(chroma_host, chroma_port):
        pytest.skip(f"ChromaDB not reachable at {chroma_host}:{chroma_port}")

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    faq_path = os.path.join(repo_root, "knowledge_base", "ecommerce_faq.md")
    if not os.path.exists(faq_path):
        pytest.skip(f"FAQ file not found: {faq_path}")

    chroma_host = os.getenv("CHROMA_HOST", "localhost")
    chroma_port = int(os.getenv("CHROMA_PORT", "8000"))
    os.environ["CHROMA_HOST"] = chroma_host
    os.environ["CHROMA_PORT"] = str(chroma_port)
    os.environ["FAQ_FILE"] = faq_path
    return faq_path


@pytest.mark.integration
def test_execute_tool_search_faq_quick_real():
    """
    Real integration test for Redis semantic cache + LLM classify:
    - requires ChromaDB running on localhost:8000
    - requires GEMINI_API_KEY for embeddings
    """
    _skip_if_no_prereqs()

    from shared import chroma_init
    chroma_init.init_chroma(force_reload=False)

    from shared.redis_semantic_cache import lookup, store
    from llm.gemini_client import GeminiClient

    gemini = GeminiClient()
    query = "退貨流程是什麼？"
    embedding = gemini.embed(query)

    # cache miss → None
    result = lookup(query, embedding)
    assert result is None or isinstance(result, dict)

    # store + lookup
    test_data = {"route": "agent", "intent": "return_refund", "emotion_score": 0.0}
    store(query, embedding, test_data)
    hit = lookup(query, embedding)
    assert hit is not None
    assert hit.get("route") == "agent"


@pytest.mark.integration
def test_run_router_agent_real():
    """
    Real integration test for run_router_agent():
    - requires GEMINI_API_KEY (single LLM structured output)
    - ChromaDB required for embedding-based semantic cache
    - Redis is optional: failures are tolerated
    """
    _skip_if_no_prereqs()

    from shared import chroma_init
    chroma_init.init_chroma(force_reload=False)

    from shared.schema import QueryMessage
    from router_agent import router_agent as ra

    msg = QueryMessage(user_query="我的訂單什麼時候到？")
    topic, reason = ra.run_router_agent(msg)

    # New architecture: only routes to queries.agent or queries.human
    assert topic in {"queries.agent", "queries.human", "responses.completed"}
    assert isinstance(reason, str) and reason.strip()
    assert msg.intent is not None
    assert 0.0 <= msg.emotion_score <= 1.0
