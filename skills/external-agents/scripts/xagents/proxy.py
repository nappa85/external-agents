"""The coding sandbox's only way out: an HTTPS CONNECT proxy on a Unix socket.

Only allowlisted hosts on port 443 are tunnelled, and the tunnel's TLS ClientHello must
name the same host, so an allowed CDN address cannot front another site.
"""

import contextlib
import os
import selectors
import socket
import sys
import threading
import time


def client_hello_sni(data):
    """The server name in a TLS ClientHello, or None when `data` is not a complete
    ClientHello with exactly one host_name entry."""
    try:
        if data[0] != 0x16 or data[5] != 0x01:
            return None
        record = int.from_bytes(data[3:5], "big")
        if len(data) < 5 + record:
            return None
        pos = 5 + 4 + 2 + 32  # record header, handshake header, version, random
        pos += 1 + data[pos]  # session id
        pos += 2 + int.from_bytes(data[pos : pos + 2], "big")  # cipher suites
        pos += 1 + data[pos]  # compression methods
        end = pos + 2 + int.from_bytes(data[pos : pos + 2], "big")
        pos += 2
        names = []
        while pos + 4 <= end:
            kind = int.from_bytes(data[pos : pos + 2], "big")
            size = int.from_bytes(data[pos + 2 : pos + 4], "big")
            body = data[pos + 4 : pos + 4 + size]
            if kind == 0:  # server_name
                inner = 2
                while inner + 3 <= len(body):
                    name_type = body[inner]
                    length = int.from_bytes(body[inner + 1 : inner + 3], "big")
                    if name_type == 0:
                        names.append(body[inner + 3 : inner + 3 + length].decode("ascii"))
                    inner += 3 + length
            pos += 4 + size
        return names[0].lower() if len(names) == 1 else None
    except (IndexError, UnicodeDecodeError):
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
        self.server = socket.socket(socket.AF_UNIX)
        # A relative name keeps long state paths under the 108-byte socket path limit.
        cwd = os.getcwd()
        os.chdir(path.parent)
        try:
            self.server.bind(path.name)
        finally:
            os.chdir(cwd)
        self.server.listen(64)

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
                print(f"xagents-proxy: CONNECT {target}", file=sys.stderr)
            if not self.allowed(target):
                print(f"xagents-proxy: refused {target}", file=sys.stderr)
                client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            host = target.rpartition(":")[0].lower()
            # The ClientHello comes after the 200, so it is read before any upstream byte.
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            hello = rest
            deadline = time.monotonic() + self.HEADER_SECONDS
            while client_hello_sni(hello) is None and len(hello) < 16384 + 5:
                chunk = self.receive(client, deadline)
                if not chunk:
                    return
                hello += chunk
            if client_hello_sni(hello) != host:
                print(
                    f"xagents-proxy: refused TLS name {client_hello_sni(hello)} in {target}",
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
