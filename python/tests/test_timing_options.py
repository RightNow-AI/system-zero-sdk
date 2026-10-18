"""Shared SDK timing values are bounded before a transport is constructed."""

import httpx
import pytest

from system_zero import SystemZero


@pytest.mark.parametrize("option", ["timeout", "max_retry_wait", "retry_budget"])
@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), 2_147_484, "1", None])
def test_http_timing_options_reject_invalid_or_oversized_values(option, value):
    with pytest.raises(ValueError, match=option):
        with SystemZero(api_key="SYNTHETIC", **{option: value},
                        transport=httpx.MockTransport(lambda _request: pytest.fail("unexpected transport call"))):
            pass


def test_zero_attempt_timeout_is_invalid_but_zero_retry_wait_and_maximum_are_valid():
    with pytest.raises(ValueError, match="timeout"):
        SystemZero(api_key="SYNTHETIC", timeout=0)
    with SystemZero(api_key="SYNTHETIC", timeout=2_147_483.647, max_retry_wait=0,
                    transport=httpx.MockTransport(lambda _request: pytest.fail("unexpected transport call"))):
        pass
