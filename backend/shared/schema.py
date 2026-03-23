"""
統一訊息 Schema
所有在 Kafka topic 間流通的訊息都遵循此格式
"""
import uuid
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field, asdict
import json


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    embedding_calls: int = 0  # Embedding API 呼叫次數（獨立計費）

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class QueryMessage:
    user_query: str
    user_id: str = "anonymous"
    query_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    intent: Optional[str] = None
    emotion_score: float = 0.0
    routed_to: Optional[str] = None
    retry_count: int = 0
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: float = 0.0
    response: Optional[str] = None
    error: Optional[str] = None
    routing_reason: Optional[str] = None
    stage: Optional[str] = None          # 失敗發生的 stage，供 DLQ Handler 分類（router / resolution / consumer）
    origin_topic: Optional[str] = None   # 失敗時所在的 topic，供 retry 直接重發
    # responses.completed 擴充欄位（Output Layer 使用）
    tool_calls: list = field(default_factory=list)   # Agent 呼叫的工具紀錄
    escalated: bool = False                           # 是否升級人工
    emotion: str = ""                                 # 情緒標籤（human-readable）
    # 對話連續性
    session_id: Optional[str] = None
    conversation_history: list = field(default_factory=list)

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> "QueryMessage":
        d = json.loads(data)
        token_usage_data = d.pop("token_usage", {})
        d["token_usage"] = TokenUsage(**token_usage_data)
        return cls(**d)

    def add_tokens(self, prompt: int, completion: int):
        self.token_usage.prompt_tokens += prompt
        self.token_usage.completion_tokens += completion


@dataclass
class TelemetryEvent:
    event_type: str = ""       # routing.started / tool.called / routing.decided / ...
    query_id: str = ""         # = query_id
    source: str = ""           # router_agent / resolution_agent / faq_consumer / ...
    payload: dict = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> "TelemetryEvent":
        d = json.loads(data)
        return cls(**d)


def emotion_label(score: float) -> str:
    """Convert emotion_score (0.0–1.0) to human-readable label."""
    if score >= 0.7:
        return "negative"
    if score >= 0.4:
        return "mild"
    return "neutral"
