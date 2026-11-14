import httpx
import pytest

from rightnow import RightNow, SystemZero, RightNowStreamClient
from rightnow._wire import _wire_for


REQUEST = {"model": "system-zero", "state": "ready", "questions": {"q": {"type": "boolean", "instructions": "Ready?"}}}


@pytest.mark.parametrize("name,path,model,header,code_prefix", [
    ("system-zero", "/system-zero", "system-zero", "sz-danger-zone", "system_zero"),
    ("rightnow-decision", "/decision-model", "rightnow-decision", "rightnow-danger-zone", "model"),
])
def test_current_wire(name, path, model, header, code_prefix):
    class Client(RightNow):
        _wire = _wire_for(name)

    def respond(request):
        assert str(request.url) == "https://api.rightnowai.co/v1" + path + "/decide"
        import json
        assert json.loads(request.content)["model"] == model
        assert request.headers[header] == "accept"
        assert ("rightnow-danger-zone" if name == "system-zero" else "sz-danger-zone") not in request.headers
        prefix = "system-zero" if name == "system-zero" else "rightnow"
        assert request.headers["user-agent"].startswith(prefix + "-python/0.5.1 python/")
        return httpx.Response(200, json={})

    with Client(api_key="test-key", transport=httpx.MockTransport(respond)) as client:
        client.decide({**REQUEST, "model": model}, danger_zone="accept")
        assert client._wire.model == model
        assert client._wire.codes.timeout == code_prefix + "_timeout"
        assert client._wire.codes.unavailable == code_prefix + "_unavailable"
        assert client._wire.codes.serving_unattested == code_prefix + "_serving_unattested"

    def timeout(request):
        raise httpx.ReadTimeout("timeout")

    with Client(api_key="test-key", max_retries=0, transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(Exception) as caught:
            client.decide(REQUEST)
        assert caught.value.code == code_prefix + "_timeout"


@pytest.mark.parametrize("Client", [RightNow, SystemZero])
def test_package_selects_wire(Client):
    with Client(api_key="test-key") as client:
        assert client._wire.name == "rightnow-decision"


@pytest.mark.parametrize("name", ["system-zero", "rightnow-decision"])
@pytest.mark.parametrize("header", ["SZ-Danger-Zone", "RightNow-Danger-Zone", "sZ-dAnGeR-zOnE", "rIgHtNoW-dAnGeR-zOnE"])
def test_guard(name, header):
    class Client(RightNow):
        _wire = _wire_for(name)
    with Client(api_key="test-key") as client:
        with pytest.raises(Exception, match="do not supply its header"):
            client.decide(REQUEST, headers={header: "accept"})


@pytest.mark.parametrize("name", ["system-zero", "rightnow-decision"])
def test_stream_and_webhook_routes(name):
    class Stream(RightNowStreamClient):
        _wire = _wire_for(name)
    assert Stream(api_key="test-key").stream_url == "wss://api.rightnowai.co/v1/" + ("system-zero" if name == "system-zero" else "decision-model") + "/stream"
    class Client(RightNow):
        _wire = _wire_for(name)
    def respond(request):
        assert str(request.url) == "https://api.rightnowai.co/v1/" + ("system-zero" if name == "system-zero" else "decision-model") + "/webhooks"
        return httpx.Response(200, json={"object": "list", "data": []})
    with Client(api_key="test-key", transport=httpx.MockTransport(respond)) as client:
        client.webhooks.list()
