import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import Mock

import pytest

from plane.coordination import notifications


def test_callback_task_has_a_wall_clock_bound_matching_the_transport_lease(settings):
    from plane.bgtasks.coordination_notification_task import deliver_agent_event

    assert deliver_agent_event.time_limit == 2 * settings.COORDINATION_WEBHOOK_TIMEOUT


def test_callback_transport_sends_to_allowlisted_receiver_without_following_redirects(settings):
    settings.WEBHOOK_ALLOWED_IPS = [ipaddress.ip_network("127.0.0.1/32")]
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(302)
            self.send_header("Location", "/must-not-follow")
            self.end_headers()

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status = notifications.send_callback(
                f"http://127.0.0.1:{server.server_port}/worker?channel=task", b'{"id":"event"}', {}
            )
        finally:
            server.shutdown()
            thread.join()
    assert status == 302
    assert received == [("/worker?channel=task", b'{"id":"event"}')]


def test_callback_pins_validated_address_and_preserves_tls_identity(settings, monkeypatch):
    settings.WEBHOOK_ALLOWED_IPS = []
    resolver = Mock(return_value=[(2, 1, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(notifications.socket, "getaddrinfo", resolver)
    pool = Mock()
    pool.urlopen.return_value.status = 204
    constructor = Mock(return_value=pool)
    monkeypatch.setattr(notifications.urllib3, "HTTPSConnectionPool", constructor)
    assert notifications.send_callback("https://worker.example.test/callback", b"{}", {}) == 204
    assert resolver.call_count == 1
    assert constructor.call_args.args == ("93.184.216.34",)
    assert constructor.call_args.kwargs["server_hostname"] == "worker.example.test"
    assert constructor.call_args.kwargs["assert_hostname"] == "worker.example.test"
    assert pool.urlopen.call_args.kwargs["headers"]["Host"] == "worker.example.test"
    assert pool.urlopen.call_args.kwargs["redirect"] is False
    assert pool.urlopen.call_args.kwargs["retries"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/callback",
        "https://user:secret@worker.example.test/",
        "file:///etc/passwd",
        "https://worker.example.test/#fragment",
        "http://worker.example.test/",
    ],
)
def test_callback_rejects_untrusted_destinations(settings, monkeypatch, url):
    settings.WEBHOOK_ALLOWED_IPS = []
    monkeypatch.setattr(notifications.socket, "getaddrinfo", Mock(return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]))
    with pytest.raises(ValueError):
        notifications.callback_destination(url)


def test_callback_rechecks_every_resolved_address(settings, monkeypatch):
    settings.WEBHOOK_ALLOWED_IPS = []
    monkeypatch.setattr(
        notifications.socket,
        "getaddrinfo",
        Mock(
            return_value=[
                (2, 1, 6, "", ("93.184.216.34", 443)),
                (2, 1, 6, "", ("169.254.169.254", 443)),
            ]
        ),
    )
    with pytest.raises(ValueError):
        notifications.callback_destination("https://worker.example.test/")
