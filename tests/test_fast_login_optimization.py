from __future__ import annotations

import threading
import time
from email.message import EmailMessage
from email.utils import formatdate

import otp_mail
import relogin


def _code_message() -> bytes:
    message = EmailMessage()
    message["From"] = "Amazon Jobs <no-reply@amazon.com>"
    message["Subject"] = "Your Amazon Jobs verification code"
    message["Date"] = formatdate(localtime=True)
    message.set_content("Your verification code is 123456")
    return message.as_bytes()


def test_fetch_code_reuses_one_imap_connection_and_skips_slow_logout(monkeypatch):
    clients = []

    class Client:
        def __init__(self):
            self.shutdown_called = False
            self.logout_called = False
            self.search_calls = 0
            self.noop_calls = 0

        def login(self, _user, _password):
            return "OK", []

        def select(self, _folder, readonly=True):
            assert readonly is True
            return "OK", []

        def search(self, *_args):
            self.search_calls += 1
            # One complete INBOX + Spam cycle has no code. The next cycle
            # finds it, proving that the same authenticated client was reused.
            return "OK", [b"1" if self.search_calls > 6 else b""]

        def fetch(self, _num, _query):
            return "OK", [(b"1", _code_message())]

        def noop(self):
            self.noop_calls += 1
            return "OK", []

        def shutdown(self):
            self.shutdown_called = True

        def logout(self):
            self.logout_called = True
            raise AssertionError("successful code retrieval must not wait for LOGOUT")

    def connect(_host, timeout=None):
        assert timeout == 10
        client = Client()
        clients.append(client)
        return client

    monkeypatch.setattr(otp_mail, "configured", lambda: ("imap.test", "user", "password"))
    monkeypatch.setattr(otp_mail.imaplib, "IMAP4_SSL", connect)

    assert otp_mail.fetch_code(time.time(), timeout_s=1, poll_s=0.01) == "123456"
    assert len(clients) == 1
    assert clients[0].noop_calls >= 1
    assert clients[0].shutdown_called is True
    assert clients[0].logout_called is False


def test_background_otp_waiter_overlaps_other_auth_work(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def fetch(_since, *, timeout_s, poll_s, stop_event):
        assert timeout_s == 150
        assert poll_s == 2
        assert stop_event.is_set() is False
        entered.set()
        release.wait(1)
        return "123456"

    monkeypatch.setattr(otp_mail, "fetch_code", fetch)
    waiter = otp_mail.OtpCodeWaiter(100.0).start()

    assert entered.wait(0.5) is True
    release.set()
    assert waiter.result(timeout_s=0.5) == "123456"


def test_interactive_waf_grid_skips_token_parameter_wait():
    solver = relogin.TwoCaptchaSolver.__new__(relogin.TwoCaptchaSolver)
    solver._collect_params = lambda *_args: {
        "key": None,
        "iv": None,
        "context": None,
    }
    routed = []
    solver._solve_image_grid = lambda *_args: routed.append("grid") or True
    solver._solve_via_token = lambda *_args: routed.append("token") or True

    assert solver._solve_amazon_waf(object(), challenge_element=object()) is True
    assert routed == ["grid"]


def test_complete_waf_parameters_keep_existing_token_route():
    solver = relogin.TwoCaptchaSolver.__new__(relogin.TwoCaptchaSolver)
    solver._collect_params = lambda *_args: {
        "key": "public-site-key",
        "iv": "challenge-iv",
        "context": "challenge-context",
    }
    routed = []
    solver._solve_image_grid = lambda *_args: routed.append("grid") or True
    solver._solve_via_token = lambda *_args: routed.append("token") or True

    assert solver._solve_amazon_waf(object(), challenge_element=object()) is True
    assert routed == ["token"]


def test_fast_login_sources_do_not_log_credentials_or_code_values():
    import inspect

    mailbox_source = inspect.getsource(otp_mail.fetch_code)
    captcha_source = inspect.getsource(relogin.TwoCaptchaSolver._solve_amazon_waf)
    assert "log.info(code" not in mailbox_source
    assert "log.info(pin" not in mailbox_source.lower()
    assert "cap[\"key\"]" not in captcha_source
    assert "shadow root innerHTML" not in captcha_source
