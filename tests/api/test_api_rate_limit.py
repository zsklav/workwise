from workwise.core.security import auth_settings


def test_api_routes_are_limited_per_ip(anonymous_client, monkeypatch):
    monkeypatch.setattr(auth_settings, "api_ip_limit", 3)
    statuses = [anonymous_client.get("/jobs").status_code for _ in range(5)]
    assert statuses == [200, 200, 200, 429, 429]
    blocked = anonymous_client.get("/jobs")
    assert blocked.json() == {"detail": "Too many requests"}
    assert int(blocked.headers["Retry-After"]) >= 1


def test_probes_are_never_limited(anonymous_client, monkeypatch):
    monkeypatch.setattr(auth_settings, "api_ip_limit", 1)
    assert anonymous_client.get("/jobs").status_code == 200
    assert anonymous_client.get("/jobs").status_code == 429
    assert all(anonymous_client.get("/health").status_code == 200 for _ in range(5))
    assert all(anonymous_client.get("/ready").status_code == 200 for _ in range(5))
