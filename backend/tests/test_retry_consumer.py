"""
測試 consumers/retry_consumer.py
涵蓋：指數退避邏輯、DLQ 升級、重試 re-queue
"""
import sys
import json
from unittest.mock import MagicMock, patch

with patch.dict("sys.modules", {"shared.chroma_init": MagicMock()}):
    import consumers.retry_consumer as rc
sys.modules["retry_consumer"] = rc  # patch() requires module to be in sys.modules


class TestRetryHandleMessage:
    def test_within_retry_limit_no_origin_topic_goes_to_incoming(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="重試問題", retry_count=1, error="timeout")
        mock_producer = MagicMock()

        print(f"\n  INPUT : retry_count={msg.retry_count}  error={repr(msg.error)}")
        with patch("retry_consumer.get_producer", return_value=mock_producer):
            with patch("retry_consumer.time.sleep"):
                rc.handle_message(msg.to_json())

        topic = mock_producer.produce.call_args.kwargs["topic"]
        print(f"  OUTPUT: published to topic={repr(topic)}")
        assert topic == "queries.incoming"

    def test_exceed_max_retries_goes_to_dlq(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="一直失敗", retry_count=4, error="persistent error")
        mock_producer = MagicMock()

        print(f"\n  INPUT : retry_count={msg.retry_count} (> MAX_RETRIES=3)  error={repr(msg.error)}")
        with patch("retry_consumer.get_producer", return_value=mock_producer):
            rc.handle_message(msg.to_json())

        topic = mock_producer.produce.call_args.kwargs["topic"]
        print(f"  OUTPUT: escalated to topic={repr(topic)}")
        assert topic == "queries.dlq"

    def test_exactly_at_max_retries_no_origin_topic_goes_to_incoming(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="第三次重試", retry_count=3, error="err")
        mock_producer = MagicMock()

        print(f"\n  INPUT : retry_count={msg.retry_count} (== MAX_RETRIES=3, 仍允許)")
        with patch("retry_consumer.get_producer", return_value=mock_producer):
            with patch("retry_consumer.time.sleep"):
                rc.handle_message(msg.to_json())

        topic = mock_producer.produce.call_args.kwargs["topic"]
        print(f"  OUTPUT: published to topic={repr(topic)}")
        assert topic == "queries.incoming"

    def test_exponential_backoff_delay(self):
        from shared.schema import QueryMessage
        delays = []
        for retry_count in [1, 2, 3]:
            msg = QueryMessage(user_query="test", retry_count=retry_count)
            mock_producer = MagicMock()
            with patch("retry_consumer.get_producer", return_value=mock_producer):
                with patch("retry_consumer.time.sleep") as mock_sleep:
                    rc.handle_message(msg.to_json())
                    delays.append(mock_sleep.call_args.args[0])

        print(f"\n  retry_count=1 → delay={delays[0]}s")
        print(f"  retry_count=2 → delay={delays[1]}s")
        print(f"  retry_count=3 → delay={delays[2]}s")
        assert delays == [5, 10, 20]

    def test_retry_clears_error_and_response(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(
            user_query="重試",
            retry_count=1,
            error="previous error",
            response="old response",
            routed_to="queries.agent",
        )
        mock_producer = MagicMock()

        print(f"\n  INPUT : error={repr(msg.error)}  response={repr(msg.response)}  routed_to={repr(msg.routed_to)}")
        with patch("retry_consumer.get_producer", return_value=mock_producer):
            with patch("retry_consumer.time.sleep"):
                rc.handle_message(msg.to_json())

        sent = json.loads(mock_producer.produce.call_args.kwargs["value"].decode("utf-8"))
        print(f"  OUTPUT: error={sent['error']}  response={sent['response']}  routed_to={sent['routed_to']}")
        assert sent["error"] is None
        assert sent["response"] is None
        # routed_to 保留，供追蹤用；retry 目標由 origin_topic 決定，不是 routed_to

    def test_retry_preserves_query_id_and_content(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="保留的問題", user_id="C123", retry_count=2)
        original_id = msg.query_id
        mock_producer = MagicMock()

        print(f"\n  INPUT : query_id={repr(original_id[:8])}...  user_query={repr(msg.user_query)}")
        with patch("retry_consumer.get_producer", return_value=mock_producer):
            with patch("retry_consumer.time.sleep"):
                rc.handle_message(msg.to_json())

        sent = json.loads(mock_producer.produce.call_args.kwargs["value"].decode("utf-8"))
        print(f"  OUTPUT: query_id={repr(sent['query_id'][:8])}...  user_query={repr(sent['user_query'])}")
        assert sent["query_id"] == original_id
        assert sent["user_query"] == "保留的問題"
        assert sent["user_id"] == "C123"

    def test_with_origin_topic_routes_to_origin(self):
        """有 origin_topic 時應重發到該 topic，不走 queries.incoming"""
        from shared.schema import QueryMessage
        msg = QueryMessage(
            user_query="複雜問題",
            retry_count=1,
            error="resolution error",
            origin_topic="queries.agent",
        )
        mock_producer = MagicMock()

        print(f"\n  INPUT : origin_topic={repr(msg.origin_topic)}  retry_count={msg.retry_count}")
        with patch("retry_consumer.get_producer", return_value=mock_producer):
            with patch("retry_consumer.time.sleep"):
                rc.handle_message(msg.to_json())

        topic = mock_producer.produce.call_args.kwargs["topic"]
        print(f"  OUTPUT: published to topic={repr(topic)}")
        assert topic == "queries.agent"

    def test_without_origin_topic_fallback_to_incoming(self):
        """無 origin_topic 時 fallback 到 queries.incoming（向下相容）"""
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="舊格式訊息", retry_count=1, error="err")
        assert msg.origin_topic is None
        mock_producer = MagicMock()

        print(f"\n  INPUT : origin_topic=None（舊訊息格式）")
        with patch("retry_consumer.get_producer", return_value=mock_producer):
            with patch("retry_consumer.time.sleep"):
                rc.handle_message(msg.to_json())

        topic = mock_producer.produce.call_args.kwargs["topic"]
        print(f"  OUTPUT: fallback to topic={repr(topic)}")
        assert topic == "queries.incoming"
