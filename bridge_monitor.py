#!/usr/bin/env python3
"""
Socat Bridge Monitor — web UI for Klipper UART-over-TCP bridges.

CLIENT side  (this host, Odroid): manages klipper-bridge-client@700x.service via systemd.
SERVER side  (AD5X printer):      polls printer_api.py over HTTP (no SSH on every refresh).

Usage:
  python3 bridge_monitor.py [--port 8080] [--printer-ip 192.168.100.1]
                             [--api-port 7001] [--ports 7002,7004,7005,7007]

Access:   http://<odroid-ip>:8080
JSON API: http://<odroid-ip>:8080/api/client-status
"""

import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote_plus, urlparse

PRINTER_IP       = "192.168.100.1"
PRINTER_API_PORT = 8000
LISTEN_PORT      = 8888

VERSION           = "0.9"
CLIENT_ACTION_LOG = "/tmp/bridge-monitor-action.log"

# ports.conf — JSON file shared conceptually with printer side.
# Format: [{"port": 7002, "enabled": true}, ...]
_HERE      = os.path.dirname(os.path.abspath(__file__))
PORTS_CONF = os.path.join(_HERE, "ports.conf")
_DEFAULT_PORTS = [
    {"port": 7002, "enabled": True},
    {"port": 7004, "enabled": True},
    {"port": 7005, "enabled": True},
    {"port": 7007, "enabled": True},
]


def load_ports_conf() -> list:
    try:
        with open(PORTS_CONF) as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return [{"port": int(e["port"]), "enabled": bool(e.get("enabled", True))}
                    for e in data if "port" in e]
    except Exception:
        pass
    return list(_DEFAULT_PORTS)


def save_ports_conf(entries: list) -> bool:
    try:
        with open(PORTS_CONF, "w") as f:
            json.dump(entries, f, indent=2)
        return True
    except Exception:
        return False


def _rebuild_port_list():
    global BRIDGE_PORTS, _PORTS_CONF_ALL
    _PORTS_CONF_ALL = load_ports_conf()
    BRIDGE_PORTS    = [e["port"] for e in _PORTS_CONF_ALL if e["enabled"]]


_PORTS_CONF_ALL: list = []
BRIDGE_PORTS:    list = []
_rebuild_port_list()
_client_alog: list = []

CLIENT_SVCS_CONF = os.path.join(_HERE, "client_services.conf")
_DEFAULT_CLIENT_SERVICES = [
    {"name": "klipper",   "unit": "klipper.service"},
    {"name": "moonraker", "unit": "moonraker.service"},
    {"name": "nginx",     "unit": "nginx.service"},
]


def load_client_services() -> list:
    try:
        with open(CLIENT_SVCS_CONF) as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return [{"name": str(e["name"]), "unit": str(e["unit"])}
                    for e in data if "name" in e and "unit" in e]
    except Exception:
        pass
    save_client_services(list(_DEFAULT_CLIENT_SERVICES))
    return list(_DEFAULT_CLIENT_SERVICES)


def save_client_services(entries: list) -> bool:
    try:
        with open(CLIENT_SVCS_CONF, "w") as f:
            json.dump(entries, f, indent=2)
        return True
    except Exception:
        return False


def _rebuild_client_svc_list():
    global CLIENT_SERVICES
    CLIENT_SERVICES = load_client_services()


CLIENT_SERVICES: list = []
_rebuild_client_svc_list()

LOG = logging.getLogger("bridge-monitor")

HTML_DIR  = os.path.join(_HERE, "html")
HTML_FILE = os.path.join(HTML_DIR, "index.html")

_MIME = {
    ".html":        "text/html; charset=utf-8",
    ".css":         "text/css",
    ".js":          "application/javascript",
    ".png":         "image/png",
    ".svg":         "image/svg+xml",
    ".ico":         "image/x-icon",
    ".webmanifest": "application/manifest+json",
    ".json":        "application/json",
}


def _get_local_hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "odroid"


def _get_local_ip() -> str:
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        if s:
            try: s.close()
            except Exception: pass


def _client_alog_append(line: str):
    _client_alog.append(line)
    if len(_client_alog) > 500:
        _client_alog[:] = _client_alog[-500:]


def _flush_client_alog():
    try:
        with open(CLIENT_ACTION_LOG, "w") as f:
            f.write("\n".join(_client_alog) + "\n")
    except Exception:
        pass


def _printer_url(path: str) -> str:
    return f"http://{PRINTER_IP}:{PRINTER_API_PORT}{path}"


def printer_get(path: str, timeout: int = 5) -> tuple:
    try:
        r = urllib.request.urlopen(_printer_url(path), timeout=timeout)
        return True, json.loads(r.read().decode())
    except Exception as e:
        LOG.warning("printer GET %s: %s", path, e)
        return False, {"error": str(e)}


def printer_post(path: str, params: dict, timeout: int = 10) -> tuple:
    body = "&".join(f"{k}={v}" for k, v in params.items()).encode()
    req  = urllib.request.Request(
        _printer_url(path), data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return True, json.loads(r.read().decode())
    except Exception as e:
        LOG.warning("printer POST %s: %s", path, e)
        return False, {"error": str(e)}


def _printer_post_json(path: str, raw_body: bytes, timeout: int = 10) -> tuple:
    """POST raw JSON body to printer API."""
    req = urllib.request.Request(
        _printer_url(path), data=raw_body,
        headers={"Content-Type": "application/json"},
    )
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return True, json.loads(r.read().decode())
    except Exception as e:
        LOG.warning("printer POST JSON %s: %s", path, e)
        return False, {"error": str(e)}


def _client_tcp_connected(port: int) -> bool:
    """True if there is an ESTABLISHED TCP connection involving our port."""
    hex_port = f"{port:04X}"
    for tcp_file in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            for line in open(tcp_file).readlines()[1:]:
                parts = line.split()
                if len(parts) < 4:
                    continue
                remote_addr = parts[2]
                state       = parts[3]
                rport = remote_addr.split(":")[1] if ":" in remote_addr else ""
                if rport.upper() == hex_port and state == "01":
                    return True
        except Exception:
            pass
    return False


def _systemd_show(unit: str) -> dict:
    props = "ActiveState,SubState,MainPID,ActiveEnterTimestamp"
    try:
        r = subprocess.run(
            ["systemctl", "show", unit, f"--property={props}"],
            capture_output=True, text=True, timeout=3,
        )
        result = {}
        for line in r.stdout.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                result[k] = v
        return result
    except Exception:
        return {}


def _fmt_since_systemd(ts: str) -> str:
    """Convert systemd timestamp to YYYY-MM-DD HH:MM:SS."""
    if not ts:
        return ""
    parts = ts.strip().split()
    for i, p in enumerate(parts):
        if len(p) == 10 and p[4] == "-" and p[7] == "-":
            time_part = parts[i + 1] if i + 1 < len(parts) else ""
            time_part = time_part.split(".")[0]
            return f"{p} {time_part}"
    return ts


def client_bridge_status(port: int) -> dict:
    unit = f"klipper-bridge-client@{port}.service"
    res  = {"port": port, "unit": unit, "ok": False,
            "active": "unknown", "substate": "", "pid": "", "since": "",
            "connected": False}
    try:
        r = subprocess.run(["systemctl", "is-active", unit],
                           capture_output=True, text=True, timeout=3)
        res["active"] = r.stdout.strip()
        res["ok"]     = (res["active"] == "active")
        show = _systemd_show(unit)
        res["substate"] = show.get("SubState", "")
        raw_pid = show.get("MainPID", "0")
        res["pid"]   = raw_pid if raw_pid not in ("0", "") else ""
        res["since"] = _fmt_since_systemd(show.get("ActiveEnterTimestamp", ""))
        if res["ok"]:
            res["connected"] = _client_tcp_connected(port)
    except Exception as e:
        res["active"] = "error"
        res["error"]  = str(e)
    return res


def client_svc_status(unit: str) -> dict:
    res = {"unit": unit, "ok": False, "active": "unknown",
           "substate": "", "pid": "", "since": ""}
    try:
        r = subprocess.run(["systemctl", "is-active", unit],
                           capture_output=True, text=True, timeout=3)
        res["active"] = r.stdout.strip()
        res["ok"]     = (res["active"] == "active")
        show = _systemd_show(unit)
        raw_pid = show.get("MainPID", "0")
        res["pid"]   = raw_pid if raw_pid not in ("0", "") else ""
        res["since"] = _fmt_since_systemd(show.get("ActiveEnterTimestamp", ""))
    except Exception as e:
        res["active"] = "error"
        res["error"]  = str(e)
    return res


def client_uptime() -> dict:
    try:
        raw  = open("/proc/uptime").read().strip()
        secs = float(raw.split()[0])
        h, r = divmod(int(secs), 3600)
        m    = r // 60
        pretty = f"{h}h {m}m"
    except Exception:
        pretty = "unknown"
    try:
        load = open("/proc/loadavg").read().strip()
    except Exception:
        load = ""
    mem = {}
    try:
        vals = {}
        for line in open("/proc/meminfo"):
            parts = line.split()
            if len(parts) >= 2:
                vals[parts[0].rstrip(':')] = int(parts[1])
        total = vals.get("MemTotal", 0)
        avail = vals.get("MemAvailable",
                         vals.get("MemFree", 0) + vals.get("Buffers", 0) + vals.get("Cached", 0))
        mem = {"used_mb": max(0, total - avail) // 1024, "total_mb": total // 1024}
    except Exception:
        pass
    return {"pretty": pretty, "loadavg": load, "mem": mem}


def apply_ports_conf_client() -> list:
    """Start/stop klipper-bridge-client@PORT systemd units to match ports.conf."""
    log = []
    def emit(s):
        log.append(s)
        LOG.info("%s", s)

    emit(f"[ports] applying client ports.conf: {_PORTS_CONF_ALL}")
    # Collect currently active bridge units
    try:
        r = subprocess.run(
            ["systemctl", "list-units", "--state=active", "--no-legend",
             "klipper-bridge-client@*.service"],
            capture_output=True, text=True, timeout=5)
        active_ports = set()
        for line in r.stdout.splitlines():
            # line: "klipper-bridge-client@7002.service loaded active running ..."
            part = line.strip().split()[0] if line.strip() else ""
            try:
                active_ports.add(int(part.split("@")[1].split(".")[0]))
            except Exception:
                pass
    except Exception:
        active_ports = set()

    enabled_ports = set(BRIDGE_PORTS)
    all_ports     = {e["port"] for e in _PORTS_CONF_ALL}

    # Stop units that are active but should be disabled
    for port in active_ports - enabled_ports:
        emit(f"[ports] stopping client bridge port {port} (disabled)")
        try:
            r = subprocess.run(["sudo", "systemctl", "stop",
                                f"klipper-bridge-client@{port}.service"],
                               capture_output=True, text=True, timeout=10)
            emit((r.stdout + r.stderr).strip() or "ok")
        except Exception as e:
            emit(f"ERROR: {e}")

    # Start units that should be enabled but aren't active
    for port in enabled_ports - active_ports:
        emit(f"[ports] starting client bridge port {port}")
        try:
            r = subprocess.run(["sudo", "systemctl", "start",
                                f"klipper-bridge-client@{port}.service"],
                               capture_output=True, text=True, timeout=10)
            emit((r.stdout + r.stderr).strip() or "ok")
        except Exception as e:
            emit(f"ERROR: {e}")

    emit("[ports] done")
    return log


def do_client_action(port: int, action: str) -> str:
    unit = f"klipper-bridge-client@{port}.service"
    _client_alog_append(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} {action} bridge:{port} ===")
    try:
        r = subprocess.run(["sudo", "systemctl", action, unit],
                           capture_output=True, text=True, timeout=10)
        out = (r.stdout + r.stderr).strip() or "ok"
        for line in out.splitlines():
            _client_alog_append(f"  {line}")
        _client_alog_append("=== done ===")
        _flush_client_alog()
        return out
    except Exception as e:
        _client_alog_append(f"ERROR: {e}")
        _client_alog_append("=== done ===")
        _flush_client_alog()
        return str(e)


def do_client_svc_action(unit: str, action: str) -> str:
    _client_alog_append(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} {action} svc:{unit} ===")
    try:
        r = subprocess.run(["sudo", "systemctl", action, unit],
                           capture_output=True, text=True, timeout=10)
        out = (r.stdout + r.stderr).strip() or "ok"
        for line in out.splitlines():
            _client_alog_append(f"  {line}")
        _client_alog_append("=== done ===")
        _flush_client_alog()
        return out
    except Exception as e:
        _client_alog_append(f"ERROR: {e}")
        _client_alog_append("=== done ===")
        _flush_client_alog()
        return str(e)


def client_bridge_journal(port: int, lines: int = 60) -> list:
    unit = f"klipper-bridge-client@{port}.service"
    try:
        r = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "--output=short"],
            capture_output=True, text=True, timeout=5)
        return (r.stdout or "(no logs)").splitlines()
    except Exception as e:
        return [str(e)]


def client_svc_journal(unit: str, lines: int = 60) -> list:
    try:
        r = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "--output=short"],
            capture_output=True, text=True, timeout=5)
        return (r.stdout or "(no logs)").splitlines()
    except Exception as e:
        return [str(e)]


def _build_client_status() -> dict:
    bridges  = {port: client_bridge_status(port) for port in BRIDGE_PORTS}
    services = {e["unit"]: client_svc_status(e["unit"]) for e in CLIENT_SERVICES}
    upt      = client_uptime()
    return {
        "ok":       True,
        "hostname": _get_local_hostname(),
        "ip":       _get_local_ip(),
        "bridges":  bridges,
        "services": services,
        "uptime":   upt["pretty"],
        "loadavg":  upt["loadavg"],
        "mem":      upt["mem"],
    }


def _send_json(h, data: dict, code: int = 200):
    body = json.dumps(data, ensure_ascii=False).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Access-Control-Allow-Origin", "*")
    h.end_headers()
    h.wfile.write(body)


def _send_html(h, content: bytes, code: int = 200):
    h.send_response(code)
    h.send_header("Content-Type", "text/html; charset=utf-8")
    h.send_header("Content-Length", str(len(content)))
    h.end_headers()
    h.wfile.write(content)


def _parse_post(raw: bytes) -> dict:
    params = {}
    for pair in raw.decode("utf-8", errors="replace").split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            params[k.strip()] = unquote_plus(v.strip())
    return params


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)
        path   = parsed.path

        if path in ("/", "/index.html"):
            try:
                html = open(HTML_FILE, "rb").read()
            except Exception:
                html = b"<h1>html/index.html not found</h1>"
            cfg    = json.dumps({
                "printer_ip":      PRINTER_IP,
                "ports":           BRIDGE_PORTS,
                "ports_conf":      _PORTS_CONF_ALL,
                "client_svcs":     CLIENT_SERVICES,
                "version_monitor": VERSION,
            })
            inject = ("<script>window._BM_CFG=" + cfg + ";</script>").encode()
            html   = html.replace(b"</head>", inject + b"</head>", 1)
            _send_html(self, html)
            return

        # ── Static files from html/ directory ─────────────────────────────
        safe = os.path.normpath(path.lstrip("/"))
        if ".." not in safe:
            disk = os.path.join(HTML_DIR, safe)
            if os.path.isfile(disk):
                ext  = os.path.splitext(disk)[1].lower()
                mime = _MIME.get(ext, "application/octet-stream")
                data = open(disk, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

        if path == "/api/client-status":
            _send_json(self, _build_client_status())

        elif path == "/api/server-status":
            ok, data = printer_get("/api/status")
            if not ok:
                data["ok"] = False
            _send_json(self, data)

        elif path == "/api/logs/client-bridge":
            port = int(qs.get("port", [BRIDGE_PORTS[0]])[0])
            n    = int(qs.get("n", ["60"])[0])
            _send_json(self, {"ok": True, "lines": client_bridge_journal(port, n)})

        elif path == "/api/logs/client-svc":
            unit = qs.get("unit", [""])[0].strip()
            n    = int(qs.get("n", ["60"])[0])
            _send_json(self, {"ok": True, "lines": client_svc_journal(unit, n)})

        elif path == "/api/logs/action":
            n = int(qs.get("n", ["100"])[0])
            ok, data = printer_get(f"/api/logs/action?n={n}")
            _send_json(self, data if ok else {"ok": False, "lines": [], "error": data.get("error")})

        elif path == "/api/logs/client-action":
            n = int(qs.get("n", ["100"])[0])
            if _client_alog:
                lines = list(_client_alog[-n:])
            else:
                try:
                    lines = open(CLIENT_ACTION_LOG).read().strip().splitlines()[-n:]
                except Exception:
                    lines = ["(no client action log yet)"]
            _send_json(self, {"ok": True, "lines": lines, "n": n})

        elif path == "/api/logs/server-svc":
            name = qs.get("name", [""])[0].strip()
            n    = int(qs.get("n", ["60"])[0])
            ok, data = printer_get(f"/api/logs/service?name={name}&n={n}")
            _send_json(self, data if ok else {"ok": False, "lines": [], "error": data.get("error")})

        elif path == "/api/ports-config":
            _send_json(self, {"ok": True, "ports": _PORTS_CONF_ALL})

        elif path == "/api/server-ports-config":
            ok, data = printer_get("/api/ports-config")
            _send_json(self, data if ok else {"ok": False, "error": data.get("error")})

        elif path == "/api/server-tcp-fwds-config":
            ok, data = printer_get("/api/tcp-fwds-config")
            _send_json(self, data if ok else {"ok": False, "error": data.get("error")})

        elif path == "/api/server-tcp-fwds":
            ok, data = printer_get("/api/tcp-fwds")
            _send_json(self, data if ok else {"ok": False, "error": data.get("error")})

        elif path == "/api/client-services-config":
            _send_json(self, {"ok": True, "services": CLIENT_SERVICES})

        elif path == "/api/server-services-config":
            ok, data = printer_get("/api/services-config")
            _send_json(self, data if ok else {"ok": False, "error": data.get("error")})

        elif path == "/api/logs/server-bridge":
            port = int(qs.get("port", [BRIDGE_PORTS[0]])[0])
            n    = int(qs.get("n", ["60"])[0])
            ok, data = printer_get(f"/api/logs/bridge?port={port}&n={n}")
            _send_json(self, data if ok else {"ok": False, "lines": [], "error": data.get("error")})

        elif path == "/api/logs/server-tcpfwd":
            port = int(qs.get("port", ["0"])[0])
            n    = int(qs.get("n", ["60"])[0])
            ok, data = printer_get(f"/api/logs/tcpfwd?port={port}&n={n}")
            _send_json(self, data if ok else {"ok": False, "lines": [], "error": data.get("error")})

        else:
            _send_html(self, b'<meta http-equiv="refresh" content="0;url=/">')

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length)
        params = _parse_post(raw)
        path   = urlparse(self.path).path

        LOG.info("POST %s  params=%r", path, params)

        if path == "/api/client-action":
            port_str = params.get("port", "")
            action   = params.get("action", "").strip()
            if action not in ("start", "stop", "restart"):
                _send_json(self, {"ok": False, "error": "invalid action"}, 400)
                return
            if port_str == "all":
                for p in BRIDGE_PORTS:
                    do_client_action(p, action)
                _send_json(self, {"ok": True, "msg": f"{action} all bridge clients"})
            else:
                try:
                    port = int(port_str)
                except ValueError:
                    _send_json(self, {"ok": False, "error": "invalid port"}, 400)
                    return
                out = do_client_action(port, action)
                _send_json(self, {"ok": True, "msg": out})

        elif path == "/api/client-svc-action":
            unit   = params.get("unit", "").strip()
            action = params.get("action", "").strip()
            valid_units = {e["unit"] for e in CLIENT_SERVICES}
            if not unit or unit not in valid_units:
                _send_json(self, {"ok": False, "error": f"unknown unit {unit!r}"}, 400)
                return
            if action not in ("start", "stop", "restart"):
                _send_json(self, {"ok": False, "error": "invalid action"}, 400)
                return
            out = do_client_svc_action(unit, action)
            _send_json(self, {"ok": True, "msg": out})

        elif path == "/api/ports-config":
            # Body: JSON array [{"port": 7002, "enabled": true}, ...]
            try:
                entries = json.loads(raw.decode("utf-8", errors="replace"))
                if not isinstance(entries, list):
                    raise ValueError("expected list")
                entries = [{"port": int(e["port"]), "enabled": bool(e.get("enabled", True))}
                           for e in entries if "port" in e]
            except Exception as ex:
                _send_json(self, {"ok": False, "error": f"bad body: {ex}"}, 400)
                return
            if save_ports_conf(entries):
                _rebuild_port_list()
                _client_alog_append(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} ports-config saved ===")
                for line in apply_ports_conf_client():
                    _client_alog_append(f"  {line}")
                _client_alog_append("=== done ===")
                _flush_client_alog()
                _send_json(self, {"ok": True, "msg": "saved and applied", "ports": _PORTS_CONF_ALL})
            else:
                _send_json(self, {"ok": False, "error": "failed to write ports.conf"}, 500)

        elif path == "/api/server-ports-config":
            # Proxy raw JSON body to printer
            ok, data = _printer_post_json("/api/ports-config", raw)
            _send_json(self, data if ok else {"ok": False, "error": data.get("error", "printer unreachable")})

        elif path == "/api/server-tcp-fwds-config":
            ok, data = _printer_post_json("/api/tcp-fwds-config", raw)
            _send_json(self, data if ok else {"ok": False, "error": data.get("error", "printer unreachable")})

        elif path == "/api/client-services-config":
            try:
                entries = json.loads(raw.decode("utf-8", errors="replace"))
                if not isinstance(entries, list):
                    raise ValueError("expected list")
                entries = [{"name": str(e["name"]), "unit": str(e["unit"])}
                           for e in entries if "name" in e and "unit" in e]
            except Exception as ex:
                _send_json(self, {"ok": False, "error": f"bad body: {ex}"}, 400)
                return
            if save_client_services(entries):
                _rebuild_client_svc_list()
                _send_json(self, {"ok": True, "msg": "saved", "services": CLIENT_SERVICES})
            else:
                _send_json(self, {"ok": False, "error": "failed to write client_services.conf"}, 500)

        elif path == "/api/server-services-config":
            ok, data = _printer_post_json("/api/services-config", raw)
            _send_json(self, data if ok else {"ok": False, "error": data.get("error", "printer unreachable")})

        elif path == "/api/server-action":
            action = params.get("action", "").strip()
            ok, data = printer_post("/api/action", {"action": action})
            _send_json(self, data if ok else {"ok": False, "error": data.get("error", "printer unreachable")})

        else:
            _send_json(self, {"ok": False, "error": "not found"}, 404)


def main():
    global PRINTER_IP, PRINTER_API_PORT
    global LISTEN_PORT, BRIDGE_PORTS, HTML_FILE

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--printer-ip"   and i+1 < len(args): PRINTER_IP       = args[i+1];      i+=2; continue
        if args[i] == "--api-port"     and i+1 < len(args): PRINTER_API_PORT = int(args[i+1]); i+=2; continue
        if args[i] == "--port"         and i+1 < len(args): LISTEN_PORT      = int(args[i+1]); i+=2; continue
        if args[i] == "--html"         and i+1 < len(args): HTML_FILE        = args[i+1];      i+=2; continue
        i+=1

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%H:%M:%S",
    )
    LOG.info("Starting bridge-monitor on 0.0.0.0:%d", LISTEN_PORT)
    LOG.info("Printer API : http://%s:%d", PRINTER_IP, PRINTER_API_PORT)
    LOG.info("Ports conf  : %s", PORTS_CONF)
    LOG.info("Ports       : %s", BRIDGE_PORTS)
    LOG.info("HTML file   : %s", HTML_FILE)

    # Apply ports.conf on startup
    apply_ports_conf_client()

    try:
        HTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge-monitor] Stopped.")


if __name__ == "__main__":
    main()
