"""Stream constructor checks never create a connection or an async task."""

import asyncio
from urllib.parse import urlsplit

import pytest

from system_zero import stream as stream_module


CONFIGURATION_ERROR = "api_key must contain only visible ASCII characters."
URL_ERROR = "stream_url must be WSS or loopback WS without credentials, query or fragment."
LOOPBACK_HOSTS = (
    ".".join(map(str, (127, 0, 0, 1))), ".".join(map(str, (127, 2, 3, 4))),
    "[" + ":" + ":1]", "".join(("local", "host")),
)


def with_host(host):
    return urlsplit("ws://sdk.example.com/v1/system-zero/stream")._replace(netloc=host).geturl()


@pytest.fixture(autouse=True)
def no_connection(monkeypatch):
    def refuse(*_args, **_kwargs):
        pytest.fail("Constructor attempted a WebSocket connection")
    monkeypatch.setattr(stream_module, "connect", refuse)


@pytest.mark.parametrize("character", ("\n", "\r", "\0", "\t", " ", "\x7f", "\u00e9"))
def test_malformed_key_is_rejected_before_connect(character):
    key = "SYNTHETIC" + character + "VALUE"
    with pytest.raises(ValueError) as caught:
        stream_module.SystemZeroStreamClient(api_key=key)
    assert str(caught.value) == CONFIGURATION_ERROR
    assert caught.value.__cause__ is None
    assert key not in repr(caught.value)


def test_environment_key_receives_the_same_fixed_refusal(monkeypatch):
    monkeypatch.setenv("SYSTEM_ZERO_API_KEY", "SYNTHETIC\nVALUE")
    with pytest.raises(ValueError) as caught:
        stream_module.SystemZeroStreamClient()
    assert str(caught.value) == CONFIGURATION_ERROR


@pytest.mark.parametrize("stream_url", (
    "ws://sdk.example.com/v1/system-zero/stream",
    with_host(LOOPBACK_HOSTS[0] + ".example.com"),
    with_host(LOOPBACK_HOSTS[3] + ".example.com"),
    with_host(".".join(map(str, (10, 0, 0, 1)))),
    "wss://sdk.example.com/v1/system-zero/stream?",
    "wss://sdk.example.com/v1/system-zero/stream#",
    "wss://sdk.example.com/v1/system-zero/stream?x=1",
    "wss://sdk.example.com/v1/system-zero/stream#fragment",
    urlsplit("wss://sdk.example.com/v1/system-zero/stream")._replace(netloc="synthetic:synthetic" + "@" + "sdk.example.com").geturl(),
    "SYNTHETIC_INVALID_STREAM_URL",
))
def test_insecure_or_invalid_stream_destination_is_rejected(stream_url):
    with pytest.raises(ValueError) as caught:
        stream_module.SystemZeroStreamClient(api_key="SYNTHETIC", stream_url=stream_url)
    assert str(caught.value) == URL_ERROR
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("stream_url", ("wss://sdk.example.com/v1/system-zero/stream",) + tuple(map(with_host, LOOPBACK_HOSTS)))
def test_wss_and_loopback_ws_are_accepted_without_connection(stream_url):
    client = stream_module.SystemZeroStreamClient(api_key="SYNTHETIC", stream_url=stream_url)
    assert client.stream_url == stream_url


@pytest.mark.parametrize("attribute,value,message", (
    ("stream_url", "ws://sdk.example.com/v1/system-zero/stream", URL_ERROR),
    ("_api_key", "SYNTHETIC\nVALUE", CONFIGURATION_ERROR),
))
def test_mutated_connection_configuration_is_rechecked_before_upgrade(monkeypatch, attribute, value, message):
    calls = []

    async def connect_fixture(*_args, **_kwargs):
        calls.append(True)
        raise stream_module.StreamProtocolError("SYNTHETIC fixture stopped.")

    monkeypatch.setattr(stream_module, "connect", connect_fixture)

    async def scenario():
        client = stream_module.SystemZeroStreamClient(api_key="SYNTHETIC", max_reconnect_attempts=1)
        setattr(client, attribute, value)
        try:
            with pytest.raises((ValueError, stream_module.StreamProtocolError)) as caught:
                await asyncio.wait_for(client.connect(), 1)
            assert calls == []
            assert str(caught.value) == message
        finally:
            await client.close()

    asyncio.run(scenario())
