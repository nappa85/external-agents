"""The coding sandbox's only way out: an HTTPS CONNECT proxy on a Unix socket.

Only allowlisted hosts on port 443 are tunnelled, and the tunnel's TLS ClientHello must
name the same host, so an allowed CDN address cannot front another site.
"""

import contextlib
import selectors
import socket
import sys
import threading
import time

from .common import bind_unix

# The largest ClientHello the proxy reads before giving up on finding its server name.
MAX_HELLO = 16384 + 5


class Malformed(Exception):
    pass


class Reader:
    """Reads length-prefixed TLS fields, refusing any that runs past its container."""

    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, size):
        if self.pos + size > len(self.data):
            raise Malformed
        chunk = self.data[self.pos : self.pos + size]
        self.pos += size
        return chunk

    def number(self, size):
        return int.from_bytes(self.take(size), "big")

    def vector(self, length_size):
        return Reader(self.take(self.number(length_size)))

    def done(self):
        return self.pos == len(self.data)


def handshake_bytes(data):
    """The handshake bytes of the TLS records at the start of `data`, which may split one
    handshake message across several records; None when a record is not a handshake."""
    out, pos = b"", 0
    while pos + 5 <= len(data):
        if data[pos] != 0x16:
            return None
        size = int.from_bytes(data[pos + 3 : pos + 5], "big")
        if pos + 5 + size > len(data):
            break
        out += data[pos + 5 : pos + 5 + size]
        pos += 5 + size
    return out


def client_hello_sni(data):
    """The server name in a TLS ClientHello, or None when `data` is not a complete,
    well-formed ClientHello with exactly one host_name entry. Every length is checked
    against its container, and the ClientHello may span several records."""
    try:
        handshake = handshake_bytes(data)
        if handshake is None or len(handshake) < 4 or handshake[0] != 0x01:
            return None
        hello = Reader(handshake[1:])
        hello = hello.vector(3)
        hello.take(2 + 32)  # version, random
        hello.vector(1)  # session id
        hello.vector(2)  # cipher suites
        hello.vector(1)  # compression methods
        extensions = hello.vector(2)
        if not hello.done():
            return None
        names = []
        while not extensions.done():
            kind = extensions.number(2)
            body = extensions.vector(2)
            if kind != 0:  # server_name
                continue
            entries = body.vector(2)
            if not body.done():
                return None
            while not entries.done():
                name_type = entries.number(1)
                name = entries.vector(2)
                if name_type == 0:
                    names.append(name.data.decode("ascii"))
        return names[0].lower() if len(names) == 1 else None
    except (Malformed, UnicodeDecodeError):
        return None


class Proxy(threading.Thread):
    """HTTPS CONNECT proxy on a Unix socket: only allowlisted hosts on port 443, and the
    tunnel's TLS ClientHello must name the same host (no fronting another site through an
    allowed CDN address). Bounded: MAX_TUNNELS at once, a header deadline, an idle limit."""

    MAX_TUNNELS = 32
    HEADER_SECONDS = 10
    IDLE_SECONDS = 600
    MAX_HEADER = 8192

    def __init__(self, path, hosts, log_hosts):
        super().__init__(daemon=True)
        self.slots = threading.BoundedSemaphore(self.MAX_TUNNELS)
        self.hosts = {h.lower() for h in hosts}
        self.log_hosts = log_hosts
        self.server = bind_unix(path, 64)

    def run(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            if not self.slots.acquire(blocking=False):
                client.close()
                continue
            try:
                threading.Thread(target=self.guarded, args=(client,), daemon=True).start()
            except RuntimeError:
                self.slots.release()
                client.close()

    def guarded(self, client):
        try:
            self.serve(client)
        except OSError:
            pass
        finally:
            self.slots.release()

    def allowed(self, target):
        host, sep, port = target.rpartition(":")
        return bool(sep) and port == "443" and host.lower() in self.hosts

    @staticmethod
    def receive(client, deadline):
        """One read that ends by `deadline` (time.monotonic()); b"" when time is up."""
        left = deadline - time.monotonic()
        if left <= 0:
            return b""
        client.settimeout(left)
        try:
            return client.recv(4096)
        except TimeoutError:
            return b""

    def serve(self, client):
        with contextlib.closing(client):
            # Absolute deadlines: trickled bytes cannot hold a slot past them.
            deadline = time.monotonic() + self.HEADER_SECONDS
            head = b""
            while b"\r\n\r\n" not in head and len(head) <= self.MAX_HEADER:
                chunk = self.receive(client, deadline)
                if not chunk:
                    return
                head += chunk
            if b"\r\n\r\n" not in head or head.index(b"\r\n\r\n") > self.MAX_HEADER:
                client.sendall(b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n")
                return
            head, rest = head.split(b"\r\n\r\n", 1)
            request, *fields = head.decode("latin-1").split("\r\n")
            parts = request.split(" ")
            if (
                len(parts) != 3
                or parts[0] != "CONNECT"
                or parts[2] not in {"HTTP/1.0", "HTTP/1.1"}
                or any(":" not in field or field[0] in " \t" for field in fields)
            ):
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return
            target = parts[1]
            if self.log_hosts:
                print(f"xagents-proxy: CONNECT {target!r}", file=sys.stderr)
            if not self.allowed(target):
                # repr: the target is the agent's text, which must not write control
                # characters into the coordinator's terminal or log.
                print(f"xagents-proxy: refused {target!r}", file=sys.stderr)
                client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            host = target.rpartition(":")[0].lower()
            # The ClientHello comes after the 200, so it is read before any upstream byte.
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            hello = rest
            deadline = time.monotonic() + self.HEADER_SECONDS
            while client_hello_sni(hello) is None and len(hello) < MAX_HELLO:
                chunk = self.receive(client, deadline)
                if not chunk:
                    return
                hello += chunk
            if client_hello_sni(hello) != host:
                print(
                    f"xagents-proxy: refused TLS name {client_hello_sni(hello)!r} in {target!r}",
                    file=sys.stderr,
                )
                return
            try:
                upstream = socket.create_connection((host, 443), timeout=30)
            except OSError:
                return
            with contextlib.closing(upstream):
                upstream.sendall(hello)
                relay(client, upstream, self.IDLE_SECONDS)


def relay(a, b, idle):
    """Copy both ways until either side closes or nothing moves for `idle` seconds."""
    a.settimeout(idle)
    b.settimeout(idle)
    with selectors.DefaultSelector() as sel:
        sel.register(a, selectors.EVENT_READ, b)
        sel.register(b, selectors.EVENT_READ, a)
        while True:
            ready = sel.select(timeout=idle)
            if not ready:
                return
            for key, _ in ready:
                data = key.fileobj.recv(65536)
                if not data:
                    return
                key.data.sendall(data)
