"""EPANET unit-system helpers.

EPANET has exactly two unit families, selected by the [OPTIONS] Units keyword.
Everything else (head, pressure, diameter, length) follows from that choice.
Getting this wrong is the single most common source of silently wrong answers,
so every tool result echoes the unit system back to the caller.
"""

US_FLOW_UNITS = ("CFS", "GPM", "MGD", "IMGD", "AFD")
SI_FLOW_UNITS = ("LPS", "LPM", "MLD", "CMH", "CMD", "CMS")
ALL_FLOW_UNITS = US_FLOW_UNITS + SI_FLOW_UNITS

#: flow unit -> (family, label used in reports)
FLOW_LABELS = {
    "CFS": "cfs",
    "GPM": "gpm",
    "MGD": "mgd",
    "IMGD": "Imgd",
    "AFD": "afd",
    "LPS": "L/s",
    "LPM": "Lpm",
    "MLD": "MLd",
    "CMH": "m3/h",
    "CMD": "m3/d",
    "CMS": "m3/s",
}


def is_us(flow_units):
    """True when the network uses US customary units."""
    return str(flow_units).strip().upper() in US_FLOW_UNITS


def family(flow_units):
    return "US" if is_us(flow_units) else "SI"


def unit_set(flow_units):
    """Return the derived unit labels for a given flow-unit keyword."""
    us = is_us(flow_units)
    return {
        "flow_units": str(flow_units).strip().upper(),
        "family": "US" if us else "SI",
        "flow": FLOW_LABELS.get(str(flow_units).strip().upper(), str(flow_units)),
        "head": "ft" if us else "m",
        "pressure": "psi" if us else "m",
        "elevation": "ft" if us else "m",
        "length": "ft" if us else "m",
        "diameter": "in" if us else "mm",
        "velocity": "fps" if us else "m/s",
        "headloss": "/1000ft" if us else "/1000m",
        "roughness": "C" if us else "C",
        "power": "kw",
        "volume": "ft3" if us else "m3",
    }


#: Pressure conversion: report pressure is psi (US) or metres of head (SI).
def pressure_to_head(pressure, flow_units):
    """Convert a reported pressure value into head units (ft or m)."""
    if is_us(flow_units):
        return pressure * 2.306658728  # psi -> ft of water at 4 C
    return pressure


def head_to_pressure(head, flow_units):
    """Convert head (ft or m) into the reported pressure unit (psi or m)."""
    if is_us(flow_units):
        return head / 2.306658728
    return head


#: Common design thresholds expressed in the reported pressure unit.
def default_min_pressure(flow_units):
    """A conventional minimum service pressure: 20 psi or 14 m."""
    return 20.0 if is_us(flow_units) else 14.0
