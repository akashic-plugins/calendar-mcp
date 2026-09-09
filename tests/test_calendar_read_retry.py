from types import SimpleNamespace

import httplib2
import pytest
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest

from src import calendar_actions


@pytest.mark.parametrize("statuses", [(503, 200), (503, 503, 503), (401,)])
def test_calendar_read_retries_only_transient_upstream_failures(monkeypatch, statuses):
    calls = []

    class Http:
        def request(self, uri, method, **kwargs):
            status = statuses[len(calls)]
            calls.append(status)
            payload = b'{"items": []}' if status == 200 else b'{"error": {"message": "upstream failure"}}'
            return httplib2.Response({"status": str(status)}), payload

    import json
    request = HttpRequest(Http(), lambda response, content: json.loads(content), "https://example.com/events")
    request._sleep = lambda seconds: None
    service = SimpleNamespace(events=lambda: SimpleNamespace(list=lambda **kwargs: request))
    monkeypatch.setattr(calendar_actions, "_get_calendar_service", lambda credentials: service)
    if statuses[-1] == 200:
        assert calendar_actions.find_events(None).items == []
    else:
        with pytest.raises(HttpError) as failure:
            calendar_actions.find_events(None)
        assert failure.value.resp.status == statuses[-1]
    assert tuple(calls) == statuses
