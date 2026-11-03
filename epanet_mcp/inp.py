"""EPANET .inp input-file parser and writer.

The .inp format is a flat, section-based text format. This module turns it into
a plain dict model that the MCP tools can inspect, validate and rebuild, and
turns a dict model back into .inp text.

Design notes
------------
* Comments start with ';' and may appear at the end of a data line.
* Tokens are whitespace separated; double quotes group a token that contains
  spaces (EPANET allows this for IDs and descriptions).
* Unknown sections are preserved verbatim in raw_sections so a round trip
  never silently drops data.
"""

from __future__ import annotations

import re

from . import units as U

#: Canonical section order used when writing a file.
SECTION_ORDER = [
    "TITLE", "JUNCTIONS", "RESERVOIRS", "TANKS", "PIPES", "PUMPS", "VALVES",
    "EMITTERS", "DEMANDS", "STATUS", "PATTERNS", "CURVES", "CONTROLS", "RULES",
    "ENERGY", "QUALITY", "SOURCES", "REACTIONS", "MIXING", "TIMES", "REPORT",
    "OPTIONS", "COORDINATES", "VERTICES", "LABELS", "BACKDROP", "TAGS",
]

_TOKEN_RE = re.compile(r'"([^"]*)"|(\S+)')


def tokenize(line):
    """Split one .inp data line into tokens, honouring double quotes."""
    out = []
    for quoted, bare in _TOKEN_RE.findall(line):
        out.append(quoted if quoted != "" else bare)
    return out


def strip_comment(line):
    """Remove a trailing ';' comment, respecting double quotes."""
    in_quote = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == ";" and not in_quote:
            return line[:i]
    return line


def _num(value, default=None):
    """Parse a float, tolerating blanks and EPANET's '0' placeholders."""
    if value is None:
        return default
    text = str(value).strip()
    if text == "":
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _int(value, default=None):
    parsed = _num(value)
    return default if parsed is None else int(parsed)


def _bool_status(value):
    text = str(value or "").strip().upper()
    if text in ("CLOSED", "CLOSE", "0"):
        return "Closed"
    if text in ("CV",):
        return "CV"
    return "Open"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_inp(text):
    """Parse .inp text into a model dict."""
    model = {
        "title": [],
        "options": {},
        "times": {},
        "report": {},
        "junctions": [],
        "reservoirs": [],
        "tanks": [],
        "pipes": [],
        "pumps": [],
        "valves": [],
        "emitters": [],
        "demands": [],
        "status": [],
        "patterns": {},
        "curves": {},
        "controls": [],
        "rules": [],
        "energy": [],
        "quality": [],
        "sources": [],
        "reactions": [],
        "mixing": [],
        "coordinates": {},
        "vertices": {},
        "labels": [],
        "tags": {},
        "raw_sections": {},
        "section_order": [],
    }

    section = None
    for raw_line in text.splitlines():
        line = strip_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("[") and "]" in line:
            section = line[1 : line.index("]")].strip().upper()
            if section not in model["section_order"]:
                model["section_order"].append(section)
            model["raw_sections"].setdefault(section, [])
            continue
        if section is None:
            continue
        model["raw_sections"].setdefault(section, []).append(line)
        tokens = tokenize(line)
        if not tokens:
            continue
        _consume(model, section, tokens)

    _finalise(model)
    return model


def _consume(model, section, tok):
    if section == "TITLE":
        model["title"].append(" ".join(tok))

    elif section == "JUNCTIONS":
        model["junctions"].append(
            {
                "id": tok[0],
                "elevation": _num(tok[1] if len(tok) > 1 else None, 0.0),
                "demand": _num(tok[2] if len(tok) > 2 else None, 0.0),
                "pattern": tok[3] if len(tok) > 3 else "",
            }
        )

    elif section == "RESERVOIRS":
        model["reservoirs"].append(
            {
                "id": tok[0],
                "head": _num(tok[1] if len(tok) > 1 else None, 0.0),
                "pattern": tok[2] if len(tok) > 2 else "",
            }
        )

    elif section == "TANKS":
        model["tanks"].append(
            {
                "id": tok[0],
                "elevation": _num(tok[1] if len(tok) > 1 else None, 0.0),
                "init_level": _num(tok[2] if len(tok) > 2 else None, 0.0),
                "min_level": _num(tok[3] if len(tok) > 3 else None, 0.0),
                "max_level": _num(tok[4] if len(tok) > 4 else None, 0.0),
                "diameter": _num(tok[5] if len(tok) > 5 else None, 0.0),
                "min_volume": _num(tok[6] if len(tok) > 6 else None, 0.0),
                "volume_curve": tok[7] if len(tok) > 7 else "",
            }
        )

    elif section == "PIPES":
        model["pipes"].append(
            {
                "id": tok[0],
                "node1": tok[1] if len(tok) > 1 else "",
                "node2": tok[2] if len(tok) > 2 else "",
                "length": _num(tok[3] if len(tok) > 3 else None, 0.0),
                "diameter": _num(tok[4] if len(tok) > 4 else None, 0.0),
                "roughness": _num(tok[5] if len(tok) > 5 else None, 0.0),
                "minor_loss": _num(tok[6] if len(tok) > 6 else None, 0.0),
                "status": _bool_status(tok[7] if len(tok) > 7 else "Open"),
            }
        )

    elif section == "PUMPS":
        entry = {
            "id": tok[0],
            "node1": tok[1] if len(tok) > 1 else "",
            "node2": tok[2] if len(tok) > 2 else "",
        }
        rest = tok[3:]
        i = 0
        while i < len(rest):
            key = rest[i].upper()
            val = rest[i + 1] if i + 1 < len(rest) else ""
            if key == "HEAD":
                entry["curve"] = val
            elif key == "POWER":
                entry["power"] = _num(val)
            elif key == "SPEED":
                entry["speed"] = _num(val, 1.0)
            elif key == "PATTERN":
                entry["pattern"] = val
            i += 2
        entry.setdefault("curve", "")
        entry.setdefault("power", None)
        entry.setdefault("speed", 1.0)
        entry.setdefault("pattern", "")
        model["pumps"].append(entry)

    elif section == "VALVES":
        model["valves"].append(
            {
                "id": tok[0],
                "node1": tok[1] if len(tok) > 1 else "",
                "node2": tok[2] if len(tok) > 2 else "",
                "diameter": _num(tok[3] if len(tok) > 3 else None, 0.0),
                "type": (tok[4] if len(tok) > 4 else "").upper(),
                "setting": _num(tok[5] if len(tok) > 5 else None, 0.0),
                "minor_loss": _num(tok[6] if len(tok) > 6 else None, 0.0),
            }
        )

    elif section == "EMITTERS":
        model["emitters"].append(
            {"id": tok[0], "coefficient": _num(tok[1] if len(tok) > 1 else None, 0.0)}
        )

    elif section == "DEMANDS":
        model["demands"].append(
            {
                "id": tok[0],
                "demand": _num(tok[1] if len(tok) > 1 else None, 0.0),
                "pattern": tok[2] if len(tok) > 2 else "",
                "category": tok[3] if len(tok) > 3 else "",
            }
        )

    elif section == "STATUS":
        model["status"].append({"id": tok[0], "status": tok[1] if len(tok) > 1 else ""})

    elif section == "PATTERNS":
        pid = tok[0]
        values = [_num(v) for v in tok[1:]]
        model["patterns"].setdefault(pid, []).extend([v for v in values if v is not None])

    elif section == "CURVES":
        cid = tok[0]
        x = _num(tok[1] if len(tok) > 1 else None)
        y = _num(tok[2] if len(tok) > 2 else None)
        if x is not None and y is not None:
            model["curves"].setdefault(cid, []).append([x, y])

    elif section == "CONTROLS":
        model["controls"].append(" ".join(tok))

    elif section == "RULES":
        model["rules"].append(" ".join(tok))

    elif section == "ENERGY":
        model["energy"].append(" ".join(tok))

    elif section == "QUALITY":
        model["quality"].append(" ".join(tok))

    elif section == "SOURCES":
        model["sources"].append(" ".join(tok))

    elif section == "REACTIONS":
        model["reactions"].append(" ".join(tok))

    elif section == "MIXING":
        model["mixing"].append(" ".join(tok))

    elif section == "COORDINATES":
        if len(tok) >= 3:
            model["coordinates"][tok[0]] = [_num(tok[1], 0.0), _num(tok[2], 0.0)]

    elif section == "VERTICES":
        if len(tok) >= 3:
            model["vertices"].setdefault(tok[0], []).append([_num(tok[1], 0.0), _num(tok[2], 0.0)])

    elif section == "LABELS":
        model["labels"].append(" ".join(tok))

    elif section == "TAGS":
        if len(tok) >= 3:
            model["tags"].setdefault(tok[0], {})[tok[1].lower()] = tok[2]

    elif section == "OPTIONS":
        _consume_option(model, tok)

    elif section == "TIMES":
        _consume_time(model, tok)

    elif section == "REPORT":
        _consume_report(model, tok)


def _consume_option(model, tok):
    opts = model["options"]
    key = tok[0].upper()
    val = tok[1] if len(tok) > 1 else ""
    if key == "UNITS":
        opts["units"] = val.upper()
    elif key == "HEADLOSS":
        opts["headloss"] = val.upper()
    elif key == "QUALITY":
        opts["quality"] = " ".join(tok[1:])
    elif key == "VISCOSITY":
        opts["viscosity"] = _num(val, 1.0)
    elif key == "DIFFUSIVITY":
        opts["diffusivity"] = _num(val, 1.0)
    elif key == "SPECIFIC" and len(tok) > 2 and tok[1].upper() == "GRAVITY":
        opts["specific_gravity"] = _num(tok[2], 1.0)
    elif key == "TRIALS":
        opts["trials"] = _int(val, 200)
    elif key == "ACCURACY":
        opts["accuracy"] = _num(val, 0.001)
    elif key == "UNBALANCED":
        opts["unbalanced"] = " ".join(tok[1:])
    elif key == "PATTERN":
        opts["default_pattern"] = val
    elif key == "DEMAND" and len(tok) > 2 and tok[1].upper() == "MULTIPLIER":
        opts["demand_multiplier"] = _num(tok[2], 1.0)
    elif key == "DEMAND" and len(tok) > 2 and tok[1].upper() == "MODEL":
        opts["demand_model"] = tok[2].upper()
    elif key == "MINIMUM" and len(tok) > 2 and tok[1].upper() == "PRESSURE":
        opts["minimum_pressure"] = _num(tok[2], 0.0)
    elif key == "REQUIRED" and len(tok) > 2 and tok[1].upper() == "PRESSURE":
        opts["required_pressure"] = _num(tok[2], 0.0)
    elif key == "PRESSURE" and len(tok) > 2 and tok[1].upper() == "EXPONENT":
        opts["pressure_exponent"] = _num(tok[2], 0.5)
    elif key == "EMITTER" and len(tok) > 2 and tok[1].upper() == "EXPONENT":
        opts["emitter_exponent"] = _num(tok[2], 0.5)
    elif key == "HEADERROR":
        opts["max_head_error"] = _num(val, 0.0)
    elif key == "FLOWCHANGE":
        opts["max_flow_change"] = _num(val, 0.0)
    elif key == "CHECKFREQ":
        opts["check_frequency"] = _int(val, 2)
    elif key == "MAXCHECK":
        opts["max_check"] = _int(val, 10)
    elif key == "DAMPLIMIT":
        opts["damping_limit"] = _num(val, 0.0)
    elif key == "HYDRAULICS":
        opts["hydraulics_file"] = val
    elif key == "MAP":
        opts["map_file"] = val
    else:
        opts.setdefault("other", {})[key] = " ".join(tok[1:])


def _consume_time(model, tok):
    times = model["times"]
    joined = " ".join(tok[1:])
    key = tok[0].upper()
    if key == "DURATION":
        times["duration"] = joined
    elif key == "HYDRAULIC" and len(tok) > 2 and tok[1].upper() == "TIMESTEP":
        times["hydraulic_timestep"] = " ".join(tok[2:])
    elif key == "QUALITY" and len(tok) > 2 and tok[1].upper() == "TIMESTEP":
        times["quality_timestep"] = " ".join(tok[2:])
    elif key == "RULE" and len(tok) > 2 and tok[1].upper() == "TIMESTEP":
        times["rule_timestep"] = " ".join(tok[2:])
    elif key == "PATTERN" and len(tok) > 2 and tok[1].upper() == "TIMESTEP":
        times["pattern_timestep"] = " ".join(tok[2:])
    elif key == "PATTERN" and len(tok) > 2 and tok[1].upper() == "START":
        times["pattern_start"] = " ".join(tok[2:])
    elif key == "REPORT" and len(tok) > 2 and tok[1].upper() == "TIMESTEP":
        times["report_timestep"] = " ".join(tok[2:])
    elif key == "REPORT" and len(tok) > 2 and tok[1].upper() == "START":
        times["report_start"] = " ".join(tok[2:])
    elif key == "START" and len(tok) > 2 and tok[1].upper() == "CLOCKTIME":
        times["start_clocktime"] = " ".join(tok[2:])
    elif key == "STATISTIC":
        times["statistic"] = joined.upper()
    else:
        times.setdefault("other", {})[key] = joined


def _consume_report(model, tok):
    rep = model["report"]
    key = tok[0].upper()
    val = tok[1] if len(tok) > 1 else ""
    if key == "STATUS":
        rep["status"] = val.upper()
    elif key == "SUMMARY":
        rep["summary"] = val.upper()
    elif key == "ENERGY":
        rep["energy"] = val.upper()
    elif key == "NODES":
        rep["nodes"] = " ".join(tok[1:])
    elif key == "LINKS":
        rep["links"] = " ".join(tok[1:])
    elif key == "PAGE":
        rep["page"] = _int(val, 0)
    elif key == "FILE":
        rep["file"] = val
    else:
        rep.setdefault("other", {})[key] = " ".join(tok[1:])


def _finalise(model):
    """Fill defaults and derive convenience fields."""
    opts = model["options"]
    opts.setdefault("units", "GPM")
    opts.setdefault("headloss", "H-W")
    opts.setdefault("trials", 200)
    opts.setdefault("accuracy", 0.001)
    opts.setdefault("demand_model", "DDA")
    opts.setdefault("demand_multiplier", 1.0)
    opts.setdefault("specific_gravity", 1.0)
    opts.setdefault("viscosity", 1.0)
    opts.setdefault("diffusivity", 1.0)
    opts.setdefault("quality", "None")

    times = model["times"]
    times.setdefault("duration", "0:00")
    times.setdefault("hydraulic_timestep", "1:00")
    times.setdefault("pattern_timestep", "1:00")
    times.setdefault("report_timestep", "1:00")
    times.setdefault("statistic", "NONE")

    rep = model["report"]
    rep.setdefault("status", "NO")
    rep.setdefault("summary", "YES")
    rep.setdefault("nodes", "NONE")
    rep.setdefault("links", "NONE")

    model["flow_units"] = str(opts["units"]).strip().upper()
    model["units"] = U.unit_set(model["flow_units"])
    model["node_ids"] = (
        [j["id"] for j in model["junctions"]]
        + [r["id"] for r in model["reservoirs"]]
        + [t["id"] for t in model["tanks"]]
    )
    model["link_ids"] = (
        [p["id"] for p in model["pipes"]]
        + [p["id"] for p in model["pumps"]]
        + [v["id"] for v in model["valves"]]
    )


# --------------------------------------------------------------------------
# Derived views
# --------------------------------------------------------------------------


def node_table(model):
    """Return a dict of node_id -> node description for every node."""
    table = {}
    for j in model["junctions"]:
        table[j["id"]] = {
            "id": j["id"],
            "kind": "Junction",
            "elevation": j["elevation"],
            "demand": j["demand"],
            "pattern": j["pattern"],
        }
    for r in model["reservoirs"]:
        table[r["id"]] = {
            "id": r["id"],
            "kind": "Reservoir",
            "head": r["head"],
            "pattern": r["pattern"],
        }
    for t in model["tanks"]:
        table[t["id"]] = {
            "id": t["id"],
            "kind": "Tank",
            "elevation": t["elevation"],
            "init_level": t["init_level"],
            "min_level": t["min_level"],
            "max_level": t["max_level"],
            "diameter": t["diameter"],
        }
    return table


def link_table(model):
    """Return a dict of link_id -> link description for every link."""
    table = {}
    for p in model["pipes"]:
        table[p["id"]] = dict(p, kind="Pipe")
    for p in model["pumps"]:
        table[p["id"]] = dict(p, kind="Pump")
    for v in model["valves"]:
        table[v["id"]] = dict(v, kind="Valve")
    return table


def adjacency(model):
    """Undirected adjacency list over all links."""
    adj = {nid: set() for nid in model["node_ids"]}
    for link in link_table(model).values():
        a, b = link.get("node1"), link.get("node2")
        if a in adj and b in adj:
            adj[a].add(b)
            adj[b].add(a)
    return adj


def summary(model):
    """Compact, JSON-safe description of a parsed network."""
    opts = model["options"]
    times = model["times"]
    return {
        "title": " ".join(model["title"]).strip(),
        "units": model["units"],
        "options": {
            "headloss": opts.get("headloss"),
            "demand_model": opts.get("demand_model"),
            "trials": opts.get("trials"),
            "accuracy": opts.get("accuracy"),
            "demand_multiplier": opts.get("demand_multiplier"),
            "quality": opts.get("quality"),
            "minimum_pressure": opts.get("minimum_pressure"),
            "required_pressure": opts.get("required_pressure"),
        },
        "times": {
            "duration": times.get("duration"),
            "hydraulic_timestep": times.get("hydraulic_timestep"),
            "pattern_timestep": times.get("pattern_timestep"),
            "report_timestep": times.get("report_timestep"),
            "statistic": times.get("statistic"),
        },
        "counts": {
            "junctions": len(model["junctions"]),
            "reservoirs": len(model["reservoirs"]),
            "tanks": len(model["tanks"]),
            "pipes": len(model["pipes"]),
            "pumps": len(model["pumps"]),
            "valves": len(model["valves"]),
            "patterns": len(model["patterns"]),
            "curves": len(model["curves"]),
            "controls": len(model["controls"]),
        },
        "nodes": node_table(model),
        "links": link_table(model),
        "patterns": model["patterns"],
        "curves": model["curves"],
        "controls": model["controls"],
        "coordinates": model["coordinates"],
    }


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def _fmt(value):
    """Format a number the way EPANET expects (no exponent, trimmed)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return ("%.6f" % value).rstrip("0").rstrip(".")
    return str(value)


def build_inp(spec):
    """Build .inp text from a structured spec dict."""
    lines = []
    title = spec.get("title")
    if title:
        lines.append("[TITLE]")
        for part in (title if isinstance(title, list) else [title]):
            lines.append(str(part))
        lines.append("")

    def section(name, header, rows):
        if not rows:
            return
        lines.append("[" + name + "]")
        if header:
            lines.append(";" + header)
        lines.extend(rows)
        lines.append("")

    section(
        "JUNCTIONS",
        "ID              Elev        Demand      Pattern",
        [
            " %-15s %-11s %-11s %s"
            % (j["id"], _fmt(j.get("elevation", 0)), _fmt(j.get("demand", 0)), j.get("pattern", ""))
            for j in spec.get("junctions", [])
        ],
    )
    section(
        "RESERVOIRS",
        "ID              Head        Pattern",
        [
            " %-15s %-11s %s" % (r["id"], _fmt(r.get("head", 0)), r.get("pattern", ""))
            for r in spec.get("reservoirs", [])
        ],
    )
    section(
        "TANKS",
        "ID              Elevation   InitLevel   MinLevel    MaxLevel    Diameter    MinVol      VolCurve",
        [
            " %-15s %-11s %-11s %-11s %-11s %-11s %-11s %s"
            % (
                t["id"],
                _fmt(t.get("elevation", 0)),
                _fmt(t.get("init_level", 0)),
                _fmt(t.get("min_level", 0)),
                _fmt(t.get("max_level", 0)),
                _fmt(t.get("diameter", 0)),
                _fmt(t.get("min_volume", 0)),
                t.get("volume_curve", ""),
            )
            for t in spec.get("tanks", [])
        ],
    )
    section(
        "PIPES",
        "ID              Node1           Node2           Length      Diameter    Roughness   MinorLoss   Status",
        [
            " %-15s %-15s %-15s %-11s %-11s %-11s %-11s %s"
            % (
                p["id"],
                p["node1"],
                p["node2"],
                _fmt(p.get("length", 0)),
                _fmt(p.get("diameter", 0)),
                _fmt(p.get("roughness", 100)),
                _fmt(p.get("minor_loss", 0)),
                p.get("status", "Open"),
            )
            for p in spec.get("pipes", [])
        ],
    )

    pump_rows = []
    for p in spec.get("pumps", []):
        parts = [" %-15s %-15s %-15s" % (p["id"], p["node1"], p["node2"])]
        if p.get("curve"):
            parts.append("HEAD " + str(p["curve"]))
        elif p.get("power") is not None:
            parts.append("POWER " + _fmt(p["power"]))
        if p.get("speed") not in (None, 1, 1.0):
            parts.append("SPEED " + _fmt(p["speed"]))
        if p.get("pattern"):
            parts.append("PATTERN " + str(p["pattern"]))
        pump_rows.append(" ".join(parts))
    section("PUMPS", "ID              Node1           Node2           Parameters", pump_rows)

    section(
        "VALVES",
        "ID              Node1           Node2           Diameter    Type        Setting     MinorLoss",
        [
            " %-15s %-15s %-15s %-11s %-11s %-11s %s"
            % (
                v["id"],
                v["node1"],
                v["node2"],
                _fmt(v.get("diameter", 0)),
                v.get("type", "TCV"),
                _fmt(v.get("setting", 0)),
                _fmt(v.get("minor_loss", 0)),
            )
            for v in spec.get("valves", [])
        ],
    )

    section(
        "DEMANDS",
        "ID              Demand      Pattern     Category",
        [
            " %-15s %-11s %-11s %s"
            % (d["id"], _fmt(d.get("demand", 0)), d.get("pattern", ""), d.get("category", ""))
            for d in spec.get("demands", [])
        ],
    )

    section(
        "EMITTERS",
        "ID              Coefficient",
        [" %-15s %s" % (e["id"], _fmt(e.get("coefficient", 0))) for e in spec.get("emitters", [])],
    )

    section(
        "STATUS",
        "ID              Status/Setting",
        [" %-15s %s" % (s["id"], s.get("status", "")) for s in spec.get("status", [])],
    )

    pattern_rows = []
    for pid, values in (spec.get("patterns") or {}).items():
        chunk = []
        for value in values:
            chunk.append(_fmt(value))
            if len(chunk) == 6:
                pattern_rows.append(" %-15s %s" % (pid, " ".join(chunk)))
                chunk = []
        if chunk:
            pattern_rows.append(" %-15s %s" % (pid, " ".join(chunk)))
    section("PATTERNS", "ID              Multipliers", pattern_rows)

    curve_rows = []
    for cid, points in (spec.get("curves") or {}).items():
        for point in points:
            curve_rows.append(" %-15s %-11s %s" % (cid, _fmt(point[0]), _fmt(point[1])))
    section("CURVES", "ID              X-Value     Y-Value", curve_rows)

    section("CONTROLS", "", [" " + c for c in spec.get("controls", [])])
    section("RULES", "", [" " + r for r in spec.get("rules", [])])

    times = spec.get("times") or {}
    time_rows = []
    if times.get("duration"):
        time_rows.append(" Duration           %s" % times["duration"])
    if times.get("hydraulic_timestep"):
        time_rows.append(" Hydraulic Timestep %s" % times["hydraulic_timestep"])
    if times.get("pattern_timestep"):
        time_rows.append(" Pattern Timestep   %s" % times["pattern_timestep"])
    if times.get("report_timestep"):
        time_rows.append(" Report Timestep    %s" % times["report_timestep"])
    if times.get("start_clocktime"):
        time_rows.append(" Start ClockTime    %s" % times["start_clocktime"])
    if times.get("statistic"):
        time_rows.append(" Statistic          %s" % times["statistic"])
    section("TIMES", "", time_rows)

    report = spec.get("report") or {}
    report_rows = []
    for key, label in (
        ("status", "Status"),
        ("summary", "Summary"),
        ("energy", "Energy"),
        ("nodes", "Nodes"),
        ("links", "Links"),
    ):
        if report.get(key) is not None:
            report_rows.append(" %-8s %s" % (label, report[key]))
    section("REPORT", "", report_rows)

    options = spec.get("options") or {}
    option_rows = []
    if options.get("units"):
        option_rows.append(" Units              %s" % options["units"])
    if options.get("headloss"):
        option_rows.append(" Headloss           %s" % options["headloss"])
    if options.get("quality"):
        option_rows.append(" Quality            %s" % options["quality"])
    if options.get("trials") is not None:
        option_rows.append(" Trials             %s" % _fmt(options["trials"]))
    if options.get("accuracy") is not None:
        option_rows.append(" Accuracy           %s" % _fmt(options["accuracy"]))
    if options.get("demand_model"):
        option_rows.append(" Demand Model       %s" % options["demand_model"])
    if options.get("demand_multiplier") is not None:
        option_rows.append(" Demand Multiplier  %s" % _fmt(options["demand_multiplier"]))
    if options.get("minimum_pressure") is not None:
        option_rows.append(" Minimum Pressure   %s" % _fmt(options["minimum_pressure"]))
    if options.get("required_pressure") is not None:
        option_rows.append(" Required Pressure  %s" % _fmt(options["required_pressure"]))
    if options.get("pressure_exponent") is not None:
        option_rows.append(" Pressure Exponent  %s" % _fmt(options["pressure_exponent"]))
    if options.get("specific_gravity") is not None:
        option_rows.append(" Specific Gravity   %s" % _fmt(options["specific_gravity"]))
    if options.get("viscosity") is not None:
        option_rows.append(" Viscosity          %s" % _fmt(options["viscosity"]))
    if options.get("unbalanced"):
        option_rows.append(" Unbalanced         %s" % options["unbalanced"])
    section("OPTIONS", "", option_rows)

    coords = spec.get("coordinates") or {}
    section(
        "COORDINATES",
        "Node            X-Coord     Y-Coord",
        [" %-15s %-11s %s" % (nid, _fmt(xy[0]), _fmt(xy[1])) for nid, xy in coords.items()],
    )

    lines.append("[END]")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate(model):
    """Structural checks that do not need a solver run."""
    problems = []

    def add(severity, code, message):
        problems.append({"severity": severity, "code": code, "message": message})

    if not model["junctions"] and not model["tanks"]:
        add("error", "no_demand_nodes", "Network has no junctions and no tanks.")

    if not model["reservoirs"] and not model["tanks"]:
        add("error", "no_fixed_head", "Network has no reservoir or tank to fix a hydraulic grade.")

    seen = {}
    for nid in model["node_ids"]:
        if nid in seen:
            add("error", "duplicate_node", "Duplicate node ID '" + nid + "'.")
        seen[nid] = True

    seen_links = {}
    for lid in model["link_ids"]:
        if lid in seen_links:
            add("error", "duplicate_link", "Duplicate link ID '" + lid + "'.")
        seen_links[lid] = True

    for link in link_table(model).values():
        for endpoint in ("node1", "node2"):
            nid = link.get(endpoint)
            if nid and nid not in seen:
                add(
                    "error",
                    "unknown_node",
                    "Link '" + link["id"] + "' references unknown node '" + nid + "'.",
                )
        if link.get("node1") and link.get("node1") == link.get("node2"):
            add(
                "error",
                "self_loop",
                "Link '" + link["id"] + "' connects node '" + link["node1"] + "' to itself.",
            )

    for pipe in model["pipes"]:
        if pipe["length"] is not None and pipe["length"] <= 0:
            add("error", "bad_length", "Pipe '" + pipe["id"] + "' has non-positive length.")
        if pipe["diameter"] is not None and pipe["diameter"] <= 0:
            add("error", "bad_diameter", "Pipe '" + pipe["id"] + "' has non-positive diameter.")
        if pipe["roughness"] is not None and pipe["roughness"] <= 0:
            add("error", "bad_roughness", "Pipe '" + pipe["id"] + "' has non-positive roughness.")

    for tank in model["tanks"]:
        if tank["max_level"] is not None and tank["min_level"] is not None:
            if tank["max_level"] <= tank["min_level"]:
                add("error", "bad_tank_levels", "Tank '" + tank["id"] + "' has max level <= min level.")
        if tank["init_level"] is not None and tank["min_level"] is not None:
            if tank["init_level"] < tank["min_level"]:
                add(
                    "warning",
                    "tank_init_below_min",
                    "Tank '" + tank["id"] + "' initial level is below its minimum.",
                )

    for pid, values in model["patterns"].items():
        if not values:
            add("warning", "empty_pattern", "Pattern '" + pid + "' has no multipliers.")

    for cid, points in model["curves"].items():
        if len(points) < 2:
            add("warning", "short_curve", "Curve '" + cid + "' has fewer than two points.")

    known_patterns = set(model["patterns"]) | {""}
    for junction in model["junctions"]:
        if junction["pattern"] and junction["pattern"] not in known_patterns:
            add(
                "error",
                "unknown_pattern",
                "Junction '" + junction["id"] + "' uses undefined pattern '" + junction["pattern"] + "'.",
            )
    for pump in model["pumps"]:
        if pump.get("curve") and pump["curve"] not in model["curves"]:
            add(
                "error",
                "unknown_curve",
                "Pump '" + pump["id"] + "' uses undefined curve '" + pump["curve"] + "'.",
            )
        if not pump.get("curve") and pump.get("power") is None:
            add(
                "error",
                "pump_no_definition",
                "Pump '" + pump["id"] + "' has neither a head curve nor a power rating.",
            )

    adj = adjacency(model)
    sources = [r["id"] for r in model["reservoirs"]] + [t["id"] for t in model["tanks"]]
    reached = set()
    queue = list(sources)
    while queue:
        node = queue.pop()
        if node in reached:
            continue
        reached.add(node)
        queue.extend(adj.get(node, ()))
    for nid in model["node_ids"]:
        if nid not in reached:
            add(
                "error",
                "disconnected_node",
                "Node '" + nid + "' is not connected to any reservoir or tank.",
            )

    return problems
