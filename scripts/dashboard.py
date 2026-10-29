"""Native Zabbix 7.0 dashboard fields, matching the official widget forms."""


def field(name, value, kind="STRING"):
    return {"type": kind, "name": name, "value": str(value) if kind == "INTEGER" else value}


def integer(name, value):
    return field(name, value, "INTEGER")


def item(template, name, key):
    return field(name, {"host": template, "key": key}, "ITEM")


def widget(kind, title, x, y, width, height, fields, reference):
    return dict(type=kind, name=title, x=str(x), y=str(y), width=str(width), height=str(height),
                fields=fields + [field("reference", reference)])


def kpi(template, key, title, x, y, reference, decimals=0, extra=None):
    return widget("item", title, x, y, 18, 3,
                  [item(template, "itemid.0", key), integer("show.0", 1), integer("show.1", 2),
                   field("description", title), integer("decimal_places", decimals)] + (extra or []), reference)


def build_dashboard(template, graphs, uid):
    summary = lambda key: "dgx.summary[" + key + "]"
    overview = [
        kpi(template, summary("uptime"), "Host uptime", 0, 0, "UPTIM"),
        kpi(template, summary("engines_detected"), "Detected engine endpoints", 18, 0, "ENGCT"),
        kpi(template, summary("load15"), "CPU load - 15 minutes", 36, 0, "LOADF", decimals=2),
        kpi(template, summary("page_cache"), "RAM page cache", 54, 0, "CACHE"),
        kpi(template, summary("cpu_pct"), "CPU utilization", 0, 3, "CPUPT", decimals=1),
        kpi(template, summary("gpu_power"), "NVIDIA-reported power", 18, 3, "POWER", decimals=1),
        kpi(template, summary("engines_active"), "Active engine endpoints", 36, 3, "ENGUP"),
        kpi(template, "dgx.snapshot.age", "Snapshot age - stale after 300 s", 54, 3, "FRESH", extra=[
            field("thresholds.0.color", "199C0D"), field("thresholds.0.threshold", "0"),
            field("thresholds.1.color", "F63100"), field("thresholds.1.threshold", "300")]),
    ]
    overview.append(widget("piechart", "Root filesystem (/) - used / free", 0, 6, 24, 6, [
        integer("ds.0.dataset_type", 0),
        item(template, "ds.0.itemids.0", summary("root_used")),
        item(template, "ds.0.itemids.1", summary("root_free")),
        integer("ds.0.type.0", 0), integer("ds.0.type.1", 0),
        field("ds.0.color.0", "F63100"), field("ds.0.color.1", "199C0D"),
        integer("ds.0.aggregate_function", 7), integer("draw_type", 0),
        integer("legend", 1), integer("legend_value", 1), integer("legend_lines", 2)
    ], "FSPIE"))
    overview.append(widget("honeycomb", "Discovered ports - green UP / red DOWN", 24, 6, 48, 6, [
        field("items.0", "Service * TCP availability"),
        field("primary_label", "{ITEM.NAME}"), integer("primary_label_type", 0),
        integer("secondary_label_type", 1), integer("secondary_label_decimal_places", 0),
        integer("show.0", 1), integer("show.1", 2),
        field("bg_color", "808080"), integer("interpolation", 0),
        field("thresholds.0.color", "F63100"), field("thresholds.0.threshold", "0"),
        field("thresholds.1.color", "199C0D"), field("thresholds.1.threshold", "1")
    ], "PORTS"))
    by_name = {g["name"]: g for g in graphs}
    for name, x, reference in [("DGX: Network IN and OUT", 0, "NETIO"),
                               ("DGX: Disk read and write", 36, "DSKIO")]:
        assert name in by_name
        overview.append(widget("graph", name, x, 12, 36, 6, [
            field("graphid.0", {"host": template, "name": name}, "GRAPH")], reference))
    overview.append(widget("svggraph", "All discovered temperatures - by sensor", 0, 18, 72, 7, [
        integer("ds.0.dataset_type", 1),
        field("ds.0.items.0", "Temperature sensor *"),
        field("ds.0.items.1", "Temperature zone *"),
        field("ds.0.items.2", "GPU * temperature"),
        field("ds.0.color", "FF465C"), integer("ds.0.fill", 0),
        integer("righty", 0), integer("legend", 1), integer("legend_lines", 10),
        integer("legend_columns", 2), integer("legend_statistic", 1)
    ], "TEMPS"))
    overview.append(widget("itemhistory", "Monitored directories - largest first (overlapping paths are not additive)", 0, 25, 72, 8, [
        field("columns.0.name", "Allocated size, measurement age and directory"),
        item(template, "columns.0.itemid", "dgx.directory.ranking"),
        integer("columns.0.display", 1), integer("columns.0.monospace_font", 1),
        integer("show_lines", 1), integer("show_timestamp", 1), integer("show_column_header", 1)
    ], "DIRSZ"))
    detail = []
    for i, graph in enumerate(graphs):
        detail.append(widget("graph", graph["name"], i % 2 * 36, i // 2 * 5, 36, 5, [
            field("graphid.0", {"host": template, "name": graph["name"]}, "GRAPH")], f"DG{i:03d}"))
    return dict(uuid=uid("dashboard"), name="DGX Spark - host overview", pages=[
        dict(name="Overview", widgets=overview), dict(name="History and resource details", widgets=detail)])
