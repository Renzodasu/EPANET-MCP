# EPANET 2.2 MCP Server

An MCP server that runs **EPA's own EPANET 2.2 hydraulic solver** and hands the
results back as structured JSON, so an agent can build a network, solve it, and
check the answer without touching the GUI.

The solver is not reimplemented and not re-bound: it is the official
`runepanet.exe` from the EPANET 2.2.0 release, bundled in `bin/`. Every number this
server returns is exactly what the EPANET 2.2 GUI would print.

## Why this shape

- **Exact 2.2.** The community engine (OWA-EPANET 2.3.x, ePyT, epanet-js) has moved
  on. If an assignment says EPANET 2.2, this runs 2.2.
- **No dependencies.** Pure Python standard library. No `pip install`, no DLL
  binding, no 32/64-bit mismatch (the EPA DLL is 32-bit; this never loads it).
- **File in, file out.** `runepanet.exe network.inp network.rpt` is the whole
  interface. Each run happens in a private temporary directory.

## Requirements

- Windows
- Python 3.9 or newer
- Nothing else

## Layout

```
epanet-mcp/
  server.py              entry point (python server.py)
  epanet_mcp/
    __init__.py
    units.py             US / SI unit families
    inp.py               .inp parser, writer, validator
    rpt.py               .rpt report parser
    engine.py            locates and runs runepanet.exe
    server.py            MCP JSON-RPC over stdio + tool schemas
  bin/
    runepanet.exe        EPANET 2.2.0 solver
    epanet2.dll          its engine
    EPANET2.chm          the manual
  tests/
    test_protocol.py     end-to-end test over stdio
```

## Tools

| Tool | What it does |
| --- | --- |
| `engine_info` | Which solver binary is used, its version, whether the DLL is present |
| `inspect` | Parse an .inp and describe it: units, formula, counts, patterns, curves |
| `validate` | Structural checks without solving: duplicates, unknown nodes, disconnected nodes, bad tanks |
| `build` | Build .inp text from a JSON spec, then parse and validate it |
| `run` | Solve and return the verdict, summary, and last-period node and link results |
| `build_and_run` | Build, validate, solve, and return results in one call |
| `results` | Query node or link results, filtered by ID, column, and period |
| `envelope` | Min and max of a field per node or link across a 24-hour run |
| `series` | One field of one node or link at every reporting period |
| `check` | Solve and report what usually goes wrong: errors, warnings, negative pressure, mass balance, tank levels, units |
| `save_report` | Solve and write the full .rpt report to disk |

## Quick start

Describe the network as JSON and let the server build and solve it:

```json
{
  "options": { "units": "GPM", "headloss": "H-W" },
  "junctions": [
    { "id": "J1", "elevation": 100, "demand": 100 },
    { "id": "J2", "elevation": 90,  "demand": 50 }
  ],
  "reservoirs": [{ "id": "R1", "head": 200 }],
  "pipes": [
    { "id": "P1", "node1": "R1", "node2": "J1", "length": 1000, "diameter": 12, "roughness": 100 },
    { "id": "P2", "node1": "J1", "node2": "J2", "length": 800,  "diameter": 8,  "roughness": 100 }
  ],
  "report": { "status": "Yes", "summary": "Yes", "nodes": "All", "links": "All" }
}
```

Call `build_and_run` with that as `spec` and you get back:

```
J1  demand 100.00 gpm  head 199.88 ft  pressure 43.28 psi
J2  demand  50.00 gpm  head 199.78 ft  pressure 47.57 psi
R1  demand -150.00 gpm head 200.00 ft  pressure  0.00 psi  (Reservoir)
P1  flow 150.00 gpm  velocity 0.43 fps  headloss 0.12 /1000ft
P2  flow  50.00 gpm  velocity 0.32 fps  headloss 0.12 /1000ft
```

## The spec format

Only the sections you need. Everything is optional except the objects themselves.

| Key | Shape |
| --- | --- |
| `title` | string |
| `options` | `units`, `headloss` (H-W, D-W, C-M), `trials`, `accuracy`, `demand_model` (DDA or PDA), `demand_multiplier`, `minimum_pressure`, `required_pressure`, `pressure_exponent`, `specific_gravity`, `viscosity`, `quality` |
| `times` | `duration`, `hydraulic_timestep`, `pattern_timestep`, `report_timestep`, `start_clocktime`, `statistic` - all `H:MM` or `H:MM:SS` |
| `report` | `status`, `summary`, `energy`, `nodes`, `links` - set nodes and links to `All` to get result tables |
| `junctions` | `id`, `elevation`, `demand`, `pattern` |
| `reservoirs` | `id`, `head`, `pattern` |
| `tanks` | `id`, `elevation`, `init_level`, `min_level`, `max_level`, `diameter`, `min_volume`, `volume_curve` |
| `pipes` | `id`, `node1`, `node2`, `length`, `diameter`, `roughness`, `minor_loss`, `status` |
| `pumps` | `id`, `node1`, `node2`, `curve` or `power`, `speed`, `pattern` |
| `valves` | `id`, `node1`, `node2`, `diameter`, `type` (PRV, PSV, PBV, FCV, TCV, GPV), `setting`, `minor_loss` |
| `demands` | `id`, `demand`, `pattern`, `category` - extra demands on an existing junction |
| `emitters` | `id`, `coefficient` |
| `status` | `id`, `status` - force a link open or closed |
| `patterns` | map of pattern ID to a list of multipliers |
| `curves` | map of curve ID to a list of `[x, y]` points |
| `controls` | list of raw control statements |
| `rules` | list of raw rule statements |
| `coordinates` | map of node ID to `[x, y]` |

## Units

EPANET has exactly two unit families, chosen by `options.units`:

| Family | Flow units | Head | Pressure | Diameter | Length | Velocity | Headloss |
| --- | --- | --- | --- | --- | --- | --- | --- |
| US | CFS, GPM, MGD, IMGD, AFD | ft | psi | inches | ft | fps | /1000ft |
| SI | LPS, LPM, MLD, CMH, CMD, CMS | m | m | mm | m | m/s | /1000m |

This is the single most common source of silently wrong answers. A diameter of
`12` means 12 inches under GPM and 12 millimetres under LPS. Every tool result
echoes the unit system back so the mismatch is visible.

Roughness also changes meaning with the headloss formula: Hazen-Williams uses a
C-factor (about 100 to 140), Darcy-Weisbach uses an absolute roughness in
millifeet (US) or millimetres (SI), and Chezy-Manning uses Manning n (about
0.013).

## Report requirements

EPANET writes no result tables unless the input file asks for them. The server
sets sensible defaults, but if you supply your own .inp make sure it contains:

```
[REPORT]
 Status  Yes
 Summary Yes
 Nodes   All
 Links   All
```

## Troubleshooting

**"runepanet.exe not found"** - set the `EPANET_HOME` environment variable to the
folder containing it, or put it in `bin/`.

**"Error 223: not enough nodes in network"** - you fed it a binary `.net` file.
The `Examples` folder of the EPANET 2.2 install ships EPANET 2.0 binary networks.
Use text `.inp` files.

**"Error 202: illegal numeric value 24:00 in [OPTIONS] section"** - `Duration` is a
`[TIMES]` keyword, not an `[OPTIONS]` keyword.

**No result tables in the report** - the `[REPORT]` section did not ask for them.
See above.

## Testing

```
python tests/test_protocol.py
```

Spawns the server, speaks JSON-RPC over stdio, and checks 29 assertions
including the reference numbers from the worked example.

## Using with other MCP clients

The server is plain JSON-RPC over stdio, so any MCP client works.

**Claude Code**

```
claude mcp add epanet -- "C:\path\to\python.exe" "C:\path\to\epanet-mcp\server.py"
```

**Codex** (`~/.codex/config.toml`)

```toml
[mcp_servers.epanet]
command = "C:\\path\\to\\python.exe"
args = ["C:\\path\\to\\epanet-mcp\\server.py"]
```

**DeepSeek Harness** registers it through the MCP panel, which writes an entry
into `cordis.patch.yml`.

## License

MIT - see [LICENSE](LICENSE).

The binaries in `bin/` are EPA's, redistributed unmodified under EPANET's own
MIT license (Copyright (c) 2017 EPANET contributors), from the
[EPANET 2.2.0 release](https://github.com/USEPA/EPANET2.2/releases/tag/v2.2.0).
