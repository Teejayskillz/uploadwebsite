"""SSRF guard tests (brief section 4 and section 18)."""

import socket

from django.test import SimpleTestCase

from uploader.services.netguard import (
    UnsafeURLError,
    check_address,
    safe_host_label,
    validate_redirect,
    validate_url,
)


def resolver_for(*addresses):
    """A fake getaddrinfo that always returns the given addresses."""

    def _resolver(host, port, type=None, **kwargs):  # noqa: A002 - mirror stdlib
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]

    return _resolver


def failing_resolver(host, port, type=None, **kwargs):  # noqa: A002
    raise socket.gaierror("name or service not known")


PUBLIC = "93.184.216.34"


class SchemeAndShapeTests(SimpleTestCase):
    def test_https_public_host_is_allowed(self):
        result = validate_url("https://example.com/video.mp4", resolver=resolver_for(PUBLIC))
        self.assertEqual(result.host, "example.com")
        self.assertEqual(result.port, 443)
        self.assertEqual(result.addresses, (PUBLIC,))
        self.assertTrue(result.is_https)

    def test_http_is_allowed_by_default(self):
        result = validate_url("http://example.com/a.mp4", resolver=resolver_for(PUBLIC))
        self.assertEqual(result.port, 80)

    def test_http_can_be_disabled(self):
        with self.assertRaises(UnsafeURLError):
            validate_url(
                "http://example.com/a.mp4", resolver=resolver_for(PUBLIC), allow_http=False
            )

    def test_non_http_scheme_is_rejected(self):
        for url in ("ftp://example.com/a.mp4", "file:///etc/passwd", "gopher://example.com/"):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeURLError):
                    validate_url(url, resolver=resolver_for(PUBLIC))

    def test_credentials_in_url_are_rejected(self):
        with self.assertRaises(UnsafeURLError):
            validate_url("https://user:pass@example.com/a.mp4", resolver=resolver_for(PUBLIC))

    def test_unusual_port_is_rejected(self):
        with self.assertRaises(UnsafeURLError):
            validate_url("https://example.com:8080/a.mp4", resolver=resolver_for(PUBLIC))

    def test_control_characters_are_rejected(self):
        with self.assertRaises(UnsafeURLError):
            validate_url("https://example.com/a.mp4\r\nHost: evil", resolver=resolver_for(PUBLIC))

    def test_fragment_is_stripped_from_the_normalised_url(self):
        result = validate_url(
            "https://example.com/a.mp4#frag", resolver=resolver_for(PUBLIC)
        )
        self.assertNotIn("#", result.url)
        self.assertEqual(result.url, "https://example.com/a.mp4")


class HostAndAddressTests(SimpleTestCase):
    def test_localhost_name_is_blocked(self):
        with self.assertRaises(UnsafeURLError):
            validate_url("http://localhost/a.mp4", resolver=resolver_for(PUBLIC))

    def test_internal_suffixes_are_blocked(self):
        for host in ("db.internal", "printer.local", "router.lan", "x.home.arpa"):
            with self.subTest(host=host):
                with self.assertRaises(UnsafeURLError):
                    validate_url(f"http://{host}/a.mp4", resolver=resolver_for(PUBLIC))

    def test_loopback_literal_is_blocked(self):
        for url in ("http://127.0.0.1/a.mp4", "http://[::1]/a.mp4", "https://127.1.2.3/"):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeURLError):
                    validate_url(url, resolver=resolver_for(PUBLIC))

    def test_ipv4_mapped_loopback_is_blocked(self):
        with self.assertRaises(UnsafeURLError):
            validate_url("http://[::ffff:127.0.0.1]/a.mp4", resolver=resolver_for(PUBLIC))

    def test_private_ranges_are_blocked(self):
        for url in (
            "http://10.0.0.5/a.mp4",
            "http://172.16.4.4/a.mp4",
            "http://192.168.1.1/a.mp4",
            "http://169.254.169.254/latest/meta-data/",
            "http://0.0.0.0/a.mp4",
        ):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeURLError):
                    validate_url(url, resolver=resolver_for(PUBLIC))

    def test_hostname_resolving_to_private_address_is_blocked(self):
        with self.assertRaises(UnsafeURLError):
            validate_url("https://rebind.example.com/a.mp4", resolver=resolver_for("10.1.2.3"))

    def test_mixed_public_and_private_answers_are_blocked(self):
        """A hostile record must not be able to slip one internal IP through."""
        with self.assertRaises(UnsafeURLError):
            validate_url(
                "https://rebind.example.com/a.mp4", resolver=resolver_for(PUBLIC, "127.0.0.1")
            )

    def test_dns_failure_is_rejected_not_retried(self):
        with self.assertRaises(UnsafeURLError):
            validate_url("https://nope.example.com/a.mp4", resolver=failing_resolver)

    def test_empty_resolution_is_rejected(self):
        with self.assertRaises(UnsafeURLError):
            validate_url("https://empty.example.com/a.mp4", resolver=resolver_for())

    def test_check_address_accepts_public_and_rejects_private(self):
        self.assertEqual(check_address("8.8.8.8"), "8.8.8.8")
        with self.assertRaises(UnsafeURLError):
            check_address("192.168.0.1")
        with self.assertRaises(UnsafeURLError):
            check_address("not-an-ip")


class RedirectTests(SimpleTestCase):
    def test_relative_redirect_is_resolved_against_base(self):
        result = validate_redirect(
            "https://example.com/a/b.mp4", "/c/d.mp4", resolver=resolver_for(PUBLIC)
        )
        self.assertEqual(result.url, "https://example.com/c/d.mp4")

    def test_redirect_to_private_address_is_blocked(self):
        with self.assertRaises(UnsafeURLError):
            validate_redirect(
                "https://example.com/a.mp4", "http://127.0.0.1/a.mp4", resolver=resolver_for(PUBLIC)
            )

    def test_redirect_to_internal_hostname_is_blocked(self):
        with self.assertRaises(UnsafeURLError):
            validate_redirect(
                "https://example.com/a.mp4", "http://db.internal/a.mp4", resolver=resolver_for(PUBLIC)
            )

    def test_empty_redirect_is_blocked(self):
        with self.assertRaises(UnsafeURLError):
            validate_redirect("https://example.com/a.mp4", "", resolver=resolver_for(PUBLIC))


class LogSafetyTests(SimpleTestCase):
    def test_host_label_omits_query_string(self):
        result = validate_url(
            "https://example.com/a.mp4?token=SECRET", resolver=resolver_for(PUBLIC)
        )
        label = safe_host_label(result)
        self.assertNotIn("SECRET", label)
        self.assertIn("example.com", label)
