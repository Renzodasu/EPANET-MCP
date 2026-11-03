"""EPANET 2.2 MCP server.

Speaks the Model Context Protocol over stdio: one JSON-RPC 2.0 object per line
on stdin, one response per line on stdout. Nothing is ever written to stdout
except protocol messages; diagnostics go to stderr.

The solver is EPA's own runepanet.exe (EPANET 2.2.0), bundled in ./bin, so the
answers are exactly what the official EPANET 2.2 GUI would produce.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys

from . import __version__
from . import engine
from . import inp as I
from . import rpt as R
from . import units as U

SERVER_NAME = "epanet-mcp"
PROTOCOL_VERSION = "2024-11-05"

#: EPANET repeats a warning once per timestep; strip the time so they collapse.
_TIME_IN_MESSAGE = re.compile(r"\s+at\s+\d+:\d{2}:\d{2}\s+hrs\.?")

#: EPANET reports a disconnected node with this sentinel pressure.
_DISCONNECTED_PRESSURE = -1.0e6


# --------------------------------------------------------------------------
# Transport helpers
# --------------------------------------------------------------------------


def _text(value):
    # Compact JSON: the consumer is a language model, not a human reading a
    # terminal. Indenting every nested object roughly doubles the payload for
    # no gain in comprehension.
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def _send(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _configure_streams():
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except Exception:
        pass


# --------------------------------------------------------------------------
# Shared argument handling
# --------------------------------------------------------------------------

_SOURCE_PROPS = {
    "path": {
        "type": "string",
        "description": "Absolute path to an .inp network file.",
    },
    "inp_text": {
        "type": "string",
        "description": "The .inp file contents as text. Use this instead of path when the network is generated on the fly.",
    },
}


def _source(args):
    """Return (inp_text, label) from either path or inp_text."""
    text = args.get("inp_text")
    if text:
        return text, "<inline>"
    path = args.get("path")
    if path:
        if not os.path.isfile(path):
            raise ValueError("No such file: " + str(path))
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(), path
    raise ValueError("Provide either 'path' (an .inp file) or 'inp_text' (the .inp contents).")


#: Summary keys worth keeping. The rest are solver defaults that never change
#: and only cost tokens; "Input Data File" is a temp path that differs on every
#: run, which also defeats prompt caching.
_SUMMARY_KEYS = (
    "Number of Junctions",
    "Number of Reservoirs",
    "Number of Tanks",
    "Number of Pipes",
    "Number of Pumps",
    "Number of Valves",
    "Headloss Formula",
    "Nodal Demand Model",
    "Hydraulic Timestep",
    "Total Duration",
)


def _summary(parsed):
    """The summary lines that actually help interpret the results."""
    raw = parsed.get("summary") or {}
    return {key: raw[key] for key in _SUMMARY_KEYS if key in raw}


def _headline(parsed):
    """The few numbers that tell you whether a run is sane.

    Replaces the full last-period table, which was ~4 KB on a 26-node network
    and almost never read in full.
    """
    period = R.last_period(parsed)
    nodes = period.get("nodes") or {}
    links = period.get("links") or {}
    out = {"time": period.get("time")}
    pressures = {
        key: value["pressure"]
        for key, value in nodes.items()
        if isinstance(value.get("pressure"), (int, float))
    }
    if pressures:
        low = min(pressures, key=pressures.get)
        high = max(pressures, key=pressures.get)
        out["pressure"] = {
            "min": pressures[low],
            "min_node": low,
            "max": pressures[high],
            "max_node": high,
        }
    velocities = {
        key: value["velocity"]
        for key, value in links.items()
        if isinstance(value.get("velocity"), (int, float))
    }
    if velocities:
        low = min(velocities, key=velocities.get)
        high = max(velocities, key=velocities.get)
        out["velocity"] = {
            "min": velocities[low],
            "min_link": low,
            "max": velocities[high],
            "max_link": high,
        }
    return out


def _condense(parsed, include_results=False):
    """Compact view of a parsed report: verdict, summary and a headline.

    The full last-period table is opt-in via include_results, because it is
    the single largest part of the payload and is rarely read in full.
    """
    out = {
        "ok": parsed["ok"],
        "errors": parsed["errors"],
        "warnings": parsed["warnings"],
        "summary": _summary(parsed),
        "period_count": len(parsed["periods"]),
        "node_columns": parsed["node_columns"],
        "node_units": parsed["node_units"],
        "link_columns": parsed["link_columns"],
        "link_units": parsed["link_units"],
        "headline": _headline(parsed),
    }
    if include_results:
        out["last_period"] = R.last_period(parsed)
    return out


def _run_source(args):
    """Resolve the network, run it, and return (text, label, outcome)."""
    text, label = _source(args)
    timeout = float(args.get("timeout") or 120)
    outcome = engine.run(text, timeout=timeout)
    return text, label, outcome


def _select_periods(parsed, period_arg):
    if period_arg in (None, "", "last"):
        return [R.last_period(parsed)]
    if period_arg == "all":
        return parsed["periods"]
    if period_arg == "first":
        return parsed["periods"][:1]
    chosen = [period for period in parsed["periods"] if period["time"] == period_arg]
    if not chosen:
        available = [period["time"] for period in parsed["periods"]]
        raise ValueError(
            "No reporting period at " + str(period_arg) + ". Available: " + ", ".join(available[:8])
        )
    return chosen


def _period_summary(periods, kind, ids, fields):
    """Per-object min/max/mean across the selected periods."""
    out = {}
    for period in periods:
        for object_id, record in period[kind].items():
            if ids and object_id not in ids:
                continue
            entry = out.setdefault(object_id, {})
            for field, value in record.items():
                if not isinstance(value, (int, float)):
                    continue
                if fields and field not in fields:
                    continue
                stats = entry.get(field)
                if stats is None:
                    entry[field] = {"min": value, "max": value, "sum": value, "n": 1}
                    continue
                if value < stats["min"]:
                    stats["min"] = value
                if value > stats["max"]:
                    stats["max"] = value
                stats["sum"] += value
                stats["n"] += 1
    for entry in out.values():
        for field, stats in list(entry.items()):
            entry[field] = {
                "min": round(stats["min"], 4),
                "max": round(stats["max"], 4),
                "mean": round(stats["sum"] / stats["n"], 4),
            }
    return out


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


def tool_engine_info(args):
    """Report which solver binary will be used."""
    info = engine.engine_info()
    info["server_version"] = __version__
    info["protocol_version"] = PROTOCOL_VERSION
    return info


def tool_inspect(args):
    """Parse an .inp file and describe the network."""
    text, label = _source(args)
    model = I.parse_inp(text)
    issues = I.validate(model)
    summary = I.summary(model)
    if not args.get("include_objects"):
        # The per-object dicts are ~9 KB on a 26-node network and only repeat
        # what the .inp already says. Counts and options are what you need to
        # orient yourself; ask for the objects when you actually want them.
        summary = {
            key: value for key, value in summary.items() if key not in ("nodes", "links")
        }
    return {
        "source": label,
        "summary": summary,
        "issues": issues,
        "error_count": len([i for i in issues if i["severity"] == "error"]),
        "warning_count": len([i for i in issues if i["severity"] == "warning"]),
    }


def tool_validate(args):
    """Structural checks only - no solver run."""
    text, label = _source(args)
    model = I.parse_inp(text)
    issues = I.validate(model)
    return {
        "source": label,
        "ok": not [i for i in issues if i["severity"] == "error"],
        "issues": issues,
    }


def tool_build(args):
    """Build .inp text from a structured spec."""
    spec = args.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("spec must be a JSON object describing the network.")
    text = I.build_inp(spec)
    model = I.parse_inp(text)
    issues = I.validate(model)
    ok = not [i for i in issues if i["severity"] == "error"]
    save_to = args.get("save_to")
    if save_to:
        with open(save_to, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    result = {
        "ok": ok,
        "issues": issues,
        "summary": I.summary(model),
    }
    if save_to:
        result["saved_to"] = save_to
    if args.get("include_inp") or not ok:
        result["inp_text"] = text
    return result


def tool_run(args):
    """Run a network and return the results."""
    text, label, outcome = _run_source(args)
    parsed = outcome["parsed"]
    result = {
        "source": label,
        "returncode": outcome["returncode"],
    }
    result.update(_condense(parsed, include_results=bool(args.get("include_results"))))
    if not parsed["ok"] and outcome["console"]:
        # The console is progress spam on a good run and the only clue on a
        # bad one, so keep it only when something went wrong.
        result["console"] = outcome["console"]
    if args.get("full_report"):
        result["report_text"] = outcome["report_text"]
    return result


def tool_build_and_run(args):
    """Build a network from a spec, validate it, run it, return results."""
    spec = args.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("spec must be a JSON object describing the network.")
    text = I.build_inp(spec)
    model = I.parse_inp(text)
    issues = I.validate(model)
    structural_errors = [i for i in issues if i["severity"] == "error"]
    save_to = args.get("save_to")
    if save_to:
        with open(save_to, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    timeout = float(args.get("timeout") or 120)
    outcome = engine.run(text, timeout=timeout)
    parsed = outcome["parsed"]
    result = {
        "source": save_to or "<built>",
        "structural_issues": issues,
        "returncode": outcome["returncode"],
    }
    result.update(_condense(parsed, include_results=bool(args.get("include_results"))))
    if not parsed["ok"] and outcome["console"]:
        result["console"] = outcome["console"]
    if save_to:
        result["saved_to"] = save_to
    if args.get("include_inp") or structural_errors or not parsed["ok"]:
        result["inp_text"] = text
    return result


def tool_results(args):
    """Query node or link results from a run."""
    text, label, outcome = _run_source(args)
    parsed = outcome["parsed"]
    if not parsed["ok"]:
        return {"source": label, "ok": False, "errors": parsed["errors"]}
    kind = (args.get("kind") or "nodes").lower()
    if kind not in ("nodes", "links"):
        raise ValueError("kind must be 'nodes' or 'links'.")
    period_arg = args.get("period")
    periods = _select_periods(parsed, period_arg)
    ids = args.get("ids")
    fields = args.get("fields")
    if fields:
        fields = [str(field).lower() for field in fields]
    # Dumping every timestep is the biggest payload this server can produce
    # (62 KB for links on a 24-hour run). Default to a per-object summary when
    # the caller asks for every period; opt back in with mode="full".
    mode = str(args.get("mode") or "").lower()
    if not mode:
        mode = "summary" if period_arg == "all" else "full"
    if mode not in ("full", "summary"):
        raise ValueError("mode must be 'full' or 'summary'.")
    units = parsed["node_units"] if kind == "nodes" else parsed["link_units"]
    if mode == "summary":
        return {
            "source": label,
            "ok": True,
            "kind": kind,
            "mode": "summary",
            "units": units,
            "period_count": len(periods),
            "summary": _period_summary(periods, kind, ids, fields),
        }
    output = []
    for period in periods:
        table = period[kind]
        records = {}
        for object_id, record in table.items():
            if ids and object_id not in ids:
                continue
            if fields:
                records[object_id] = {field: record.get(field) for field in fields}
            else:
                records[object_id] = record
        output.append({"time": period["time"], kind: records})
    return {
        "source": label,
        "ok": True,
        "kind": kind,
        "mode": "full",
        "units": units,
        "periods": output,
    }


def tool_envelope(args):
    """Min/max of a field across every reporting period."""
    text, label, outcome = _run_source(args)
    parsed = outcome["parsed"]
    if not parsed["ok"]:
        return {"source": label, "ok": False, "errors": parsed["errors"]}
    kind = (args.get("kind") or "nodes").lower()
    if kind not in ("nodes", "links"):
        raise ValueError("kind must be 'nodes' or 'links'.")
    field = args.get("field") or ("pressure" if kind == "nodes" else "flow")
    field = str(field).lower()
    data = R.envelope(parsed, kind, field)
    return {
        "source": label,
        "ok": True,
        "kind": kind,
        "field": field,
        "units": parsed["node_units"] if kind == "nodes" else parsed["link_units"],
        "period_count": len(parsed["periods"]),
        "envelope": data,
    }


def tool_series(args):
    """One field of one node or link across every reporting period."""
    text, label, outcome = _run_source(args)
    parsed = outcome["parsed"]
    if not parsed["ok"]:
        return {"source": label, "ok": False, "errors": parsed["errors"]}
    kind = (args.get("kind") or "nodes").lower()
    if kind not in ("nodes", "links"):
        raise ValueError("kind must be 'nodes' or 'links'.")
    object_id = args.get("id")
    if not object_id:
        raise ValueError("id is required (the node or link ID).")
    field = str(args.get("field") or ("pressure" if kind == "nodes" else "flow")).lower()
    if kind == "nodes":
        series = R.node_series(parsed, object_id, field)
    else:
        series = R.link_series(parsed, object_id, field)
    if not series:
        raise ValueError("No results for " + kind[:-1] + " '" + str(object_id) + "'.")
    return {
        "source": label,
        "ok": True,
        "kind": kind,
        "id": object_id,
        "field": field,
        "units": parsed["node_units"] if kind == "nodes" else parsed["link_units"],
        "series": series,
    }


def tool_check(args):
    """Run the network and report the things that usually go wrong."""
    text, label = _source(args)
    model = I.parse_inp(text)
    issues = I.validate(model)
    timeout = float(args.get("timeout") or 120)
    outcome = engine.run(text, timeout=timeout)
    parsed = outcome["parsed"]
    checks = []

    checks.append(
        {
            "check": "solver",
            "ok": parsed["ok"],
            "detail": parsed["errors"] if parsed["errors"] else "ran without errors",
        }
    )
    templates = []
    for warning in parsed["warnings"]:
        template = _TIME_IN_MESSAGE.sub("", warning).strip()
        if template not in templates:
            templates.append(template)
    checks.append(
        {
            "check": "warnings",
            "ok": not parsed["warnings"],
            "detail": {
                "count": len(parsed["warnings"]),
                "kinds": templates[:10],
            }
            if parsed["warnings"]
            else "none",
        }
    )
    structural_errors = [i for i in issues if i["severity"] == "error"]
    checks.append(
        {
            "check": "structure",
            "ok": not structural_errors,
            "detail": issues if issues else "no structural problems",
        }
    )

    if parsed["ok"]:
        last = R.last_period(parsed)
        nodes = last["nodes"]

        negative = {}
        disconnected = {}
        for node_id, record in nodes.items():
            pressure = record.get("pressure")
            if not isinstance(pressure, (int, float)):
                continue
            if pressure < _DISCONNECTED_PRESSURE:
                disconnected[node_id] = pressure
            elif pressure < 0:
                negative[node_id] = pressure
        checks.append(
            {
                "check": "negative_pressure",
                "ok": not negative,
                "detail": negative if negative else "every node has positive pressure",
            }
        )
        checks.append(
            {
                "check": "connectivity",
                "ok": not disconnected,
                "detail": disconnected
                if disconnected
                else "every node is connected to a supply",
            }
        )

        total = 0.0
        magnitude = 0.0
        counted = 0
        for record in nodes.values():
            demand = record.get("demand")
            if isinstance(demand, (int, float)):
                total += demand
                magnitude += abs(demand)
                counted += 1
        # EPANET prints demands to two decimals, so every node carries up to
        # 0.005 of rounding error. Summing N of them can legitimately drift by
        # 0.005*N, and a tolerance below that reports false failures on runs
        # that are actually fine.
        rounding = 0.005 * counted
        tolerance = max(1e-6, rounding + 1e-4 * magnitude)
        checks.append(
            {
                "check": "mass_balance",
                "ok": abs(total) <= tolerance,
                "detail": {
                    "net_demand": round(total, 6),
                    "tolerance": round(tolerance, 6),
                    "nodes_counted": counted,
                    "note": "net demand should be about zero: supply equals consumption",
                },
            }
        )

        tank_problems = {}
        for tank in model.get("tanks", []):
            record = nodes.get(tank["id"])
            if not record:
                continue
            head = record.get("head")
            if not isinstance(head, (int, float)):
                continue
            level = head - float(tank.get("elevation", 0))
            low = float(tank.get("min_level", 0))
            high = float(tank.get("max_level", 0))
            if level < low - 1e-6 or level > high + 1e-6:
                tank_problems[tank["id"]] = {
                    "level": round(level, 3),
                    "min_level": low,
                    "max_level": high,
                }
        checks.append(
            {
                "check": "tank_levels",
                "ok": not tank_problems,
                "detail": tank_problems if tank_problems else "every tank stays within its limits",
            }
        )

        expected = model["units"]
        checks.append(
            {
                "check": "units",
                "ok": True,
                "detail": {
                    "declared": model["flow_units"],
                    "report_units": parsed["node_units"],
                    "expected": [expected["flow"], expected["head"], expected["pressure"]],
                },
            }
        )

    return {
        "source": label,
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
        "summary": _summary(parsed),
    }


def tool_save_report(args):
    """Run a network and write the full .rpt report to disk."""
    text, label = _source(args)
    out_path = args.get("out_path")
    if not out_path:
        raise ValueError("out_path is required (where to write the .rpt file).")
    timeout = float(args.get("timeout") or 120)
    outcome = engine.run(text, timeout=timeout, keep_report=True)
    try:
        shutil.copyfile(outcome["rpt_path"], out_path)
        saved_inp = None
        if args.get("save_inp"):
            saved_inp = os.path.splitext(out_path)[0] + ".inp"
            shutil.copyfile(outcome["inp_path"], saved_inp)
    finally:
        shutil.rmtree(outcome["workdir"], ignore_errors=True)
    return {
        "source": label,
        "ok": outcome["parsed"]["ok"],
        "report_path": out_path,
        "inp_path": saved_inp,
        "errors": outcome["parsed"]["errors"],
        "summary": _summary(outcome["parsed"]),
    }


HANDLERS = {
    "engine_info": tool_engine_info,
    "inspect": tool_inspect,
    "validate": tool_validate,
    "build": tool_build,
    "run": tool_run,
    "build_and_run": tool_build_and_run,
    "results": tool_results,
    "envelope": tool_envelope,
    "series": tool_series,
    "check": tool_check,
    "save_report": tool_save_report,
}


# --------------------------------------------------------------------------
# Tool schemas
# --------------------------------------------------------------------------

_SPEC_SCHEMA = {
    "type": "object",
    "description": "Structured network description. Only the sections you need.",
    "properties": {
        "title": {"type": "string"},
        "options": {
            "type": "object",
            "description": "Flow units and headloss formula. units is one of CFS, GPM, MGD, IMGD, AFD, LPS, LPM, MLD, CMH, CMD, CMS. headloss is H-W, D-W or C-M.",
            "properties": {
                "units": {"type": "string"},
                "headloss": {"type": "string"},
                "trials": {"type": "number"},
                "accuracy": {"type": "number"},
                "demand_model": {"type": "string", "description": "DDA or PDA"},
                "demand_multiplier": {"type": "number"},
                "minimum_pressure": {"type": "number"},
                "required_pressure": {"type": "number"},
                "pressure_exponent": {"type": "number"},
                "specific_gravity": {"type": "number"},
                "viscosity": {"type": "number"},
                "quality": {"type": "string"},
            },
        },
        "times": {
            "type": "object",
            "description": "Extended-period settings. duration and timesteps use H:MM or H:MM:SS.",
            "properties": {
                "duration": {"type": "string"},
                "hydraulic_timestep": {"type": "string"},
                "pattern_timestep": {"type": "string"},
                "report_timestep": {"type": "string"},
                "start_clocktime": {"type": "string"},
                "statistic": {"type": "string"},
            },
        },
        "report": {
            "type": "object",
            "description": "What the .rpt should contain. Set nodes and links to All to get result tables.",
            "properties": {
                "status": {"type": "string"},
                "summary": {"type": "string"},
                "energy": {"type": "string"},
                "nodes": {"type": "string"},
                "links": {"type": "string"},
            },
        },
        "junctions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "elevation": {"type": "number"},
                    "demand": {"type": "number"},
                    "pattern": {"type": "string"},
                },
                "required": ["id"],
            },
        },
        "reservoirs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "head": {"type": "number"},
                    "pattern": {"type": "string"},
                },
                "required": ["id"],
            },
        },
        "tanks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "elevation": {"type": "number"},
                    "init_level": {"type": "number"},
                    "min_level": {"type": "number"},
                    "max_level": {"type": "number"},
                    "diameter": {"type": "number"},
                    "min_volume": {"type": "number"},
                    "volume_curve": {"type": "string"},
                },
                "required": ["id"],
            },
        },
        "pipes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "node1": {"type": "string"},
                    "node2": {"type": "string"},
                    "length": {"type": "number"},
                    "diameter": {"type": "number"},
                    "roughness": {"type": "number"},
                    "minor_loss": {"type": "number"},
                    "status": {"type": "string"},
                },
                "required": ["id", "node1", "node2"],
            },
        },
        "pumps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "node1": {"type": "string"},
                    "node2": {"type": "string"},
                    "curve": {"type": "string"},
                    "power": {"type": "number"},
                    "speed": {"type": "number"},
                    "pattern": {"type": "string"},
                },
                "required": ["id", "node1", "node2"],
            },
        },
        "valves": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "node1": {"type": "string"},
                    "node2": {"type": "string"},
                    "diameter": {"type": "number"},
                    "type": {"type": "string", "description": "PRV, PSV, PBV, FCV, TCV, GPV"},
                    "setting": {"type": "number"},
                    "minor_loss": {"type": "number"},
                },
                "required": ["id", "node1", "node2"],
            },
        },
        "demands": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "demand": {"type": "number"},
                    "pattern": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["id"],
            },
        },
        "emitters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "coefficient": {"type": "number"}},
                "required": ["id"],
            },
        },
        "status": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "status": {"type": "string"}},
                "required": ["id"],
            },
        },
        "patterns": {
            "type": "object",
            "description": "Map of pattern ID to a list of multipliers, e.g. {\"P1\": [1.0, 0.9, 0.8]}.",
            "additionalProperties": {"type": "array", "items": {"type": "number"}},
        },
        "curves": {
            "type": "object",
            "description": "Map of curve ID to a list of [x, y] points, e.g. {\"C1\": [[0, 100], [1000, 0]]}.",
            "additionalProperties": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "number"}},
            },
        },
        "controls": {"type": "array", "items": {"type": "string"}},
        "rules": {"type": "array", "items": {"type": "string"}},
        "coordinates": {
            "type": "object",
            "description": "Map of node ID to [x, y] map coordinates.",
            "additionalProperties": {"type": "array", "items": {"type": "number"}},
        },
    },
}

TOOLS = [
    {
        "name": "engine_info",
        "description": "Report which EPANET solver binary will be used, its version and whether epanet2.dll is next to it. Call this first if a run fails unexpectedly.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "inspect",
        "description": "Parse an .inp network file and describe it: units, headloss formula, counts of junctions/reservoirs/tanks/pipes/pumps/valves, patterns, curves, plus structural problems. Does not run the solver. Returns counts and options only; set include_objects for the full per-node and per-link listing.",
        "inputSchema": {
            "type": "object",
            "properties": dict(
                _SOURCE_PROPS,
                include_objects={
                    "type": "boolean",
                    "description": "Also return the full per-node and per-link dicts. Large; usually unnecessary.",
                },
            ),
        },
    },
    {
        "name": "validate",
        "description": "Check an .inp network for structural problems without running it: duplicate IDs, unknown node references, self-loops, bad lengths or diameters, tanks with impossible levels, missing patterns or curves, and nodes disconnected from any reservoir or tank.",
        "inputSchema": {
            "type": "object",
            "properties": dict(_SOURCE_PROPS),
        },
    },
    {
        "name": "build",
        "description": "Build .inp file text from a structured JSON spec, then parse it back and validate it. Use this to author a network from an assignment description. Returns the .inp text when include_inp is true or when validation fails.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": _SPEC_SCHEMA,
                "save_to": {"type": "string", "description": "Optional path to write the .inp file to."},
                "include_inp": {"type": "boolean", "description": "Return the generated .inp text."},
            },
            "required": ["spec"],
        },
    },
    {
        "name": "run",
        "description": "Run a hydraulic simulation and return the verdict, a compact summary, and a headline (min/max pressure and velocity with the node or link they occur at). This is the main tool: give it an .inp path or the .inp text. Set include_results for the full last-period tables.",
        "inputSchema": {
            "type": "object",
            "properties": dict(
                _SOURCE_PROPS,
                timeout={"type": "number", "description": "Seconds before the solver is killed. Default 120."},
                include_results={
                    "type": "boolean",
                    "description": "Also return the full node and link tables for the last reporting period.",
                },
                full_report={"type": "boolean", "description": "Also return the entire raw .rpt text."},
            ),
        },
    },
    {
        "name": "build_and_run",
        "description": "Build a network from a JSON spec, validate it, run it, and return the results in one call. The fastest path from an assignment description to numbers. Returns a headline by default; set include_results for the full last-period tables.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": _SPEC_SCHEMA,
                "save_to": {"type": "string", "description": "Optional path to write the .inp file to."},
                "timeout": {"type": "number", "description": "Seconds before the solver is killed. Default 120."},
                "include_inp": {"type": "boolean", "description": "Return the generated .inp text."},
                "include_results": {
                    "type": "boolean",
                    "description": "Also return the full node and link tables for the last reporting period.",
                },
            },
            "required": ["spec"],
        },
    },
    {
        "name": "results",
        "description": "Run a network and query node or link results, filtered by ID and by column. Use period 'last' (default), 'first', 'all', or a time label such as '12:00:00'. With period 'all' the default mode is 'summary' (per-object min/max/mean); set mode 'full' for every timestep.",
        "inputSchema": {
            "type": "object",
            "properties": dict(
                _SOURCE_PROPS,
                kind={"type": "string", "enum": ["nodes", "links"], "description": "Which table to read. Default nodes."},
                ids={"type": "array", "items": {"type": "string"}, "description": "Only these node or link IDs."},
                fields={
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Only these columns, e.g. [\"demand\", \"head\", \"pressure\"] for nodes or [\"flow\", \"velocity\", \"headloss\"] for links.",
                },
                period={"type": "string", "description": "last, first, all, or a time label."},
                mode={
                    "type": "string",
                    "enum": ["full", "summary"],
                    "description": "full returns every selected period; summary returns per-object min/max/mean. Defaults to summary when period is 'all', otherwise full.",
                },
                timeout={"type": "number"},
            ),
        },
    },
    {
        "name": "envelope",
        "description": "Run an extended-period simulation and report the minimum and maximum of one field per node or link across every reporting period, with the times they occur. Use this for 24-hour pressure envelopes.",
        "inputSchema": {
            "type": "object",
            "properties": dict(
                _SOURCE_PROPS,
                kind={"type": "string", "enum": ["nodes", "links"]},
                field={"type": "string", "description": "Column to track, e.g. pressure or head for nodes, flow or velocity for links."},
                timeout={"type": "number"},
            ),
        },
    },
    {
        "name": "series",
        "description": "Run an extended-period simulation and return one field of one node or link at every reporting period, as a time series.",
        "inputSchema": {
            "type": "object",
            "properties": dict(
                _SOURCE_PROPS,
                kind={"type": "string", "enum": ["nodes", "links"]},
                id={"type": "string", "description": "The node or link ID."},
                field={"type": "string", "description": "Column to read, e.g. pressure or head for nodes, flow or velocity for links."},
                timeout={"type": "number"},
            ),
            "required": ["id"],
        },
    },
    {
        "name": "check",
        "description": "Run a network and report the things that usually go wrong: solver errors, warnings, structural problems, negative pressures, mass balance, tanks outside their levels, and the units actually used. Use this to double-check an answer before trusting it.",
        "inputSchema": {
            "type": "object",
            "properties": dict(
                _SOURCE_PROPS,
                timeout={"type": "number"},
            ),
        },
    },
    {
        "name": "save_report",
        "description": "Run a network and write the complete .rpt report to disk, optionally alongside the .inp file. Use this when the user wants the actual EPANET report file.",
        "inputSchema": {
            "type": "object",
            "properties": dict(
                _SOURCE_PROPS,
                out_path={"type": "string", "description": "Where to write the .rpt file."},
                save_inp={"type": "boolean", "description": "Also write the .inp next to the report."},
                timeout={"type": "number"},
            ),
            "required": ["out_path"],
        },
    },
]


# --------------------------------------------------------------------------
# JSON-RPC dispatch
# --------------------------------------------------------------------------


def _call_tool(params):
    name = params.get("name")
    arguments = params.get("arguments") or {}
    handler = HANDLERS.get(name)
    if handler is None:
        return {
            "content": [{"type": "text", "text": "Unknown tool: " + str(name)}],
            "isError": True,
        }
    try:
        result = handler(arguments)
        return {"content": [{"type": "text", "text": _text(result)}], "isError": False}
    except Exception as exc:
        return {
            "content": [{"type": "text", "text": type(exc).__name__ + ": " + str(exc)}],
            "isError": True,
        }


def _handle(message):
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}
    if method is None:
        return None
    if message_id is None:
        return None
    try:
        if method == "initialize":
            requested = params.get("protocolVersion") or PROTOCOL_VERSION
            result = {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = _call_tool(params)
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "prompts/list":
            result = {"prompts": []}
        else:
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32601, "message": "Method not found: " + str(method)},
            }
        return {"jsonrpc": "2.0", "id": message_id, "result": result}
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "error": {"code": -32603, "message": type(exc).__name__ + ": " + str(exc)},
        }


def _selftest():
    """Run a small network end to end and print the result."""
    spec = {
        "title": "epanet-mcp self test",
        "options": {"units": "GPM", "headloss": "H-W"},
        "junctions": [
            {"id": "J1", "elevation": 100, "demand": 100},
            {"id": "J2", "elevation": 90, "demand": 50},
        ],
        "reservoirs": [{"id": "R1", "head": 200}],
        "pipes": [
            {"id": "P1", "node1": "R1", "node2": "J1", "length": 1000, "diameter": 12, "roughness": 100},
            {"id": "P2", "node1": "J1", "node2": "J2", "length": 800, "diameter": 8, "roughness": 100},
        ],
        "report": {"status": "Yes", "summary": "Yes", "nodes": "All", "links": "All"},
    }
    print(_text(tool_engine_info({})))
    print(_text(tool_build_and_run({"spec": spec})))
    print(_text(tool_check({"inp_text": I.build_inp(spec)})))


def main():
    _configure_streams()
    if "--selftest" in sys.argv:
        _selftest()
        return
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except Exception:
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        response = _handle(message)
        if response is not None:
            _send(response)


if __name__ == "__main__":
    main()
