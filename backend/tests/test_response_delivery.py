"""
測試 consumers/response_delivery.py
涵蓋：print_response, handle_message（MongoDB 寫入、graceful degradation）
"""
import pytest
from unittest.mock import MagicMock, patch, call

with patch.dict("sys.modules", {"shared.chroma_init": MagicMock(), "shared.mongo_client": MagicMock()}):
    import consumers.response_delivery as rd


def _make_msg(**kwargs):
    from shared.schema import QueryMessage, TokenUsage
    msg = QueryMessage(user_query="測試問題", **kwargs)
    return msg


class TestPrintResponse:
    def test_prints_query_id(self, capsys):
        msg = _make_msg(response="這是回覆", routed_to="responses.completed")
        rd.print_response(msg)
        captured = capsys.readouterr()
        print(f"\n  stdout contains query_id: {msg.query_id[:8] in captured.out}")
        assert msg.query_id in captured.out

    def test_prints_response_content(self, capsys):
        msg = _make_msg(response="保費可以用信用卡繳納。")
        rd.print_response(msg)
        captured = capsys.readouterr()
        assert "保費可以用信用卡繳納" in captured.out

    def test_none_response_shows_placeholder(self, capsys):
        msg = _make_msg(response=None)
        rd.print_response(msg)
        captured = capsys.readouterr()
        assert "無回覆" in captured.out


class TestHandleMessage:
    def test_writes_to_mongodb(self, mongo_col):
        msg = _make_msg(
            user_id="C001",
            response="您好，回覆如下",
            routed_to="responses.completed",
        )
        with patch.object(rd, "get_collection", return_value=mongo_col):
            rd.handle_message(msg.to_json())

        assert mongo_col.insert_one.called
        doc = mongo_col.insert_one.call_args[0][0]
        print(f"\n  MongoDB doc keys: {list(doc.keys())}")
        assert doc["query_id"] == msg.query_id
        assert doc["user_id"] == "C001"
        assert doc["response"] == "您好，回覆如下"
        assert "session_id" in doc
        assert doc["user_query"] == msg.user_query
        assert "created_at" in doc

    def test_graceful_degradation_no_mongo(self, capsys):
        """MongoDB 不可用時不應拋例外"""
        msg = _make_msg(response="回覆內容")
        with patch.object(rd, "get_collection", return_value=None):
            rd.handle_message(msg.to_json())  # 不應拋例外

    def test_escalated_field_written(self, mongo_col):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="複雜問題")
        msg.escalated = True
        with patch.object(rd, "get_collection", return_value=mongo_col):
            rd.handle_message(msg.to_json())

        doc = mongo_col.insert_one.call_args[0][0]
        assert doc["escalated"] is True

    def test_mongodb_exception_not_propagated(self, mongo_col):
        """MongoDB insert 拋例外時，consumer 仍正常完成（不中斷 consume_loop）"""
        mongo_col.insert_one.side_effect = Exception("DB timeout")
        msg = _make_msg(response="回覆")
        with patch.object(rd, "get_collection", return_value=mongo_col):
            rd.handle_message(msg.to_json())  # 不應拋例外
