"""EPANET .rpt output-report parser.

EPANET writes a fixed-width text report. The layout depends on the [REPORT]
section of the input file, so this parser reads the column headers instead of
assuming a fixed set of columns.

Recognised blocks
-----------------
* the run summary (Input Data File, Number of Junctions, Headloss Formula, ...)
* Error / WARNING lines
* the Hydraulic Status log
* Node Results tables, once per reporting period
* Link Results tables, once per reporting period
"""

from __future__ import annotations

import re

_TIME_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})$")
_ERROR_RE = re.compile(r"^\s*Error\s+(\d+):\s*(.*)$")
_WARNING_RE = re.compile(r"^\s*WARNING[:\s]\s*(.*)$", re.IGNORECASE)
_STATUS_RE = re.compile(r"^\s*(\d+:\d{2}:\d{2}):\s*(.*)$")
_NODE_HDR_RE = re.compile(r"^\s*Node Results(?:\s+at\s+(\S+)\s+hrs)?:\s*$")
_LINK_HDR_RE = re.compile(r"^\s*Link Results(?:\s+at\s+(\S+)\s+hrs)?:\s*$")
_SUMMARY_RE = re.compile(r"^\s{2,}([A-Za-z][A-Za-z0-9 /()\-]*?)\s*\.{3,}\s*(.*)$")


def _to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _split_row(line):
    """Split a fixed-width result row into (id, [values], trailing_note)."""
    tokens = line.split()
    if not tokens:
        return None, [], ""
    node_id = tokens[0]
    rest = tokens[1:]
    note = ""
    if rest and _to_float(rest[-1]) is None:
        note = rest[-1]
        rest = rest[:-1]
    values = []
    for token in rest:
        number = _to_float(token)
        values.append(number if number is not None else token)
    return node_id, values, note


def _is_rule(line):
    """True for the dashed horizontal rules EPANET draws around table headers."""
    stripped = line.strip()
    return bool(stripped) and set(stripped) <= set("- ")


def _read_table(lines, start):
    """Read one result table beginning at the header line index.

    Returns (columns, units, rows, next_index).
    """
    index = start
    # A dashed rule usually sits between the block title and the column header.
    while index < len(lines) and _is_rule(lines[index]):
        index += 1
    if index >= len(lines):
        return [], [], [], index
    header = lines[index].split()
    index += 1
    units = []
    if index < len(lines):
        candidate = lines[index].split()
        if candidate and _to_float(candidate[0]) is None and len(candidate) >= 2:
            units = candidate[1:]
            index += 1
    # Skip the dashed rule under the units line.
    while index < len(lines) and _is_rule(lines[index]):
        index += 1
    rows = []
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            break
        if set(line.strip()) <= set("- "):
            index += 1
            continue
        node_id, values, note = _split_row(line)
        if node_id is None:
            break
        rows.append({"id": node_id, "values": values, "note": note})
        index += 1
    return header, units, rows, index


def _rows_to_dicts(columns, units, rows):
    """Turn positional rows into dicts keyed by column name."""
    out = {}
    for row in rows:
        record = {}
        for position, name in enumerate(columns):
            if position < len(row["values"]):
                record[name.lower()] = row["values"][position]
        if row["note"]:
            record["note"] = row["note"]
        out[row["id"]] = record
    return out


def parse_rpt(text):
    """Parse .rpt text into a structured dict."""
    lines = text.splitlines()
    result = {
        "ok": False,
        "errors": [],
        "warnings": [],
        "summary": {},
        "hydraulic_status": [],
        "periods": [],
        "node_columns": [],
        "link_columns": [],
        "node_units": [],
        "link_units": [],
        "raw_line_count": len(lines),
    }

    current_period = None
    index = 0
    while index < len(lines):
        line = lines[index]

        error_match = _ERROR_RE.match(line)
        if error_match:
            result["errors"].append(
                {"code": int(error_match.group(1)), "message": error_match.group(2).strip()}
            )
            index += 1
            continue

        warning_match = _WARNING_RE.match(line)
        if warning_match:
            message = warning_match.group(1).strip()
            if message:
                result["warnings"].append(message)
            index += 1
            continue

        summary_match = _SUMMARY_RE.match(line)
        if summary_match:
            key = summary_match.group(1).strip()
            value = summary_match.group(2).strip()
            if key and value:
                result["summary"][key] = value
            index += 1
            continue

        if line.strip().startswith("Hydraulic Status:"):
            index += 1
            while index < len(lines):
                candidate = lines[index]
                if not candidate.strip():
                    index += 1
                    continue
                if set(candidate.strip()) <= set("- "):
                    index += 1
                    continue
                status_match = _STATUS_RE.match(candidate)
                if not status_match:
                    break
                result["hydraulic_status"].append(
                    {"time": status_match.group(1), "message": status_match.group(2).strip()}
                )
                index += 1
            continue

        node_match = _NODE_HDR_RE.match(line)
        if node_match:
            time_label = node_match.group(1) or "0:00:00"
            columns, units, rows, index = _read_table(lines, index + 1)
            result["node_columns"] = columns
            result["node_units"] = units
            current_period = _get_period(result, time_label)
            current_period["nodes"] = _rows_to_dicts(columns, units, rows)
            continue

        link_match = _LINK_HDR_RE.match(line)
        if link_match:
            time_label = link_match.group(1) or "0:00:00"
            columns, units, rows, index = _read_table(lines, index + 1)
            result["link_columns"] = columns
            result["link_units"] = units
            current_period = _get_period(result, time_label)
            current_period["links"] = _rows_to_dicts(columns, units, rows)
            continue

        index += 1

    result["ok"] = not result["errors"]
    result["periods"].sort(key=lambda period: _time_seconds(period["time"]))
    return result


def _get_period(result, time_label):
    for period in result["periods"]:
        if period["time"] == time_label:
            return period
    period = {"time": time_label, "nodes": {}, "links": {}}
    result["periods"].append(period)
    return period


def _time_seconds(label):
    match = _TIME_RE.match(str(label))
    if not match:
        return 0
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))


def time_seconds(label):
    """Public helper: 'H:MM:SS' -> seconds."""
    return _time_seconds(label)


def last_period(parsed):
    """Return the final reporting period, or an empty one."""
    if not parsed["periods"]:
        return {"time": "0:00:00", "nodes": {}, "links": {}}
    return parsed["periods"][-1]


def node_series(parsed, node_id, field):
    """Extract one field of one node across every reporting period."""
    series = []
    for period in parsed["periods"]:
        record = period["nodes"].get(node_id)
        if record is None:
            continue
        series.append({"time": period["time"], "value": record.get(field)})
    return series


def link_series(parsed, link_id, field):
    """Extract one field of one link across every reporting period."""
    series = []
    for period in parsed["periods"]:
        record = period["links"].get(link_id)
        if record is None:
            continue
        series.append({"time": period["time"], "value": record.get(field)})
    return series


def envelope(parsed, kind, field):
    """Min/max of a field across all periods, per object.

    Returns {id: {min, max, min_time, max_time}}.
    """
    out = {}
    for period in parsed["periods"]:
        table = period[kind]
        for object_id, record in table.items():
            value = record.get(field)
            if not isinstance(value, (int, float)):
                continue
            entry = out.get(object_id)
            if entry is None:
                out[object_id] = {
                    "min": value,
                    "max": value,
                    "min_time": period["time"],
                    "max_time": period["time"],
                }
            else:
                if value < entry["min"]:
                    entry["min"] = value
                    entry["min_time"] = period["time"]
                if value > entry["max"]:
                    entry["max"] = value
                    entry["max_time"] = period["time"]
    return out
