#!/usr/bin/env python3
"""DGX telemetry: run on the Linux HOST. Python 3.10+, standard library only."""
import argparse
import concurrent.futures
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET


def read(path):
    return Path(path).read_text().strip()


def number(value):
    match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(?:MiB|GiB|W|C|%|MHz|RPM)?\s*", value or "")
    return float(match[1]) if match else None


def ident(value):
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def command(args, timeout=8):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=True,
                       env={**os.environ, "LC_ALL": "C"})
    return p.stdout


def atomic_json(path, data, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".dgx-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Collector:
    def __init__(self, config, state=None, now=None):
        self.config = config
        self.state = state if state is not None else {}
        self.now = time.time() if now is None else now
        self.data = {"timestamp": int(self.now), "metrics": {}, "discovery": {},
                     "summary": {}, "errors": [], "inventory": {}}
        self.previous = self.state.get("counters", {})
        self.counters = {}
        self.boot = read("/proc/sys/kernel/random/boot_id") if Path("/proc").exists() else "test"

    def metric(self, group, key, label, value, unit="", alarm=None, direction=1):
        if value is None or not math.isfinite(float(value)):
            return
        mid = ident(key)
        self.data["metrics"][mid] = float(value)
        row = {"{#ID}": mid, "{#LABEL}": label, "{#UNIT}": unit,
               "{#PROFILE}": alarm or "NONE"}
        self.data["discovery"].setdefault(group, []).append(row)

    def rate(self, key, value):
        self.counters[key] = [self.now, value, self.boot]
        old = self.previous.get(key)
        if not old or len(old) != 3 or old[2] != self.boot:
            return None
        dt = self.now - old[0]
        if not 0 < dt <= 180 or value < old[1]:
            return None
        return (value - old[1]) / dt

    def safe(self, label, function):
        try:
            function()
        except Exception as exc:
            self.data["errors"].append(f"{label}: {type(exc).__name__}: {exc}")

    def system(self):
        mem = {}
        for line in read("/proc/meminfo").splitlines():
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0]) * 1024
        total, available = mem["MemTotal"], mem["MemAvailable"]
        used = total - available
        self.data["summary"].update(mem_total=total, mem_available=available, mem_used=used,
                                    mem_used_pct=100 * used / total, page_cache=mem.get("Cached", 0))
        for k, title, v in [("total", "total", total), ("available", "available", available),
                             ("free", "strictly free", mem["MemFree"]), ("used", "used (total - available)", used),
                             ("cached", "page cache", mem.get("Cached", 0)),
                             ("buffers", "buffers", mem.get("Buffers", 0))]:
            self.metric("system", "mem." + k, "RAM " + title, v, "B")
        self.metric("system", "mem.used_pct", "RAM usage / pressure", 100 * used / total, "%", "MEM.WARN")
        swap_total = mem.get("SwapTotal", 0)
        self.metric("system", "swap.total", "Swap total", swap_total, "B")
        self.metric("system", "swap.used", "Swap used", swap_total - mem.get("SwapFree", 0), "B")
        if swap_total:
            self.metric("system", "swap.used_pct", "Swap used", 100 * (swap_total - mem["SwapFree"]) / swap_total, "%", "SWAP.WARN")
        uptime = float(read("/proc/uptime").split()[0])
        self.data["summary"]["uptime"] = uptime
        self.metric("system", "uptime", "Host uptime", uptime, "uptime")
        cpus = os.cpu_count() or 1
        self.metric("system", "cpu.count", "CPU logical cores", cpus)
        stat = read("/proc/stat").splitlines()[0].split()[1:]
        ticks = [int(v) for v in stat[:8]]  # guest fields are already included in user/nice
        total_rate = self.rate("cpu.total", sum(ticks))
        idle_rate = self.rate("cpu.idle", ticks[3] + ticks[4])
        if total_rate and idle_rate is not None:
            pct = max(0, min(100, 100 * (1 - idle_rate / total_rate)))
            self.data["summary"]["cpu_pct"] = pct
            self.metric("system", "cpu.pct", "CPU total utilization", pct, "%", "CPU.WARN")
        loads = read("/proc/loadavg").split()[:3]
        for minutes, load in zip((1, 5, 15), loads):
            self.metric("system", f"load.{minutes}", f"CPU load {minutes}m", float(load))
        self.data["summary"]["load15"] = float(loads[2])
        pressure = Path("/proc/pressure/memory")
        if pressure.exists():
            for line in read(pressure).splitlines():
                fields = line.split()
                values = dict(v.split("=") for v in fields[1:])
                self.metric("system", "psi.memory." + fields[0], "RAM PSI " + fields[0] + " avg10", float(values["avg10"]), "%")
        for line in read("/proc/vmstat").splitlines():
            if line.startswith("oom_kill "):
                self.metric("system", "oom_kill", "OOM killed processes cumulative", int(line.split()[1]))
        for policy in Path("/sys/devices/system/cpu/cpufreq").glob("policy*"):
            try:
                self.metric("system", str(policy), f"CPU frequency {policy.name}", int(read(policy / "scaling_cur_freq")) * 1000, "Hz")
            except OSError:
                pass
        self.data["inventory"]["kernel"] = os.uname().release
        self.data["inventory"]["hostname"] = socket.gethostname()

    def gpu(self):
        root = ET.fromstring(command(["nvidia-smi", "-q", "-x"]))
        self.data["inventory"]["driver"] = root.findtext("driver_version")
        self.data["inventory"]["gpus"] = []
        specs = [
            ("temperature/gpu_temp", "temp", "temperature", "C", "GPU.TEMP.WARN", 1),
            ("utilization/gpu_util", "util", "utilization", "%", None, 1),
            ("utilization/memory_util", "mem_activity", "memory controller activity", "%", None, 1),
            ("gpu_power_readings/power_draw", "power", "NVIDIA-reported power", "W", "POWER.WARN", 1),
            ("gpu_power_readings/power_limit", "power_limit", "reported power limit", "W", None, 1),
            ("fan_speed", "fan", "fan speed", "%", None, 1),
            ("clocks/graphics_clock", "clock", "graphics clock", "Hz", None, 1e6),
            ("clocks/mem_clock", "mem_clock", "memory clock", "Hz", None, 1e6),
            ("fb_memory_usage/total", "fb_total", "framebuffer total (if supported)", "B", None, 1048576),
            ("fb_memory_usage/used", "fb_used", "framebuffer used (if supported)", "B", None, 1048576),
            ("fb_memory_usage/free", "fb_free", "framebuffer free (if supported)", "B", None, 1048576)]
        energy = self.state.setdefault("energy", {})
        for index, gpu in enumerate(root.findall("gpu")):
            uuid = gpu.findtext("uuid") or gpu.get("id", str(index))
            label = f"GPU {index} {gpu.findtext('product_name', 'NVIDIA')}"
            self.data["inventory"]["gpus"].append({"id": uuid, "name": label, "pstate": gpu.findtext("performance_state")})
            measured = {}
            for path, key, title, unit, alarm, multiplier in specs:
                text = gpu.findtext(path)
                if key in ("power", "power_limit") and number(text) is None:
                    text = gpu.findtext("power_readings/" + ("power_draw" if key == "power" else "power_limit"))
                # Some driver XML uses average_power_draw instead of power_draw.
                if key == "power" and number(text) is None:
                    text = gpu.findtext("gpu_power_readings/average_power_draw")
                value = number(text)
                if value is None:
                    continue
                value *= multiplier
                measured[key] = value
                self.metric("gpu", uuid + "." + key, label + " " + title, value, unit, alarm)
            if index == 0:
                for key in ("temp", "util", "power"):
                    if key in measured:
                        self.data["summary"]["gpu_" + key] = measured[key]
            power = measured.get("power")
            if power is not None:
                old = energy.get(uuid, {})
                kwh = old.get("kwh", 0)
                dt = self.now - old.get("timestamp", self.now)
                if old.get("boot") == self.boot and 0 < dt <= 180:
                    kwh += (old["power"] + power) / 2 * dt / 3600000
                energy[uuid] = {"timestamp": self.now, "power": power, "kwh": kwh, "boot": self.boot}
                self.metric("gpu", uuid + ".energy", label + " estimated cumulative energy", kwh, "kWh")
                if index == 0:
                    self.data["summary"]["gpu_kwh"] = kwh
            proc = gpu.findall("processes/process_info")
            values = [number(p.findtext("used_memory")) for p in proc]
            values = [v for v in values if v is not None]
            self.metric("gpu", uuid + ".process_count", label + " listed processes", len(proc))
            if values:
                self.metric("gpu", uuid + ".process_memory", label + " sum of process memory (not total VRAM)", sum(values) * 1048576, "B")

        self.data["summary"]["gpu_query_ok"] = int(bool(root.findall("gpu")))

    def temperatures(self):
        seen = set()
        for chip in Path("/sys/class/hwmon").glob("hwmon*"):
            name = read(chip / "name") if (chip / "name").exists() else chip.name
            for path in list(chip.glob("temp*_input")) + list(chip.glob("fan*_input")) + list(chip.glob("power*_input")) + list(chip.glob("power*_average")):
                try:
                    stem = path.name.rsplit("_", 1)[0]
                    label = read(chip / (stem + "_label")) if (chip / (stem + "_label")).exists() else stem
                    canonical = str(path.resolve())
                    if canonical in seen:
                        continue
                    seen.add(canonical)
                    is_temp, is_power = stem.startswith("temp"), stem.startswith("power")
                    unit = "C" if is_temp else "W" if is_power else "rpm"
                    multiplier = .001 if is_temp else .000001 if is_power else 1
                    kind = "Temperature sensor" if is_temp else "Power sensor" if is_power else "Fan sensor"
                    self.metric("sensors", canonical, f"{kind} {name} {label} {path.name}", float(read(path)) * multiplier, unit, "SENSOR.TEMP.WARN" if is_temp else None)
                except (OSError, ValueError):
                    continue
        # thermal zones often expose SoC/CPU sensors absent from hwmon.
        for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
            try:
                self.metric("sensors", str(zone.resolve()), f"Temperature zone {read(zone / 'type')} {zone.name}", float(read(zone / "temp")) / 1000, "C", "SENSOR.TEMP.WARN")
            except (OSError, ValueError):
                continue

    def filesystems(self):
        excluded = re.compile(self.config.get("filesystem_exclude", r"^/(proc|sys|dev|run|snap)(/|$)|/var/lib/docker/(overlay2|containers)/"))
        pseudo = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup2", "cgroup", "squashfs", "nsfs", "overlay", "autofs", "securityfs", "debugfs", "tracefs", "pstore", "efivarfs", "configfs", "fusectl", "mqueue", "hugetlbfs", "rpc_pipefs", "binfmt_misc"}
        # PID 1 mount table avoids systemd ProtectSystem private bind mounts.
        mountinfo = "/proc/1/mountinfo" if Path("/proc/1/mountinfo").exists() else "/proc/self/mountinfo"
        for line in read(mountinfo).splitlines():
            left, right = line.split(" - ", 1)
            mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), left.split()[4])
            if right.split()[0] in pseudo or excluded.search(mount):
                continue
            try:
                v = os.statvfs(mount)
                total, free, available = v.f_blocks * v.f_frsize, v.f_bfree * v.f_frsize, v.f_bavail * v.f_frsize
                if not total:
                    continue
                used = total - free
                for key, title, value in [("total", "total", total), ("used", "used", used), ("available", "available to users", available), ("free", "free including reserved", free)]:
                    self.metric("filesystems", mount + "." + key, f"FS {mount} {title}", value, "B")
                # User-usable capacity accounts for blocks reserved for root.
                pct = 100 * used / (used + available) if used + available else 100
                self.metric("filesystems", mount + ".used_pct", f"FS {mount} used", pct, "%", "FS.WARN")
                if v.f_files:
                    self.metric("filesystems", mount + ".inodes", f"FS {mount} inodes used", 100 * (v.f_files - v.f_ffree) / v.f_files, "%", "INODE.WARN")
                if mount == "/":
                    self.data["summary"]["root_used_pct"] = pct
                    self.data["summary"].update(root_used=used, root_free=free, root_total=total)
            except OSError as exc:
                self.data["errors"].append(f"FS {mount}: {exc}")

    def disks(self):
        selected_rates = {"read": [], "write": []}
        selected = []
        for line in read("/proc/diskstats").splitlines():
            fields = line.split()
            device = fields[2]
            if not (Path("/sys/block") / device).exists() or device.startswith(("loop", "ram", "zram")):
                continue
            values = [int(x) for x in fields[3:]]
            configured = self.config.get("dashboard_disks", [])
            include = device in configured if configured else (Path("/sys/block") / device / "device").exists()
            if include:
                selected.append(device)
            for name, title, raw, scale, unit in [("read", "read", values[2], 512, "Bps"), ("write", "write", values[6], 512, "Bps"), ("busy", "busy time", values[9], .1, "%")]:
                rate = self.rate(device + "." + name, raw)
                if include and name in selected_rates:
                    selected_rates[name].append(None if rate is None else rate * scale)
                if rate is not None:
                    self.metric("disks", device + "." + name, f"Disk {device} {title}", rate * scale, unit)
            ops = self.rate(device + ".ops", values[0] + values[4])
            ms = self.rate(device + ".ms", values[3] + values[7])
            if ops is not None and ms is not None:
                self.metric("disks", device + ".latency", f"Disk {device} average IO latency", ms / ops / 1000 if ops else 0, "s")
            self.metric("disks", device + ".queue", f"Disk {device} IO in progress", values[8])
        self.data["inventory"]["dashboard_disks"] = selected
        for name, values in selected_rates.items():
            if values and all(v is not None for v in values):
                self.data["summary"]["disk_" + name + "_bps"] = sum(values)

    def network(self):
        selected_rates = {"RX": [], "TX": []}
        selected = []
        for line in read("/proc/net/dev").splitlines()[2:]:
            iface, values = line.split(":", 1)
            iface, fields = iface.strip(), values.split()
            if iface == "lo" or re.match(self.config.get("interface_exclude", r"^(veth|docker|br-)"), iface):
                continue
            configured = self.config.get("dashboard_interfaces", [])
            include = iface in configured if configured else (Path("/sys/class/net") / iface / "device").exists()
            if include:
                selected.append(iface)
            for title, index in [("RX", 0), ("TX", 8)]:
                rate = self.rate(iface + title, int(fields[index]))
                if include:
                    selected_rates[title].append(None if rate is None else rate * 8)
                if rate is not None:
                    self.metric("network", iface + title, f"Network {iface} {title}", rate * 8, "bps")
            for title, index in [("errores RX", 2), ("errores TX", 10), ("descartes RX", 3), ("descartes TX", 11)]:
                # Keep legacy identity suffixes to preserve existing item history.
                english = title.replace("errores", "errors").replace("descartes", "drops")
                self.metric("network", iface + title, f"Network {iface} {english} cumulative", int(fields[index]))
        self.data["inventory"]["dashboard_interfaces"] = selected
        for name, values in selected_rates.items():
            if values and all(v is not None for v in values):
                self.data["summary"]["network_" + name.lower() + "_bps"] = sum(values)

    def caches(self, scan=False):
        ranking = []
        sizes = self.state.setdefault("directories", {})
        interval = self.config.get("directory_interval_seconds", 1800)
        for pattern in self.config.get("directories", []):
            # glob expansion is local; no shell or command interpolation.
            import glob
            paths = glob.glob(pattern) if glob.has_magic(pattern) else [pattern]
            for path in paths:
                old = sizes.get(path, {})
                exists = Path(path).is_dir()
                self.metric("cache", path + ".exists", f"Directory {path} exists", int(exists))
                if not exists:
                    continue
                if scan and self.now - old.get("timestamp", 0) >= interval:
                    try:
                        result = command(["du", "-s", "-B1", "-x", "--", path], timeout=self.config.get("directory_timeout_seconds", 45))
                        old = {"timestamp": self.now, "bytes": int(result.split()[0]), "ok": 1}
                    except Exception as exc:
                        old = {**old, "ok": 0}
                        self.data["errors"].append(f"du {path}: {type(exc).__name__}")
                    sizes[path] = old
                self.metric("cache", path + ".ok", f"Directory {path} measurement successful", old.get("ok", 0), alarm="AVAIL.MIN", direction=-1)
                if "bytes" in old:
                    age = max(0, self.now - old["timestamp"])
                    ranking.append(dict(path=path, bytes=old["bytes"], age=age, ok=old.get("ok", 0)))
                    self.metric("cache", path + ".bytes", f"Directory {path} allocated space", old["bytes"], "B")
                    self.metric("cache", path + ".age", f"Directory {path} measurement age", age, "s", "CACHE.AGE.WARN")
                    capacity = os.statvfs(path)
                    total = capacity.f_blocks * capacity.f_frsize
                    if total:
                        self.metric("cache", path + ".pct", f"Directory {path} share of filesystem", old["bytes"] / total * 100, "%", "CACHE.WARN")
        ranking.sort(key=lambda row: (-row["bytes"], row["path"]))
        self.data["directories"] = ranking
        lines = ["SIZE (GiB) | AGE (s) | STATUS | DIRECTORY"]
        for row in ranking:
            status = "OK" if row["ok"] and row["age"] <= 5400 else "STALE/FAILED"
            lines.append(f"{row['bytes'] / 1073741824:10.3f} | {row['age']:7.0f} | {status} | {row['path']}")
        self.data["directory_ranking"] = "\n".join(lines) if ranking else "No measured directories yet. Check the directory scan timer and configured paths."

    def probe(self, endpoint):
        host, port = endpoint.get("host", "127.0.0.1"), int(endpoint["port"])
        started = time.monotonic()
        out = {"tcp": 0, "seconds": 0}
        try:
            with socket.create_connection((host, port), timeout=endpoint.get("timeout", 1)):
                out["tcp"] = 1
        except OSError:
            pass
        out["seconds"] = time.monotonic() - started
        if endpoint.get("health_path"):
            out.update(http_ok=0, http_status=0)
            if out["tcp"]:
                scheme = endpoint.get("scheme", "http")
                address = f"[{host}]" if ":" in host else host
                url = f"{scheme}://{address}:{port}{endpoint['health_path']}"
                headers = {}
                if endpoint.get("token_env") and os.environ.get(endpoint["token_env"]):
                    headers["Authorization"] = "Bearer " + os.environ[endpoint["token_env"]]
                # No redirects and no ambient HTTP proxy for local monitoring.
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, req, fp, code, msg, headers, newurl):
                        return None
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                try:
                    with opener.open(urllib.request.Request(url, headers=headers), timeout=endpoint.get("timeout", 1)) as response:
                        out["http_status"] = response.status
                except urllib.error.HTTPError as exc:
                    out["http_status"] = exc.code
                    exc.close()
                except (OSError, urllib.error.URLError):
                    pass
                out["http_ok"] = int(out["http_status"] in endpoint.get("ok_codes", [200]))
        return out

    def services(self):
        remembered = self.state.setdefault("services", {})
        endpoints = self.config.get("services", [])
        self.data["summary"].update(engines_detected=0, engines_active=0)
        if not endpoints:
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            probes = list(pool.map(self.probe, endpoints))
        for endpoint, result in zip(endpoints, probes):
            key = endpoint.get("host", "127.0.0.1") + ":" + str(endpoint["port"])
            if result["tcp"]:
                remembered[key] = {"last_seen": self.now}
            if key not in remembered:
                continue
            self.data["summary"]["engines_detected"] += 1
            self.data["summary"]["engines_active"] += result["tcp"]
            title = f"Service {endpoint['name']} {key}"
            self.metric("services", key + ".tcp", title + " TCP availability", result["tcp"], alarm="AVAIL.MIN", direction=-1)
            self.metric("services", key + ".latency", title + " TCP connection time", result["seconds"], "s", "SERVICE.LATENCY.WARN")
            for field in ("http_ok", "http_status"):
                if field in result:
                    self.metric("services", key + "." + field, title + " " + field, result[field], alarm="AVAIL.MIN" if field == "http_ok" else None, direction=-1)

    def processes(self):
        profiles = self.config.get("process_groups", [])
        if not profiles:
            return
        processes = {}
        hz, page = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")
        for folder in Path("/proc").glob("[0-9]*"):
            try:
                cmd = (folder / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                fields = read(folder / "stat").rsplit(")", 1)[1].split()
                pid = int(folder.name)
                processes[pid] = dict(cmd=cmd, ppid=int(fields[1]), ticks=int(fields[11]) + int(fields[12]), start=fields[19], rss=int(fields[21]) * page)
            except (OSError, ValueError, IndexError):
                continue
        for profile in profiles:
            ids = {pid for pid, p in processes.items() if re.search(profile["match"], p["cmd"], re.I) and not (profile.get("exclude") and re.search(profile["exclude"], p["cmd"], re.I))}
            if not ids:
                continue
            if profile.get("children", True):
                while True:
                    more = {pid for pid, p in processes.items() if p["ppid"] in ids}
                    if more <= ids:
                        break
                    ids |= more
            title, name = profile["name"], profile["name"]
            self.metric("processes", name + ".count", title + " processes (group)", len(ids))
            rss = sum(processes[pid]["rss"] for pid in ids)
            self.metric("processes", name + ".rss", title + " sum RSS (pages may be shared)", rss, "B")
            total = self.data["summary"].get("mem_total")
            if total:
                self.metric("processes", name + ".rss_pct", title + " RSS / total RAM", rss / total * 100, "%", "PROCESS.RSS.WARN")
            rates = [self.rate(f"pid.{pid}.{processes[pid]['start']}", processes[pid]["ticks"]) for pid in ids]
            if any(rate is not None for rate in rates):
                pct = sum(rate or 0 for rate in rates) / hz / (os.cpu_count() or 1) * 100
                self.metric("processes", name + ".cpu", title + " CPU / total capacity", pct, "%", "PROCESS.CPU.WARN")

    def run(self):
        for name in ("system", "gpu", "temperatures", "filesystems", "disks", "network", "services", "processes", "caches"):
            self.safe(name, getattr(self, name))
        self.data["summary"].setdefault("gpu_query_ok", 0)
        self.state["counters"] = self.counters
        # Preserve a discovered capability through a transient N/A; discard its absent value.
        capabilities = self.state.setdefault("capabilities", {})
        for group, rows in self.data["discovery"].items():
            for row in rows:
                capabilities.setdefault(group, {})[row["{#ID}"]] = {"row": row, "seen": self.now}
        for group, entries in capabilities.items():
            self.data["discovery"][group] = [v["row"] for v in entries.values() if self.now - v["seen"] < 86400 * 7]
        self.data["error_count"] = len(self.data["errors"])
        return self.data


def validate_config(config):
    endpoints = config.get("services", [])
    keys = []
    for e in endpoints:
        if not 1 <= int(e["port"]) <= 65535:
            raise ValueError("Port out of range")
        host = e.get("host", "127.0.0.1")
        # Numeric addresses only: deterministic timeout and no DNS stalls.
        import ipaddress
        ipaddress.ip_address(host)
        keys.append((host, int(e["port"])))
        if e.get("scheme", "http") not in ("http", "https"):
            raise ValueError("scheme must be http or https")
        if e.get("health_path") and not e["health_path"].startswith("/"):
            raise ValueError("health_path must start with /")
        if not 0 < e.get("timeout", 1) <= 5:
            raise ValueError("timeout must be >0 and <=5s")
    if len(set(keys)) != len(keys):
        raise ValueError("Only one profile per host:port; do not duplicate engines on the same endpoint")
    if len(keys) > 64:
        raise ValueError("Maximum 64 endpoints")
    for p in config.get("process_groups", []):
        re.compile(p["match"])
        re.compile(p.get("exclude", ""))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/zabbix/dgx-spark.json")
    parser.add_argument("--state", default="/var/lib/dgx-spark/state.json")
    parser.add_argument("--output", default="/var/lib/dgx-spark/public/telemetry.json")
    parser.add_argument("--directory-state", default="/var/lib/dgx-spark/directories.json")
    parser.add_argument("--scan-directories", action="store_true")
    args = parser.parse_args()
    # Host service only. Serialize timer/manual invocations, including cache scans.
    import fcntl
    state_path = Path(args.directory_state if args.scan_directories else args.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(state_path) + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = json.loads(read(args.config))
        validate_config(config)
        state = json.loads(read(state_path)) if state_path.exists() else {}
        if not args.scan_directories and Path(args.directory_state).exists():
            state.update(json.loads(read(args.directory_state)))
        collector = Collector(config, state)
        if args.scan_directories:
            collector.caches(scan=True)
            atomic_json(args.directory_state, {"directories": state.get("directories", {})}, 0o600)
            return
        data = collector.run()
        atomic_json(args.state, state, 0o600)
        atomic_json(args.output, data)


if __name__ == "__main__":
    main()
