#!/usr/bin/env python3
"""Generate deterministic Zabbix 7.0 JSON exports and a structured release ZIP."""
import hashlib
import json
from pathlib import Path
import uuid
import zipfile

from scripts.dashboard import build_dashboard

NAME = "DGX Spark GB10 by host collector"
BASE = Path(__file__).resolve().parents[1]
TEMPLATES = BASE / "templates"
DIST = BASE / "dist"
PACKAGE_FILES = (
    "README.md", "docs/README.es.md",
    "docs/images/dashboard-overview.png", "docs/images/dashboard-io-temperatures-directories.png",
    "docs/images/dashboard-history.png", "docs/images/host-integration.png",
    "scripts/collector.py", "scripts/build_template.py", "scripts/dashboard.py",
    "config/config.example.json", "docker/compose.yaml", "docker/dgx-spark.conf", "docker/.env.example",
    "services/dgx-spark-collector.service", "services/dgx-spark-collector.timer",
    "services/dgx-spark-directories.service", "services/dgx-spark-directories.timer",
    "tests/test_collector.py", "tests/test_dashboard.py", ".gitignore",
    "templates/template_dgx_spark_7_active.json", "templates/template_dgx_spark_7_passive.json",
)


def uid(text):
    return uuid.UUID(bytes=hashlib.md5((NAME + text).encode()).digest(), version=4).hex


def preprocess(path):
    return [{"type": "JSONPATH", "parameters": [path], "error_handler": "DISCARD_VALUE"}]


def tag(value):
    return [{"tag": "component", "value": value}]


def dep(key, name, path, unit="", value_type="FLOAT", component="system"):
    out = dict(uuid=uid(key), name=name, type="DEPENDENT", key=key, delay="0",
               history="7d", value_type=value_type,
               preprocessing=preprocess(path), master_item={"key": "dgx.telemetry"}, tags=tag(component))
    if unit:
        out["units"] = unit
    if value_type in ("TEXT", "CHAR"):
        out["trends"] = "0"
    return out


LIMITS = {
    "MEM.WARN": (90, 85), "SWAP.WARN": (80, 70), "CPU.WARN": (95, 85),
    "GPU.TEMP.WARN": (85, 80), "SENSOR.TEMP.WARN": (85, 80),
    "POWER.WARN": (150, 140), "FS.WARN": (85, 80), "INODE.WARN": (90, 85),
    "CACHE.WARN": (50, 45), "CACHE.AGE.WARN": (5400, 3600),
    "AVAIL.MIN": (0.5, 0.5), "SERVICE.LATENCY.WARN": (0.8, 0.5),
    "PROCESS.RSS.WARN": (90, 80), "PROCESS.CPU.WARN": (101, 95)
}
GROUPS = {
    "system": ["NONE", "MEM.WARN", "SWAP.WARN", "CPU.WARN"],
    "gpu": ["NONE", "GPU.TEMP.WARN", "POWER.WARN"],
    "sensors": ["NONE", "SENSOR.TEMP.WARN"],
    "filesystems": ["NONE", "FS.WARN", "INODE.WARN"],
    "disks": ["NONE"], "network": ["NONE"],
    "cache": ["NONE", "AVAIL.MIN", "CACHE.WARN", "CACHE.AGE.WARN"],
    "services": ["NONE", "AVAIL.MIN", "SERVICE.LATENCY.WARN"],
    "processes": ["NONE", "PROCESS.RSS.WARN", "PROCESS.CPU.WARN"]
}


def trigger(key, suffix, expression, name, priority="WARNING", recovery=None):
    result = dict(uuid=uid(key + suffix), expression=expression, name=name, priority=priority,
                  tags=[{"tag": "scope", "value": "availability" if suffix == "stale" else "performance"}])
    if recovery:
        result.update(recovery_mode="RECOVERY_EXPRESSION", recovery_expression=recovery)
    return result


def build(active):
    template = dict(uuid=uid("template"), template=NAME, name=NAME,
                    description="DGX Spark / NVIDIA GB10. Linux HOST collector + read-only JSON for containerized Agent 2. Base format 7.0; target deployment reported as Server 7.0.31 and Agent 2 7.4.15 (live validation pending). Default service candidates include Ollama, vLLM, ComfyUI, llama.cpp, LM Studio, SGLang and TensorFold TCP/8888; ports are configured in the host collector JSON, not this export. UMA RAM is not dedicated VRAM. Unsupported NVIDIA values are omitted. Power is the NVIDIA-reported domain, not wall power. See README.md.",
                    groups=[{"name": "Templates/Servers/Hardware"}], items=[], discovery_rules=[], macros=[], dashboards=[])
    items = template["items"]
    items.append(dict(uuid=uid("master"), name="DGX: host telemetry JSON", type="ZABBIX_ACTIVE" if active else "ZABBIX_PASSIVE",
                      key="dgx.telemetry", delay="{$DGX.INTERVAL}", history="1d", value_type="TEXT", trends="0", tags=tag("collector")))
    items += [dep("dgx.timestamp", "DGX: host snapshot timestamp", "$.timestamp", "unixtime"),
              dep("dgx.errors", "DGX: collector error count", "$.error_count"),
              dep("dgx.diagnostics", "DGX: diagnostics JSON", "$.errors", value_type="TEXT"),
              dep("dgx.inventory", "DGX: inventory JSON", "$.inventory", value_type="TEXT")]
    summary = {
        "cpu_pct": ("CPU utilization", "%"), "mem_total": ("Unified RAM total", "B"),
        "mem_available": ("RAM available", "B"), "mem_used": ("RAM used", "B"),
        "mem_used_pct": ("RAM pressure", "%"), "root_used_pct": ("Root filesystem used", "%"),
        "gpu_temp": ("GPU 0 temperature", "C"), "gpu_util": ("GPU 0 utilization", "%"),
        "gpu_power": ("GPU 0 NVIDIA-reported power", "W"), "gpu_kwh": ("GPU 0 estimated cumulative energy", "kWh"),
        "gpu_query_ok": ("NVIDIA query successful", ""),
        "uptime": ("Host uptime", "uptime"), "load15": ("CPU load 15m", ""),
        "page_cache": ("RAM page cache", "B"), "engines_detected": ("Detected engine endpoints", ""),
        "engines_active": ("Active engine endpoints", ""),
        "root_used": ("Root filesystem used space", "B"), "root_free": ("Root filesystem free space including reserved", "B"),
        "root_total": ("Root filesystem total space", "B"),
        "network_rx_bps": ("Network IN (selected interfaces)", "bps"),
        "network_tx_bps": ("Network OUT (selected interfaces)", "bps"),
        "disk_read_bps": ("Disk read (selected devices)", "Bps"),
        "disk_write_bps": ("Disk write (selected devices)", "Bps")}
    for key, (title, unit) in summary.items():
        items.append(dep("dgx.summary[" + key + "]", "DGX: " + title, "$.summary." + key, unit))
    fresh = f'fuzzytime(/{NAME}/dgx.timestamp,{{$DGX.STALE}})=1'
    items[1]["triggers"] = [trigger("dgx.timestamp", "stale", f'nodata(/{NAME}/dgx.timestamp,{{$DGX.STALE}})=1 or fuzzytime(/{NAME}/dgx.timestamp,{{$DGX.STALE}})=0', "DGX: host snapshot missing, stale or clock out of sync", "HIGH")]
    items[2]["triggers"] = [trigger("dgx.errors", "errors", f'min(/{NAME}/dgx.errors,{{$DGX.WINDOW}})>0 and {fresh}', "DGX: persistent collector errors; check diagnostics")]
    next(i for i in items if i["key"] == "dgx.summary[gpu_query_ok]")["triggers"] = [trigger("dgx.gpu_query", "failed", f'max(/{NAME}/dgx.summary[gpu_query_ok],{{$DGX.WINDOW}})=0 and {fresh}', "DGX: nvidia-smi query failing")]
    items.append(dep("dgx.directory.ranking", "DGX: directory sizes ranked descending", "$.directory_ranking", value_type="TEXT", component="cache"))
    age = dep("dgx.snapshot.age", "DGX: host snapshot age", "$.timestamp", "s", component="collector")
    age["preprocessing"].append(dict(type="JAVASCRIPT", parameters=["return Math.max(0, Math.floor(Date.now() / 1000 - Number(value))); "]))
    items.append(age)
    for group, profiles in GROUPS.items():
        for profile in profiles:
            rid = group + "." + profile
            key = "dgx.metric[" + rid + ",{#ID}]"
            item = dep(key, "{#LABEL}", "$.metrics['{#ID}']", "{#UNIT}", component=group)
            item["uuid"] = uid("prototype." + rid)
            item["description"] = "Host metric. Missing readings are discarded; N/A is never converted to zero."
            item["trigger_prototypes"] = [trigger(rid, "stale", f'nodata(/{NAME}/{key},{{$DGX.METRIC.STALE}})=1 and {fresh}', "DGX: no readings for {#LABEL}")]
            if profile != "NONE":
                low = profile == "AVAIL.MIN"
                op, rop, agg = ("<", ">=", "max") if low else (">", "<=", "min")
                threshold = "{$DGX." + profile + ':"{#ID}"}'
                recovery = "{$DGX." + profile + '.RECOVERY:"{#ID}"}'
                condition = f'{agg}(/{NAME}/{key},{{$DGX.WINDOW}}){op}{threshold} and count(/{NAME}/{key},{{$DGX.WINDOW}})>=3 and {fresh}'
                item["trigger_prototypes"].append(trigger(rid, "limit", condition, "DGX: {#LABEL} sustained threshold violation", "AVERAGE" if low else "WARNING", f'last(/{NAME}/{key}){rop}{recovery}'))
                if low and group == "services":
                    item["valuemap"] = {"name": "Service availability"}
            rule = dict(uuid=uid("discovery." + rid), name="DGX: discover " + rid,
                        type="DEPENDENT", key="dgx.discovery[" + rid + "]", delay="0", lifetime="30d",
                        master_item={"key": "dgx.telemetry"},
                        preprocessing=preprocess("$.discovery." + group) + [dict(type="JAVASCRIPT", parameters=['return JSON.stringify(JSON.parse(value).filter(function(x) { return x["{#PROFILE}"] === ' + json.dumps(profile) + '; }));']), dict(type="DISCARD_UNCHANGED_HEARTBEAT", parameters=["1h"])],
                        # 7.0.31 can reject inheritance of equally named graph
                        # prototypes across different LLD rules (ZBX-26421).
                        # Scope the literal name by rule and expanded name by ID.
                        item_prototypes=[item], graph_prototypes=[dict(uuid=uid("graph." + rid), name="DGX " + rid + ": {#LABEL} [{#ID}]", graph_items=[dict(color="199C0D", item={"host": NAME, "key": key})])])
            template["discovery_rules"].append(rule)
    for key, value in {"INTERVAL": "1m", "WINDOW": "5m", "STALE": "5m", "METRIC.STALE": "15m"}.items():
        template["macros"].append({"macro": "{$DGX." + key + "}", "value": value})
    for key, (warning, recovery) in LIMITS.items():
        for suffix, value in [("", warning), (".RECOVERY", recovery)]:
            template["macros"].append(dict(macro="{$DGX." + key + suffix + "}", value=str(value), description="Configurable provisional threshold; not a manufacturer specification."))
    graphs = []
    # Keep legacy UUID seeds while translating display names, preserving history.
    definitions = [("CPU uso", "CPU utilization", ["cpu_pct"]),
                   ("RAM unificada", "Unified RAM", ["mem_used", "mem_available"]),
                   ("GPU temperatura", "GPU temperature", ["gpu_temp"]),
                   ("GPU utilizacion", "GPU utilization", ["gpu_util"]),
                   ("Potencia reportada NVIDIA", "NVIDIA-reported power", ["gpu_power"]),
                   ("Energia estimada NVIDIA", "Estimated NVIDIA energy", ["gpu_kwh"]),
                   ("Presion RAM y disco raiz", "RAM pressure and root filesystem", ["mem_used_pct", "root_used_pct"]),
                   ("Network IN and OUT", "Network IN and OUT", ["network_rx_bps", "network_tx_bps"]),
                   ("Disk read and write", "Disk read and write", ["disk_read_bps", "disk_write_bps"])]
    colors = ["199C0D", "F63100", "2774A4"]
    for legacy, title, keys in definitions:
        graphs.append(dict(uuid=uid("summarygraph." + legacy), name="DGX: " + title,
                           graph_items=[dict(sortorder=str(i), color=colors[i], item={"host": NAME, "key": "dgx.summary[" + key + "]"}) for i, key in enumerate(keys)]))
    template["valuemaps"] = [dict(uuid=uid("valuemap.availability"), name="Service availability", mappings=[
        {"value": "0", "newvalue": "DOWN"}, {"value": "1", "newvalue": "UP"}])]
    template["dashboards"] = [build_dashboard(NAME, graphs, uid)]
    return {"zabbix_export": dict(version="7.0", template_groups=[dict(uuid=uid("group"), name="Templates/Servers/Hardware")], templates=[template], graphs=graphs)}


def main():
    TEMPLATES.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    for active in (False, True):
        export = build(active)
        name = "template_dgx_spark_7_" + ("active" if active else "passive")
        (TEMPLATES / (name + ".json")).write_text(json.dumps(export, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("Generated active/passive JSON in templates/ (import only ONE variant).")
    # Explicit manifest excludes local configs, secrets, caches and prior archives.
    with zipfile.ZipFile(DIST / "dgx-spark-zabbix7.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in PACKAGE_FILES:
            archive.write(BASE / relative, "dgx-spark-zabbix7/" + relative)
    print("Refreshed dist/dgx-spark-zabbix7.zip.")


if __name__ == "__main__":
    main()
