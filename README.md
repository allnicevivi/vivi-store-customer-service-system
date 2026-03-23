# ViviStore Customer Service System

PCA Life 面試作業 — Agentic System with Kafka + Agent + GCP Gemini

## System Architecture

```
客戶輸入（Web UI / CLI producer）
    ↓
topic: queries.incoming
    ↓
[Router Agent] ← Gemini 單次結構化輸出
  輸出：emotion_score | intent | needs_clarification | route
    ↓
    ├─ queries.agent  → [Resolution Agent] ← ReAct 迭代（search_faq / get_policy_info / escalate_to_human / get_order_status / get_inventory_info / track_logistics）
    └─ queries.human  → human-consumer（建立工單）
              ↓
      responses.completed（fan-out，三個獨立 Consumer Group）
              ├─ response-delivery  → SSE 推播 + MongoDB 儲存
              ├─ telemetry-consumer → 完整 Telemetry Events → MongoDB
              └─ cost-tracker       → Token 計費 + Latency 統計
              └─ analytics-consumer → 分析事件追蹤

queries.retry → retry-consumer（指數退避 5s/10s/20s）→ queries.dlq → dlq-handler（告警記錄）
```

---

## Quick Start

### 1. Set .env variables

```bash
cp .env.example .env
# 編輯 .env，填入 GEMINI_API_KEY（必填）
# 取得：https://aistudio.google.com/app/apikey
```

### 2. Build docker services

```bash
docker-compose up --build
```

### 3. Open Demo UI

瀏覽器開啟 **http://localhost:8080**

介面提供：
- 即時對話 (SSE 推播 + MongoDB 儲存)
- 回覆 + Token 用量 + Latency 統計
- 知識庫管理（上傳 FAQ / Policy markdown）

---

## CLI Usage

```bash
# 訂單查詢（→ queries.agent → Resolution Agent）
docker-compose run --rm producer python /app/backend/producer/producer.py "我的訂單什麼時候會到？"

# FAQ 查詢
docker-compose run --rm producer python /app/backend/producer/producer.py "退貨需要幾天？"

# 情緒激動查詢（emotion_score 高 → 調整語氣回覆）
docker-compose run --rm producer python /app/backend/producer/producer.py "你們都是騙人的！訂單一直沒來！"

# 互動模式（逐行輸入）
docker-compose run --rm producer python /app/backend/producer/producer.py

# 停止 Resolution Agent，觀察 retry → dlq 流程
docker-compose stop resolution-agent
docker-compose run --rm producer python /app/backend/producer/producer.py "退款申請怎麼辦？"
```

---

## API Key-less Fallback Mode

不設定 `GEMINI_API_KEY` 系統仍可運行：
- **Router**：改用本地關鍵字規則引擎路由
- **Resolution**：直接以 ChromaDB 向量搜尋回答，不呼叫 Gemini

---

## Service Overview

### Infrastructure Services

| 服務 | Port | 說明 |
|------|------|------|
| kafka | 9092/9094 | KRaft 模式（無 Zookeeper），9094 供宿主機連線 |
| chromadb | 8000 | 向量資料庫（Gemini embedding） |
| redis | 6379 | 語意快取（路由結果 TTL 快取） |
| mongodb | 27017 | 完整訊息審計 + Telemetry 儲存 |

### Application Services

| 服務 | 消費 Topic | 說明 |
|------|-----------|------|
| demo-ui | — | Flask Web UI + 知識庫管理 API（port 8080） |
| router-agent | queries.incoming | 單次 LLM 結構化輸出決定路由，結果存入 Redis 快取 |
| resolution-agent | queries.agent | ReAct 迭代解決複雜問題，最多 3 次重試 |
| human-consumer | queries.human | 模擬建立 CRM 工單 |
| retry-consumer | queries.retry | 指數退避重試（5s/10s/20s） |
| dlq-handler | queries.dlq | 死信記錄 + 告警 |
| response-delivery | responses.completed | SSE 推播 + MongoDB 儲存 |
| telemetry-consumer | responses.completed | 完整 Telemetry Events 寫入 MongoDB |
| cost-tracker | responses.completed | Token 計費 + Latency 統計 |
| analytics-consumer | responses.completed | 分析事件追蹤 |

---

## Testing

```bash
# Unit tests（不需外部服務）
pytest

# Integration tests（需先啟動基礎設施）
docker-compose up kafka chromadb mongodb redis -d
pytest -m integration

# E2E test
pytest backend/tests/e2e_test.py
```

---

## Evaluation（離線品質評估）

對 80 筆 golden dataset 做三層自動評測，確認系統品質未退步。
**eval query 不會寫入正式 DB（MongoDB / Redis）**，由 `EVAL_MODE` 自動隔離。

### How to Run

```bash
# Docker 環境（推薦，需啟動 ChromaDB + 設定 GEMINI_API_KEY）
docker-compose run --rm producer python /app/backend/evaluation/run_eval.py

# 本地環境
PYTHONPATH=backend python backend/evaluation/run_eval.py

# 快速驗證路由邏輯（不呼叫 Gemini，僅跑 Layer 1）
PYTHONPATH=backend python backend/evaluation/run_eval.py --layer 1 --mock

# 開發期抽樣（前 10 筆）
PYTHONPATH=backend python backend/evaluation/run_eval.py --sample 10
```

### Parameter Description

| 參數 | 預設 | 說明 |
|------|------|------|
| `--layer {1,2,3,all}` | `all` | 只跑指定評估層 |
| `--mock` | false | Layer 1 改用關鍵字 fallback（不呼叫 Gemini） |
| `--sample N` | 全部 | 只跑前 N 筆（開發期快速測試） |
| `--dataset PATH` | `evaluation/golden_dataset.json` | 指定 golden dataset 路徑 |
| `--output PATH` | `eval_results/run_<timestamp>.json` | 指定 JSON 輸出路徑 |

### Evaluation Layer Descriptions

| 層 | 需要 | 評估內容 | 通過條件 |
|----|------|---------|---------|
| **Layer 1 — Routing** | — | Router Agent 路由 topic 是否正確、intent 分類是否正確 | route ✓ AND intent ✓ |
| **Layer 2 — Retrieval** | ChromaDB | FAQ / Policy 搜尋 top-3 是否涵蓋預期關鍵字 | recall@3 ≥ 0.5 |
| **Layer 3 — Response** | ChromaDB + Gemini | Resolution Agent 回覆品質（LLM-as-Judge，四維度各 1–5 分） | overall ≥ 3 |

### Output Files

每次執行產生兩個檔案（路徑：`eval_results/`）：

| 檔案 | 內容 |
|------|------|
| `run_<timestamp>.json` | 完整資料：`summary`（彙總指標）、`results`（逐筆）、`failures`（失敗清單）、`category_breakdown`（分類通過率） |
| `run_<timestamp>_report.md` | 人讀報告：Summary 表格、Category Breakdown、失敗案例一覽 |

### Interpreting Failed Results

`failures` 陣列只包含失敗案例，每筆有 `failed_layers` 欄位：

```json
{
  "id": "order_001",
  "query": "我的訂單什麼時候會到？",
  "category": "standard",
  "failed_layers": {
    "layer1": {
      "reason": "wrong_route",
      "actual_route": "queries.faq",
      "expected_route": "queries.agent"
    },
    "layer3": {
      "reason": "judge_score_low",
      "overall": 2,
      "critique": "回覆缺乏具體步驟"
    }
  }
}
```

| 層 | reason | 說明 |
|----|--------|------|
| layer1 | `wrong_route` | 路由到錯誤 topic |
| layer1 | `wrong_intent` | 意圖分類錯誤 |
| layer2 | `low_recall` | 搜尋未找到足夠關鍵字（`keywords_missing` 列出遺漏項） |
| layer3 | `judge_score_low` | LLM Judge overall < 3（`critique` 說明原因） |

### Adding New Test Cases

編輯 `backend/evaluation/golden_dataset.json`，格式如下：

```json
{
  "id": "唯一 ID",
  "category": "standard",
  "query": "測試問句",
  "expected_route": "queries.agent",
  "expected_intent": "order_inquiry",
  "expected_emotion_band": "neutral",
  "kb_answerable": true,
  "expected_answer_contains": ["關鍵字1", "關鍵字2"],
  "must_not_contain": ["無法回答", "不知道"],
  "tags": ["order"]
}
```

---

## Knowledge Base Management

知識庫以 Markdown 格式維護，上傳後自動 embedding 並存入 ChromaDB：

```bash
# 透過 Web UI：http://localhost:8080（KB 管理頁面）

# 或直接執行 reload 腳本（需 demo-ui 運行中）
./reload_kb.sh
```

**API Endpoints:**

| 端點 | 說明 |
|------|------|
| `POST /kb/reload` | 上傳 FAQ markdown，重新 embedding |
| `POST /kb/policy/reload` | 上傳 Policy markdown |
| `GET /kb/status` | 查詢知識庫狀態（文件數、embedding 模型） |
| `GET /kb/search?q=...` | 直接搜尋（不經過 Agent） |

---

## Environment Variables

複製 `.env.example` 為 `.env`，只需填入 `GEMINI_API_KEY`，其餘預設值可直接搭配 Docker Compose 使用：

| 變數 | 預設值（本機） | 說明 |
|------|--------|------|
| `GEMINI_API_KEY` | **（必填）** | [Google AI Studio](https://aistudio.google.com/app/apikey) API Key |
| `GEMINI_MODEL` | `gemini-2.0-flash` | 文字生成模型 |
| `GEMINI_EMBED_MODEL` | `gemini-embedding-001` | 向量嵌入模型 |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9094` | Kafka 連線位址（Docker 內：`kafka:9092`） |
| `CHROMA_HOST` | `localhost` | ChromaDB 主機（Docker 內：`chromadb`） |
| `CHROMA_PORT` | `8000` | ChromaDB 連接埠 |
| `REDIS_HOST` | `localhost` | Redis 主機（Docker 內：`redis`） |
| `REDIS_PORT` | `6379` | Redis 連接埠 |
| `MONGODB_URI` | `mongodb://localhost:27017` | MongoDB 連線字串（Docker 內：`mongodb://mongodb:27017`） |
| `MONGODB_DB` | `pcalife` | MongoDB 資料庫名稱 |
| `FAQ_FILE` | `./knowledge_base/ecommerce_faq.md` | FAQ 知識庫路徑 |
| `POLICY_FILE` | `./knowledge_base/ecommerce_policy.md` | 退換貨政策路徑 |
| `LOG_LEVEL` | `INFO` | 日誌等級（`DEBUG` / `INFO` / `WARNING` / `ERROR`） |
| `QUIET_THIRD_PARTY_LOGS` | `1` | 設 `0` 可顯示 kafka/httpx 等依賴套件 log |

---

## File Structure

```
├── docker-compose.yml          # 所有服務定義（12 個容器）
├── Dockerfile                  # 單一映像檔，SERVICE 環境變數決定啟動哪個服務
├── requirements.txt
├── pytest.ini
├── reload_kb.sh                # 知識庫 reload 工具腳本
├── .env.example                # 環境變數範本（填入後複製為 .env）
├── backend/
│   ├── producer/
│   │   └── producer.py        # CLI 查詢發送工具
│   ├── router_agent/
│   │   └── router_agent.py    # Router Agent（單次 LLM 結構化輸出）
│   ├── resolution_agent/
│   │   └── resolution_agent.py # Resolution Agent（ReAct 迭代）
│   ├── consumers/
│   │   ├── human_consumer.py  # 人工工單 consumer
│   │   ├── retry_consumer.py  # 指數退避重試
│   │   ├── dlq_handler.py     # 死信隊列告警
│   │   ├── response_delivery.py # SSE + MongoDB
│   │   ├── telemetry_consumer.py # Telemetry Events 審計
│   │   ├── cost_tracker.py    # Token 計費
│   │   └── analytics_consumer.py
│   ├── demo_app.py             # Flask Web UI + KB API（port 8080）
│   ├── shared/
│   │   ├── schema.py          # QueryMessage / TokenUsage / TelemetryEvent
│   │   ├── kafka_utils.py     # Producer/Consumer 工廠 + consume_loop
│   │   ├── chroma_init.py     # ChromaDB 初始化 + search_faq / search_policy
│   │   ├── redis_semantic_cache.py # 語意快取層
│   │   ├── mongo_client.py    # MongoDB 連線工廠
│   │   └── log_utils.py       # 結構化 logging
│   ├── llm/
│   │   └── gemini_client.py   # Gemini API 封裝
│   ├── mock_apis/             # 模擬外部服務（訂單、庫存、物流）
│   ├── knowledge_base/
│   │   ├── ecommerce_faq.md   # 電商 FAQ 知識庫
│   │   └── ecommerce_policy.md # 退換貨政策
│   └── tests/                 # 完整測試套件（unit + integration + e2e）
└── frontend/
    ├── index.html
    ├── app.js
    └── style.css
```

---

## Cost Control

- Gemini 2.0 Flash：$0.075/1M prompt tokens + $0.30/1M completion tokens
- Router Agent 僅單次 LLM 呼叫；Redis 快取命中時零 token
- Resolution Agent 最多迭代 3 次
- Human 路由不呼叫 Gemini（零 token）
- cost-tracker consumer 記錄每筆查詢的 token 消耗與費用估算
