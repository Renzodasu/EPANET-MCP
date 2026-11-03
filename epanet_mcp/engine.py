"""Locate and run the EPANET 2.2 command-line solver.

The solver is EPA's own runepanet.exe (EPANET 2.2.0), bundled in ./bin next to
this package. It is a plain file-in / file-out program:

    runepanet.exe <network.inp> <report.rpt>

so no DLL binding, no 32/64-bit problem and no third-party dependency is
involved. Every run happens in a private temporary directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from . import rpt as R

#: Where the bundled solver lives, relative to the package directory.
_BUNDLED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")

#: Extra places worth checking when the bundled copy is missing.
_FALLBACKS = [
    r"C:\Program Files (x86)\EPANET 2.2",
    r"C:\Program Files\EPANET 2.2",
    r"C:\EPANET2",
]

_VERSION_CACHE = {}


class EngineError(RuntimeError):
    """Raised when the solver cannot be located or executed."""


def engine_path():
    """Return the absolute path of runepanet.exe."""
    candidates = []
    env_home = os.environ.get("EPANET_HOME")
    if env_home:
        candidates.append(os.path.join(env_home, "runepanet.exe"))
        candidates.append(env_home)
    candidates.append(os.path.join(_BUNDLED, "runepanet.exe"))
    for folder in _FALLBACKS:
        candidates.append(os.path.join(folder, "runepanet.exe"))
    found = shutil.which("runepanet")
    if found:
        candidates.append(found)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    raise EngineError(
        "runepanet.exe not found. Set EPANET_HOME to the folder that contains it, "
        "or place it in " + _BUNDLED
    )


def engine_info():
    """Describe the solver that will be used."""
    path = engine_path()
    return {
        "path": path,
        "directory": os.path.dirname(path),
        "version": version(),
        "dll_present": os.path.isfile(os.path.join(os.path.dirname(path), "epanet2.dll")),
    }


def version():
    """Return the solver version string, e.g. '2.2.0'."""
    if "version" in _VERSION_CACHE:
        return _VERSION_CACHE["version"]
    path = engine_path()
    text = ""
    try:
        completed = subprocess.run(
            [path],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=os.path.dirname(path),
        )
        text = (completed.stdout or "") + (completed.stderr or "")
    except Exception:
        text = ""
    label = "unknown"
    for token in text.replace("\r", "\n").split():
        if token[:1].isdigit() and token.count(".") >= 1:
            label = token.strip(".,;:")
            break
    if label == "unknown":
        label = "2.2"
    _VERSION_CACHE["version"] = label
    return label


def run(inp_text, timeout=120, keep_report=False):
    """Run a network given as .inp text.

    Returns a dict with the raw report text, the parsed report, the solver's
    console output and the temporary paths used.
    """
    path = engine_path()
    workdir = tempfile.mkdtemp(prefix="epanet-mcp-")
    inp_path = os.path.join(workdir, "network.inp")
    rpt_path = os.path.join(workdir, "network.rpt")
    try:
        with open(inp_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(inp_text)
        try:
            completed = subprocess.run(
                [path, inp_path, rpt_path],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=workdir,
            )
        except subprocess.TimeoutExpired:
            raise EngineError("EPANET did not finish within " + str(timeout) + " seconds.")
        console = ((completed.stdout or "") + (completed.stderr or "")).strip()
        report_text = ""
        if os.path.isfile(rpt_path):
            with open(rpt_path, "r", encoding="utf-8", errors="replace") as handle:
                report_text = handle.read()
        parsed = R.parse_rpt(report_text)
        return {
            "returncode": completed.returncode,
            "console": console,
            "report_text": report_text,
            "parsed": parsed,
            "workdir": workdir if keep_report else None,
            "inp_path": inp_path if keep_report else None,
            "rpt_path": rpt_path if keep_report else None,
        }
    finally:
        if not keep_report:
            shutil.rmtree(workdir, ignore_errors=True)


def run_file(inp_path, timeout=120, keep_report=False):
    """Run a network given as a path to an .inp file."""
    with open(inp_path, "r", encoding="utf-8", errors="replace") as handle:
        return run(handle.read(), timeout=timeout, keep_report=keep_report)
