from types import SimpleNamespace

import pytest

from workwise.core.auth_limits import client_ip
from workwise.core.security import auth_settings


def request(peer, forwarded=None):
    headers = {"x-forwarded-for": forwarded} if forwarded is not None else {}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


@pytest.fixture
def trust_docker(monkeypatch):
    monkeypatch.setattr(auth_settings, "trusted_proxies", "172.16.0.0/12")


def test_header_is_ignored_without_trusted_proxies(monkeypatch):
    monkeypatch.setattr(auth_settings, "trusted_proxies", "")
    assert client_ip(request("172.18.0.5", "9.9.9.9")) == "172.18.0.5"


def test_header_is_ignored_from_untrusted_peer(trust_docker):
    assert client_ip(request("8.8.8.8", "9.9.9.9")) == "8.8.8.8"


def test_trusted_proxy_forwards_client_address(trust_docker):
    assert client_ip(request("172.18.0.5", "203.0.113.7")) == "203.0.113.7"


def test_client_cannot_spoof_by_prepending_addresses(trust_docker):
    assert client_ip(request("172.18.0.5", "1.1.1.1, 203.0.113.7")) == "203.0.113.7"


def test_chained_trusted_proxies_are_skipped(trust_docker):
    assert client_ip(request("172.18.0.5", "203.0.113.7, 172.18.0.9")) == "203.0.113.7"


def test_malformed_header_falls_back_to_proxy(trust_docker):
    assert client_ip(request("172.18.0.5", "not-an-ip")) == "172.18.0.5"
    assert client_ip(request("172.18.0.5")) == "172.18.0.5"
