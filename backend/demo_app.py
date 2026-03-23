"""
Demo UI — ViviStore 客服系統展示介面 + Knowledge Base API

本地執行：python demo_app.py
Docker 執行：SERVICE=demo_app.py，使用 KAFKA_BOOTSTRAP_SERVERS / MONGODB_URI 環境變數
"""
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

# ── Optional deps（未安裝時 graceful degradation）──────────────────────────────
try:
    from confluent_kafka import Producer as KafkaProducer
    KAFKA_AVAILABLE = True
except ImportError:
    KAFKA_AVAILABLE = False

try:
    from pymongo import MongoClient
    MONGO_AVAILABLE = True
except ImportError:
    MONGO_AVAILABLE = False

try:
    import redis as _redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

try:
    from shared.chroma_init import (
        get_chroma_client,
        get_collection,
        get_policy_collection,
        parse_faq_chunks,
        parse_policy_chunks,
        COLLECTION_NAME,
        POLICY_COLLECTION_NAME,
        FAQ_FILE,
        POLICY_FILE,
    )
    CHROMA_AVAILABLE = True
except ImportError:
    CHROMA_AVAILABLE = False

# ──────────────────────────────────────────────────────────────────────────────
_FRONTEND_DIR = str(Path(__file__).parent.parent / "frontend")
app = Flask(__name__, template_folder=_FRONTEND_DIR, static_folder=_FRONTEND_DIR, static_url_path="/static")
# 本地執行時為專案根目錄；Docker 內透過 COMPOSE_DIR 指向掛載的 compose 檔案目錄
PROJECT_DIR = Path(os.getenv("COMPOSE_DIR", str(Path(__file__).parent.parent)))
ENABLE_DOCKER_MANAGEMENT = os.getenv("ENABLE_DOCKER_MANAGEMENT", "false").lower() == "true"

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9094")
MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGODB_DB", "ecommerce")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
INCOMING_TOPIC = "queries.incoming"

DEMO_QUESTIONS = [
    "我的訂單什麼時候到？",
    "如何申請退換貨？",
    "我想取消剛剛下的訂單",
    "你們有提供哪些付款方式？",
    "我買錯東西了，很沮喪，可以幫我嗎？",
    "商品破損，我要求立即退款！！很生氣！",
    "一件衣服配一條牛仔褲的穿搭建議",   # complex / 需人工
    "你好你好你好",                        # 情緒 / 無意義
]

# ── Kafka Helper ──────────────────────────────────────────────────────────────

def _kafka_producer() -> None|KafkaProducer:
    if not KAFKA_AVAILABLE:
        return None
    try:
        return KafkaProducer({"bootstrap.servers": KAFKA_BOOTSTRAP,
                               "socket.timeout.ms": 5000})
    except Exception:
        return None


def send_to_kafka(query: str, user_id: str = "demo_user", session_id: str = None) -> tuple[str | None, str]:
    query_id = str(uuid.uuid4())
    sid = session_id or f"SES-{uuid.uuid4()}"
    msg = {
        "user_query": query,
        "user_id": user_id,
        "query_id": query_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "intent": None,
        "emotion_score": 0.0,
        "routed_to": None,
        "retry_count": 0,
        "token_usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "latency_ms": 0.0,
        "response": None,
        "error": None,
        "routing_reason": None,
        "stage": None,
        "origin_topic": None,
        "tool_calls": [],
        "escalated": False,
        "emotion": "",
        "session_id": sid,
        "conversation_history": [],
    }
    p = _kafka_producer()
    if p is None:
        return None, sid
    try:
        p.produce(INCOMING_TOPIC, key=query_id, value=json.dumps(msg, ensure_ascii=False).encode())
        p.flush(timeout=5)
        return query_id, sid
    except Exception:
        return None, sid



# ── Docker Helpers ────────────────────────────────────────────────────────────

def _run(cmd: list[str], check=False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=PROJECT_DIR, capture_output=True, text=True, check=check
    )


def get_service_status() -> list[dict]:
    result = _run(["docker", "compose", "ps", "--format", "json"])
    services = []
    if result.returncode != 0:
        return services
    for line in result.stdout.strip().splitlines():
        try:
            obj = json.loads(line)
            services.append({
                "name": obj.get("Service", obj.get("Name", "?")),
                "state": obj.get("State", "?"),
                "health": obj.get("Health", ""),
                "status": obj.get("Status", ""),
            })
        except Exception:
            pass
    return services

def _parse_mongo_host(uri: str) -> str:
    """Extract host:port from mongodb://host:port/db URI."""
    return uri.split("://", 1)[-1].split("/")[0]


def get_service_status_internal() -> list[dict]:
    """When Docker management is disabled (running inside container),
    check actual service connectivity to report health."""
    checks = [
        ("kafka",    os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9094")),
        ("chromadb", f"{os.getenv('CHROMA_HOST','localhost')}:{os.getenv('CHROMA_PORT','8000')}"),
        ("redis",    f"{os.getenv('REDIS_HOST','localhost')}:{os.getenv('REDIS_PORT','6379')}"),
        ("mongodb",  _parse_mongo_host(os.getenv("MONGODB_URI", "mongodb://localhost:27017"))),
    ]
    services = []
    for name, addr in checks:
        host, _, port_str = addr.rpartition(":")
        try:
            with socket.create_connection((host, int(port_str)), timeout=2):
                health, state = "healthy", "running"
        except Exception:
            health, state = "unhealthy", "exited"
        services.append({"name": name, "state": state, "health": health, "status": ""})
    return services


# ── Knowledge Base Helpers ────────────────────────────────────────────────────

def _seed(filepath: str, force_reload: bool = False) -> dict:
    """將 FAQ markdown 檔案 parse 後寫入 ChromaDB。"""
    client = get_chroma_client()
    collection = get_collection(client)

    existing_count = collection.count()
    if existing_count > 0 and not force_reload:
        return {"skipped": True, "existing_count": existing_count}

    chunks = parse_faq_chunks(filepath)

    if force_reload and existing_count > 0:
        all_ids = collection.get()["ids"]
        if all_ids:
            collection.delete(ids=all_ids)

    collection.add(
        ids=[c["id"] for c in chunks],
        documents=[c["document"] for c in chunks],
        metadatas=[c["metadata"] for c in chunks],
    )
    return {"loaded": len(chunks)}


def _seed_policy(filepath: str, force_reload: bool = False) -> dict:
    """將服務條款 markdown 檔案 parse 後寫入 ChromaDB policy collection。"""
    client = get_chroma_client()
    collection = get_policy_collection(client)

    existing_count = collection.count()
    if existing_count > 0 and not force_reload:
        return {"skipped": True, "existing_count": existing_count}

    chunks = parse_policy_chunks(filepath)

    if force_reload and existing_count > 0:
        all_ids = collection.get()["ids"]
        if all_ids:
            collection.delete(ids=all_ids)

    collection.add(
        ids=[c["id"] for c in chunks],
        documents=[c["document"] for c in chunks],
        metadatas=[c["metadata"] for c in chunks],
    )
    return {"loaded": len(chunks)}

# ── API Routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", demo_questions=DEMO_QUESTIONS)


@app.route("/api/status")
def api_status():
    if not ENABLE_DOCKER_MANAGEMENT:
        services = get_service_status_internal()
    else:
        services = get_service_status()
    return jsonify({"services": services})


@app.route("/api/query", methods=["POST"])
def api_query():
    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400
    session_id = data.get("session_id") or None
    user_id = data.get("user_id", "demo_user")
    query_id, sid = send_to_kafka(query, user_id=user_id, session_id=session_id)
    if query_id is None:
        return jsonify({"error": f"無法連接 Kafka（{KAFKA_BOOTSTRAP}）。請確認服務已啟動。"}), 503
    return jsonify({"query_id": query_id, "query": query, "session_id": sid})


@app.get("/api/stream/<query_id>")
def api_stream(query_id: str):
    def generate():
        if not REDIS_AVAILABLE:
            yield f"data: {json.dumps({'error': 'Redis 不可用'})}\n\n"
            return
        try:
            r = _redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
            ps = r.pubsub()
            ps.subscribe(f"response:{query_id}")
            deadline = time.time() + 60
            for message in ps.listen():
                if time.time() > deadline:
                    break
                if message["type"] == "message":
                    ps.unsubscribe()
                    yield f"data: {message['data']}\n\n"
                    return
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            return
        yield f"data: {json.dumps({'warning': '等待逾時（60s），系統可能仍在處理中。'})}\n\n"

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/history")
def api_history():
    if not MONGO_AVAILABLE:
        return jsonify({"records": []})
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        col = client[MONGO_DB]["responses"]
        docs = list(col.find({}, {"_id": 0}).sort("created_at", -1).limit(20))
        return jsonify({"records": docs})
    except Exception as e:
        return jsonify({"records": [], "error": str(e)})


@app.route("/api/logs")
def api_logs():
    """SSE stream — tail logs from all services."""
    if not ENABLE_DOCKER_MANAGEMENT:
        return jsonify({"error": "Docker management disabled in this deployment"}), 503
    service = request.args.get("service", "")
    cmd = ["docker", "compose", "logs", "--no-color", "--tail", "50", "-f"]
    if service:
        cmd.append(service)

    def generate():
        with subprocess.Popen(
            cmd, cwd=PROJECT_DIR, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1
        ) as proc:
            try:
                for line in proc.stdout:
                    yield f"data: {json.dumps(line.rstrip())}\n\n"
            except GeneratorExit:
                proc.terminate()

    return Response(stream_with_context(generate()),
                    content_type="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})

# ── Knowledge Base Routes (/kb/*) ─────────────────────────────────────────────

@app.get("/kb/status")
def kb_status():
    if not CHROMA_AVAILABLE:
        return jsonify({"error": "ChromaDB 模組不可用"}), 503
    client = get_chroma_client()
    faq_count = get_collection(client).count()
    policy_count = get_policy_collection(client).count()
    return jsonify({
        "faq": {"collection": COLLECTION_NAME, "document_count": faq_count},
        "policy": {"collection": POLICY_COLLECTION_NAME, "document_count": policy_count},
    })


@app.post("/kb/upload")
def kb_upload():
    """上傳 FAQ .md 並載入 ChromaDB（已有資料則跳過，需更新請用 /kb/reload）。"""
    if not CHROMA_AVAILABLE:
        return jsonify({"error": "ChromaDB 模組不可用"}), 503
    if "file" not in request.files:
        return jsonify({"error": "No file provided. Use multipart field 'file'."}), 400
    file = request.files["file"]
    if not file.filename.endswith(".md"):
        return jsonify({"error": "Only .md files are supported."}), 400
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name
    try:
        result = _seed(tmp_path, force_reload=False)
    finally:
        os.unlink(tmp_path)
    return jsonify(result)


@app.post("/kb/reload")
def kb_reload():
    """強制清空 FAQ collection 並重新載入。"""
    if not CHROMA_AVAILABLE:
        return jsonify({"error": "ChromaDB 模組不可用"}), 503
    if "file" not in request.files:
        return jsonify({"error": "No file provided. Use multipart field 'file'."}), 400
    file = request.files["file"]
    if not file.filename.endswith(".md"):
        return jsonify({"error": "Only .md files are supported."}), 400
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name
    try:
        result = _seed(tmp_path, force_reload=True)
    finally:
        os.unlink(tmp_path)
    return jsonify(result)


@app.get("/kb/policy/status")
def kb_policy_status():
    if not CHROMA_AVAILABLE:
        return jsonify({"error": "ChromaDB 模組不可用"}), 503
    client = get_chroma_client()
    count = get_policy_collection(client).count()
    return jsonify({"collection": POLICY_COLLECTION_NAME, "document_count": count})


@app.post("/kb/policy/upload")
def kb_policy_upload():
    """上傳服務條款 .md 並載入 ChromaDB（已有資料則跳過）。"""
    if not CHROMA_AVAILABLE:
        return jsonify({"error": "ChromaDB 模組不可用"}), 503
    if "file" not in request.files:
        return jsonify({"error": "No file provided. Use multipart field 'file'."}), 400
    file = request.files["file"]
    if not file.filename.endswith(".md"):
        return jsonify({"error": "Only .md files are supported."}), 400
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name
    try:
        result = _seed_policy(tmp_path, force_reload=False)
    finally:
        os.unlink(tmp_path)
    return jsonify(result)


@app.post("/kb/policy/reload")
def kb_policy_reload():
    """強制清空 policy collection 並重新載入。"""
    if not CHROMA_AVAILABLE:
        return jsonify({"error": "ChromaDB 模組不可用"}), 503
    if "file" not in request.files:
        return jsonify({"error": "No file provided. Use multipart field 'file'."}), 400
    file = request.files["file"]
    if not file.filename.endswith(".md"):
        return jsonify({"error": "Only .md files are supported."}), 400
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name
    try:
        result = _seed_policy(tmp_path, force_reload=True)
    finally:
        os.unlink(tmp_path)
    return jsonify(result)


@app.route("/api/llm-metrics")
def llm_metrics():
    """LLM 層級監控端點：最近 5 分鐘的呼叫統計（跨所有 Agent）。"""
    try:
        from pymongo import MongoClient as _MongoClient
        client = _MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
        col = client[MONGO_DB]["llm_metrics"]
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        cutoff_iso = cutoff.isoformat()

        pipeline = [
            {"$match": {"recorded_at": {"$gte": cutoff_iso}}},
            {"$group": {
                "_id": "$event",
                "count": {"$sum": 1},
                "avg_latency_ms": {"$avg": "$latency_ms"},
                "total_prompt_tokens": {"$sum": "$prompt_tokens"},
                "total_completion_tokens": {"$sum": "$completion_tokens"},
            }}
        ]
        results = {r["_id"]: r for r in col.aggregate(pipeline)}
        call_stats = results.get("call", {})
        error_stats = results.get("error", {})
        total_calls = call_stats.get("count", 0)
        total_errors = error_stats.get("count", 0)
        return jsonify({
            "window": "last_5m",
            "total_calls": total_calls,
            "total_errors": total_errors,
            "error_rate": round(total_errors / total_calls, 4) if total_calls else 0.0,
            "avg_latency_ms": round(call_stats.get("avg_latency_ms") or 0, 1),
            "total_tokens": (call_stats.get("total_prompt_tokens", 0) or 0) +
                            (call_stats.get("total_completion_tokens", 0) or 0),
        })
    except Exception as e:
        return jsonify({"error": str(e), "available": False}), 503


@app.route("/api/analytics/faq-gaps")
def analytics_faq_gaps():
    """最常被升級的 intent（top 10，含 sample queries）"""
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB 不可用"}), 503
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        col = client[MONGO_DB]["analytics_events"]
        pipeline = [
            {"$match": {"escalated": True}},
            {"$group": {
                "_id": "$intent",
                "count": {"$sum": 1},
                "sample_queries": {"$push": "$user_query"},
            }},
            {"$sort": {"count": -1}},
            {"$limit": 10},
            {"$project": {
                "intent": "$_id",
                "count": 1,
                "sample_queries": {"$slice": ["$sample_queries", 3]},
                "_id": 0,
            }},
        ]
        results = list(col.aggregate(pipeline))
        return jsonify({"faq_gaps": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route("/api/analytics/intent-distribution")
def analytics_intent_distribution():
    """各路由分佈統計"""
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB 不可用"}), 503
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        col = client[MONGO_DB]["analytics_events"]
        pipeline = [
            {"$group": {
                "_id": {"intent": "$intent", "routed_to": "$routed_to"},
                "count": {"$sum": 1},
            }},
            {"$sort": {"count": -1}},
        ]
        results = list(col.aggregate(pipeline))
        distribution = [
            {"intent": r["_id"].get("intent"), "routed_to": r["_id"].get("routed_to"), "count": r["count"]}
            for r in results
        ]
        return jsonify({"distribution": distribution})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route("/api/analytics/escalation-rate")
def analytics_escalation_rate():
    """每日升級率"""
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB 不可用"}), 503
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        col = client[MONGO_DB]["analytics_events"]
        pipeline = [
            {"$group": {
                "_id": {"$substr": ["$timestamp", 0, 10]},
                "total": {"$sum": 1},
                "escalated": {"$sum": {"$cond": ["$escalated", 1, 0]}},
            }},
            {"$sort": {"_id": -1}},
            {"$limit": 30},
            {"$project": {
                "date": "$_id",
                "total": 1,
                "escalated": 1,
                "rate": {"$cond": [
                    {"$gt": ["$total", 0]},
                    {"$divide": ["$escalated", "$total"]},
                    0,
                ]},
                "_id": 0,
            }},
        ]
        results = list(col.aggregate(pipeline))
        return jsonify({"escalation_rate": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route("/api/usage")
def api_usage():
    """查詢成本用量統計（from cost_records collection）"""
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB 不可用"}), 503
    try:
        now = datetime.now(timezone.utc)
        default_from = now - timedelta(hours=24)
        default_to = now

        from_str = request.args.get("from", default_from.isoformat())
        to_str = request.args.get("to", default_to.isoformat())

        def _parse_dt(s):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))

        from_dt = _parse_dt(from_str) if isinstance(from_str, str) else default_from
        to_dt   = _parse_dt(to_str)   if isinstance(to_str,   str) else default_to

        match_stage = {"$match": {
            "recorded_at": {"$gte": from_dt, "$lte": to_dt}
        }}

        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        col = client[MONGO_DB]["cost_records"]

        # 整體統計
        total_pipeline = [
            match_stage,
            {"$group": {
                "_id": None,
                "queries": {"$sum": 1},
                "sessions": {"$addToSet": "$session_id"},
                "prompt_tokens": {"$sum": "$token_usage.prompt"},
                "completion_tokens": {"$sum": "$token_usage.completion"},
                "embedding_calls": {"$sum": "$token_usage.embedding_calls"},
                "cost_usd": {"$sum": "$estimated_cost_usd"},
                "avg_latency_ms": {"$avg": "$latency_ms"},
            }},
        ]
        total_results = list(col.aggregate(total_pipeline))
        if total_results:
            r = total_results[0]
            total = {
                "queries": r["queries"],
                "sessions": len([s for s in r["sessions"] if s]),
                "prompt_tokens": r["prompt_tokens"],
                "completion_tokens": r["completion_tokens"],
                "embedding_calls": r["embedding_calls"],
                "cost_usd": round(r["cost_usd"], 6),
                "avg_latency_ms": round(r["avg_latency_ms"] or 0, 1),
            }
        else:
            total = {"queries": 0, "sessions": 0, "prompt_tokens": 0,
                     "completion_tokens": 0, "embedding_calls": 0, "cost_usd": 0.0, "avg_latency_ms": 0.0}

        # 按 route 分組
        by_route_pipeline = [
            match_stage,
            {"$group": {
                "_id": "$routed_to",
                "queries": {"$sum": 1},
                "cost_usd": {"$sum": "$estimated_cost_usd"},
                "avg_latency_ms": {"$avg": "$latency_ms"},
            }},
            {"$sort": {"cost_usd": -1}},
            {"$project": {
                "route": "$_id",
                "queries": 1,
                "cost_usd": {"$round": ["$cost_usd", 6]},
                "avg_latency_ms": {"$round": ["$avg_latency_ms", 1]},
                "_id": 0,
            }},
        ]
        by_route = list(col.aggregate(by_route_pipeline))

        # per-session 平均
        per_session_pipeline = [
            match_stage,
            {"$group": {
                "_id": "$session_id",
                "queries": {"$sum": 1},
                "cost_usd": {"$sum": "$estimated_cost_usd"},
                "tokens": {"$sum": "$token_usage.total"},
            }},
            {"$group": {
                "_id": None,
                "avg_queries": {"$avg": "$queries"},
                "avg_cost_usd": {"$avg": "$cost_usd"},
                "avg_tokens": {"$avg": "$tokens"},
            }},
        ]
        per_session_results = list(col.aggregate(per_session_pipeline))
        if per_session_results:
            ps = per_session_results[0]
            per_session_avg = {
                "queries": round(ps["avg_queries"] or 0, 2),
                "cost_usd": round(ps["avg_cost_usd"] or 0, 6),
                "tokens": round(ps["avg_tokens"] or 0, 1),
            }
        else:
            per_session_avg = {"queries": 0.0, "cost_usd": 0.0, "tokens": 0.0}

        return jsonify({
            "period": {"from": from_str, "to": to_str},
            "total": total,
            "per_session_avg": per_session_avg,
            "by_route": by_route,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route("/api/tickets")
def api_tickets():
    """回傳依 priority 排序的開放工單"""
    if not MONGO_AVAILABLE:
        return jsonify({"tickets": []})
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        col = client[MONGO_DB]["handoff_tickets"]
        docs = list(col.find({"status": "open"}, {"_id": 0}).sort("priority", 1).limit(50))
        return jsonify({"tickets": docs})
    except Exception as e:
        return jsonify({"tickets": [], "error": str(e)})


@app.route("/api/eval/latest")
def api_eval_latest():
    """
    回傳最近一次 run_eval.py 的評估報告摘要。
    用途：確認每次優化後系統品質沒有退步（regression 驗證）。
    資料來源：eval_results/ 目錄下最新的 run_*.json 檔案。
    與 /api/analytics/* 的監控指標不同，此端點顯示離線評估結果。
    """
    import glob
    eval_dir = os.path.join(os.path.dirname(__file__), "../eval_results")
    pattern = os.path.join(eval_dir, "run_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    if not files:
        return jsonify({
            "available": False,
            "message": "尚無評估結果。請先執行：python backend/evaluation/run_eval.py",
        })
    try:
        with open(files[0], encoding="utf-8") as f:
            report = json.load(f)
        summary = report.get("summary", {})
        return jsonify({
            "available": True,
            "run_at": summary.get("run_at"),
            "total_cases": summary.get("total_cases"),
            "elapsed_seconds": summary.get("elapsed_seconds"),
            "layers_run": summary.get("layers_run"),
            "layer1": summary.get("layer1"),
            "layer2": summary.get("layer2"),
            "layer3": summary.get("layer3"),
            "report_file": os.path.basename(files[0]),
        })
    except Exception as e:
        return jsonify({"available": False, "error": str(e)})


if __name__ == "__main__":
    from shared.mongo_client import ensure_indexes
    ensure_indexes()

    # 啟動時自動 seed ChromaDB（若已有資料則跳過）
    if CHROMA_AVAILABLE:
        _seed(FAQ_FILE, force_reload=False)
        _seed_policy(POLICY_FILE, force_reload=False)

    port = int(os.environ.get("PORT", 8080))
    print(f"\n🚀 Demo UI 啟動中：http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
