# MCP Container Tracking — learning project

An MCP server that gives Claude tools to track ocean containers from
**DCSA Track & Trace events** (the format Maersk and other carriers use).

For now the data is **hardcoded**: one JSON file per container in `data/`
(currently `data/MSKU1712406.json`). Later, replace `load_events()` in
`tracking_server.py` with a real carrier API call. None of the tools need to change.

```
├── tracking_server.py     # the MCP server
├── data/MSKU1712406.json  # hardcoded DCSA events for one container
├── test_client.py         # calls every tool without Claude
├── requirements.txt
├── Dockerfile / render.yaml
└── README.md
```

## Tools

| Tool | What it answers |
|---|---|
| `list_containers` | Which containers have data |
| `get_container_status` | Shipment summary: **status, last location, origin country, destination country, ETD, ETA, transit time**, booking, route |
| `get_milestones` | Key steps: empty pickup → gate in → loaded → departed → transshipment → arrived → discharged → delivered → empty return |
| `get_timeline` | Every event in time order, DCSA codes translated to plain English (`include_estimates`, `include_documents`) |
| `get_vessel_legs` | Each ocean leg: vessel, IMO, voyage, load/discharge port and days on board |
| `get_documents` | Shipping instructions / bill of lading events |

There is also a **resource** `tracking://containers` and a **prompt** `container_report`.

### How the data is interpreted

- **Event types:** `TRANSPORT` (vessel/rail/truck ARRI/DEPA), `EQUIPMENT`
  (container LOAD, DISC, GTIN, GTOT, empty/laden) and `SHIPMENT` (documents).
- **Classifier:** `ACT` = actual, `EST` = estimated, `PLN` = planned.
  Milestones and the current status use **actual events only**.
- **Port of loading** = where the first LOAD happened; **port of discharge** =
  where the last DISC happened; any DISC in between = **transshipment**.
- **Sorting** uses real UTC time, because each event has its own local
  time zone (India +05:30, Brazil −03:00). Times are *displayed* in local time.
- **ETD** = vessel departure from the port of loading; **ETA** = vessel arrival
  at the port of discharge. Each is marked *Actual* (ATD/ATA) or *Estimated*.
- **Transit time:** `port_to_port_days` (ATD → ATA) and `door_to_door_days`
  (full gate-in at origin → full gate-out at destination). While the container
  is still moving you get `days_at_sea_so_far` / `days_since_origin_gate_in`.
- **Countries** come from the first 2 letters of the UN/LOCODE (`IN`PIR → India).
- **Status codes:** EMPTY_PICKED_UP, RECEIVED_AT_ORIGIN, TO_PORT_OF_LOADING,
  AT_PORT_OF_LOADING, ON_VESSEL, TRANSSHIPMENT, ARRIVED, DISCHARGED, DELIVERED,
  COMPLETED.
- **Limitation:** the destination port and ETA can only be known if the carrier
  data includes it (normally as estimated future vessel arrivals).
- **Input validation:** container numbers are cleaned up (`msku 171240-6` →
  `MSKU1712406`), must match 4 letters + 7 digits, and must pass the
  ISO 6346 check digit.

## Run and test locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python test_client.py                     # over stdio

MCP_TRANSPORT=http MCP_API_KEY=my-secret python tracking_server.py   # terminal 1
MCP_API_KEY=my-secret python test_client.py http://localhost:8000/mcp # terminal 2
```

## Deploy (Render) and connect to Claude

- Render start command: `python tracking_server.py`
- Env vars: `MCP_TRANSPORT=http`, `MCP_API_KEY=<secret>`, `PYTHON_VERSION=3.12.7`
- Claude → Customize → Connectors → Add custom connector
  - URL: `https://<your-app>.onrender.com/mcp` (exactly one slash before `mcp`)
  - Request header: `Authorization` = `Bearer <secret>`

## Add another container

Save its DCSA events JSON as `data/<CONTAINER_NUMBER>.json` (same
`{"events": [...]}` shape), then commit and push. `list_containers` picks it
up automatically.

## Next steps

- Replace `load_events()` with a live Maersk DCSA API call (keep the API
  credentials in an env var, never in the repo)
- Add more carriers: normalise each carrier's format into the same event shape
- Add a `get_eta` tool once you have data with future `EST`/`PLN` events
