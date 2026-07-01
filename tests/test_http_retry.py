"""Bounded exponential-backoff retry for transient sync HTTP failures."""
import httpx
import pytest

from app.services.http_retry import RETRYABLE_STATUS, send_with_retry


class _Resp:
    """Minimal stand-in — send_with_retry only reads .status_code."""
    def __init__(self, status_code: int):
        self.status_code = status_code


def test_returns_immediately_on_success_without_sleeping():
    slept: list[float] = []
    calls = {"n": 0}

    def send():
        calls["n"] += 1
        return _Resp(200)

    resp = send_with_retry(send, sleep=slept.append)
    assert resp.status_code == 200
    assert calls["n"] == 1
    assert slept == []          # no retries → no backoff


def test_retries_on_retryable_status_then_succeeds():
    slept: list[float] = []
    seq = iter([503, 503, 200])

    resp = send_with_retry(lambda: _Resp(next(seq)),
                           base_delay=0.5, sleep=slept.append)
    assert resp.status_code == 200
    assert slept == [0.5, 1.0]  # exponential backoff between the two retries


def test_retries_on_transport_error_then_succeeds():
    slept: list[float] = []
    calls = {"n": 0}

    def send():
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("boom")
        return _Resp(200)

    resp = send_with_retry(send, base_delay=0.5, sleep=slept.append)
    assert resp.status_code == 200
    assert calls["n"] == 2
    assert slept == [0.5]


def test_gives_up_and_returns_last_retryable_response():
    """After exhausting attempts on a retryable status, return the final response
    so the caller's existing raise_for_status/_unwrap error path is preserved."""
    slept: list[float] = []
    resp = send_with_retry(lambda: _Resp(503), attempts=3, sleep=slept.append)
    assert resp.status_code == 503
    assert len(slept) == 2      # slept between the 3 attempts, not after the last


def test_reraises_transport_error_on_final_attempt():
    def send():
        raise httpx.ReadTimeout("still down")

    with pytest.raises(httpx.ReadTimeout):
        send_with_retry(send, attempts=2, sleep=lambda _s: None)


def test_does_not_retry_non_retryable_status():
    slept: list[float] = []
    calls = {"n": 0}

    def send():
        calls["n"] += 1
        return _Resp(400)

    resp = send_with_retry(send, sleep=slept.append)
    assert resp.status_code == 400
    assert calls["n"] == 1      # 4xx (except 429) is a real error, not transient
    assert slept == []


def test_429_is_retryable():
    assert 429 in RETRYABLE_STATUS
    assert all(c in RETRYABLE_STATUS for c in (500, 502, 503, 504))
