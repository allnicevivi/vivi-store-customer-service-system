"""
測試 shared/kafka_utils.py
涵蓋：consume_loop 邏輯、produce_message、delivery_report
使用 Mock 取代實際 Kafka 連線
"""
import logging
from unittest.mock import MagicMock
from confluent_kafka import KafkaError
from shared.kafka_utils import consume_loop, produce_message, delivery_report


def _make_mock_message(value: str, error=None):
    msg = MagicMock()
    msg.value.return_value = value.encode("utf-8")
    msg.error.return_value = error
    return msg


class TestConsumeLoop:
    def test_processes_single_message(self):
        consumer = MagicMock()
        msg = _make_mock_message('{"user_query": "test"}')
        consumer.poll.return_value = msg
        received = []
        consume_loop(consumer, received.append, max_messages=1)
        print(f"\n  INPUT : one message '{{\"user_query\": \"test\"}}'")
        print(f"  OUTPUT: handler called with {received}")
        assert received == ['{"user_query": "test"}']
        consumer.commit.assert_called_once_with(asynchronous=True)
        consumer.close.assert_called_once()

    def test_skips_none_message(self):
        consumer = MagicMock()
        real_msg = _make_mock_message("hello")
        consumer.poll.side_effect = [None, None, real_msg]
        received = []
        consume_loop(consumer, received.append, max_messages=1)
        print(f"\n  INPUT : [None, None, 'hello']")
        print(f"  OUTPUT: handler called with {received}")
        assert received == ["hello"]

    def test_skips_partition_eof(self):
        consumer = MagicMock()
        eof_error = MagicMock()
        eof_error.code.return_value = KafkaError._PARTITION_EOF
        eof_msg = MagicMock()
        eof_msg.error.return_value = eof_error
        real_msg = _make_mock_message("data")
        consumer.poll.side_effect = [eof_msg, real_msg]
        received = []
        consume_loop(consumer, received.append, max_messages=1)
        print(f"\n  INPUT : [PARTITION_EOF, 'data']")
        print(f"  OUTPUT: handler skipped EOF, received={received}")
        assert received == ["data"]

    def test_handler_exception_does_not_stop_loop(self):
        consumer = MagicMock()
        consumer.poll.side_effect = [_make_mock_message("bad"), _make_mock_message("good")]
        call_log = []

        def flaky_handler(raw):
            call_log.append(raw)
            if raw == "bad":
                raise ValueError("parse error")

        # max_messages=1：等到第一筆成功訊息（"good"）後停止
        consume_loop(consumer, flaky_handler, max_messages=1)
        print(f"\n  INPUT : ['bad'(raises), 'good']")
        print(f"  OUTPUT: handler called {len(call_log)} times: {call_log}")
        assert call_log == ["bad", "good"]  # "bad" 不中斷迴圈，繼續處理 "good"
        assert consumer.commit.call_count == 1  # 只有 "good" 成功才 commit

    def test_processes_multiple_messages(self):
        consumer = MagicMock()
        consumer.poll.side_effect = [_make_mock_message(f"msg{i}") for i in range(3)]
        received = []
        consume_loop(consumer, received.append, max_messages=3)
        print(f"\n  INPUT : ['msg0', 'msg1', 'msg2']")
        print(f"  OUTPUT: {received}")
        assert received == ["msg0", "msg1", "msg2"]


class TestProduceMessage:
    def test_calls_produce_with_correct_args(self):
        producer = MagicMock()
        produce_message(producer, "queries.incoming", '{"key":"val"}', key="abc")
        call_kwargs = producer.produce.call_args.kwargs
        print(f"\n  INPUT : topic='queries.incoming'  value='{{\"key\":\"val\"}}'  key='abc'")
        print(f"  OUTPUT: producer.produce called with topic={repr(call_kwargs['topic'])}  key={repr(call_kwargs['key'])}")
        assert call_kwargs["topic"] == "queries.incoming"
        assert call_kwargs["value"] == b'{"key":"val"}'
        assert call_kwargs["key"] == b"abc"
        producer.poll.assert_called_once_with(0)

    def test_no_key(self):
        producer = MagicMock()
        produce_message(producer, "queries.faq", "data")
        call_kwargs = producer.produce.call_args.kwargs
        print(f"\n  INPUT : key=None")
        print(f"  OUTPUT: producer called with key={call_kwargs['key']}")
        assert call_kwargs["key"] is None


class TestDeliveryReport:
    def test_no_error(self):
        msg = MagicMock()
        msg.topic.return_value = "queries.incoming"
        msg.partition.return_value = 0
        msg.offset.return_value = 42
        print(f"\n  INPUT : err=None  topic='queries.incoming'  partition=0  offset=42")
        delivery_report(None, msg)  # 不應拋出例外
        print(f"  OUTPUT: no exception raised")

    def test_with_error(self, caplog):
        msg = MagicMock()
        msg.topic.return_value = "queries.incoming"
        print(f"\n  INPUT : err='some error'")
        with caplog.at_level(logging.ERROR, logger="kafka_utils"):
            delivery_report("some error", msg)
        print(f"  OUTPUT: logged error={repr(caplog.text[:80])}")
        assert "Delivery failed" in caplog.text
