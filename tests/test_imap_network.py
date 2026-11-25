import imaplib
import socket
import ssl
from unittest import TestCase
from unittest.mock import Mock, patch

from mailimport.imap_network import BoundedIMAP4_SSL, resolve_public_host, validate_public_hostname
from mailimport.parser import MAX_EML_BYTES


class ImapNetworkTests(TestCase):
    def test_hostname_grammar_rejects_ip_literals_paths_and_local_names(self):
        for hostname in ("https://example.com", "example.com:993", "127.0.0.1", "::1", "mail.local"):
            with self.subTest(hostname=hostname), self.assertRaises(ValueError):
                validate_public_hostname(hostname)
        self.assertEqual(validate_public_hostname("Imap.Example.ORG."), "imap.example.org")

    def test_mixed_public_and_private_dns_results_are_rejected(self):
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 993)),
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", 993, 0, 0)),
        ]
        with patch("socket.getaddrinfo", return_value=answers), self.assertRaises(ValueError):
            resolve_public_host("imap.example.org", 993)

    def test_connection_pins_numeric_addresses_and_keeps_tls_hostname(self):
        answers = [
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2606:4700::1111", 993, 0, 0)),
        ]
        with patch("socket.getaddrinfo", return_value=answers) as dns:
            pinned = resolve_public_host("imap.example.org", 993)
        dns.assert_called_once()

        client = BoundedIMAP4_SSL.__new__(BoundedIMAP4_SSL)
        client.host = "imap.example.org"
        client.port = 993
        client.pinned_addresses = pinned
        client.ssl_context = Mock(spec=ssl.SSLContext)
        with (
            patch("socket.getaddrinfo", side_effect=AssertionError("Second DNS lookup")),
            patch("socket.socket") as socket_factory,
        ):
            result = client._create_socket(timeout=10)

        sock = socket_factory.return_value
        socket_factory.assert_called_once_with(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        sock.connect.assert_called_once_with(("2606:4700::1111", 993, 0, 0))
        client.ssl_context.wrap_socket.assert_called_once_with(sock, server_hostname="imap.example.org")
        self.assertIs(result, client.ssl_context.wrap_socket.return_value)

    def test_missing_ipv6_support_falls_back_to_verified_ipv4(self):
        client = BoundedIMAP4_SSL.__new__(BoundedIMAP4_SSL)
        client.host = "imap.example.org"
        client.pinned_addresses = (
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, ("2606:4700::1111", 993, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ("8.8.8.8", 993)),
        )
        client.ssl_context = Mock(spec=ssl.SSLContext)
        ipv4_sock = Mock()
        with patch("socket.socket", side_effect=[OSError("IPv6 unavailable"), ipv4_sock]):
            client._create_socket(timeout=10)
        ipv4_sock.connect.assert_called_once_with(("8.8.8.8", 993))

    def test_server_cannot_send_unbounded_body_literal(self):
        client = BoundedIMAP4_SSL.__new__(BoundedIMAP4_SSL)
        client.shutdown = Mock()
        client.file = Mock()
        with self.assertRaises(imaplib.IMAP4.abort):
            client.read(MAX_EML_BYTES + 1)
        client.file.read.assert_not_called()
        client.shutdown.assert_called_once()
