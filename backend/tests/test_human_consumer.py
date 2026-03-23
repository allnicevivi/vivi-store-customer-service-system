"""
測試 consumers/human_consumer.py
涵蓋：create_ticket, handle_message
"""
import sys
import json
import re
from unittest.mock import MagicMock, patch

with patch.dict("sys.modules", {"shared.chroma_init": MagicMock()}):
    import consumers.human_consumer as hc
sys.modules["human_consumer"] = hc  # patch() requires module to be in sys.modules


class TestCreateTicket:
    def test_returns_ticket_id(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="我的問題很複雜", user_id="C123")
        ticket_id = hc.create_ticket(msg)
        print(f"\n  INPUT : user_id='C123'  user_query={repr(msg.user_query)}")
        print(f"  OUTPUT: ticket_id={repr(ticket_id)}")
        assert ticket_id.startswith("TKT-")

    def test_ticket_id_unique(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="問題")
        t1 = hc.create_ticket(msg)
        t2 = hc.create_ticket(msg)
        print(f"\n  ticket_1={repr(t1)}")
        print(f"  ticket_2={repr(t2)}")
        assert t1 != t2

    def test_ticket_id_format(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="問題")
        ticket_id = hc.create_ticket(msg)
        print(f"\n  INPUT : (any query)")
        print(f"  OUTPUT: ticket_id={repr(ticket_id)}  matches TKT-[A-F0-9]{{8}}: {bool(re.match(r'TKT-[A-F0-9]{8}$', ticket_id))}")
        assert re.match(r"TKT-[A-F0-9]{8}$", ticket_id)


class TestHandleMessage:
    def test_produces_to_responses_completed(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="需要人工處理的問題", user_id="C999")
        mock_producer = MagicMock()

        print(f"\n  INPUT : user_query={repr(msg.user_query)}")
        with patch("human_consumer.get_producer", return_value=mock_producer):
            hc.handle_message(msg.to_json())

        topic = mock_producer.produce.call_args.kwargs["topic"]
        print(f"  OUTPUT: published to topic={repr(topic)}")
        assert topic == "responses.completed"

    def test_response_contains_ticket_id(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="需要人工")
        mock_producer = MagicMock()

        with patch("human_consumer.get_producer", return_value=mock_producer):
            hc.handle_message(msg.to_json())

        sent = json.loads(mock_producer.produce.call_args.kwargs["value"].decode("utf-8"))
        print(f"\n  OUTPUT response (前100字): {repr(sent['response'][:100])}")
        assert "TKT-" in sent["response"]

    def test_response_contains_contact_info(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="問題")
        mock_producer = MagicMock()

        with patch("human_consumer.get_producer", return_value=mock_producer):
            hc.handle_message(msg.to_json())

        sent = json.loads(mock_producer.produce.call_args.kwargs["value"].decode("utf-8"))
        print(f"\n  OUTPUT: contains 0800={repr('0800' in sent['response'])}")
        assert "0800" in sent["response"]

    def test_latency_set(self):
        from shared.schema import QueryMessage
        msg = QueryMessage(user_query="問題")
        mock_producer = MagicMock()

        with patch("human_consumer.get_producer", return_value=mock_producer):
            hc.handle_message(msg.to_json())

        sent = json.loads(mock_producer.produce.call_args.kwargs["value"].decode("utf-8"))
        print(f"\n  OUTPUT: latency_ms={sent['latency_ms']}")
        assert sent["latency_ms"] >= 0
