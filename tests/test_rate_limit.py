from backend.app.rate_limit import RequestRateLimiter


def test_rate_limiter_limits_each_client_independently():
    limiter = RequestRateLimiter(requests=2, window_seconds=60)
    assert limiter.allow("client-a")
    assert limiter.allow("client-a")
    assert not limiter.allow("client-a")
    assert limiter.allow("client-b")
