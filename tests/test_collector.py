import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.collector import Collector, atomic_json, ident, number, validate_config
from scripts.build_template import build, GROUPS

XML = '''<nvidia_smi_log><driver_version>580.178.04</driver_version><gpu id="0000:01:00.0">
<uuid>GPU-test</uuid><product_name>NVIDIA GB10</product_name><performance_state>P0</performance_state>
<temperature><gpu_temp>69 C</gpu_temp></temperature><fan_speed>N/A</fan_speed>
<fb_memory_usage><total>Not Supported</total><used>N/A</used><free>[N/A]</free></fb_memory_usage>
<utilization><gpu_util>8 %</gpu_util></utilization>
<gpu_power_readings><average_power_draw>43 W</average_power_draw><power_limit>N/A</power_limit></gpu_power_readings>
<processes><process_info><used_memory>30347 MiB</used_memory></process_info></processes>
</gpu></nvidia_smi_log>'''


class Tests(unittest.TestCase):
    def test_unavailable_is_not_zero(self):
        for value in ("N/A", "[N/A]", "Not Supported", "NaN", "inf", "", None):
            self.assertIsNone(number(value))
        self.assertEqual(number("0 W"), 0)
        self.assertEqual(number("43.5 W"), 43.5)

    @patch("scripts.collector.command", return_value=XML)
    def test_gb10_capabilities_and_units(self, _):
        c = Collector({}, now=100)
        c.gpu()
        values = c.data["metrics"]
        self.assertNotIn(ident("GPU-test.fb_total"), values)
        self.assertNotIn(ident("GPU-test.fan"), values)
        self.assertEqual(values[ident("GPU-test.power")], 43)
        self.assertEqual(values[ident("GPU-test.process_memory")], 30347 * 1048576)
        self.assertEqual(c.data["summary"]["gpu_temp"], 69)

    @patch("scripts.collector.command", return_value=XML)
    def test_energy_integrates_and_skips_long_gaps_and_reboot(self, _):
        state = {}
        c = Collector({}, state, 100)
        c.gpu()
        c = Collector({}, state, 160)
        c.gpu()
        self.assertAlmostEqual(c.data["summary"]["gpu_kwh"], 43 * 60 / 3600000)
        before = c.data["summary"]["gpu_kwh"]
        c = Collector({}, state, 1000)
        c.gpu()
        self.assertEqual(c.data["summary"]["gpu_kwh"], before)
        c = Collector({}, state, 1060)
        c.boot = "another-boot"
        c.gpu()
        self.assertEqual(c.data["summary"]["gpu_kwh"], before)

    def test_service_persists_when_down(self):
        config = {"services": [{"name": "Ollama", "port": 11434}]}
        state = {}
        c = Collector(config, state, 100)
        with patch.object(c, "probe", return_value={"tcp": 0, "seconds": .01}):
            c.services()
        self.assertNotIn("services", c.data["discovery"])
        with patch.object(c, "probe", return_value={"tcp": 1, "seconds": .01}):
            c.services()
        c = Collector(config, state, 160)
        with patch.object(c, "probe", return_value={"tcp": 0, "seconds": 1}):
            c.services()
        self.assertEqual(c.data["metrics"][ident("127.0.0.1:11434.tcp")], 0)
        self.assertTrue(c.data["discovery"]["services"])

    def test_counter_reset_and_missing_initial_sample(self):
        c = Collector({}, now=100)
        self.assertIsNone(c.rate("x", 100))
        state = {"counters": c.counters}
        c = Collector({}, state, 160)
        self.assertEqual(c.rate("x", 220), 2)
        self.assertIsNone(c.rate("x", 50))

    def test_cache_failure_keeps_old_size_and_marks_invalid(self):
        with tempfile.TemporaryDirectory() as root:
            state = {"directories": {root: {"timestamp": 1, "bytes": 12345, "ok": 1}}}
            c = Collector({"directories": [root]}, state, 10000)
            with patch("scripts.collector.command", side_effect=TimeoutError), patch("scripts.collector.os.statvfs", create=True, return_value=SimpleNamespace(f_blocks=100000, f_frsize=4096)):
                c.caches(scan=True)
            self.assertEqual(c.data["metrics"][ident(root + ".ok")], 0)
            self.assertEqual(c.data["metrics"][ident(root + ".bytes")], 12345)
            self.assertEqual(c.data["metrics"][ident(root + ".age")], 9999)

    def test_fast_collection_never_runs_du(self):
        with tempfile.TemporaryDirectory() as root:
            c = Collector({"directories": [root]}, now=100)
            with patch("scripts.collector.command") as command:
                c.caches()
            command.assert_not_called()

    def test_config_rejects_duplicate_or_unbounded_endpoints(self):
        e = {"name": "test", "port": 8000}
        with self.assertRaises(ValueError):
            validate_config({"services": [e, e]})
        with self.assertRaises(ValueError):
            validate_config({"services": [{**e, "timeout": 1000}]})
        validate_config(json.loads((Path(__file__).resolve().parents[1] / "config" / "config.example.json").read_text()))

    def test_atomic_output_complete(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "public" / "data.json"
            atomic_json(path, {"ok": 1})
            atomic_json(path, {"ok": 2})
            self.assertEqual(json.loads(path.read_text()), {"ok": 2})

    def test_export_reference_integrity_and_unique_keys(self):
        for active in (True, False):
            data = build(active)
            t = data["zabbix_export"]["templates"][0]
            keys = [i["key"] for i in t["items"]]
            uuids = []
            def visit(value):
                if isinstance(value, dict):
                    if "uuid" in value:
                        uuids.append(value["uuid"])
                    for v in value.values():
                        visit(v)
                elif isinstance(value, list):
                    for v in value:
                        visit(v)
            visit(data)
            self.assertEqual(len(uuids), len(set(uuids)))
            for rule in t["discovery_rules"]:
                self.assertEqual(rule["master_item"]["key"], "dgx.telemetry")
                for i in rule["item_prototypes"]:
                    keys.append(i["key"])
                    self.assertIn("{#ID}", i["key"])
            self.assertEqual(len(keys), len(set(keys)))
            for graph in data["zabbix_export"]["graphs"]:
                for i in graph["graph_items"]:
                    self.assertIn(i["item"]["key"], keys)

    def test_graph_names_allow_host_inheritance_and_duplicate_labels(self):
        # Reproduce the 7.0.31 host-link constraint: different LLD rules
        # must not inherit identically named graph prototypes.
        for active in (True, False):
            export = build(active)["zabbix_export"]
            static_names = [g["name"] for g in export["graphs"]]
            names = list(static_names)
            expanded = list(static_names)
            for rule in export["templates"][0]["discovery_rules"]:
                for graph in rule["graph_prototypes"]:
                    names.append(graph["name"])
                    # Identical labels can also occur within a discovery rule.
                    for mid in ("sensor-a", "sensor-b"):
                        expanded.append(graph["name"].replace("{#LABEL}", "Temperatura CPU").replace("{#ID}", mid))
            self.assertEqual(len(names), len(set(names)), "Duplicate literal graph/prototype names prevent host linking")
            self.assertEqual(len(expanded), len(set(expanded)), "Duplicate discovered graph names")


if __name__ == "__main__":
    unittest.main()
