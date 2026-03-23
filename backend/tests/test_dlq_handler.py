"""
測試 consumers/dlq_handler.py
涵蓋：handle_message 寫入 DLQ log、記錄正確欄位
"""
import json
import logging
from unittest.mock import MagicMock, patch

with patch.dict("sys.modules", {"shared.chroma_init": MagicMock(), "shared.mongo_client": MagicMock()}):
    import consumers.dlq_handler as dh


class TestDLQHandleMessage:
    def test_writes_record_to_file(self, tmp_path):
        from shared.schema import QueryMessage
        msg = QueryMessage(
            user_query="無法處理的問題",
            user_id="CDLQ",
            retry_count=4,
            error="persistent API error",
        )
        log_file = str(tmp_path / "dlq_test.jsonl")

        print(f"\n  INPUT : query_id={repr(msg.query_id[:8])}...  retry_count={msg.retry_count}  error={repr(msg.error)}")
        with patch.object(dh, "DLQ_LOG_FILE", log_file):
            dh.handle_message(msg.to_json())

        with open(log_file, "r", encoding="utf-8") as f:
            record = json.loads(f.readline())
        print(f"  OUTPUT: {json.dumps({k: v for k, v in record.items() if k != 'dlq_received_at'}, ensure_ascii=False)}")

        assert record["query_id"] == msg.query_id
        assert record["user_id"] == "CDLQ"
        assert record["user_query"] == "無法處理的問題"
        assert record["retry_count"] == 4
        assert record["error"] == "persistent API error"

    def test_record_contains_dlq_timestamp(self, tmp_path):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="test")
        log_file = str(tmp_path / "dlq_ts.jsonl")

        with patch.object(dh, "DLQ_LOG_FILE", log_file):
            dh.handle_message(msg.to_json())

        with open(log_file) as f:
            record = json.loads(f.readline())
        print(f"\n  OUTPUT: dlq_received_at={repr(record['dlq_received_at'])}")
        assert "dlq_received_at" in record
        assert "+00:00" in record["dlq_received_at"]

    def test_appends_multiple_records(self, tmp_path):
        from shared.schema import QueryMessage
        log_file = str(tmp_path / "dlq_multi.jsonl")

        for i in range(3):
            msg = QueryMessage(user_query=f"問題 {i}", retry_count=4)
            with patch.object(dh, "DLQ_LOG_FILE", log_file):
                dh.handle_message(msg.to_json())

        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        queries = [json.loads(l)["user_query"] for l in lines]
        print(f"\n  INPUT : 3 messages")
        print(f"  OUTPUT: {len(lines)} records in file  queries={queries}")
        assert len(lines) == 3

    def test_logs_error_alert(self, tmp_path, caplog):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="告警測試", retry_count=5)
        log_file = str(tmp_path / "dlq_alert.jsonl")

        with patch.object(dh, "DLQ_LOG_FILE", log_file):
            with caplog.at_level(logging.ERROR):
                dh.handle_message(msg.to_json())

        has_alert = "DLQ ALERT" in caplog.text or "dlq" in caplog.text.lower()
        print(f"\n  OUTPUT: DLQ alert logged={has_alert}")
        assert has_alert

    def test_handles_none_error_field(self, tmp_path):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="沒有錯誤訊息", error=None)
        log_file = str(tmp_path / "dlq_none.jsonl")

        print(f"\n  INPUT : error=None")
        with patch.object(dh, "DLQ_LOG_FILE", log_file):
            dh.handle_message(msg.to_json())

        with open(log_file) as f:
            record = json.loads(f.readline())
        print(f"  OUTPUT: error={record['error']}")
        assert record["error"] is None
