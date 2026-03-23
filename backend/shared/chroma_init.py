"""
ChromaDB 初始化腳本
將 ecommerce_faq.md 切段後 embedding 存入 ChromaDB
使用 gemini-embedding-001 模型
"""
import os
import re
import chromadb
from chromadb import EmbeddingFunction, Documents, Embeddings
from dotenv import load_dotenv
load_dotenv()

from llm.gemini_client import GeminiClient
from shared.log_utils import get_logger

logger = get_logger(__name__)

_gemini = GeminiClient()

CHROMA_HOST = os.getenv("CHROMA_HOST", "chromadb")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))
COLLECTION_NAME = "ecommerce_faq"
POLICY_COLLECTION_NAME = "ecommerce_policy"
FAQ_FILE = os.getenv("FAQ_FILE", "/app/knowledge_base/ecommerce_faq.md")
POLICY_FILE = os.getenv("POLICY_FILE", "/app/knowledge_base/ecommerce_policy.md")


class GoogleEmbeddingFunction(EmbeddingFunction):
    """
    ChromaDB 自訂 Embedding Function，使用 Gemini embedding-001。
    存文件與搜尋皆用最新模型與 SEMANTIC_SIMILARITY 設定。
    """

    def __init__(self, api_key: str = os.getenv("GEMINI_API_KEY", "")):
        self._gemini = GeminiClient(api_key=api_key)

    def __call__(self, input: Documents) -> Embeddings:
        """ChromaDB 在 collection.add() 時呼叫此方法（存文件用途）"""
        return self._gemini.embed_batch(list(input), task_type="RETRIEVAL_DOCUMENT")


def get_chroma_client() -> chromadb.HttpClient:
    return chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)


def get_collection(client: chromadb.HttpClient):
    """取得（或建立）FAQ collection，使用 gemini-embedding-001"""
    ef = GoogleEmbeddingFunction()
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine", "hnsw:ef_search": 100},
    )


def get_policy_collection(client: chromadb.HttpClient):
    """取得（或建立）電商規範 collection"""
    ef = GoogleEmbeddingFunction()
    return client.get_or_create_collection(
        name=POLICY_COLLECTION_NAME,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine", "hnsw:ef_search": 100},
    )


def parse_policy_chunks(filepath: str) -> list[dict]:
    """
    將電商規範 Markdown 文件解析成條款 chunks。
    每個 chunk 對應一個 ### T: 條款區塊，包含條款標題 + 內容。
    """
    chunks = []
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    sections = re.split(r"\n(?=### T:)", content)
    for i, section in enumerate(sections):
        section = section.strip()
        if not section.startswith("### T:"):
            continue
        lines = section.split("\n", 1)
        title = lines[0].replace("### T:", "").strip()
        body = lines[1].strip() if len(lines) > 1 else ""
        chunks.append({
            "id": f"policy_{i:03d}",
            "document": f"條款：{title}\n{body}",
            "metadata": {"title": title, "source": "ecommerce_policy.md"},
        })

    logger.info(f"Parsed {len(chunks)} policy chunks from {filepath}")
    return chunks


def parse_faq_chunks(filepath: str) -> list[dict]:
    """
    將 Markdown 文件解析成 Q&A chunks
    每個 chunk 包含 question + answer 作為 document text
    """
    chunks = []
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 切分 H3 問答區塊（### Q: ...）
    sections = re.split(r"\n(?=### Q:)", content)
    for i, section in enumerate(sections):
        section = section.strip()
        if not section.startswith("### Q:"):
            continue
        # 第一行是問題，其餘是回答
        lines = section.split("\n", 1)
        question = lines[0].replace("### Q:", "").strip()
        answer = lines[1].strip() if len(lines) > 1 else ""
        chunks.append({
            "id": f"faq_{i:03d}",
            "document": f"問題：{question}\n回答：{answer}",
            "metadata": {"question": question, "source": "ecommerce_faq.md"},
        })

    logger.info(f"Parsed {len(chunks)} FAQ chunks from {filepath}")
    return chunks


def init_chroma(force_reload: bool = False):
    """
    初始化 ChromaDB：
    - 若 collection 已有資料且 force_reload=False，跳過
    - 否則清空並重新 embed
    """
    client = get_chroma_client()
    collection = get_collection(client)

    existing_count = collection.count()
    if existing_count > 0 and not force_reload:
        logger.info(f"ChromaDB already has {existing_count} documents, skipping init.")
        return collection

    logger.info("Loading FAQ documents into ChromaDB...")
    chunks = parse_faq_chunks(FAQ_FILE)

    if force_reload and existing_count > 0:
        # 刪除所有現有文件
        all_ids = collection.get()["ids"]
        if all_ids:
            collection.delete(ids=all_ids)

    collection.add(
        ids=[c["id"] for c in chunks],
        documents=[c["document"] for c in chunks],
        metadatas=[c["metadata"] for c in chunks],
    )
    logger.info(f"Loaded {len(chunks)} FAQ documents into ChromaDB")
    return collection


def init_policy_chroma(force_reload: bool = False):
    """
    初始化電商規範 ChromaDB collection。
    - 若 collection 已有資料且 force_reload=False，跳過
    - 否則清空並重新 embed
    """
    client = get_chroma_client()
    collection = get_policy_collection(client)

    existing_count = collection.count()
    if existing_count > 0 and not force_reload:
        logger.info(f"Policy ChromaDB already has {existing_count} documents, skipping init.")
        return collection

    logger.info("Loading policy term documents into ChromaDB...")
    chunks = parse_policy_chunks(POLICY_FILE)

    if force_reload and existing_count > 0:
        all_ids = collection.get()["ids"]
        if all_ids:
            collection.delete(ids=all_ids)

    collection.add(
        ids=[c["id"] for c in chunks],
        documents=[c["document"] for c in chunks],
        metadatas=[c["metadata"] for c in chunks],
    )
    logger.info(f"Loaded {len(chunks)} policy documents into ChromaDB")
    return collection


def search_policy(query: str, n_results: int = 3, threshold: float = 0.0) -> list[dict]:
    """
    搜尋電商規範 collection，回傳 [{"title": ..., "document": ..., "score": ...}]
    """
    client = get_chroma_client()
    collection = get_policy_collection(client)

    query_embeddings = _gemini.embed_batch([query], task_type="RETRIEVAL_QUERY")

    results = collection.query(
        query_embeddings=query_embeddings,
        n_results=min(n_results, max(collection.count(), 1)),
        include=["documents", "metadatas", "distances"],
    )

    hits = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        score = 1.0 - dist
        if score >= threshold:
            hits.append({
                "title": meta.get("title", ""),
                "document": doc,
                "score": round(score, 4),
            })

    return hits


def search_faq(query: str, n_results: int = 3, threshold: float = 0.0) -> list[dict]:
    """
    搜尋 FAQ，回傳 [{"question": ..., "document": ..., "score": ...}]
    score 為相似度（1 - cosine distance），越高越相關

    query 使用 task_type=retrieval_query，與存文件的 retrieval_document 對應，
    可提升搜尋準確度。
    """
    client = get_chroma_client()
    collection = get_collection(client)

    # 手動 embed query（retrieval_query task type）
    query_embeddings = _gemini.embed_batch([query], task_type="RETRIEVAL_QUERY")

    results = collection.query(
        query_embeddings=query_embeddings,
        n_results=min(n_results, collection.count()),
        include=["documents", "metadatas", "distances"],
    )

    hits = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        score = 1.0 - dist  # cosine distance → similarity
        if score >= threshold:
            hits.append({
                "question": meta.get("question", ""),
                "document": doc,
                "score": round(score, 4),
            })

    return hits


if __name__ == "__main__":
    init_chroma(force_reload=True)
    init_policy_chroma(force_reload=True)
    print("ChromaDB initialized.")

    # 快速測試 FAQ
    results = search_faq("退貨流程怎麼申請", n_results=2)
    for r in results:
        print(f"[FAQ] Score: {r['score']:.3f} | Q: {r['question']}")

    # 快速測試 policy
    policy_results = search_policy("鑑賞期規定", n_results=2)
    for r in policy_results:
        print(f"[Policy] Score: {r['score']:.3f} | T: {r['title']}")
