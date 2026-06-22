#!/usr/bin/env python3
"""
flock_os adapter-Redis pub/sub Prometheus exporter (FLO-922 / FLO-586 §6 gap G3).

``redis_exporter`` does NOT emit ``PUBSUB NUMSUB`` / ``PUBSUB CHANNELS`` counts
natively — those have to be issued as discrete ``PUBSUB`` commands against the
adapter Redis. This is the small custom probe the FLO-586 design (§1.3 + §6 G3)
names: it connects to the DEDICATED adapter Redis (the WS-scale bottleneck per
[FLO-127](/FLO/issues/FLO-127)), periodically issues ``PUBSUB CHANNELS`` +
``PUBSUB NUMSUB`` for the socket.io events channel + the adapter's
``socket.io#...`` coordination channels, and exposes Prometheus text on a small
HTTP server.

This is the scrape source for the critical adapter alerts
``AdapterRedisNearMaxMemory`` / ``AdapterRedisEvicting`` /
``AdapterSubscribersLost`` (FLO-586 §3.4). The cache-Redis exporter is a
separate job (commodity; ``redis_exporter`` default).

Pure stdlib (``socket`` + ``http.server``) so it runs in any Python 3.12 image
with no extra deps — the same way ``flock_os.telemetry`` is import-clean. The
RESP wire protocol parsing here is intentionally minimal (one-line replies +
arrays of bulk strings); ``redis_exporter`` itself already emits the
``INFO clients`` / ``INFO memory`` gauges, so this probe focuses on what that
exporter can NOT do.

Runbook: docs/operations/production-instrumentation.md (FLO-922).

Usage::

    FLOCK_ADAPTER_REDIS_URL=redis://redis-adapter:6379 \
    FLOCK_SIO_ADAPTER_KEY=socket.io \
    FLOCK_PUBSUB_EXPORTER_PORT=9300 \
    python3 scripts/ops/redis-pubsub-exporter.py

Env:
    FLOCK_ADAPTER_REDIS_URL      adapter Redis URL (default redis://127.0.0.1:6379)
    FLOCK_SIO_ADAPTER_KEY        socket.io-adapter pub/sub channel prefix
                                 (default "socket.io"; must match the worker
                                 wiring — see realtime/adapters/flock_redis_adapter.js
                                 DEFAULT_KEY)
    FLOCK_PUBSUB_EXPORTER_PORT   /metrics listen port (default 9300)
    FLOCK_PUBSUB_SCRAPE_INTERVAL how often to poll PUBSUB (seconds, default 5)
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.parse
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 9300
DEFAULT_INTERVAL = 5
DEFAULT_ADAPTER_KEY = "socket.io"


# ---- minimal RESP client --------------------------------------------------- #
# A real Redis client (redis-py) is overkill for two PUBSUB commands and would
# add a runtime dep. The RESP protocol is small enough that we can read replies
# line-by-line. We support the three reply shapes this probe issues:
#
#   * Simple-string  ``+OK\r\n``
#   * Integer        ``:3\r\n``
#   * Array of bulk strings  ``*N\r\n$len\r\n<bytes>\r\n ...``
#
# Anything else raises so a protocol drift is loud, not silent.


def _readline(buf: bytearray, fd: socket.socket) -> bytes:
	"""Read one CRLF-terminated line, refilling from ``fd`` as needed."""
	while b"\r\n" not in buf:
		chunk = fd.recv(4096)
		if not chunk:
			raise ConnectionError("adapter redis closed the connection mid-reply")
		buf.extend(chunk)
	line, _, buf_tail = buf.partition(b"\r\n")
	del buf[: len(line) + 2]
	return line


def _read_n(buf: bytearray, fd: socket.socket, n: int) -> bytes:
	"""Read exactly ``n`` bytes plus the trailing CRLF."""
	while len(buf) < n + 2:
		chunk = fd.recv(4096)
		if not chunk:
			raise ConnectionError("adapter redis closed mid-bulk")
		buf.extend(chunk)
	data = bytes(buf[:n])
	del buf[: n + 2]
	return data


def _parse_reply(buf: bytearray, fd: socket.socket):
	"""Parse a single RESP reply from ``buf`` (refilling via ``fd``)."""
	line = _readline(buf, fd)
	if not line:
		raise ConnectionError("empty reply from adapter redis")
	tag = line[:1]
	body = line[1:]
	if tag == b"+":
		return body.decode()
	if tag == b"-":
		raise RuntimeError(f"adapter redis error reply: {body.decode(errors='replace')!r}")
	if tag == b":":
		return int(body)
	if tag == b"$":
		# Bulk string; -1 = nil.
		n = int(body)
		if n == -1:
			return None
		return _read_n(buf, fd, n)
	if tag == b"*":
		# Array; -1 = nil.
		n = int(body)
		if n == -1:
			return None
		return [_parse_reply(buf, fd) for _ in range(n)]
	raise RuntimeError(f"unsupported RESP tag {tag!r} (line={line!r})")


def _connect(host: str, port: int, password: str | None) -> socket.socket:
	sock = socket.create_connection((host, port), timeout=5)
	# Select the password's logical DB index 0 explicitly (Frappe's adapter
	# Redis always uses DB 0 — the events channel lives there).
	if password:
		sock.sendall(_encode_cmd("AUTH", password))
		# Drain AUTH reply before issuing more commands.
		buf = bytearray()
		_parse_reply(buf, sock)  # raises on -ERR
	return sock


def _encode_cmd(*args: str | int) -> bytes:
	parts = [f"*{len(args)}\r\n"]
	for a in args:
		s = str(a)
		parts.append(f"${len(s)}\r\n{s}\r\n")
	return "".join(parts).encode()


def _issue(sock: socket.socket, buf: bytearray, *args: str | int):
	"""Send a command + parse its reply (single call ↔ single reply)."""
	sock.sendall(_encode_cmd(*args))
	return _parse_reply(buf, sock)


# ---- probe ----------------------------------------------------------------- #


def parse_redis_url(url: str) -> tuple[str, int, str | None]:
	"""Return ``(host, port, password)`` for a ``redis://[pw@]host[:port]`` URL."""
	parsed = urllib.parse.urlparse(url)
	if parsed.scheme not in ("", "redis"):
		raise ValueError(f"adapter redis URL must be redis:// — got {url!r}")
	host = parsed.hostname or "127.0.0.1"
	port = parsed.port or 6379
	password = parsed.password or (None if not parsed.username else parsed.username)
	return host, port, password


class AdapterProbe:
	"""Periodically polls the adapter Redis + caches the latest values."""

	def __init__(self, host: str, port: int, password: str | None, adapter_key: str):
		self.host = host
		self.port = port
		self.password = password
		self.adapter_key = adapter_key
		self._lock = threading.Lock()
		self._latest: dict[str, float | int | list[str]] = {
			"flock_redis_adapter_pubsub_channels": 0.0,
			"flock_redis_adapter_pubsub_numsub_socketio": 0.0,
			"flock_redis_adapter_pubsub_adapter_subscribers": 0.0,
			"flock_redis_adapter_pubsub_numpat": 0.0,
			"flock_redis_adapter_up": 0.0,
			"flock_redis_adapter_channel_names": [],
		}
		self._last_error: str = ""

	def _probe_once(self) -> dict[str, float | int | list[str]]:
		sock = _connect(self.host, self.port, self.password)
		buf = bytearray()
		try:
			channels_reply = _issue(sock, buf, "PUBSUB", "CHANNELS")
			channels = [
				c.decode() if isinstance(c, (bytes, bytearray)) else str(c) for c in (channels_reply or [])
			]
			# PUBSUB NUMSUB takes a list of channel names to count subscribers
			# for. Always probe (a) the Frappe "events" channel — the design's
			# `redis.pubsub_numsub_socketio` metric (one subscriber per worker
			# expected, the AdapterSubscribersLost alert basis) — and (b) the
			# @socket.io/redis-adapter coordination channels discovered above.
			events_channels = ["events"]
			adapter_channels = self._build_numsub_targets(channels)
			numsub_events_reply = (
				_issue(sock, buf, "PUBSUB", "NUMSUB", *events_channels) if events_channels else []
			)
			numsub_adapter_reply = (
				_issue(sock, buf, "PUBSUB", "NUMSUB", *adapter_channels) if adapter_channels else []
			)
			# NUMPAT (pattern subscribers) — the adapter also uses PSUBSCRIBE
			# for the namespace fan-out; the count is informative (a sudden
			# drop = workers unsubscribed) but not a per-channel alert.
			numpat_reply = _issue(sock, buf, "PUBSUB", "NUMPAT")
			numpat = int(numpat_reply) if isinstance(numpat_reply, int) else 0
			events_map = _numsub_pairs_to_map(numsub_events_reply)
			adapter_map = _numsub_pairs_to_map(numsub_adapter_reply)
			events_subscribers = sum(events_map.values())
			adapter_subscribers = sum(adapter_map.values())
			return {
				"flock_redis_adapter_pubsub_channels": float(len(channels)),
				"flock_redis_adapter_pubsub_numsub_socketio": float(events_subscribers),
				"flock_redis_adapter_pubsub_adapter_subscribers": float(adapter_subscribers),
				"flock_redis_adapter_pubsub_numpat": float(numpat),
				"flock_redis_adapter_up": 1.0,
				"flock_redis_adapter_channel_names": sorted(channels),
			}
		finally:
			sock.close()

	def _build_numsub_targets(self, discovered: Iterable[str]) -> list[str]:
		"""Pick the channel names to NUMSUB from the live CHANNELS list + the
		adapter-key prefix. Probing every live channel can blow up a request at
		scale (the response channels are per-request ephemeral); probe
		(a) the Frappe ``events`` channel + (b) every channel that starts with
		the socket.io-adapter key — which @socket.io/redis-adapter v8 keys its
		coordination channels under (``<key>-request#/<ns>#`` and
		``<key>-response#/<ns>#<reqId>``). Match any separator (``-``, ``/``,
		``#``) so the AdapterSubscribersLost alert sees every subscriber
		whether the cluster runs v7 (``<key>/``) or v8 (``<key>-request#/``).
		"""
		key = self.adapter_key
		targets = {"events"}
		for ch in discovered:
			s = ch if isinstance(ch, str) else str(ch)
			if s == key or s.startswith(key + "-") or s.startswith(key + "/") or s.startswith(key + "#"):
				targets.add(s)
		return sorted(targets)

	def poll(self) -> None:
		"""One probe pass — stores the result + records any error."""
		try:
			sample = self._probe_once()
			with self._lock:
				self._latest = sample
				self._last_error = ""
		except Exception as exc:  # noqa: BLE001 — surfaced as flock_redis_adapter_up=0
			with self._lock:
				self._latest["flock_redis_adapter_up"] = 0.0
				self._last_error = f"{type(exc).__name__}: {exc}"

	def latest(self) -> tuple[dict[str, float | int | list[str]], str]:
		with self._lock:
			return dict(self._latest), self._last_error


def _numsub_pairs_to_map(reply) -> dict[str, int]:
	"""``PUBSUB NUMSUB a b`` returns ``[a, count_a, b, count_b]`` (flat array)."""
	if not isinstance(reply, list) or len(reply) % 2 != 0:
		return {}
	out: dict[str, int] = {}
	for i in range(0, len(reply), 2):
		name = reply[i]
		count = reply[i + 1]
		name_s = name.decode() if isinstance(name, (bytes, bytearray)) else str(name)
		out[name_s] = int(count) if count is not None else 0
	return out


# ---- exposition ----------------------------------------------------------- #

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class _FastBindHTTPServer(ThreadingHTTPServer):
	"""``ThreadingHTTPServer`` without ``HTTPServer``'s 30s+ ``socket.getfqdn()``.

	On macOS with a broken reverse-DNS / mDNS config, ``socketserver.HTTPServer.
	server_bind()`` calls ``socket.getfqdn()`` which can block for 30+ seconds
	while mDNS times out. We don't need the ``server_name`` attribute (we never
	emit ``Server:`` headers from it), so override ``server_bind`` to skip the
	``getfqdn`` call entirely. ``TCPServer.server_bind`` (which we still call)
	does the real ``socket.bind`` + address retrieval.
	"""

	def server_bind(self):
		# TCPServer.server_bind does the socket bind + stores server_address;
		# HTTPServer.layer adds the getfqdn() lookup we explicitly want to skip.
		socketserver_TCPServer_server_bind(self)


def socketserver_TCPServer_server_bind(server):
	# Inlined copy of socketserver.TCPServer.server_bind so we don't poke at a
	# private symbol; the stdlib impl is small + stable.
	if server.allow_reuse_address and hasattr(socket, "SO_REUSEADDR"):
		server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
	server.socket.bind(server.server_address)
	server.server_address = server.socket.getsockname()


def render_prometheus(sample: dict[str, float | int | list[str]], error: str) -> str:
	"""Render the cached probe sample as Prometheus text exposition."""
	# Pull the sample values up front so the f-string emission lines stay
	# under the ruff line-length budget without sacrificing the long metric
	# names (which encode the FLO-586 §1.3 taxonomy).
	channels = sample.get("flock_redis_adapter_pubsub_channels", 0)
	numsub = sample.get("flock_redis_adapter_pubsub_numsub_socketio", 0)
	adapter_subs = sample.get("flock_redis_adapter_pubsub_adapter_subscribers", 0)
	numpat = sample.get("flock_redis_adapter_pubsub_numpat", 0)
	up = sample.get("flock_redis_adapter_up", 0)
	lines = [
		"# HELP flock_redis_adapter_pubsub_channels Active pub/sub channels on the adapter Redis.",
		"# TYPE flock_redis_adapter_pubsub_channels gauge",
		f"flock_redis_adapter_pubsub_channels {channels}",
		"",
		"# HELP flock_redis_adapter_pubsub_numsub_socketio 'events' channel subscribers (one per worker).",
		"# TYPE flock_redis_adapter_pubsub_numsub_socketio gauge",
		f"flock_redis_adapter_pubsub_numsub_socketio {numsub}",
		"",
		"# HELP flock_redis_adapter_pubsub_adapter_subscribers Adapter channel subscribers (fan-out).",
		"# TYPE flock_redis_adapter_pubsub_adapter_subscribers gauge",
		f"flock_redis_adapter_pubsub_adapter_subscribers {adapter_subs}",
		"",
		"# HELP flock_redis_adapter_pubsub_numpat Pattern subscribers (PSUBSCRIBE) on the adapter Redis.",
		"# TYPE flock_redis_adapter_pubsub_numpat gauge",
		f"flock_redis_adapter_pubsub_numpat {numpat}",
		"",
		"# HELP flock_redis_adapter_up 1 if the last probe succeeded, 0 otherwise.",
		"# TYPE flock_redis_adapter_up gauge",
		f"flock_redis_adapter_up {up}",
	]
	channel_names = sample.get("flock_redis_adapter_channel_names", [])
	if isinstance(channel_names, list):
		# Cap so a runaway cluster can't OOM the scrape.
		for ch in channel_names[:64]:
			safe = _escape_label(ch)
			lines.append(f'flock_redis_adapter_channel_present{{channel="{safe}"}} 1')
	if error:
		lines.append("")
		lines.append(f"# last_probe_error: {error}")
	lines.append("")
	return "\n".join(lines)


def _escape_label(value: str) -> str:
	return value.replace("\\", "\\\\").replace('"', '\\"')


def make_handler(probe: AdapterProbe):
	class _Handler(BaseHTTPRequestHandler):
		def do_GET(self):  # noqa: N802 — http.server contract
			if self.path not in ("/metrics", "/"):
				self.send_response(404)
				self.send_header("Content-Type", "text/plain")
				self.end_headers()
				self.wfile.write(b"not found\n")
				return
			sample, error = probe.latest()
			body = render_prometheus(sample, error).encode()
			self.send_response(200)
			self.send_header("Content-Type", CONTENT_TYPE)
			self.send_header("Content-Length", str(len(body)))
			self.end_headers()
			self.wfile.write(body)

		def log_message(self, fmt, *args):  # noqa: D401, ANN001 — silence default logging
			sys.stderr.write("[redis-pubsub-exporter] " + (fmt % args) + "\n")

	return _Handler


def main() -> int:
	url = os.environ.get("FLOCK_ADAPTER_REDIS_URL", "redis://127.0.0.1:6379")
	adapter_key = os.environ.get("FLOCK_SIO_ADAPTER_KEY", DEFAULT_ADAPTER_KEY)
	port = int(os.environ.get("FLOCK_PUBSUB_EXPORTER_PORT", DEFAULT_PORT))
	interval = float(os.environ.get("FLOCK_PUBSUB_SCRAPE_INTERVAL", DEFAULT_INTERVAL))
	host, rport, password = parse_redis_url(url)
	print(f"redis-pubsub-exporter: starting (adapter={url}, port={port})", file=sys.stderr, flush=True)
	probe = AdapterProbe(host=host, port=rport, password=password, adapter_key=adapter_key)

	# Construct the HTTP server FIRST so it binds + serves immediately, BEFORE
	# the probe thread spins up. A misconfigured / unreachable adapter Redis
	# (host that blocks on getaddrinfo / connect retry) would otherwise stall
	# the probe thread long enough to delay the bind past a scraper's first
	# connect. The server returns up=0 until the first successful poll lands —
	# which is exactly the desired degraded posture (failing loud, not failing
	# silent), and never blocks the realtime tier.
	server = _FastBindHTTPServer(("0.0.0.0", port), make_handler(probe))
	print(f"redis-pubsub-exporter: bound :{port}", file=sys.stderr, flush=True)

	def _loop():
		# Initial probe so the first scrape has data, then on the configured
		# cadence forever. Errors never stop the loop — a transient Redis
		# outage is surfaced as flock_redis_adapter_up=0, not a dead exporter.
		while True:
			probe.poll()
			time.sleep(interval)

	t = threading.Thread(target=_loop, daemon=True)
	t.start()

	print(
		f"redis-pubsub-exporter: /metrics listening on :{port} (adapter={url}, key={adapter_key})",
		file=sys.stderr,
		flush=True,
	)
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		pass
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
