"""Tests for directdnsonly.app.backends.nsd — NSDBackend."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from directdnsonly.app.backends.nsd import NSDBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ZONE_DATA = """\
$ORIGIN example.com.
$TTL 300
@ 300 IN SOA ns1.example.com. hostmaster.example.com. (2024010101 3600 900 604800 300)
@ 300 IN NS ns1.example.com.
@ 300 IN A 192.0.2.1
"""


def _make_backend(tmp_path) -> NSDBackend:
    """Return an NSDBackend pointing at tmp_path directories.

    is_available() is patched so the tests do not require a real nsd install.
    """
    zones_dir = tmp_path / "zones"
    nsd_conf = tmp_path / "nsd.conf.d" / "zones.conf"
    config = {
        "instance_name": "test_nsd",
        "zones_dir": str(zones_dir),
        "nsd_conf": str(nsd_conf),
    }
    with patch.object(NSDBackend, "is_available", return_value=True):
        return NSDBackend(config)


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------


def test_is_available_true(monkeypatch):
    monkeypatch.setattr(
        "directdnsonly.app.backends.nsd.subprocess.run",
        lambda *a, **kw: MagicMock(returncode=0),
    )
    assert NSDBackend.is_available()


def test_is_available_false_when_not_installed(monkeypatch):
    def raise_fnf(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("directdnsonly.app.backends.nsd.subprocess.run", raise_fnf)
    assert not NSDBackend.is_available()


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def test_init_creates_zones_dir(tmp_path):
    backend = _make_backend(tmp_path)
    assert backend.zones_dir.exists()


def test_init_creates_nsd_conf(tmp_path):
    backend = _make_backend(tmp_path)
    assert backend.nsd_conf.exists()


def test_get_name():
    assert NSDBackend.get_name() == "nsd"


# ---------------------------------------------------------------------------
# write_zone
# ---------------------------------------------------------------------------


def test_write_zone_creates_zone_file(tmp_path):
    backend = _make_backend(tmp_path)
    assert backend.write_zone("example.com", ZONE_DATA)
    assert (backend.zones_dir / "example.com.db").exists()


def test_write_zone_content_matches(tmp_path):
    backend = _make_backend(tmp_path)
    backend.write_zone("example.com", ZONE_DATA)
    content = (backend.zones_dir / "example.com.db").read_text()
    assert content == ZONE_DATA


def test_write_zone_adds_to_conf(tmp_path):
    backend = _make_backend(tmp_path)
    backend.write_zone("example.com", ZONE_DATA)
    conf = backend.nsd_conf.read_text()
    assert 'name: "example.com"' in conf
    assert "example.com.db" in conf


def test_write_zone_idempotent_conf_entry(tmp_path):
    backend = _make_backend(tmp_path)
    backend.write_zone("example.com", ZONE_DATA)
    backend.write_zone("example.com", ZONE_DATA)
    conf = backend.nsd_conf.read_text()
    # Should appear exactly once
    assert conf.count('name: "example.com"') == 1


def test_write_zone_multiple_zones(tmp_path):
    backend = _make_backend(tmp_path)
    backend.write_zone("example.com", ZONE_DATA)
    backend.write_zone("other.com", ZONE_DATA)
    conf = backend.nsd_conf.read_text()
    assert 'name: "example.com"' in conf
    assert 'name: "other.com"' in conf


# ---------------------------------------------------------------------------
# zone_exists
# ---------------------------------------------------------------------------


def test_zone_exists_after_write(tmp_path):
    backend = _make_backend(tmp_path)
    backend.write_zone("example.com", ZONE_DATA)
    assert backend.zone_exists("example.com")


def test_zone_not_exists_before_write(tmp_path):
    backend = _make_backend(tmp_path)
    assert not backend.zone_exists("missing.com")


# ---------------------------------------------------------------------------
# delete_zone
# ---------------------------------------------------------------------------


def test_delete_zone_removes_file(tmp_path):
    backend = _make_backend(tmp_path)
    backend.write_zone("example.com", ZONE_DATA)
    assert backend.delete_zone("example.com")
    assert not (backend.zones_dir / "example.com.db").exists()


def test_delete_zone_removes_conf_entry(tmp_path):
    backend = _make_backend(tmp_path)
    backend.write_zone("example.com", ZONE_DATA)
    backend.delete_zone("example.com")
    conf = backend.nsd_conf.read_text()
    assert 'name: "example.com"' not in conf


def test_delete_zone_returns_false_when_missing(tmp_path):
    backend = _make_backend(tmp_path)
    assert not backend.delete_zone("ghost.com")


def test_delete_zone_leaves_other_zones(tmp_path):
    backend = _make_backend(tmp_path)
    backend.write_zone("example.com", ZONE_DATA)
    backend.write_zone("other.com", ZONE_DATA)
    backend.delete_zone("example.com")
    assert 'name: "other.com"' in backend.nsd_conf.read_text()


# ---------------------------------------------------------------------------
# reload_zone — subprocess interactions
# ---------------------------------------------------------------------------


def test_reload_zone_calls_nsd_control_reload(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("directdnsonly.app.backends.nsd.subprocess.run", fake_run)
    assert backend.reload_zone()
    assert calls[0] == ["nsd-control", "reload"]


def test_reload_single_zone_passes_zone_name(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("directdnsonly.app.backends.nsd.subprocess.run", fake_run)
    assert backend.reload_zone("example.com")
    assert calls[0] == ["nsd-control", "reload", "example.com"]


def test_reload_zone_returns_false_on_failure(tmp_path, monkeypatch):
    backend = _make_backend(tmp_path)

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="nsd-control: error")

    monkeypatch.setattr("directdnsonly.app.backends.nsd.subprocess.run", fake_run)
    assert not backend.reload_zone()


# ---------------------------------------------------------------------------
# update_nsd_conf — full rewrite
# ---------------------------------------------------------------------------


def test_update_nsd_conf_replaces_all_zones(tmp_path):
    backend = _make_backend(tmp_path)
    backend.write_zone("old.com", ZONE_DATA)
    backend.update_nsd_conf(["new1.com", "new2.com"])
    conf = backend.nsd_conf.read_text()
    assert 'name: "old.com"' not in conf
    assert 'name: "new1.com"' in conf
    assert 'name: "new2.com"' in conf
