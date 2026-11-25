"""Bounded IMAP/TLS sockets and destination validation for UI settings."""

import imaplib
import ipaddress
import re
import socket
import ssl

from .parser import MAX_EML_BYTES


_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_TLD_LABEL = r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rf"(?:{_DNS_LABEL}\.)+{_TLD_LABEL}", re.ASCII)
_INTERNAL_TLDS = {"home", "internal", "local", "localhost", "invalid", "test", "onion"}
_IPV6_GLOBAL_SPACE = ipaddress.IPv6Network("2000::/3")


def validate_public_hostname(value: str) -> str:
    hostname = value.strip().removesuffix(".").lower()
    if len(hostname) > 253 or not _HOSTNAME.fullmatch(hostname):
        raise ValueError("Für den IMAP-Server wird ein öffentlicher DNS-Hostname benötigt.")
    if hostname.rsplit(".", 1)[-1] in _INTERNAL_TLDS:
        raise ValueError("Für den IMAP-Server wird ein öffentlicher DNS-Hostname benötigt.")
    return hostname


def _is_public_ip(address):
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        return address in _IPV6_GLOBAL_SPACE and address.sixtofour is None and address.teredo is None
    return True


def resolve_public_host(host: str, port: int) -> tuple:
    """Resolve once and reject the hostname if any answer is unsuitable."""
    host = validate_public_hostname(host)
    answers = socket.getaddrinfo(
        host + ".",
        port,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    addresses = []
    for family, kind, protocol, _, address in answers:
        if family not in (socket.AF_INET, socket.AF_INET6) or not _is_public_ip(
            ipaddress.ip_address(address[0])
        ):
            raise ValueError("Der IMAP-Hostname hat keine ausschließlich öffentlichen Adressen.")
        if family == socket.AF_INET6 and address[3]:
            raise ValueError("Der IMAP-Hostname hat eine Adresse mit lokalem Geltungsbereich.")
        entry = (family, kind, protocol, address)
        if entry not in addresses:
            addresses.append(entry)
    if not addresses:
        raise ValueError("Der IMAP-Hostname hat keine öffentlichen Adressen.")
    return tuple(addresses)


class BoundedIMAP4_SSL(imaplib.IMAP4_SSL):
    def __init__(self, host, port, *, ssl_context, timeout, pinned_addresses=None):
        self.pinned_addresses = pinned_addresses
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def _create_socket(self, timeout):
        if self.pinned_addresses is None:
            return super()._create_socket(timeout)

        for family, kind, protocol, address in self.pinned_addresses[:4]:
            try:
                sock = socket.socket(family, kind, protocol)
            except OSError:
                continue
            try:
                sock.settimeout(timeout)
                sock.connect(address)
                return self.ssl_context.wrap_socket(sock, server_hostname=self.host)
            except ssl.SSLError:
                sock.close()
                raise
            except OSError:
                sock.close()
        raise OSError("Keine bestätigte IP-Adresse des IMAP-Servers ist erreichbar.")

    def read(self, size):
        if size > MAX_EML_BYTES:
            self.state = "LOGOUT"
            self.shutdown()
            raise self.abort("IMAP-Literal überschreitet das Größenlimit.")
        return super().read(size)
