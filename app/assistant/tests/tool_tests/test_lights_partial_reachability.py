"""One dead switch must not brick the whole house (2026-08-02).

A discovery timeout on one dimmer used to raise out of _kasa_load_devices, so
every lights command — including broadcast off — failed with HTTP 400 while two
healthy switches sat ignored. Now unreachable hosts are collected and reported
in every response; commands act on every reachable light; zero reachable
devices is still a loud failure.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import app.assistant.tests.test_setup  # noqa: F401

from app.routes import smart_home_bridge as bridge

_ALIASES = {
    "192.168.4.21": "Living room light",
    "192.168.4.22": "Family room lights",
    "192.168.4.24": "Kitchen lights",
}
_HOSTS = list(_ALIASES)


class FakeDimmer:
    def __init__(self, host):
        self.host = host
        self.alias = _ALIASES[host]
        self.model = "HS220"
        self.device_type = "Dimmer"
        self.brightness = 40
        self.is_on = True
        self.calls = []

    async def update(self):
        self.calls.append("update")

    async def turn_on(self):
        self.calls.append("turn_on")
        self.is_on = True

    async def turn_off(self):
        self.calls.append("turn_off")
        self.is_on = False

    async def set_brightness(self, pct):
        self.calls.append(f"set_brightness:{pct}")
        self.brightness = pct


def _patch_discovery(monkeypatch, dead_hosts):
    devices = {}

    async def fake_discover_single(host, timeout=None):
        if host in dead_hosts:
            raise TimeoutError("discovery timed out")
        devices.setdefault(host, FakeDimmer(host))
        return devices[host]

    import kasa
    monkeypatch.setattr(kasa.Discover, "discover_single",
                        staticmethod(fake_discover_single))
    return devices


class TestPartialReachability:

    def test_broadcast_off_survives_one_dead_host(self, monkeypatch):
        devices = _patch_discovery(monkeypatch, dead_hosts={"192.168.4.21"})
        result = asyncio.run(bridge._kasa_set_light_power(
            hosts=_HOSTS, timeout_seconds=1, state="off",
            light_id="", room="", host_alias_map=_ALIASES))
        assert len(result["changed"]) == 2
        assert all("turn_off" in d.calls for d in devices.values())
        assert result["unreachable"] == [{
            "host": "192.168.4.21", "error": "discovery timed out",
            "alias": "Living room light",
        }]

    def test_on_is_full_brightness_for_reachable(self, monkeypatch):
        devices = _patch_discovery(monkeypatch, dead_hosts={"192.168.4.21"})
        result = asyncio.run(bridge._kasa_set_light_power(
            hosts=_HOSTS, timeout_seconds=1, state="on",
            light_id="", room="", host_alias_map=_ALIASES))
        assert len(result["changed"]) == 2
        assert all("set_brightness:100" in d.calls for d in devices.values())

    def test_all_dead_is_a_loud_failure(self, monkeypatch):
        _patch_discovery(monkeypatch, dead_hosts=set(_HOSTS))
        with pytest.raises(RuntimeError, match="No Kasa lights reachable"):
            asyncio.run(bridge._kasa_set_light_power(
                hosts=_HOSTS, timeout_seconds=1, state="off",
                light_id="", room="", host_alias_map=_ALIASES))

    def test_targeting_the_dead_light_names_it(self, monkeypatch):
        _patch_discovery(monkeypatch, dead_hosts={"192.168.4.21"})
        with pytest.raises(RuntimeError, match="Living room light"):
            asyncio.run(bridge._kasa_set_light_power(
                hosts=_HOSTS, timeout_seconds=1, state="off",
                light_id="", room="Living room", host_alias_map=_ALIASES))

    def test_list_lights_reports_both_sides(self, monkeypatch):
        _patch_discovery(monkeypatch, dead_hosts={"192.168.4.21"})
        result = asyncio.run(bridge._kasa_list_lights(
            hosts=_HOSTS, timeout_seconds=1, host_alias_map=_ALIASES))
        assert len(result["lights"]) == 2
        assert result["unreachable"][0]["alias"] == "Living room light"
