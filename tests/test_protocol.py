"""End-to-end test of the EPANET MCP server over stdio."""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "server.py")

EPS_SPEC = {
    "title": "EPS test",
    "options": {"units": "GPM", "headloss": "H-W"},
    "junctions": [
        {"id": "J1", "elevation": 100, "demand": 100, "pattern": "PAT1"},
        {"id": "J2", "elevation": 90, "demand": 50, "pattern": "PAT1"},
    ],
    "tanks": [
        {"id": "T1", "elevation": 180, "init_level": 20, "min_level": 0, "max_level": 40, "diameter": 100}
    ],
    "pipes": [
        {"id": "P1", "node1": "T1", "node2": "J1", "length": 1000, "diameter": 12, "roughness": 100},
        {"id": "P2", "node1": "J1", "node2": "J2", "length": 800, "diameter": 8, "roughness": 100},
    ],
    "patterns": {
        "PAT1": [1.0, 0.8, 0.6, 0.5, 0.6, 0.8, 1.0, 1.2, 1.4, 1.3, 1.1, 1.0,
                 0.9, 0.8, 0.7, 0.6, 0.7, 0.9, 1.1, 1.3, 1.4, 1.2, 1.0, 0.9]
    },
    "times": {
        "duration": "24:00",
        "hydraulic_timestep": "1:00",
        "pattern_timestep": "1:00",
        "report_timestep": "1:00",
    },
    "report": {"status": "Yes", "summary": "Yes", "nodes": "All", "links": "All"},
}

BROKEN_INP = """[TITLE]
no fixed head

[JUNCTIONS]
 J1  100  100
 J2   90   50

[PIPES]
 P1  J1  J2  1000  12  100

[OPTIONS]
 Units GPM
 Headloss H-W

[REPORT]
 Nodes All
 Links All
"""


class Client:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.counter = 0

    def request(self, method, params=None):
        self.counter += 1
        message = {"jsonrpc": "2.0", "id": self.counter, "method": method}
        if params is not None:
            message["params"] = params
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed unexpectedly: " + self.proc.stderr.read())
        return json.loads(line)

    def tool(self, name, arguments=None):
        response = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        if "error" in response:
            raise RuntimeError("JSON-RPC error: " + json.dumps(response["error"]))
        result = response["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            raise RuntimeError("tool error: " + text)
        return json.loads(text)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.wait(timeout=10)


PASSED = []
FAILED = []


def check(label, condition, detail=""):
    if condition:
        PASSED.append(label)
        print("  PASS  " + label)
    else:
        FAILED.append(label)
        print("  FAIL  " + label + ("  -> " + str(detail) if detail else ""))


def main():
    client = Client()
    try:
        print("protocol")
        init = client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        check("initialize returns serverInfo", "serverInfo" in init.get("result", {}), init)
        check("initialize echoes protocolVersion", init["result"].get("protocolVersion") == "2024-11-05")

        listing = client.request("tools/list")
        tools = listing["result"]["tools"]
        names = [tool["name"] for tool in tools]
        check("tools/list returns 11 tools", len(tools) == 11, names)
        check("every tool has an inputSchema", all("inputSchema" in tool for tool in tools))

        print("engine")
        info = client.tool("engine_info")
        check("engine found", bool(info.get("path")), info)
        check("engine version is 2.2", str(info.get("version", "")).startswith("2.2"), info.get("version"))
        check("epanet2.dll present", info.get("dll_present") is True)

        print("single period")
        run = client.tool("build_and_run", {"spec": {
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
        }, "include_results": True})
        check("run ok", run.get("ok") is True, run.get("errors"))
        nodes = run["last_period"]["nodes"]
        check("J1 pressure 43.28 psi", abs(nodes["J1"]["pressure"] - 43.28) < 0.01, nodes["J1"])
        check("J2 pressure 47.57 psi", abs(nodes["J2"]["pressure"] - 47.57) < 0.01, nodes["J2"])
        check("R1 demand -150 gpm", abs(nodes["R1"]["demand"] + 150.0) < 0.01, nodes["R1"])
        check("P1 flow 150 gpm", abs(run["last_period"]["links"]["P1"]["flow"] - 150.0) < 0.01)
        check("node units are gpm/ft/psi", run["node_units"] == ["gpm", "ft", "psi"], run["node_units"])

        print("headline")
        headline = run["headline"]
        check("headline reports the lowest pressure", abs(headline["pressure"]["min"] - 0.0) < 0.01, headline["pressure"])
        check("headline names the lowest-pressure node", headline["pressure"]["min_node"] == "R1", headline["pressure"])
        check("headline names the highest-pressure node", headline["pressure"]["max_node"] == "J2", headline["pressure"])
        check("headline names the fastest link", headline["velocity"]["max_link"] == "P1", headline["velocity"])
        check("headline names the slowest link", headline["velocity"]["min_link"] == "P2", headline["velocity"])
        check("run omits the full tables by default", "last_period" not in client.tool("run", {"inp_text": client.tool("build", {"spec": EPS_SPEC, "include_inp": True})["inp_text"]}))

        print("extended period")
        eps = client.tool("build_and_run", {"spec": EPS_SPEC})
        check("EPS run ok", eps.get("ok") is True, eps.get("errors"))
        check("EPS has 25 periods", eps.get("period_count") == 25, eps.get("period_count"))

        env = client.tool("envelope", {"inp_text": client.tool("build", {"spec": EPS_SPEC, "include_inp": True})["inp_text"], "kind": "nodes", "field": "pressure"})
        check("envelope covers J1 and J2", {"J1", "J2"} <= set(env["envelope"]), list(env["envelope"]))
        check("envelope min <= max", all(v["min"] <= v["max"] for v in env["envelope"].values()))

        series = client.tool("series", {"inp_text": client.tool("build", {"spec": EPS_SPEC, "include_inp": True})["inp_text"], "kind": "nodes", "id": "J1", "field": "pressure"})
        check("series has 25 points", len(series["series"]) == 25, len(series["series"]))
        check("series values are numbers", all(isinstance(p["value"], (int, float)) for p in series["series"]))

        print("results filtering")
        built = client.tool("build", {"spec": EPS_SPEC, "include_inp": True})["inp_text"]
        filtered = client.tool("results", {"inp_text": built, "kind": "links", "ids": ["P1"], "fields": ["flow"], "period": "last"})
        check("filtered to one link", list(filtered["periods"][0]["links"]) == ["P1"], filtered["periods"][0]["links"])
        check("filtered to one field", list(filtered["periods"][0]["links"]["P1"]) == ["flow"])

        print("checks")
        report = client.tool("check", {"inp_text": built})
        check("check passes on a good network", report.get("ok") is True, [c for c in report["checks"] if not c["ok"]])
        mass = [c for c in report["checks"] if c["check"] == "mass_balance"][0]
        check(
            "mass balance tolerance covers report rounding",
            mass["detail"]["tolerance"] >= 0.005 * mass["detail"]["nodes_counted"],
            mass["detail"],
        )
        check("check omits the full tables", "last_period" not in report)

        print("failure handling")
        broken = client.tool("check", {"inp_text": BROKEN_INP})
        check("broken network is rejected", broken.get("ok") is False)
        codes = [c["check"] for c in broken["checks"] if not c["ok"]]
        check("structure check fires", "structure" in codes, codes)

        print("validation")
        bad = client.tool("validate", {"inp_text": BROKEN_INP})
        check("validate reports no_fixed_head", any(i["code"] == "no_fixed_head" for i in bad["issues"]), bad["issues"])

        print("inspect")
        inspected = client.tool("inspect", {"inp_text": built})
        check("inspect counts 2 junctions", inspected["summary"]["counts"]["junctions"] == 2, inspected["summary"]["counts"])
        check("inspect reports GPM", inspected["summary"]["units"]["flow_units"] == "GPM", inspected["summary"]["units"])

        print("results modes")
        summary = client.tool("results", {"inp_text": built, "kind": "nodes", "period": "all", "fields": ["pressure"]})
        check("period all defaults to summary", summary.get("mode") == "summary", summary.get("mode"))
        check("summary has min/max/mean", set(summary["summary"]["J1"]["pressure"]) == {"min", "max", "mean"}, summary["summary"]["J1"])
        check(
            "summary min <= mean <= max",
            all(
                stats["min"] <= stats["mean"] <= stats["max"]
                for entry in summary["summary"].values()
                for stats in entry.values()
            ),
        )
        full = client.tool("results", {"inp_text": built, "kind": "nodes", "period": "all", "mode": "full", "fields": ["pressure"], "ids": ["J1"]})
        check("mode full returns every period", len(full["periods"]) == 25, len(full["periods"]))
        check("mode full is labelled", full.get("mode") == "full", full.get("mode"))

        print("inspect objects")
        check("inspect omits objects by default", "nodes" not in inspected["summary"] and "links" not in inspected["summary"], list(inspected["summary"]))
        with_objects = client.tool("inspect", {"inp_text": built, "include_objects": True})
        check("include_objects returns them", "nodes" in with_objects["summary"] and "links" in with_objects["summary"], list(with_objects["summary"]))

        print("cache")
        fresh = client.tool("build", {"spec": {
            "options": {"units": "GPM", "headloss": "H-W"},
            "junctions": [{"id": "C1", "elevation": 0, "demand": 10}],
            "reservoirs": [{"id": "CR", "head": 100}],
            "pipes": [{"id": "CP", "node1": "CR", "node2": "C1", "length": 100, "diameter": 6, "roughness": 100}],
            "report": {"status": "Yes", "summary": "Yes", "nodes": "All", "links": "All"},
        }, "include_inp": True})["inp_text"]
        before = client.tool("engine_info").get("cache_entries", 0)
        first = client.tool("run", {"inp_text": fresh})
        after_first = client.tool("engine_info").get("cache_entries", 0)
        second = client.tool("run", {"inp_text": fresh})
        after_second = client.tool("engine_info").get("cache_entries", 0)
        check("first run adds one cache entry", after_first == before + 1, (before, after_first))
        check("second run reuses it", after_second == after_first, (after_first, after_second))
        check("cached run returns the same headline", first["headline"] == second["headline"])

        print("report file")
        out_rpt = os.path.join(HERE, "out.rpt")
        saved = client.tool("save_report", {"inp_text": built, "out_path": out_rpt, "save_inp": True})
        check("report written", os.path.isfile(out_rpt) and os.path.getsize(out_rpt) > 500, saved)
        check("inp written next to it", os.path.isfile(os.path.join(HERE, "out.inp")))
    finally:
        client.close()

    print("")
    print(str(len(PASSED)) + " passed, " + str(len(FAILED)) + " failed")
    if FAILED:
        print("failed: " + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
