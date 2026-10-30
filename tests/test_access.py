"""Tests for utils/access.py (remote request detection and password check)."""
import unittest

from utils.access import check_access_password, is_remote_request


class IsRemoteRequestTests(unittest.TestCase):
    def test_missing_headers_are_local(self):
        self.assertFalse(is_remote_request(None))
        self.assertFalse(is_remote_request({}))

    def test_loopback_hosts_are_local(self):
        for host in ("localhost:8501", "127.0.0.1:8501", "LOCALHOST", "[::1]:8501", "::1"):
            with self.subTest(host=host):
                self.assertFalse(is_remote_request({"Host": host}))

    def test_tunnel_host_is_remote(self):
        self.assertTrue(is_remote_request({"Host": "abc-def.trycloudflare.com"}))

    def test_lan_host_is_remote(self):
        self.assertTrue(is_remote_request({"Host": "192.168.1.20:8501"}))

    def test_proxy_headers_make_request_remote_even_with_local_host(self):
        for header in ("Cf-Connecting-Ip", "cf-ray", "X-Forwarded-For", "x-forwarded-host"):
            with self.subTest(header=header):
                self.assertTrue(is_remote_request({"Host": "localhost:8501", header: "1.2.3.4"}))

    def test_empty_proxy_header_is_ignored(self):
        self.assertFalse(is_remote_request({"Host": "localhost:8501", "X-Forwarded-For": ""}))


class CheckAccessPasswordTests(unittest.TestCase):
    def test_match(self):
        self.assertTrue(check_access_password("s3cret", "s3cret"))

    def test_mismatch(self):
        self.assertFalse(check_access_password("wrong", "s3cret"))

    def test_empty_expected_never_matches(self):
        self.assertFalse(check_access_password("", ""))
        self.assertFalse(check_access_password("anything", None))

    def test_none_provided(self):
        self.assertFalse(check_access_password(None, "s3cret"))

    def test_unicode(self):
        self.assertTrue(check_access_password("contraseña", "contraseña"))


if __name__ == "__main__":
    unittest.main()

