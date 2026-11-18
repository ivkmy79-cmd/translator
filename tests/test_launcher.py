"""Certificate tests use an isolated CA; no Windows trust-store installation."""
import os
from pathlib import Path
import shutil
import ssl
import subprocess
from types import SimpleNamespace

import pytest

import launcher


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    folder = tmp_path_factory.mktemp("launcher-tls")
    ca_dir = folder / "ca"
    ca_dir.mkdir()
    exe = shutil.which("mkcert") or str(Path(os.environ["LOCALAPPDATA"]) / "Programs/mkcert/mkcert.exe")
    assert Path(exe).is_file(), "These TLS integration tests require the existing mkcert installation."
    cert, key = folder / "localhost.pem", folder / "localhost-key.pem"
    result = subprocess.run(
        [exe, "-cert-file", str(cert), "-key-file", str(key), "localhost", "127.0.0.1", "::1", "192.0.2.10"],
        env=dict(os.environ, CAROOT=str(ca_dir), TRUST_STORES="system"),
        capture_output=True, timeout=30,
    )
    assert result.returncode == 0
    return SimpleNamespace(exe=exe, ca=ca_dir / "rootCA.pem", cert=cert, key=key)


def pair_copy(bundle, folder):
    cert, key = folder / "localhost.pem", folder / "localhost-key.pem"
    shutil.copyfile(bundle.cert, cert)
    shutil.copyfile(bundle.key, key)
    return cert, key


def test_real_certificate_validates_and_unchanged_ip_needs_no_renewal(certificates):
    info = launcher.inspect_pair(certificates.cert, certificates.key, certificates.ca)
    assert launcher.renewal_reason(info, "192.0.2.10") is None
    assert launcher.renewal_reason(info, "192.0.2.11") is not None
    assert launcher.renewal_reason(info, None) is None


def test_expiry_threshold(certificates):
    info = launcher.inspect_pair(certificates.cert, certificates.key, certificates.ca)
    expiry = ssl.cert_time_to_seconds(info["notAfter"])
    assert launcher.renewal_reason(info, "192.0.2.10", now=expiry - 31 * 86400) is None
    assert launcher.renewal_reason(info, "192.0.2.10", now=expiry - 30 * 86400)


def test_real_renewal_changes_server_pair_but_preserves_ca(certificates, tmp_path):
    cert, key = pair_copy(certificates, tmp_path)
    before = cert.read_bytes()
    root_before = certificates.ca.read_bytes()
    root_key_before = (certificates.ca.parent / "rootCA-key.pem").read_bytes()
    info = launcher.renew(certificates.exe, certificates.ca, cert, key, "192.0.2.11")
    assert launcher.renewal_reason(info, "192.0.2.11") is None
    assert cert.read_bytes() != before
    assert certificates.ca.read_bytes() == root_before
    assert (certificates.ca.parent / "rootCA-key.pem").read_bytes() == root_key_before
    assert not list(tmp_path.glob("renew-*"))


def test_generation_failure_preserves_both_files(certificates, tmp_path, monkeypatch):
    cert, key = pair_copy(certificates, tmp_path)
    before = (cert.read_bytes(), key.read_bytes())
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match="재발급 실패"):
        launcher.renew(certificates.exe, certificates.ca, cert, key, "192.0.2.11")
    assert (cert.read_bytes(), key.read_bytes()) == before
    assert not list(tmp_path.glob("renew-*"))


def test_invalid_generated_pair_never_replaces_original(certificates, tmp_path, monkeypatch):
    cert, key = pair_copy(certificates, tmp_path)
    before = (cert.read_bytes(), key.read_bytes())

    def fake_generate(args, **kwargs):
        Path(args[2]).write_text("not a certificate")
        Path(args[4]).write_text("not a key")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(launcher.subprocess, "run", fake_generate)
    with pytest.raises(ssl.SSLError):
        launcher.renew(certificates.exe, certificates.ca, cert, key, "192.0.2.11")
    assert (cert.read_bytes(), key.read_bytes()) == before


def test_second_replace_failure_restores_pair(tmp_path, monkeypatch):
    cert, key, new_cert, new_key = [tmp_path / name for name in ("cert", "key", "new-cert", "new-key")]
    for path in (cert, key, new_cert, new_key):
        path.write_bytes(path.name.encode())
    replace = os.replace

    def fail_key(source, target):
        if target == key:
            raise OSError("injected file lock")
        replace(source, target)

    monkeypatch.setattr(launcher.os, "replace", fail_key)
    with pytest.raises(OSError):
        launcher.install_pair(new_cert, new_key, cert, key)
    assert cert.read_bytes() == b"cert"
    assert key.read_bytes() == b"key"


def test_missing_ca_never_generates_one(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=str(tmp_path)))
    with pytest.raises(RuntimeError, match="새 CA는 만들지 않습니다"):
        launcher.existing_ca("mkcert", tmp_path / "pin.pem")
    assert not (tmp_path / "rootCA.pem").exists()


def test_changed_ca_rejected(certificates, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=str(certificates.ca.parent)))
    wrong_pin = tmp_path / "pin.pem"
    shutil.copyfile(certificates.cert, wrong_pin)
    with pytest.raises(RuntimeError, match="일치하지 않습니다"):
        launcher.existing_ca(certificates.exe, wrong_pin)


def test_existing_ca_pin_accepted(certificates, monkeypatch):
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=str(certificates.ca.parent)))
    assert launcher.existing_ca(certificates.exe, certificates.ca) == certificates.ca


def test_ca_access_denied_identifies_path(certificates, monkeypatch):
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=str(certificates.ca.parent)))
    real_open = Path.open
    private_key = certificates.ca.parent / "rootCA-key.pem"

    def denied(path, *args, **kwargs):
        if path == private_key:
            raise PermissionError("test access denial")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(RuntimeError, match="접근 권한 없음") as caught:
        launcher.existing_ca(certificates.exe, certificates.ca)
    assert str(private_key) in str(caught.value)


def test_wrong_key_rejected(certificates):
    with pytest.raises(ssl.SSLError):
        launcher.inspect_pair(certificates.cert, certificates.ca.parent / "rootCA-key.pem", certificates.ca)


def test_project_ca_works_without_appdata(certificates, tmp_path, monkeypatch):
    ca_dir = tmp_path / "ca"
    ca_dir.mkdir()
    for name in ("rootCA.pem", "rootCA-key.pem"):
        shutil.copyfile(certificates.ca.parent / name, ca_dir / name)
    pinned = tmp_path / "rootCA.pem"
    shutil.copyfile(certificates.ca, pinned)

    def unavailable_appdata(*args, **kwargs):
        pytest.fail("Project CA must not query the unavailable AppData location")

    monkeypatch.setattr(launcher.subprocess, "run", unavailable_appdata)
    assert launcher.existing_ca(certificates.exe, pinned) == ca_dir / "rootCA.pem"
    (ca_dir / "rootCA-key.pem").chmod(0o600)
    (ca_dir / "rootCA-key.pem").unlink()
    with pytest.raises(RuntimeError, match="rootCA-key.pem"):
        launcher.existing_ca(certificates.exe, pinned)


def test_real_network_inventory():
    # An empty physical adapter list is valid for offline localhost use.
    networks = launcher.discover_networks()
    assert isinstance(networks, list)
    for item in networks:
        assert item["name"]
        assert launcher.ipaddress.IPv4Address(item["ip"])


def test_network_selection():
    options = [{"name": "Wi-Fi", "ip": "192.0.2.10"}, {"name": "Ethernet", "ip": "192.0.2.11"}]
    answers = iter(["bad", "3", "2"])
    assert launcher.choose_network(options, lambda _: next(answers)) == options[1]
    assert launcher.choose_network(options[:1]) == options[0]
    assert launcher.choose_network([]) is None
    with pytest.raises(RuntimeError):
        launcher.choose_network(options, lambda _: "q")


def test_network_query_failure_is_not_treated_as_offline(monkeypatch):
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match="네트워크 조회 실패"):
        launcher.discover_networks()


def test_real_server_start_health_and_owned_process_cleanup(certificates, monkeypatch):
    with launcher.socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(launcher, "PORT", port)
    real_context = ssl.create_default_context
    monkeypatch.setattr(launcher.ssl, "create_default_context", lambda: real_context(cafile=str(certificates.ca)))
    children = []
    real_popen = subprocess.Popen

    def record_child(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    class BrowserReached(Exception):
        pass

    def opened(url):
        assert url == f"https://localhost:{port}"
        raise BrowserReached()

    monkeypatch.setattr(launcher.subprocess, "Popen", record_child)
    monkeypatch.setattr(launcher.webbrowser, "open", opened)
    with pytest.raises(BrowserReached):
        launcher.serve(certificates.cert, certificates.key, "192.0.2.10")
    assert len(children) == 1 and children[0].poll() is not None


def test_locked_launcher_does_not_regenerate(certificates, tmp_path, monkeypatch):
    import msvcrt
    cert_dir = tmp_path / "certs"
    cert_dir.mkdir()
    cert, key = pair_copy(certificates, cert_dir)
    shutil.copyfile(certificates.ca, cert_dir / "rootCA.pem")
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(launcher, "discover_networks", lambda: [{"name": "LAN", "ip": "192.0.2.10"}])
    monkeypatch.setattr(launcher, "existing_ca", lambda *a: certificates.ca)
    monkeypatch.setattr(launcher, "port_in_use", lambda: True)
    monkeypatch.setattr(launcher, "check_running_server", lambda *a: None)
    opened = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url))
    before = (cert.read_bytes(), key.read_bytes())
    with (cert_dir / "launcher.lock").open("w+b") as lock:
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        assert launcher.main() == 0
    assert opened == ["https://localhost:8443"]
    assert (cert.read_bytes(), key.read_bytes()) == before


@pytest.mark.parametrize("ip,busy", [("192.0.2.10", False), ("192.0.2.10", True), ("192.0.2.11", True)])
def test_main_unchanged_and_busy_cases(certificates, tmp_path, monkeypatch, ip, busy):
    cert_dir = tmp_path / "certs"
    cert_dir.mkdir()
    cert, key = pair_copy(certificates, cert_dir)
    shutil.copyfile(certificates.ca, cert_dir / "rootCA.pem")
    before = (cert.read_bytes(), key.read_bytes())
    calls = []
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(launcher, "discover_networks", lambda: [{"name": "LAN", "ip": ip}])
    monkeypatch.setattr(launcher, "existing_ca", lambda *a: certificates.ca)
    monkeypatch.setattr(launcher, "port_in_use", lambda: busy)
    monkeypatch.setattr(launcher, "check_running_server", lambda *a: calls.append("checked"))
    monkeypatch.setattr(launcher.webbrowser, "open", lambda *a: calls.append("browser"))
    monkeypatch.setattr(launcher, "serve", lambda *a: calls.append("serve") or 0)
    if ip != "192.0.2.10":
        with pytest.raises(RuntimeError, match="종료"):
            launcher.main()
        assert not calls
    else:
        assert launcher.main() == 0
        assert calls == (["checked", "browser"] if busy else ["serve"])
    assert (cert.read_bytes(), key.read_bytes()) == before
