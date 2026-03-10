#!/usr/prog/Python-3.8.2/bin/python3
# Run with: LD_LIBRARY_PATH=/usr/prog/Python-3.8.2/lib:/usr/prog/openssl-1.0.2d/lib python3 printer_api.py
"""
Socat Bridge API Server
=======================
Runs ON the AD5X printer (MIPS/BusyBox).  Pure Python 3 stdlib, no deps.

Why this exists
---------------
The host monitor used to poll the printer via SSH on every page load.
SSH setup (key exchange, auth, channel) costs ~300-600 ms and significant
CPU on the MIPS printer.  This API server is a tiny long-lived process that
keeps all bridge/service state in memory and answers HTTP in <5 ms.

API reference
-------------
GET  /api/health
     Always returns 200.
     → {"ok": true, "version": "1.0"}

GET  /api/status
     Full status snapshot.
     → {
         "ok":      true,
         "version": "1.0",
         "uptime":  {"raw": "1234.5 ...", "seconds": 1234.5, "pretty": "up 0h 20m"},
         "loadavg": "0.12 0.34 0.45 1/234 5678",
         "busy":    false,          ← true while a long action is running
         "bridges": {
           "7002": {"pid": 1234, "running": true,  "dev": "ttyS2"},
           "7004": {"pid": 0,    "running": false, "dev": "ttyS4"},
           ...
         }
       }

GET  /api/logs/action[?n=200]
     Last N lines of the action log (/tmp/bridge-action.log).
     → {"ok": true, "lines": [...], "n": 200}

GET  /api/logs/syslog[?n=80]
     Last N lines of syslog filtered for socat/bridge/ttyS.
     → {"ok": true, "lines": [...], "n": 80}

POST /api/action
     Body (application/x-www-form-urlencoded): action=<name>

     Long actions (async — check /api/logs/action for result):
       start           stop services → kill bridges → start bridges
       stop            kill bridges → start services
       restart         same as start

     Fast actions (synchronous — result in response):
       start-port-N    (re)start single socat bridge for port N
       stop-port-N     stop single socat bridge for port N

     Response (async): {"ok": true, "async": true,
                        "msg": "action started, check /api/logs/action"}
     Response (sync):  {"ok": true, "log": ["[bridge] started ...", ...]}
     Busy:             {"ok": false, "error": "busy"} HTTP 409
     Unknown action:   {"ok": false, "error": "..."} HTTP 400

Usage
-----
  python3 printer_api.py [--port 7001]

Deploy
------
  scp printer_api.py root@192.168.100.1:/usr/data/config/mod_data/bridge-api/
  ssh root@192.168.100.1 'python3 /usr/data/config/mod_data/bridge-api/printer_api.py &'

Auto-start via S-script (persistent path):
  ssh root@192.168.100.1 'cat > /opt/config/mod/.shell/root/S55bridge-api' << 'EOF'
  #!/bin/sh
  API=/usr/data/config/mod_data/bridge-api/printer_api.py
  case "$1" in
    start)   nohup python3 $API > /tmp/bridge-api.log 2>&1 & echo $! > /run/bridge-api.pid ;;
    stop)    [ -f /run/bridge-api.pid ] && kill $(cat /run/bridge-api.pid); rm -f /run/bridge-api.pid ;;
    restart) $0 stop; sleep 1; $0 start ;;
  esac
  EOF
  chmod +x /opt/config/mod/.shell/root/S55bridge-api
"""

import json
import datetime
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

# ── Configuration ─────────────────────────────────────────────────────────────
VERSION     = "0.8"
LISTEN_PORT = 8000
PID_DIR     = "/run/socat-bridges"
BRIDGE_LOG_DIR = "/tmp"
SOCAT       = "/usr/bin/socat"
ACTION_LOG  = "/tmp/bridge-action.log"
LOG_MAX_BYTES  = 1_000_000   # rotate socat logs when they exceed 1 MB
LOG_KEEP_BYTES = 200_000     # keep last 200 KB after rotation

# ports.conf — JSON file with port list managed via web UI.
# Format: [{"port": 7002, "enabled": true}, ...]
# Convention: port 700X  →  ttyS<X>
_HERE    = os.path.dirname(os.path.abspath(__file__))
PORTS_CONF = os.path.join(_HERE, "ports.conf")
# TCP port forwards — socat from printer → host
TCP_FWD_HOST    = "192.168.100.2"   # Odroid host IP (hardcoded)
TCP_FWD_PID_DIR = "/run/socat-tcp-fwds"
TCP_FWD_CONF    = os.path.join(_HERE, "tcp_forwards.conf")
CURRENT_MODE_FILE = os.path.join(_HERE, "current_mode")


def _get_mode() -> str:
    try:
        return open(CURRENT_MODE_FILE).read().strip()
    except Exception:
        return "remote"


def _set_mode(m: str):
    try:
        with open(CURRENT_MODE_FILE, "w") as f:
            f.write(m)
    except Exception:
        pass


#   port    — TCP port number
#   enabled — whether this port is active (can be toggled via web UI)
_DEFAULT_PORTS = [
    {"port": 7002, "enabled": True},
    {"port": 7004, "enabled": True},
    {"port": 7005, "enabled": True},
    {"port": 7007, "enabled": True},
]

# Default TCP forwards (printer → host). Each entry fields:
#   name     — display name
#   src_port — local port on printer (TCP-LISTEN)
#   dst_port — destination port on host (TCP connect to TCP_FWD_HOST:dst_port)
#   enabled  — whether this forward is active (can be toggled via web UI)
_DEFAULT_TCP_FORWARDS = [
    {"name": "ExoKlip",   "src_port": 8888, "dst_port": 8888, "enabled": True, "keep_on_local": True},
    {"name": "ssh",       "src_port": 2222, "dst_port": 22,   "enabled": True, "keep_on_local": True},
    {"name": "nginx",     "src_port": 80,   "dst_port": 80,   "enabled": True},
    {"name": "moonraker", "src_port": 7125, "dst_port": 7125, "enabled": True},
]


# Each entry fields:
#   name    — display / log name (used for ps matching)
#   script  — absolute path to the S-script
#   chroot  — True: run via `chroot CHROOT_DIR script verb`
#              False: run script verb directly on the host
#   start   — verb passed to the script for start ("start" or "up")
#   stop    — verb passed to the script for stop  ("stop"  or "down")
#   log     — log file path (null = auto-detect)
_DEFAULT_SERVICES = [
    {"name": "klipper",
     "script": "/opt/config/mod/.shell/root/S60klipper",
     "chroot": True, "start": "start", "stop": "stop",
     "stop_on_start": True, "start_on_start": False,
     "local_mode": "start",
     "log": "/usr/data/logs/printer.log"},
    {"name": "moonraker",
     "script": "/opt/config/mod/.shell/root/S65moonraker",
     "chroot": True, "start": "start", "stop": "stop",
     "stop_on_start": True, "start_on_start": False,
     "local_mode": "start",
     "log": "/usr/data/logs/moonraker.log"},
    {"name": "httpd",
     "script": "/opt/config/mod/.shell/root/S70httpd",
     "chroot": True, "start": "start", "stop": "stop",
     "stop_on_start": True, "start_on_start": False,
     "local_mode": "start",
     "log": None},
    {"name": "helix-screen",
     "script": "/opt/config/mod/.shell/root/S80helixscreen",
     "chroot": True, "start": "start", "stop": "stop",
     "stop_on_start": False, "start_on_start": True,
     "pre_start": "/opt/config/mod/.shell/zdisplay.sh off",
     "local_mode": None,
     "log": "/usr/data/config/mod_data/log/helixscreen.log"},
    {"name": "guppy",
     "script": "/opt/config/mod/.shell/root/S80guppyscreen",
     "chroot": True, "start": "start", "stop": "stop",
     "stop_on_start": False, "start_on_start": False,
     "pre_start": "/opt/config/mod/.shell/zdisplay.sh off",
     "local_mode": None,
     "log": "/usr/data/config/mod_data/log/guppyscreen.log"},
    {"name": "mjpg_streamer",
     "script": "/opt/config/mod/.shell/root/S99camera",
     "chroot": False, "start": "up", "stop": "stop",
     "stop_on_start": False, "start_on_start": True,
     "local_mode": None,
     "log": "/usr/data/config/mod_data/log/cam/mjpg_streamer.log"},
]


def load_ports_conf() -> list:
    """Load ports config from file, return list of dicts."""
    try:
        with open(PORTS_CONF) as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return [{"port": int(e["port"]), "enabled": bool(e.get("enabled", True))}
                    for e in data if "port" in e]
    except Exception:
        pass
    save_ports_conf(list(_DEFAULT_PORTS))
    return list(_DEFAULT_PORTS)


def save_ports_conf(entries: list) -> bool:
    """Save ports config to file. Returns True on success."""
    try:
        with open(PORTS_CONF, "w") as f:
            json.dump(entries, f, indent=2)
        return True
    except Exception:
        return False


def _rebuild_port_maps():
    """Rebuild BRIDGE_PORTS and PORT_TO_DEV from current ports.conf."""
    global BRIDGE_PORTS, PORT_TO_DEV, _PORTS_CONF_ALL
    _PORTS_CONF_ALL = load_ports_conf()
    BRIDGE_PORTS = [e["port"] for e in _PORTS_CONF_ALL if e["enabled"]]
    PORT_TO_DEV  = {p: f"ttyS{str(p)[-1]}" for p in BRIDGE_PORTS}


_PORTS_CONF_ALL: list = []
_rebuild_port_maps()


def load_tcp_forwards() -> list:
    """Load TCP forward config from file, return list of dicts."""
    try:
        with open(TCP_FWD_CONF) as f:
            data = json.load(f)
        if isinstance(data, list):
            return [{"name":     str(e.get("name", f"fwd{e['src_port']}")),
                     "src_port":      int(e["src_port"]),
                     "dst_port":      int(e["dst_port"]),
                     "enabled":       bool(e.get("enabled", True)),
                     "keep_on_local": bool(e.get("keep_on_local", False))}
                    for e in data if "src_port" in e and "dst_port" in e]
    except FileNotFoundError:
        save_tcp_forwards(list(_DEFAULT_TCP_FORWARDS))
        return list(_DEFAULT_TCP_FORWARDS)
    except Exception:
        pass
    return []


def save_tcp_forwards(entries: list) -> bool:
    """Save TCP forward config to file. Returns True on success."""
    try:
        with open(TCP_FWD_CONF, "w") as f:
            json.dump(entries, f, indent=2)
        return True
    except Exception:
        return False


def _rebuild_tcp_fwd_list():
    global _TCP_FWD_ALL
    _TCP_FWD_ALL = load_tcp_forwards()


_TCP_FWD_ALL: list = []
_rebuild_tcp_fwd_list()

CHROOT_DIR = "/usr/data/.mod/.zmod"
# For UART bridges we can't use chroot because /dev/ttyS* lives on the host FS.
# Instead we invoke socat via the chroot's own dynamic linker so it loads the
# correct GLIBC from the chroot, while the process itself sees the real /dev/.
_SOCAT_BIN    = CHROOT_DIR + SOCAT                       # /usr/data/.mod/.zmod/usr/bin/socat
_SOCAT_LOADER = CHROOT_DIR + "/lib/ld-linux-mipsn8.so.1" # chroot's own ld
_SOCAT_LIBPATH = ":".join([CHROOT_DIR + "/lib", CHROOT_DIR + "/usr/lib"])

def _socat_cmd(*args) -> list:
    """Build socat argv using the chroot loader so GLIBC version matches."""
    return [_SOCAT_LOADER, "--library-path", _SOCAT_LIBPATH, _SOCAT_BIN] + list(args)

SERVER_SVCS_CONF = os.path.join(_HERE, "server_services.conf")


def load_server_services() -> list:
    try:
        with open(SERVER_SVCS_CONF) as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return [{"name": str(e["name"]),
                     "script": str(e["script"]),
                     "chroot": bool(e.get("chroot", True)),
                     "start": str(e.get("start", "start")),
                     "stop": str(e.get("stop", "stop")),
                     "stop_on_start": bool(e.get("stop_on_start", False)),
                     "start_on_start": bool(e.get("start_on_start", False)),
                     "pre_start":  e.get("pre_start") or None,
                     "local_mode": e.get("local_mode") or None,
                     "log":        e.get("log") or None}
                    for e in data if "name" in e and "script" in e]
    except Exception:
        pass
    save_server_services(list(_DEFAULT_SERVICES))
    return list(_DEFAULT_SERVICES)


def save_server_services(entries: list) -> bool:
    try:
        with open(SERVER_SVCS_CONF, "w") as f:
            json.dump(entries, f, indent=2)
        return True
    except Exception:
        return False


def _rebuild_services_list():
    global SERVICES
    SERVICES = load_server_services()


SERVICES: list = []
_rebuild_services_list()


def _run_svc_cmd(svc: dict, verb: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a service verb (start/stop) respecting chroot and verb mapping."""
    cmd_word = svc.get(verb, verb)   # svc["start"] may be "up" instead of "start"
    script   = svc["script"]
    if svc.get("chroot", True):
        cmd = ["chroot", CHROOT_DIR, script, cmd_word]
    else:
        cmd = [script, cmd_word]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
# ─────────────────────────────────────────────────────────────────────────────


# ── Host info ─────────────────────────────────────────────────────────────────

def _get_hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "printer"


def _get_ip() -> str:
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


# ── Action state (thread-safe) ────────────────────────────────────────────────
_lock  = threading.Lock()
_busy  = False
_alog: list = []          # in-memory mirror of the action log


def _alog_append(line: str):
    """Append to in-memory log and print to stdout for journald."""
    _alog.append(line)
    if len(_alog) > 1000:
        _alog[:] = _alog[-1000:]
    print(line, flush=True)


def _flush_alog():
    try:
        with open(ACTION_LOG, "w") as f:
            f.write("\n".join(_alog) + "\n")
    except Exception:
        pass


# ── System info ───────────────────────────────────────────────────────────────

def read_uptime() -> dict:
    try:
        raw  = open("/proc/uptime").read().strip()
        secs = float(raw.split()[0])
        h, r = divmod(int(secs), 3600)
        m    = r // 60
        return {"raw": raw, "seconds": secs, "pretty": f"{h}h {m}m"}
    except Exception:
        return {"raw": "", "seconds": 0, "pretty": "unknown"}


def read_loadavg() -> str:
    try:
        return open("/proc/loadavg").read().strip()
    except Exception:
        return ""


def read_meminfo() -> dict:
    try:
        vals = {}
        for line in open("/proc/meminfo"):
            parts = line.split()
            if len(parts) >= 2:
                vals[parts[0].rstrip(':')] = int(parts[1])
        total = vals.get("MemTotal", 0)
        avail = vals.get("MemAvailable",
                         vals.get("MemFree", 0) + vals.get("Buffers", 0) + vals.get("Cached", 0))
        return {"used_mb": max(0, total - avail) // 1024, "total_mb": total // 1024}
    except Exception:
        return {}


# ── Bridge status ─────────────────────────────────────────────────────────────

def _tcp_connections(port: int) -> int:
    """Count ESTABLISHED TCP connections on a given local port via /proc/net/tcp."""
    hex_port = f"{port:04X}"
    count = 0
    try:
        for line in open("/proc/net/tcp").readlines()[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            local_addr = parts[1]
            state      = parts[3]
            lport = local_addr.split(":")[1] if ":" in local_addr else ""
            # state 01 = ESTABLISHED, 0A = LISTEN
            if lport.upper() == hex_port and state == "01":
                count += 1
    except Exception:
        pass
    return count


def bridge_statuses() -> dict:
    result = {}
    for port, dev in PORT_TO_DEV.items():
        pid_file = f"{PID_DIR}/bridge-{port}.pid"
        entry    = {"pid": 0, "running": False, "dev": dev, "since": "", "connected": 0}
        if os.path.exists(pid_file):
            try:
                pid = int(open(pid_file).read().strip())
                entry["pid"]     = pid
                entry["running"] = os.path.exists(f"/proc/{pid}")
                try:
                    mtime = os.path.getmtime(pid_file)
                    ts = datetime.datetime.fromtimestamp(mtime)
                    entry["since"] = ts.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
            except Exception:
                pass
        if entry["running"]:
            entry["connected"] = _tcp_connections(port)
        result[str(port)] = entry
    return result


# ── Bridge actions ────────────────────────────────────────────────────────────

def do_stop_bridges() -> list:
    log = ["[bridge] killing socat bridges..."]
    if os.path.isdir(PID_DIR):
        for fname in sorted(os.listdir(PID_DIR)):
            if not fname.endswith(".pid"):
                continue
            pid_file = os.path.join(PID_DIR, fname)
            try:
                pid = int(open(pid_file).read().strip())
                try:
                    pgid = os.getpgid(pid)
                    os.killpg(pgid, signal.SIGTERM)
                    log.append(f"[bridge] killed PGID {pgid} (pid={pid}, {fname})")
                except Exception:
                    os.kill(pid, signal.SIGTERM)
                    log.append(f"[bridge] killed PID {pid} ({fname})")
            except Exception as e:
                log.append(f"[bridge] {fname}: {e}")
            try:
                os.remove(pid_file)
            except Exception:
                pass
    # Kill any orphaned socat still holding our ports (not tracked in pid dir)
    for port in list(PORT_TO_DEV.keys()):
        try:
            subprocess.run(["fuser", "-k", f"{port}/tcp"],
                           capture_output=True, timeout=3)
        except Exception:
            pass
    time.sleep(0.5)
    return log


def do_stop_tcp_fwds_all() -> list:
    """Stop all running TCP forward socat processes."""
    log = ["[tcpfwd] stopping all TCP forwards..."]
    if os.path.isdir(TCP_FWD_PID_DIR):
        for fname in sorted(os.listdir(TCP_FWD_PID_DIR)):
            if not fname.endswith(".pid"):
                continue
            pid_file = os.path.join(TCP_FWD_PID_DIR, fname)
            try:
                pid = int(open(pid_file).read().strip())
                try:
                    pgid = os.getpgid(pid)
                    os.killpg(pgid, signal.SIGTERM)
                    log.append(f"[tcpfwd] killed PGID {pgid} (pid={pid}, {fname})")
                except Exception:
                    os.kill(pid, signal.SIGTERM)
                    log.append(f"[tcpfwd] killed PID {pid} ({fname})")
            except Exception as e:
                log.append(f"[tcpfwd] {fname}: {e}")
            try:
                os.remove(pid_file)
            except Exception:
                pass
    # Kill any orphaned socat on our forward ports
    for e in list(_TCP_FWD_ALL):
        try:
            subprocess.run(["fuser", "-k", f"{e['src_port']}/tcp"],
                           capture_output=True, timeout=3)
        except Exception:
            pass
    time.sleep(0.3)
    return log


def _log_writer_thread(r_fd: int, log_path: str):
    """Relay socat pipe output to log file; rotate when file exceeds LOG_MAX_BYTES."""
    try:
        with os.fdopen(r_fd, "rb") as pipe:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                try:
                    if os.path.getsize(log_path) > LOG_MAX_BYTES:
                        with open(log_path, "rb") as f:
                            f.seek(-LOG_KEEP_BYTES, 2)
                            tail = f.read()
                        with open(log_path, "wb") as f:
                            f.write(b"[...log rotated...]\n")
                            f.write(tail)
                except (OSError, IOError):
                    pass
                try:
                    with open(log_path, "ab") as f:
                        f.write(chunk)
                except (OSError, IOError):
                    pass
    except Exception:
        pass


def do_start_bridge(port: int, dev: str) -> list:
    dev_path = f"/dev/{dev}"
    if not os.path.exists(dev_path):
        return [f"[bridge] WARNING: {dev_path} not found, skipping port {port}"]

    pid_file = f"{PID_DIR}/bridge-{port}.pid"
    # kill stale process (and all its forked children) if pid file exists
    if os.path.exists(pid_file):
        try:
            old = int(open(pid_file).read().strip())
            try:
                os.killpg(os.getpgid(old), signal.SIGTERM)
            except Exception:
                os.kill(old, signal.SIGTERM)
            time.sleep(0.2)
        except Exception:
            pass
        try:
            os.remove(pid_file)
        except Exception:
            pass
    # Kill any orphaned process still holding this port
    try:
        subprocess.run(["fuser", "-k", f"{port}/tcp"],
                       capture_output=True, timeout=3)
        time.sleep(0.1)
    except Exception:
        pass

    log_path = f"{BRIDGE_LOG_DIR}/bridge-{port}.log"
    try:
        os.makedirs(PID_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a") as f:
            f.write(f"\n--- started {ts}  TCP:{port} <-> {dev_path} ---\n")
        r_fd, w_fd = os.pipe()
        log_w = os.fdopen(w_fd, "wb")
        proc = subprocess.Popen(
            _socat_cmd("-d", "-d",
                       f"TCP-LISTEN:{port},reuseaddr,fork,tcp-nodelay",
                       f"{dev_path},raw,echo=0"),
            stdout=log_w,
            stderr=log_w,
            preexec_fn=os.setsid,
            close_fds=True,
        )
        log_w.close()  # parent closes write end; child keeps it via fd 1/2
        threading.Thread(target=_log_writer_thread, args=(r_fd, log_path),
                         daemon=True).start()
        with open(pid_file, "w") as f:
            f.write(str(proc.pid))
        return [f"[bridge] started TCP:{port} <-> {dev_path} PID={proc.pid}"]
    except Exception as e:
        return [f"[bridge] ERROR starting port {port}: {e}"]


def do_start_bridges() -> list:
    os.makedirs(PID_DIR, exist_ok=True)
    log = []
    for port, dev in PORT_TO_DEV.items():
        log += do_start_bridge(port, dev)
    return log


# ── TCP Port Forwards ─────────────────────────────────────────────────────────

def tcp_fwd_pid_file(src_port: int) -> str:
    return f"{TCP_FWD_PID_DIR}/tcpfwd-{src_port}.pid"


def tcp_fwd_statuses() -> dict:
    """Return running status for all configured TCP forwards."""
    result = {}
    for e in _TCP_FWD_ALL:
        src      = e["src_port"]
        pid_file = tcp_fwd_pid_file(src)
        entry = {
            "name": e["name"], "src_port": src, "dst_port": e["dst_port"],
            "enabled": e["enabled"], "pid": 0, "running": False,
            "since": "", "connected": 0,
        }
        if os.path.exists(pid_file):
            try:
                pid = int(open(pid_file).read().strip())
                entry["pid"]     = pid
                entry["running"] = os.path.exists(f"/proc/{pid}")
                try:
                    mtime = os.path.getmtime(pid_file)
                    ts = datetime.datetime.fromtimestamp(mtime)
                    entry["since"] = ts.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
            except Exception:
                pass
        if entry["running"]:
            entry["connected"] = _tcp_connections(src)
        result[str(src)] = entry
    return result


def do_start_tcp_fwd(entry: dict) -> list:
    """Start socat TCP forward: TCP-LISTEN:{src} -> TCP:{TCP_FWD_HOST}:{dst}."""
    src  = entry["src_port"]
    dst  = entry["dst_port"]
    name = entry["name"]
    pid_file = tcp_fwd_pid_file(src)
    os.makedirs(TCP_FWD_PID_DIR, exist_ok=True)
    if os.path.exists(pid_file):
        try:
            old = int(open(pid_file).read().strip())
            os.kill(old, 15)
            time.sleep(0.2)
        except Exception:
            pass
        try:
            os.remove(pid_file)
        except Exception:
            pass
    log_path = f"{BRIDGE_LOG_DIR}/tcpfwd-{src}.log"
    try:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a") as f:
            f.write(f"\n--- started {ts}  TCP:{src} -> {TCP_FWD_HOST}:{dst} ({name}) ---\n")
        r_fd, w_fd = os.pipe()
        log_w = os.fdopen(w_fd, "wb")
        proc = subprocess.Popen(
            ["chroot", CHROOT_DIR, SOCAT, "-d", "-d",
             f"TCP-LISTEN:{src},reuseaddr,fork,tcp-nodelay",
             f"TCP:{TCP_FWD_HOST}:{dst},tcp-nodelay"],
            stdout=log_w,
            stderr=log_w,
            preexec_fn=os.setsid,
            close_fds=True,
        )
        log_w.close()  # parent closes write end; child keeps it via fd 1/2
        threading.Thread(target=_log_writer_thread, args=(r_fd, log_path),
                         daemon=True).start()
        with open(pid_file, "w") as f:
            f.write(str(proc.pid))
        return [f"[tcpfwd] started {name}: TCP:{src} -> {TCP_FWD_HOST}:{dst} PID={proc.pid}"]
    except Exception as e:
        return [f"[tcpfwd] ERROR starting {name} port {src}: {e}"]


def do_stop_tcp_fwd(src_port: int) -> list:
    """Stop the socat TCP forward for the given src_port."""
    pid_file = tcp_fwd_pid_file(src_port)
    if not os.path.exists(pid_file):
        return [f"[tcpfwd] port {src_port} was not running"]
    try:
        pid = int(open(pid_file).read().strip())
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            os.kill(pid, signal.SIGTERM)
        os.remove(pid_file)
        return [f"[tcpfwd] stopped port {src_port} PID={pid}"]
    except Exception as e:
        return [f"[tcpfwd] stop port {src_port} error: {e}"]


def apply_tcp_fwds(log_fn=None) -> list:
    """Start/stop TCP forward socat processes to match tcp_forwards.conf."""
    log = []
    def emit(s):
        log.append(s)
        if log_fn:
            log_fn(s)
        print(s, flush=True)
    if not _TCP_FWD_ALL:
        return log
    emit(f"[tcpfwd] applying tcp_forwards.conf ({len(_TCP_FWD_ALL)} entries)")
    enabled_srcs = {e["src_port"] for e in _TCP_FWD_ALL if e["enabled"]}
    # Stop forwards that are disabled or removed
    if os.path.isdir(TCP_FWD_PID_DIR):
        for fname in sorted(os.listdir(TCP_FWD_PID_DIR)):
            if not fname.endswith(".pid"):
                continue
            try:
                src = int(fname.replace("tcpfwd-", "").replace(".pid", ""))
            except Exception:
                continue
            if src not in enabled_srcs:
                emit(f"[tcpfwd] stopping forward port {src} (disabled or removed)")
                for line in do_stop_tcp_fwd(src):
                    emit(line)
    # Start enabled forwards that are not running
    for e in _TCP_FWD_ALL:
        if not e["enabled"]:
            continue
        src      = e["src_port"]
        pid_file = tcp_fwd_pid_file(src)
        running  = False
        if os.path.exists(pid_file):
            try:
                pid     = int(open(pid_file).read().strip())
                running = os.path.exists(f"/proc/{pid}")
            except Exception:
                pass
        if not running:
            emit(f"[tcpfwd] starting {e['name']}: {src} -> {TCP_FWD_HOST}:{e['dst_port']}")
            for line in do_start_tcp_fwd(e):
                emit(line)
        else:
            emit(f"[tcpfwd] forward port {src} already running")
    return log


def do_stop_port(port: int) -> list:
    pid_file = f"{PID_DIR}/bridge-{port}.pid"
    if not os.path.exists(pid_file):
        return [f"[bridge] port {port} was not running"]
    try:
        pid = int(open(pid_file).read().strip())
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            os.kill(pid, signal.SIGTERM)
        os.remove(pid_file)
        return [f"[bridge] stopped port {port} PID={pid}"]
    except Exception as e:
        return [f"[bridge] stop port {port} error: {e}"]


# ── Service actions ───────────────────────────────────────────────────────────

def do_stop_services() -> list:
    log = []
    for svc in SERVICES:
        name = svc["name"]
        log.append(f"[bridge] stopping {name}...")
        try:
            r = _run_svc_cmd(svc, "stop", timeout=15)
            for line in (r.stdout + r.stderr).strip().splitlines():
                log.append(f"  {line}")
            log.append(f"[bridge] {name} stop exit={r.returncode}")
            time.sleep(1)
        except Exception as e:
            log.append(f"[bridge] {name} stop error: {e}")
    return log


def do_start_services() -> list:
    log = []
    svc_names = [s["name"] for s in SERVICES]
    for svc in SERVICES:
        name = svc["name"]
        log.append(f"[bridge] starting {name}...")
        for pid_path in [f"/run/{name}.pid", f"/tmp/{name}.pid"]:
            try:
                os.remove(pid_path)
            except Exception:
                pass
        try:
            r = _run_svc_cmd(svc, "start", timeout=30)
            for line in (r.stdout + r.stderr).strip().splitlines():
                log.append(f"  {line}")
            log.append(f"[bridge] {name} start exit={r.returncode}")
            time.sleep(2)
        except Exception as e:
            log.append(f"[bridge] {name} start error: {e}")
    # Verify processes are actually running
    log.append("[bridge] --- process check ---")
    try:
        r = subprocess.run(["ps", "w"], capture_output=True, text=True, timeout=3)
        found = [
            ln for ln in r.stdout.splitlines()
            if any(n in ln for n in svc_names) and "grep" not in ln
        ]
        log += found if found else ["[bridge] WARNING: no service processes visible in ps"]
    except Exception:
        pass
    return log


def do_restart_service(name: str) -> list:
    """Stop then start a single named service from SERVICES."""
    svc = next((s for s in SERVICES if s["name"] == name), None)
    if not svc:
        return [f"[bridge] unknown service: {name!r}"]
    log = [f"[bridge] restart {name}: stopping..."]
    try:
        r = _run_svc_cmd(svc, "stop", timeout=15)
        for line in (r.stdout + r.stderr).strip().splitlines():
            log.append(f"  {line}")
        log.append(f"[bridge] {name} stop exit={r.returncode}")
        time.sleep(1)
    except Exception as e:
        log.append(f"[bridge] {name} stop error: {e}")
    log.append(f"[bridge] restart {name}: starting...")
    for pid_path in [f"/run/{name}.pid", f"/tmp/{name}.pid"]:
        try:
            os.remove(pid_path)
        except Exception:
            pass
    pre = svc.get("pre_start")
    if pre:
        log.append(f"[bridge] {name} pre_start: {pre}")
        try:
            r = subprocess.run(pre.split(), capture_output=True, text=True, timeout=10)
            for line in (r.stdout + r.stderr).strip().splitlines():
                log.append(f"  {line}")
            log.append(f"[bridge] {name} pre_start exit={r.returncode}")
        except Exception as e:
            log.append(f"[bridge] {name} pre_start error: {e}")
    try:
        r = _run_svc_cmd(svc, "start", timeout=30)
        for line in (r.stdout + r.stderr).strip().splitlines():
            log.append(f"  {line}")
        log.append(f"[bridge] {name} start exit={r.returncode}")
    except Exception as e:
        log.append(f"[bridge] {name} start error: {e}")
    return log


def do_stop_service(name: str) -> list:
    """Stop a single named service."""
    svc = next((s for s in SERVICES if s["name"] == name), None)
    if not svc:
        return [f"[bridge] unknown service: {name!r}"]
    log = [f"[bridge] stopping {name}..."]
    try:
        r = _run_svc_cmd(svc, "stop", timeout=15)
        for line in (r.stdout + r.stderr).strip().splitlines():
            log.append(f"  {line}")
        log.append(f"[bridge] {name} stop exit={r.returncode}")
    except Exception as e:
        log.append(f"[bridge] {name} stop error: {e}")
    return log


def do_start_service(name: str) -> list:
    """Start a single named service."""
    svc = next((s for s in SERVICES if s["name"] == name), None)
    if not svc:
        return [f"[bridge] unknown service: {name!r}"]
    log = [f"[bridge] starting {name}..."]
    for pid_path in [f"/run/{name}.pid", f"/tmp/{name}.pid"]:
        try:
            os.remove(pid_path)
        except Exception:
            pass
    pre = svc.get("pre_start")
    if pre:
        log.append(f"[bridge] {name} pre_start: {pre}")
        try:
            r = subprocess.run(pre.split(), capture_output=True, text=True, timeout=10)
            for line in (r.stdout + r.stderr).strip().splitlines():
                log.append(f"  {line}")
            log.append(f"[bridge] {name} pre_start exit={r.returncode}")
        except Exception as e:
            log.append(f"[bridge] {name} pre_start error: {e}")
    try:
        r = _run_svc_cmd(svc, "start", timeout=30)
        for line in (r.stdout + r.stderr).strip().splitlines():
            log.append(f"  {line}")
        log.append(f"[bridge] {name} start exit={r.returncode}")
    except Exception as e:
        log.append(f"[bridge] {name} start error: {e}")
    return log


def read_service_log(name: str, n: int = 60) -> list:
    """Return last N lines of the service log."""
    svc = next((s for s in SERVICES if s["name"] == name), None)

    # Build candidate list: explicit log path first, then common fallbacks
    candidates = []
    if svc and svc.get("log"):
        candidates.append(svc["log"])
    candidates += [
        f"{CHROOT_DIR}/usr/data/logs/{name}.log",
        f"/usr/data/logs/{name}.log",
        f"{CHROOT_DIR}/tmp/{name}.log",
        f"/tmp/{name}.log",
    ]

    for path in candidates:
        try:
            lines = open(path).read().strip().splitlines()
            if lines:
                return lines[-n:]
        except Exception:
            pass

    return [f"(no log found for {name}, tried: {candidates[0] if candidates else '?'})"]


def _proc_start_time(pid: int) -> str:
    """Return process start time as ISO string using /proc/<pid>/stat and boot time."""
    try:
        stat  = open(f"/proc/{pid}/stat").read().split()
        ticks = int(stat[21])          # starttime in clock ticks since boot
        hz    = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        boot_raw = float(open("/proc/uptime").read().split()[1])
        # boot epoch = now - uptime
        boot_epoch = time.time() - float(open("/proc/uptime").read().split()[0])
        start_epoch = boot_epoch + ticks / hz
        return datetime.datetime.fromtimestamp(start_epoch).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def service_statuses() -> dict:
    """Check whether each service process is running (via ps), return PID and since."""
    result = {}
    try:
        r = subprocess.run(["ps", "w"], capture_output=True, text=True, timeout=3)
        ps_lines = r.stdout.splitlines()
    except Exception:
        ps_lines = []
    for svc in SERVICES:
        name    = svc["name"]
        pid     = 0
        running = False
        for ln in ps_lines:
            if name in ln and "grep" not in ln and "chroot" not in ln and "S5" not in ln:
                running = True
                try:
                    pid = int(ln.strip().split()[0])
                except Exception:
                    pass
                break
        since = _proc_start_time(pid) if pid else ""
        result[name] = {"running": running, "pid": pid, "since": since}
    return result


# ── Action dispatcher ─────────────────────────────────────────────────────────

def apply_ports_conf(log_fn=None) -> list:
    """Start/stop socat bridges to match ports.conf (enabled flag).
    Stops bridges for disabled/removed ports, starts bridges for enabled ports.
    Returns log lines."""
    log = []
    def emit(s):
        log.append(s)
        if log_fn:
            log_fn(s)
        print(s, flush=True)

    emit(f"[ports] applying ports.conf: {_PORTS_CONF_ALL}")
    # Stop bridges whose port is disabled or not in conf at all
    all_conf_ports = {e["port"] for e in _PORTS_CONF_ALL}
    if os.path.isdir(PID_DIR):
        for fname in sorted(os.listdir(PID_DIR)):
            if not fname.endswith(".pid"):
                continue
            try:
                port = int(fname.replace("bridge-", "").replace(".pid", ""))
            except Exception:
                continue
            if port not in BRIDGE_PORTS:
                emit(f"[ports] stopping bridge port {port} (disabled or removed)")
                for line in do_stop_port(port):
                    emit(line)
    # Start enabled bridges that are not running
    for port in BRIDGE_PORTS:
        dev = PORT_TO_DEV.get(port)
        if not dev:
            continue
        pid_file = f"{PID_DIR}/bridge-{port}.pid"
        running = False
        if os.path.exists(pid_file):
            try:
                pid = int(open(pid_file).read().strip())
                running = os.path.exists(f"/proc/{pid}")
            except Exception:
                pass
        if not running:
            emit(f"[ports] starting bridge port {port}")
            for line in do_start_bridge(port, dev):
                emit(line)
        else:
            emit(f"[ports] bridge port {port} already running")
    return log


def _execute_action(action: str) -> list:
    """Run an action and return log lines. May take 10-60 s for global actions."""
    log = [f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} action={action!r} ==="]
    try:
        if action == "_apply_ports":
            log += apply_ports_conf(log_fn=_alog_append)

        elif action == "_apply_tcp_fwds":
            log += apply_tcp_fwds(log_fn=_alog_append)

        elif action in ("start", "start-bridges"):
            log += do_stop_bridges()
            log += do_start_bridges()
            if action == "start":
                log += apply_tcp_fwds(log_fn=_alog_append)

        elif action in ("stop", "stop-bridges"):
            log += do_stop_bridges()

        elif action in ("restart", "restart-bridges"):
            log += do_stop_bridges()
            log += do_start_bridges()
            if action == "restart":
                log += apply_tcp_fwds(log_fn=_alog_append)

        elif action == "start-tcpfwds-all":
            log += apply_tcp_fwds(log_fn=_alog_append)

        elif action == "stop-tcpfwds-all":
            log += do_stop_tcp_fwds_all()

        elif action == "restart-tcpfwds-all":
            log += do_stop_tcp_fwds_all()
            log += apply_tcp_fwds(log_fn=_alog_append)

        elif action == "switch-local":
            log.append("[mode] \u2192 Local Klipper")
            # 1. Stop all UART bridges
            log += do_stop_bridges()
            # 2. Stop TCP forwards that are not flagged keep_on_local
            for e in list(_TCP_FWD_ALL):
                if e.get("keep_on_local"):
                    log.append(f"[mode] keeping tcp fwd {e['name']} ({e['src_port']})")
                else:
                    log += do_stop_tcp_fwd(e["src_port"])
            # 3. Services: local_mode=start → start; local_mode=stop → stop
            for svc in SERVICES:
                m = svc.get("local_mode")
                if m == "stop":
                    log += do_stop_service(svc["name"])
                elif m == "start":
                    log += do_start_service(svc["name"])
            _set_mode("local")

        elif action == "switch-remote":
            log.append("[mode] \u2192 Remote Klipper")
            # 1. Stop services with local_mode=start (they hold TCP ports we need)
            svcs_to_stop = [s for s in SERVICES if s.get("local_mode") == "start"]
            for svc in svcs_to_stop:
                log += do_stop_service(svc["name"])
            # 2. Wait until TCP forward ports are free (max 20 s)
            fwd_ports = [e["src_port"] for e in _TCP_FWD_ALL if e.get("enabled")]
            if fwd_ports:
                log.append(f"[mode] waiting for ports {fwd_ports} to be free...")
                deadline = time.time() + 20
                while time.time() < deadline:
                    busy = []
                    for port in fwd_ports:
                        hex_port = f"{port:04X}"
                        try:
                            for line in open("/proc/net/tcp").readlines()[1:]:
                                parts = line.split()
                                if len(parts) >= 4:
                                    lport = parts[1].split(":")[1] if ":" in parts[1] else ""
                                    if lport.upper() == hex_port and parts[3] in ("01", "0A"):
                                        busy.append(port)
                                        break
                        except Exception:
                            pass
                    if not busy:
                        log.append("[mode] ports are free, proceeding")
                        break
                    log.append(f"[mode] still busy: {busy}, waiting...")
                    time.sleep(2)
                else:
                    log.append("[mode] timeout waiting for ports, continuing anyway")
            # 3. Start UART bridges
            log += do_start_bridges()
            # 4. Start TCP forwards
            log += apply_tcp_fwds(log_fn=_alog_append)
            # 5. Start services with local_mode=stop
            for svc in SERVICES:
                if svc.get("local_mode") == "stop":
                    log += do_start_service(svc["name"])
            _set_mode("remote")

        elif action.startswith("start-port-"):
            port = int(action.rsplit("-", 1)[-1])
            dev  = PORT_TO_DEV.get(port)
            if not dev:
                log.append(f"[bridge] unknown port {port}")
            else:
                log += do_stop_port(port)
                log += do_start_bridge(port, dev)

        elif action.startswith("stop-port-"):
            port = int(action.rsplit("-", 1)[-1])
            log += do_stop_port(port)

        elif action.startswith("restart-port-"):
            port = int(action.rsplit("-", 1)[-1])
            dev  = PORT_TO_DEV.get(port)
            if not dev:
                log.append(f"[bridge] unknown port {port}")
            else:
                log += do_stop_port(port)
                log += do_start_bridge(port, dev)

        elif action.startswith("restart-svc-"):
            svc_name = action[len("restart-svc-"):]
            log += do_restart_service(svc_name)

        elif action.startswith("stop-svc-"):
            svc_name = action[len("stop-svc-"):]
            log += do_stop_service(svc_name)

        elif action.startswith("start-svc-"):
            svc_name = action[len("start-svc-"):]
            log += do_start_service(svc_name)

        elif action.startswith("start-tcpfwd-"):
            try:
                src_port = int(action.rsplit("-", 1)[-1])
                e = next((x for x in _TCP_FWD_ALL if x["src_port"] == src_port), None)
                if not e:
                    log.append(f"[tcpfwd] unknown src_port {src_port}")
                else:
                    log += do_stop_tcp_fwd(src_port)
                    log += do_start_tcp_fwd(e)
            except Exception as ex:
                log.append(f"[tcpfwd] start-tcpfwd parse error: {ex}")

        elif action.startswith("stop-tcpfwd-"):
            try:
                src_port = int(action.rsplit("-", 1)[-1])
                log += do_stop_tcp_fwd(src_port)
            except Exception as ex:
                log.append(f"[tcpfwd] stop-tcpfwd parse error: {ex}")

        else:
            log.append(f"[bridge] unknown action: {action!r}")

    except Exception as e:
        log.append(f"[bridge] UNHANDLED EXCEPTION: {e}")

    log.append("=== done ===")
    return log


def run_action_async(action: str) -> bool:
    """Schedule a long action in a background thread. Returns False if busy."""
    global _busy
    with _lock:
        if _busy:
            return False
        _busy = True

    def _worker():
        global _busy
        _alog.clear()
        _alog_append(f"[api] starting async action: {action!r}")
        lines = _execute_action(action)
        for line in lines:
            _alog_append(line)
        _flush_alog()
        with _lock:
            _busy = False

    threading.Thread(target=_worker, daemon=True).start()
    return True


def run_action_sync(action: str) -> list:
    """Run a fast action synchronously. Returns log lines."""
    global _busy
    with _lock:
        if _busy:
            return ["[bridge] busy — try again later"]
        _busy = True
    try:
        lines = _execute_action(action)
        _alog.clear()
        for line in lines:
            _alog_append(line)
        _flush_alog()
        return lines
    finally:
        with _lock:
            _busy = False


# ── Log readers ───────────────────────────────────────────────────────────────

def read_action_log(n: int = 200) -> list:
    with _lock:
        if _alog:
            return list(_alog[-n:])
    try:
        return open(ACTION_LOG).read().strip().splitlines()[-n:]
    except Exception:
        return ["(no action log yet)"]


def read_syslog(n: int = 80) -> list:
    for cmd in [
        f"logread | grep -iE '(socat|bridge|ttyS)' | tail -{n}",
        f"logread | tail -{n}",
    ]:
        try:
            r = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().splitlines()
        except Exception:
            pass
    return ["(no syslog available)"]


def read_bridge_log(port: int, n: int = 60) -> list:
    """Return last N lines of socat output for the given port.

    Reads /tmp/bridge-{port}.log written by the socat process itself
    (socat is started with -d -d so it logs connections/errors to stderr).
    Falls back to action log entries and then a status summary.
    """
    log_path = f"{BRIDGE_LOG_DIR}/bridge-{port}.log"
    try:
        lines = open(log_path).read().splitlines()
        if lines:
            return lines[-n:]
    except Exception:
        pass

    # fallback: grep action log for this port
    try:
        raw = open(ACTION_LOG).read().strip().splitlines()
        matched = [l for l in raw if str(port) in l]
        if matched:
            return matched[-n:]
    except Exception:
        pass

    # status summary fallback
    st      = bridge_statuses().get(str(port), {})
    running = st.get("running", False)
    pid     = st.get("pid", 0)
    return [
        f"bridge port {port}: {'running' if running else 'stopped'}" +
        (f"  PID={pid}" if pid else ""),
        f"(no log yet — restart the bridge to generate {log_path})",
    ]


# ── HTTP handler ──────────────────────────────────────────────────────────────

def _send_json(handler, data: dict, code: int = 200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def _parse_post(raw: bytes) -> dict:
    params = {}
    for pair in raw.decode("utf-8", errors="replace").split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            params[k.strip()] = v.strip()
    return params


class ApiHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # silence default access log

    def do_GET(self):
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)
        path   = parsed.path

        if path == "/api/health":
            _send_json(self, {"ok": True, "version": VERSION})

        elif path == "/api/status":
            _send_json(self, {
                "ok":       True,
                "version":  VERSION,
                "hostname": _get_hostname(),
                "ip":       _get_ip(),
                "bridges":  bridge_statuses(),
                "services": service_statuses(),
                "tcp_fwds": tcp_fwd_statuses(),
                "uptime":   read_uptime(),
                "loadavg":  read_loadavg(),
                "mem":      read_meminfo(),
                "mode":     _get_mode(),
            })

        # Fast partial endpoints for JS polling
        elif path == "/api/bridges":
            _send_json(self, {"ok": True, "bridges": bridge_statuses()})

        elif path == "/api/services":
            _send_json(self, {"ok": True, "services": service_statuses()})

        elif path == "/api/logs/action":
            n = int(qs.get("n", ["200"])[0])
            _send_json(self, {"ok": True, "lines": read_action_log(n), "n": n})

        elif path == "/api/logs/syslog":
            n = int(qs.get("n", ["80"])[0])
            _send_json(self, {"ok": True, "lines": read_syslog(n), "n": n})

        elif path == "/api/logs/service":
            name = qs.get("name", [""])[0].strip()
            n    = int(qs.get("n", ["60"])[0])
            valid_names = {s["name"] for s in SERVICES}
            if not name or name not in valid_names:
                _send_json(self, {"ok": False, "error": f"unknown service {name!r}"}, 400)
            else:
                _send_json(self, {"ok": True, "lines": read_service_log(name, n), "n": n})

        elif path == "/api/ports-config":
            _send_json(self, {"ok": True, "ports": _PORTS_CONF_ALL})

        elif path == "/api/tcp-fwds-config":
            _send_json(self, {"ok": True, "forwards": _TCP_FWD_ALL, "host": TCP_FWD_HOST})

        elif path == "/api/tcp-fwds":
            _send_json(self, {"ok": True, "forwards": tcp_fwd_statuses(), "host": TCP_FWD_HOST})

        elif path == "/api/logs/tcpfwd":
            try:
                src_port = int(qs.get("port", ["0"])[0])
            except ValueError:
                src_port = 0
            n = int(qs.get("n", ["60"])[0])
            log_path = f"{BRIDGE_LOG_DIR}/tcpfwd-{src_port}.log"
            try:
                lines = open(log_path).read().splitlines()
                if lines:
                    _send_json(self, {"ok": True, "lines": lines[-n:], "n": n})
                    return
            except Exception:
                pass
            _send_json(self, {"ok": True,
                              "lines": [f"(no tcpfwd log yet for port {src_port})"], "n": n})

        elif path == "/api/services-config":
            _send_json(self, {"ok": True, "services": SERVICES})

        elif path == "/api/logs/bridge":
            try:
                port = int(qs.get("port", ["0"])[0])
            except ValueError:
                port = 0
            n = int(qs.get("n", ["60"])[0])
            if port not in PORT_TO_DEV:
                _send_json(self, {"ok": False, "error": f"unknown port {port}"}, 400)
            else:
                _send_json(self, {"ok": True, "lines": read_bridge_log(port, n), "n": n})

        else:
            _send_json(self, {"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        length  = int(self.headers.get("Content-Length", 0))
        raw     = self.rfile.read(length)
        path    = urlparse(self.path).path

        if path == "/api/ports-config":
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
                _rebuild_port_maps()
                started = run_action_async("_apply_ports")
                _send_json(self, {
                    "ok": True,
                    "msg": "saved and applying",
                    "async": started,
                    "ports": _PORTS_CONF_ALL,
                })
            else:
                _send_json(self, {"ok": False, "error": "failed to write ports.conf"}, 500)
            return

        if path == "/api/tcp-fwds-config":
            try:
                entries = json.loads(raw.decode("utf-8", errors="replace"))
                if not isinstance(entries, list):
                    raise ValueError("expected list")
                entries = [{"name":         str(e.get("name", f"fwd{e['src_port']}")),
                            "src_port":     int(e["src_port"]),
                            "dst_port":     int(e["dst_port"]),
                            "enabled":      bool(e.get("enabled", True)),
                            "keep_on_local": bool(e.get("keep_on_local", False))}
                           for e in entries if "src_port" in e and "dst_port" in e]
            except Exception as ex:
                _send_json(self, {"ok": False, "error": f"bad body: {ex}"}, 400)
                return
            if save_tcp_forwards(entries):
                _rebuild_tcp_fwd_list()
                started = run_action_async("_apply_tcp_fwds")
                _send_json(self, {
                    "ok":      True,
                    "msg":     "saved and applying",
                    "async":   started,
                    "forwards": _TCP_FWD_ALL,
                })
            else:
                _send_json(self, {"ok": False, "error": "failed to write tcp_forwards.conf"}, 500)
            return

        if path == "/api/services-config":
            try:
                entries = json.loads(raw.decode("utf-8", errors="replace"))
                if not isinstance(entries, list):
                    raise ValueError("expected list")
                entries = [{"name":          str(e["name"]),
                            "script":        str(e["script"]),
                            "chroot":        bool(e.get("chroot", True)),
                            "start":         str(e.get("start", "start")),
                            "stop":          str(e.get("stop", "stop")),
                            "stop_on_start":  bool(e.get("stop_on_start", False)),
                            "start_on_start": bool(e.get("start_on_start", False)),
                            "pre_start":     e.get("pre_start") or None,
                            "local_mode":    e.get("local_mode") or None,
                            "log":           e.get("log") or None}
                           for e in entries if "name" in e and "script" in e]
            except Exception as ex:
                _send_json(self, {"ok": False, "error": f"bad body: {ex}"}, 400)
                return
            if save_server_services(entries):
                _rebuild_services_list()
                _send_json(self, {"ok": True, "msg": "saved", "services": SERVICES})
            else:
                _send_json(self, {"ok": False, "error": "failed to write server_services.conf"}, 500)
            return

        params = _parse_post(raw)

        if path != "/api/action":
            _send_json(self, {"ok": False, "error": "not found"}, 404)
            return

        action = params.get("action", "").strip()
        if not action:
            _send_json(self, {"ok": False, "error": "missing 'action' parameter"}, 400)
            return

        # Per-port and per-tcpfwd ops are fast (<1 s) → synchronous, result in response
        if action.startswith(("start-port-", "stop-port-", "restart-port-",
                               "start-tcpfwd-", "stop-tcpfwd-")):
            lines = run_action_sync(action)
            _send_json(self, {"ok": True, "log": lines})
            return

        # Per-service ops — async (may take up to 30 s)
        if action.startswith(("restart-svc-", "stop-svc-", "start-svc-")):
            started = run_action_async(action)
            if started:
                _send_json(self, {
                    "ok":    True,
                    "async": True,
                    "msg":   "action started — poll /api/logs/action for progress",
                })
            else:
                _send_json(self, {"ok": False, "error": "busy"}, 409)
            return

        # Global ops take 10-60 s → async
        valid_global = {
            "start", "stop", "restart",
            "start-bridges", "stop-bridges", "restart-bridges",
            "start-tcpfwds-all", "stop-tcpfwds-all", "restart-tcpfwds-all",
            "switch-local", "switch-remote",
        }
        if action not in valid_global:
            _send_json(self, {"ok": False, "error": f"unknown action: {action!r}"}, 400)
            return

        started = run_action_async(action)
        if started:
            _send_json(self, {
                "ok":    True,
                "async": True,
                "msg":   "action started — poll /api/logs/action for progress",
            })
        else:
            _send_json(self, {"ok": False, "error": "busy"}, 409)


# ── Entry point ───────────────────────────────────────────────────────────────

class _ReuseHTTPServer(HTTPServer):
    allow_reuse_address = True


def stop_on_startup() -> None:
    """Stop all services with stop_on_start=True. Called once at startup."""
    to_stop = [s for s in SERVICES if s.get("stop_on_start", False)]
    if not to_stop:
        return
    names = [s["name"] for s in to_stop]
    print(f"[bridge-api] stop_on_start: {names}", flush=True)
    for svc in to_stop:
        try:
            r = _run_svc_cmd(svc, "stop", timeout=15)
            out = (r.stdout + r.stderr).strip()
            print(f"[bridge-api]   {svc['name']} stop → exit={r.returncode}"
                  + (f" {out}" if out else ""), flush=True)
        except Exception as e:
            print(f"[bridge-api]   {svc['name']} stop error: {e}", flush=True)


def start_on_startup() -> None:
    """Start all services with start_on_start=True. Called once at startup."""
    to_start = [s for s in SERVICES if s.get("start_on_start", False)]
    if not to_start:
        return
    names = [s["name"] for s in to_start]
    print(f"[bridge-api] start_on_start: {names}", flush=True)
    try:
        ps_out = subprocess.run(["ps", "w"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        ps_out = ""
    for svc in to_start:
        name = svc["name"]
        if name in ps_out:
            print(f"[bridge-api]   {name} already running, skipping", flush=True)
            continue
        try:
            r = _run_svc_cmd(svc, "start", timeout=15)
            out = (r.stdout + r.stderr).strip()
            print(f"[bridge-api]   {name} start → exit={r.returncode}"
                  + (f" {out}" if out else ""), flush=True)
        except Exception as e:
            print(f"[bridge-api]   {name} start error: {e}", flush=True)


def main():
    global LISTEN_PORT
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            LISTEN_PORT = int(args[i + 1])
            i += 2
        else:
            i += 1

    print(f"[bridge-api] v{VERSION}  listening on 0.0.0.0:{LISTEN_PORT}", flush=True)
    print(f"[bridge-api] SOCAT  : {SOCAT}",         flush=True)
    print(f"[bridge-api] PID_DIR: {PID_DIR}",       flush=True)
    print(f"[bridge-api] Ports  : {BRIDGE_PORTS}",  flush=True)
    print(f"[bridge-api] Conf   : {PORTS_CONF}",    flush=True)
    print(f"[bridge-api] TCPFwd : {TCP_FWD_HOST}  conf={TCP_FWD_CONF}", flush=True)

    # Stop/start services marked for startup before applying ports
    stop_on_startup()
    start_on_startup()

    # Apply ports.conf and tcp_forwards.conf on startup
    apply_ports_conf()
    apply_tcp_fwds()

    try:
        _ReuseHTTPServer(("0.0.0.0", LISTEN_PORT), ApiHandler).serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge-api] Stopped.")


if __name__ == "__main__":
    main()
