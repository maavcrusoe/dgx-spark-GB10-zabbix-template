import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.build_template import build, NAME, uid
from scripts.collector import Collector


class DashboardTests(unittest.TestCase):
    def test_network_summary_uses_selected_interfaces_and_needs_two_samples(self):
        def net(rx, tx):
            return "header\nheader\neth0: " + f"{rx} 0 0 0 0 0 0 0 {tx} 0 0 0 0 0 0 0\n" + "eth1: 9999 0 0 0 0 0 0 0 9999 0 0 0 0 0 0 0"
        config = {"dashboard_interfaces": ["eth0"]}
        first = Collector(config, now=100)
        with patch("scripts.collector.read", return_value=net(100, 200)):
            first.network()
        self.assertNotIn("network_rx_bps", first.data["summary"])
        second = Collector(config, {"counters": first.counters}, now=160)
        with patch("scripts.collector.read", return_value=net(220, 440)):
            second.network()
        self.assertEqual(second.data["summary"]["network_rx_bps"], 16)
        self.assertEqual(second.data["summary"]["network_tx_bps"], 32)
        self.assertEqual(second.data["inventory"]["dashboard_interfaces"], ["eth0"])

    def test_disk_summary_has_read_and_write_in_bytes_per_second(self):
        def stats(read_sectors, write_sectors):
            return f"259 0 nvme0n1 1 0 {read_sectors} 0 1 0 {write_sectors} 0 0 0 0"
        config = {"dashboard_disks": ["nvme0n1"]}
        first = Collector(config, now=100)
        with patch("scripts.collector.read", return_value=stats(100, 200)), patch("scripts.collector.Path.exists", return_value=True):
            first.disks()
        self.assertNotIn("disk_read_bps", first.data["summary"])
        second = Collector(config, {"counters": first.counters}, now=160)
        with patch("scripts.collector.read", return_value=stats(220, 440)), patch("scripts.collector.Path.exists", return_value=True):
            second.disks()
        self.assertEqual(second.data["summary"]["disk_read_bps"], 1024)
        self.assertEqual(second.data["summary"]["disk_write_bps"], 2048)

    def test_directory_ranking_sorts_bytes_not_path_and_keeps_age(self):
        with tempfile.TemporaryDirectory() as root:
            paths = [str(Path(root) / name) for name in ("a-small", "z-large", "m-medium")]
            for p in paths:
                Path(p).mkdir()
            state = {"directories": {p: dict(bytes=size, timestamp=900, ok=1) for p, size in zip(paths, (10, 1000, 100))}}
            collector = Collector({"directories": paths}, state, now=1000)
            with patch("scripts.collector.os.statvfs", create=True, return_value=SimpleNamespace(f_blocks=1000, f_frsize=4096)):
                collector.caches()
            self.assertEqual([r["bytes"] for r in collector.data["directories"]], [1000, 100, 10])
            self.assertTrue(all(r["age"] == 100 for r in collector.data["directories"]))
            lines = collector.data["directory_ranking"].splitlines()
            self.assertIn("z-large", lines[1])
            self.assertIn("a-small", lines[3])

    def test_all_dashboard_references_layouts_and_required_widgets(self):
        for active in (True, False):
            export = build(active)["zabbix_export"]
            template = export["templates"][0]
            item_keys = {i["key"] for i in template["items"]}
            graphs = {g["name"]: g for g in export["graphs"]}
            refs = set()
            kinds = set()
            for page in template["dashboards"][0]["pages"]:
                rects = []
                for widget in page["widgets"]:
                    kinds.add(widget["type"])
                    x, y, w, h = (int(widget[k]) for k in ("x", "y", "width", "height"))
                    self.assertTrue(0 <= x < x + w <= 72 and 0 <= y < y + h <= 64)
                    for ox, oy, ow, oh in rects:
                        self.assertFalse(x < ox + ow and ox < x + w and y < oy + oh and oy < y + h)
                    rects.append((x, y, w, h))
                    names = [f["name"] for f in widget["fields"]]
                    self.assertEqual(len(names), len(set(names)))
                    for field in widget["fields"]:
                        if field["name"] == "reference":
                            self.assertNotIn(field["value"], refs)
                            refs.add(field["value"])
                        if field["type"] == "ITEM":
                            self.assertEqual(field["value"]["host"], NAME)
                            self.assertIn(field["value"]["key"], item_keys)
                        if field["type"] == "GRAPH":
                            self.assertIn(field["value"]["name"], graphs)
            self.assertTrue({"piechart", "honeycomb", "svggraph", "itemhistory", "item", "graph"} <= kinds)
            for title in ("DGX: Network IN and OUT", "DGX: Disk read and write"):
                self.assertEqual(len(graphs[title]["graph_items"]), 2)
            self.assertEqual(graphs["DGX: CPU utilization"]["uuid"], uid("summarygraph.CPU uso"))
            # Literal Spanish presentation strings must not remain in exports.
            output = json.dumps(export)
            for phrase in ("descubrir ", "ocupada", "temperatura", "captura del host", "Umbral provisional"):
                self.assertNotIn(phrase, output)

    def test_detected_endpoint_count_persists_but_active_count_drops(self):
        config = {"services": [{"name": "TensorFold", "port": 8888}]}
        state = {}
        first = Collector(config, state, now=100)
        with patch.object(first, "probe", return_value={"tcp": 1, "seconds": .001}):
            first.services()
        self.assertEqual(first.data["summary"]["engines_detected"], 1)
        self.assertEqual(first.data["summary"]["engines_active"], 1)
        second = Collector(config, state, now=160)
        with patch.object(second, "probe", return_value={"tcp": 0, "seconds": 1}):
            second.services()
        self.assertEqual(second.data["summary"]["engines_detected"], 1)
        self.assertEqual(second.data["summary"]["engines_active"], 0)


if __name__ == "__main__":
    unittest.main()
