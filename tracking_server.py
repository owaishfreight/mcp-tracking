
"""
Container Tracking MCP Server (learning project).

Reads DCSA Track & Trace events for containers and gives Claude tools to
answer "where is my container?" questions.

For now the data is HARDCODED: one JSON file per container in ./data/
(data/MSKU1712406.json). Later you can swap `load_events()` for a real
carrier API call (Maersk DCSA, etc.) without changing any of the tools.

Written for the MCP Python SDK v2 (`pip install "mcp[cli]"`).
"""

import hmac
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# stdout carries the MCP protocol over stdio, so log to stderr only.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("tracking")

DATA_DIR = Path(__file__).with_name("data")

mcp = MCPServer(
    name="container-tracking",
    instructions=(
        "Ocean container tracking using DCSA events. Start with "
        "get_container_status for 'where is my container' questions; use "
        "get_milestones or get_timeline for detail and get_vessel_legs for "
        "vessels/voyages. Call list_containers to see which containers exist. "
        "Times are local to each location; 'Estimated' events are plans, not facts."
    ),
)

# ---------------------------------------------------------------------------
# DCSA code -> plain English
# ---------------------------------------------------------------------------

TRANSPORT_EVENTS = {"ARRI": "Arrived", "DEPA": "Departed"}
EQUIPMENT_EVENTS = {
    "LOAD": "Loaded",
    "DISC": "Discharged",
    "GTIN": "Gated in",
    "GTOT": "Gated out",
    "STUF": "Stuffed",
    "STRP": "Stripped",
    "PICK": "Picked up",
    "DROP": "Dropped off",
    "INSP": "Inspected",
    "RSEA": "Resealed",
    "RMVD": "Removed",
}
SHIPMENT_EVENTS = {
    "RECE": "Received",
    "DRFT": "Drafted",
    "PENA": "Pending approval",
    "PENU": "Pending update",
    "REJE": "Rejected",
    "APPR": "Approved",
    "ISSU": "Issued",
    "SURR": "Surrendered",
    "SUBM": "Submitted",
    "VOID": "Void",
    "CONF": "Confirmed",
    "REQS": "Requested",
    "CMPL": "Completed",
    "HOLD": "On hold",
    "RELS": "Released",
    "CANC": "Cancelled",
}
DOCUMENT_TYPES = {
    "SHI": "Shipping instructions",
    "TRD": "Transport document (Bill of Lading)",
    "BKG": "Booking",
    "ARN": "Arrival notice",
}
CLASSIFIERS = {"ACT": "Actual", "EST": "Estimated", "PLN": "Planned", "REQ": "Requested"}
FACILITY_TYPES = {
    "POTE": "Port terminal",
    "INTE": "Inland terminal",
    "DEPO": "Depot",
    "CLOC": "Customer location",
    "BOCR": "Border crossing",
    "COFS": "Container freight station",
    "OFFD": "Off-dock storage",
    "BRTH": "Berth",
}

CONTAINER_RE = re.compile(r"^[A-Z]{4}\d{7}$")


# ---------------------------------------------------------------------------
# Data loading + helpers
# ---------------------------------------------------------------------------

def _iso6346_check_digit_ok(container: str) -> bool:
    """Validate the last digit of a container number (ISO 6346)."""
    values, v = {}, 10
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if v % 11 == 0:  # skip multiples of 11
            v += 1
        values[ch] = v
        v += 1
    total = sum(
        (values[ch] if ch.isalpha() else int(ch)) * 2**i
        for i, ch in enumerate(container[:10])
    )
    return total % 11 % 10 == int(container[10])


def _normalise(container_number: str) -> str:
    c = re.sub(r"[\s-]", "", container_number or "").upper()
    if not CONTAINER_RE.match(c):
        raise ToolError(
            f"'{container_number}' is not a valid container number. "
            "Expected 4 letters + 7 digits, e.g. MSKU1712406."
        )
    if not _iso6346_check_digit_ok(c):
        raise ToolError(f"'{c}' has an invalid check digit (ISO 6346).")
    return c


def available_containers() -> list[str]:
    return sorted(p.stem for p in DATA_DIR.glob("*.json"))


def load_events(container_number: str) -> list[dict]:
    """Return the raw DCSA events for a container.
    HARDCODED for now: reads data/<container>.json. Replace with an API call later."""
    c = _normalise(container_number)
    path = DATA_DIR / f"{c}.json"
    if not path.exists():
        raise ToolError(
            f"No tracking data for {c}. Available containers: "
            + ", ".join(available_containers())
        )
    return json.loads(path.read_text())["events"]


def _dt(event: dict) -> datetime:
    return datetime.fromisoformat(event["eventDateTime"].replace("Z", "+00:00"))


def _fmt_time(event: dict) -> str:
    """Local time at the location, with its UTC offset, e.g. '2026-11-18 16:35 (UTC-03:00)'."""
    d = _dt(event)
    offset = d.strftime("%z")
    offset = "UTC" if offset in ("+0000", "") else f"UTC{offset[:3]}:{offset[3:]}"
    return f"{d:%Y-%m-%d %H:%M} ({offset})"


def _place(event: dict) -> str:
    tc = event.get("transportCall") or {}
    name = (tc.get("location") or {}).get("locationName", "Unknown location")
    unloc = tc.get("UNLocationCode")
    return f"{name} ({unloc})" if unloc else name


def _unloc(event: dict) -> str | None:
    return (event.get("transportCall") or {}).get("UNLocationCode")


def _vessel(event: dict) -> dict | None:
    tc = event.get("transportCall") or {}
    v = tc.get("vessel")
    if not v:
        return None
    return {
        "name": v.get("vesselName"),
        "imo": v.get("vesselIMONumber"),
        "flag": v.get("vesselFlag"),
        "voyage": tc.get("carrierVoyageNumber") or tc.get("exportVoyageNumber"),
    }


def _describe(event: dict) -> dict:
    """Turn one raw DCSA event into a readable dict."""
    etype = event["eventType"]
    status = CLASSIFIERS.get(event.get("eventClassifierCode"), event.get("eventClassifierCode"))
    out = {"time": _fmt_time(event), "status": status}

    if etype == "SHIPMENT":
        doc = DOCUMENT_TYPES.get(event.get("documentTypeCode"), event.get("documentTypeCode"))
        action = SHIPMENT_EVENTS.get(event.get("shipmentEventTypeCode"), event.get("shipmentEventTypeCode"))
        out["event"] = f"{doc} {action.lower()}"
        out["document_id"] = event.get("documentID")
        return out

    tc = event.get("transportCall") or {}
    mode = (tc.get("modeOfTransport") or "").lower()
    facility = FACILITY_TYPES.get(tc.get("facilityTypeCode"), tc.get("facilityTypeCode"))

    if etype == "EQUIPMENT":
        action = EQUIPMENT_EVENTS.get(event.get("equipmentEventTypeCode"), event.get("equipmentEventTypeCode"))
        load = (event.get("emptyIndicatorCode") or "").lower()  # empty / laden
        text = f"{action} ({load})" if load else action
        if action in ("Loaded", "Discharged") and _vessel(event):
            text += f" {'on' if action == 'Loaded' else 'from'} {_vessel(event)['name']}"
        elif mode:
            text += f" by {mode}"
        out["event"] = text
    else:  # TRANSPORT
        action = TRANSPORT_EVENTS.get(event.get("transportEventTypeCode"), event.get("transportEventTypeCode"))
        v = _vessel(event)
        out["event"] = f"{'Vessel ' + v['name'] if v else mode.capitalize() or 'Transport'} {action.lower()}"

    out["location"] = _place(event)
    if facility:
        out["facility_type"] = facility
    if _vessel(event):
        out["vessel"] = _vessel(event)
    return out


def _sorted(events: list[dict]) -> list[dict]:
    return sorted(events, key=_dt)


def _route(events: list[dict]) -> dict:
    """Work out origin, port of loading, transshipments, port of discharge, destination."""
    equip = [e for e in _sorted(events) if e["eventType"] == "EQUIPMENT"]
    loads = [e for e in equip if e.get("equipmentEventTypeCode") == "LOAD"]
    discs = [e for e in equip if e.get("equipmentEventTypeCode") == "DISC"]
    laden = [e for e in equip if e.get("emptyIndicatorCode") == "LADEN"]

    pol = _unloc(loads[0]) if loads else None
    pod = _unloc(discs[-1]) if discs else None
    tranship = []
    for d in discs[:-1]:
        if _unloc(d) not in (pol, pod) and _unloc(d) not in tranship:
            tranship.append(_unloc(d))

    names = {_unloc(e): _place(e) for e in events if _unloc(e)}
    return {
        "origin": _place(laden[0]) if laden else None,
        "port_of_loading": names.get(pol),
        "transshipment_ports": [names[t] for t in tranship],
        "port_of_discharge": names.get(pod),
        "final_laden_location": _place(laden[-1]) if laden else None,
    }


def _milestones(events: list[dict]) -> list[dict]:
    """Pick the key journey milestones out of all the events (actual events only)."""
    ev = [e for e in _sorted(events) if e.get("eventClassifierCode") == "ACT"]
    equip = [e for e in ev if e["eventType"] == "EQUIPMENT"]
    code = lambda e: e.get("equipmentEventTypeCode")  # noqa: E731
    empty = lambda e: e.get("emptyIndicatorCode") == "EMPTY"  # noqa: E731

    loads = [e for e in equip if code(e) == "LOAD"]
    discs = [e for e in equip if code(e) == "DISC"]
    pol = _unloc(loads[0]) if loads else None
    pod = _unloc(discs[-1]) if discs else None
    first_load_t = _dt(loads[0]) if loads else None
    last_disc_t = _dt(discs[-1]) if discs else None

    out = []

    def add(name, e):
        if e:
            out.append({"milestone": name, "time": _fmt_time(e), "location": _place(e), "status": "Done"})

    first = lambda items: items[0] if items else None  # noqa: E731
    last = lambda items: items[-1] if items else None  # noqa: E731

    add("Empty container picked up", first([e for e in equip if code(e) == "GTOT" and empty(e)]))
    add("Gated in full at origin", first([e for e in equip if code(e) == "GTIN" and not empty(e)]))
    if first_load_t:
        before_load = [e for e in equip if _dt(e) < first_load_t]
        add("Arrived at port of loading",
            first([e for e in before_load if code(e) == "GTIN" and _unloc(e) == pol and not empty(e)]))
    add("Loaded on vessel at port of loading", first(loads))
    add("Vessel departed port of loading",
        first([e for e in ev if e.get("transportEventTypeCode") == "DEPA" and _unloc(e) == pol and _vessel(e)]))

    for d in discs[:-1]:  # transshipment = a discharge that isn't the final one
        add(f"Transshipment: discharged at {_place(d)}", d)
        nxt = first([e for e in loads if _dt(e) > _dt(d)])
        add(f"Transshipment: loaded on {(_vessel(nxt) or {}).get('name')}", nxt)

    add("Vessel arrived at port of discharge",
        last([e for e in ev if e.get("transportEventTypeCode") == "ARRI" and _unloc(e) == pod and _vessel(e)]))
    add("Discharged at port of discharge", last(discs))
    if last_disc_t:
        after = [e for e in equip if _dt(e) > last_disc_t]
        add("Gated out full for delivery", first([e for e in after if code(e) == "GTOT" and not empty(e)]))
        add("Empty container returned", first([e for e in after if code(e) == "GTIN" and empty(e)]))
    return out


# ---------------------------------------------------------------------------
# TOOLS
# ---------------------------------------------------------------------------

@mcp.tool()
def list_containers() -> list[str]:
    """List the container numbers this server has tracking data for."""
    return available_containers()


# Country names for the first 2 letters of a UN/LOCODE (fallback: the code itself)
COUNTRIES = {
    "IN": "India", "BR": "Brazil", "MA": "Morocco", "CN": "China", "US": "United States",
    "AE": "United Arab Emirates", "SA": "Saudi Arabia", "OM": "Oman", "QA": "Qatar",
    "LK": "Sri Lanka", "BD": "Bangladesh", "PK": "Pakistan", "SG": "Singapore",
    "MY": "Malaysia", "TH": "Thailand", "VN": "Vietnam", "ID": "Indonesia", "PH": "Philippines",
    "HK": "Hong Kong", "TW": "Taiwan", "KR": "South Korea", "JP": "Japan", "AU": "Australia",
    "NZ": "New Zealand", "GB": "United Kingdom", "DE": "Germany", "NL": "Netherlands",
    "BE": "Belgium", "FR": "France", "ES": "Spain", "PT": "Portugal", "IT": "Italy",
    "GR": "Greece", "TR": "Turkey", "EG": "Egypt", "DJ": "Djibouti", "KE": "Kenya",
    "TZ": "Tanzania", "ZA": "South Africa", "NG": "Nigeria", "GH": "Ghana", "CI": "Ivory Coast",
    "SN": "Senegal", "TG": "Togo", "CA": "Canada", "MX": "Mexico", "PA": "Panama",
    "CO": "Colombia", "PE": "Peru", "CL": "Chile", "AR": "Argentina", "UY": "Uruguay",
    "EC": "Ecuador", "PL": "Poland", "SE": "Sweden", "DK": "Denmark", "NO": "Norway",
    "FI": "Finland", "RU": "Russia", "IL": "Israel", "JO": "Jordan", "IQ": "Iraq",
    "KW": "Kuwait", "BH": "Bahrain", "IR": "Iran",
}


def _country(unloc: str | None) -> str | None:
    return COUNTRIES.get(unloc[:2], unloc[:2]) if unloc else None


def _vessel_calls(events: list[dict], code: str) -> list[dict]:
    """Vessel ARRI or DEPA transport events (actual + estimated), time-ordered."""
    return [e for e in _sorted(events)
            if e["eventType"] == "TRANSPORT" and e.get("transportEventTypeCode") == code and _vessel(e)]


def _best_at(events: list[dict], unloc: str | None) -> dict | None:
    """Of several events for the same call, prefer the actual one, else the latest estimate."""
    same = [e for e in events if _unloc(e) == unloc]
    actual = [e for e in same if e.get("eventClassifierCode") == "ACT"]
    if actual:
        return actual[-1]
    return max(same, key=lambda e: e.get("eventCreatedDateTime", ""), default=None)


def _key_ports(events: list[dict]) -> tuple[str | None, str | None]:
    """(port of loading, port of discharge) as UN/LOCODEs.
    POL = first vessel departure (or first LOAD); POD = last vessel arrival
    including estimates (or last DISC), so it works for shipments still at sea."""
    equip = [e for e in _sorted(events) if e["eventType"] == "EQUIPMENT"]
    deps, arrs = _vessel_calls(events, "DEPA"), _vessel_calls(events, "ARRI")
    loads = [e for e in equip if e.get("equipmentEventTypeCode") == "LOAD"]
    discs = [e for e in equip if e.get("equipmentEventTypeCode") == "DISC"]
    pol = _unloc(deps[0]) if deps else (_unloc(loads[0]) if loads else None)
    pod = _unloc(arrs[-1]) if arrs else (_unloc(discs[-1]) if discs else None)
    # If the container was loaded again at that port after being discharged there,
    # it was a transshipment, not the destination -> destination not known yet.
    if pod:
        disc_here = [e for e in discs if _unloc(e) == pod]
        if disc_here and any(_unloc(e) == pod and _dt(e) > _dt(disc_here[-1]) for e in loads):
            pod = None
    return pol, pod


def _when(e: dict | None, actual_label: str, estimate_label: str) -> dict | None:
    if not e:
        return None
    is_actual = e.get("eventClassifierCode") == "ACT"
    return {
        "time": _fmt_time(e),
        "type": actual_label if is_actual else estimate_label,
        "is_actual": is_actual,
        "location": _place(e),
    }


def _days(a: datetime, b: datetime) -> float:
    return round((b - a).total_seconds() / 86400, 1)


def _stage(events: list[dict], pol: str | None, pod: str | None) -> tuple[str, str]:
    """Simple status (code, description) from the latest ACTUAL event."""
    actual = [e for e in _sorted(events) if e.get("eventClassifierCode") == "ACT" and e["eventType"] != "SHIPMENT"]
    if not actual:
        return "BOOKED", "Booked - no container movement yet"
    last = actual[-1]
    eq, tr = last.get("equipmentEventTypeCode"), last.get("transportEventTypeCode")
    empty = last.get("emptyIndicatorCode") == "EMPTY"
    loc = _unloc(last)
    discharged_at_pod = any(e.get("equipmentEventTypeCode") == "DISC" and _unloc(e) == pod for e in actual)
    ever_loaded = any(e.get("equipmentEventTypeCode") == "LOAD" for e in actual)

    if discharged_at_pod:
        if eq == "GTIN" and empty:
            return "COMPLETED", "Completed - empty container returned to depot"
        if eq == "GTOT" and not empty:
            return "DELIVERED", "Picked up from destination port for delivery"
        if eq == "DISC":
            return "DISCHARGED", "Discharged at destination port"
        return "AT_DESTINATION", f"At destination ({_place(last)})"
    if tr == "ARRI" and _vessel(last) and loc == pod:
        return "ARRIVED", "Vessel arrived at destination port"
    if ever_loaded:
        if eq == "LOAD" or (tr == "DEPA" and _vessel(last)):
            return "ON_VESSEL", f"On vessel {(_vessel(last) or {}).get('name')} - in transit at sea"
        return "TRANSSHIPMENT", f"At transshipment port {_place(last)}"
    at_port_terminal = (last.get("transportCall") or {}).get("facilityTypeCode") == "POTE"
    if loc == pol or (eq == "GTIN" and not empty and at_port_terminal):
        return "AT_PORT_OF_LOADING", f"At port of loading ({_place(last)}), waiting to be loaded"
    if eq == "GTOT" and empty:
        return "EMPTY_PICKED_UP", "Empty container picked up for stuffing"
    if eq == "GTIN" and not empty:
        return "RECEIVED_AT_ORIGIN", "Received full at origin"
    return "TO_PORT_OF_LOADING", "In transit to port of loading"


@mcp.tool()
def get_container_status(container_number: str) -> dict:
    """Shipment summary for a container: status, last location, origin and
    destination country, ETD (departure from port of loading), ETA (arrival at
    port of discharge), transit time, booking and route.
    ETD/ETA are marked 'Actual' (already happened) or 'Estimated'.
    Use this first for 'where is my container?' questions."""
    events = load_events(container_number)
    actual = [e for e in _sorted(events) if e.get("eventClassifierCode") == "ACT" and e["eventType"] != "SHIPMENT"]
    equip = [e for e in _sorted(events) if e["eventType"] == "EQUIPMENT"]
    pol, pod = _key_ports(events)
    status_code, status_text = _stage(events, pol, pod)

    # Origin = first place the container was laden; destination = where it left the POD full
    laden = [e for e in equip if e.get("emptyIndicatorCode") == "LADEN"]
    origin_ev = laden[0] if laden else None
    origin_unloc = _unloc(origin_ev) if origin_ev else pol
    pod_disc = [e for e in equip if e.get("equipmentEventTypeCode") == "DISC" and _unloc(e) == pod]
    delivery = next((e for e in laden if pod_disc and _dt(e) > _dt(pod_disc[-1])
                     and e.get("equipmentEventTypeCode") == "GTOT"), None)
    dest_unloc = _unloc(delivery) if delivery else pod

    etd_ev = _best_at(_vessel_calls(events, "DEPA"), pol)
    eta_ev = _best_at(_vessel_calls(events, "ARRI"), pod)

    # Transit times
    now = datetime.now(timezone.utc)
    transit = {}
    if etd_ev and etd_ev.get("eventClassifierCode") == "ACT":
        end = eta_ev if eta_ev and eta_ev.get("eventClassifierCode") == "ACT" else None
        transit["port_to_port_days"] = _days(_dt(etd_ev), _dt(end)) if end else None
        transit["port_to_port_note"] = ("Actual departure from port of loading to actual arrival at port of discharge"
                                        if end else "Still at sea")
        if not end:
            transit["days_at_sea_so_far"] = _days(_dt(etd_ev), now)
            if eta_ev:
                transit["estimated_port_to_port_days"] = _days(_dt(etd_ev), _dt(eta_ev))
    elif etd_ev and eta_ev:
        transit["estimated_port_to_port_days"] = _days(_dt(etd_ev), _dt(eta_ev))
    gate_in = next((e for e in laden if e.get("equipmentEventTypeCode") == "GTIN"
                    and e.get("eventClassifierCode") == "ACT"), None)
    if gate_in:
        if delivery and delivery.get("eventClassifierCode") == "ACT":
            transit["door_to_door_days"] = _days(_dt(gate_in), _dt(delivery))
            transit["door_to_door_note"] = "Full gate-in at origin to full gate-out at destination port"
        else:
            transit["days_since_origin_gate_in"] = _days(_dt(gate_in), now)

    last = actual[-1] if actual else None
    booking = next((r["documentReferenceValue"] for e in events for r in e.get("documentReferences", [])
                    if r.get("documentReferenceType") == "BKG"), None)

    return {
        "container": _normalise(container_number),
        "status": status_text,
        "status_code": status_code,
        "last_location": {
            "location": _place(last),
            "country": _country(_unloc(last)),
            "event": _describe(last)["event"],
            "time": _fmt_time(last),
        } if last else None,
        "origin": {"location": _place(origin_ev) if origin_ev else None, "country": _country(origin_unloc)},
        "destination": {"location": _place(delivery) if delivery else (_place(eta_ev) if eta_ev else None),
                        "country": _country(dest_unloc)},
        "etd": _when(etd_ev, "Actual departure (ATD)", "Estimated departure (ETD)")
               or "Not available yet (no vessel departure in carrier data)",
        "eta": _when(eta_ev, "Actual arrival (ATA)", "Estimated arrival (ETA)")
               or "Not available yet (destination port not in carrier data)",
        "transit_time": transit,
        "booking_number": booking,
        "container_type": equip[0].get("ISOEquipmentCode") if equip else None,
        "route": _route(events),
    }


@mcp.tool()
def get_milestones(container_number: str) -> list[dict]:
    """Key journey milestones (empty pickup, gate in, loaded, departed,
    transshipments, arrived, discharged, delivered, empty return) with times
    and locations. Only actual (completed) events are included."""
    return _milestones(load_events(container_number))


@mcp.tool()
def get_timeline(container_number: str, include_estimates: bool = True,
                 include_documents: bool = False) -> list[dict]:
    """Every event for the container in time order, translated from DCSA
    codes to plain English. Set include_documents=True to also include
    booking / shipping-instruction / bill-of-lading events."""
    events = load_events(container_number)
    out = []
    for e in _sorted(events):
        if e["eventType"] == "SHIPMENT" and not include_documents:
            continue
        if e.get("eventClassifierCode") != "ACT" and not include_estimates:
            continue
        out.append(_describe(e))
    return out


@mcp.tool()
def get_vessel_legs(container_number: str) -> list[dict]:
    """The ocean legs of the journey: vessel name, IMO, voyage number,
    where the container was loaded and discharged, and when."""
    equip = [e for e in _sorted(load_events(container_number)) if e["eventType"] == "EQUIPMENT"]
    legs = []
    for load in (e for e in equip if e.get("equipmentEventTypeCode") == "LOAD"):
        disc = next((e for e in equip if e.get("equipmentEventTypeCode") == "DISC" and _dt(e) > _dt(load)), None)
        leg = {"vessel": _vessel(load), "loaded_at": _place(load), "loaded_time": _fmt_time(load)}
        if disc:
            leg.update(discharged_at=_place(disc), discharged_time=_fmt_time(disc),
                       days_on_board=round((_dt(disc) - _dt(load)).total_seconds() / 86400, 1))
        else:
            leg["discharged_at"] = "Not yet discharged"
        legs.append(leg)
    return legs


@mcp.tool()
def get_documents(container_number: str) -> list[dict]:
    """Booking and document events for the container's shipment
    (shipping instructions received, bill of lading drafted/issued, etc.)."""
    events = load_events(container_number)
    return [_describe(e) for e in _sorted(events) if e["eventType"] == "SHIPMENT"]


# ---------------------------------------------------------------------------
# RESOURCE + PROMPT
# ---------------------------------------------------------------------------

@mcp.resource("tracking://containers", mime_type="application/json")
def containers_resource() -> str:
    """List of tracked containers."""
    return json.dumps(available_containers())


@mcp.prompt()
def container_report(container_number: str) -> str:
    """Write a short customer-friendly status report for a container."""
    return (
        f"Using the tracking tools, write a short status report for container "
        f"{container_number} that a customer would understand: where it is now, "
        "the route it took (with vessels), key dates, total transit time, and "
        "whether the journey is complete. Use a small table for milestones."
    )


# ---------------------------------------------------------------------------
# HTTP hosting + optional API key (same as before)
# ---------------------------------------------------------------------------

class ApiKeyMiddleware:
    """If MCP_API_KEY is set, every HTTP request must send
    `Authorization: Bearer <MCP_API_KEY>`, otherwise it gets 401."""

    def __init__(self, app, api_key: str):
        self.app = app
        self.expected = f"Bearer {api_key}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            if not hmac.compare_digest(headers.get(b"authorization", b""), self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error": "unauthorized"}'})
                return
        await self.app(scope, receive, send)


def run_http() -> None:
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, host=host,
    )
    api_key = os.getenv("MCP_API_KEY")
    if api_key:
        app = ApiKeyMiddleware(app, api_key)
        log.info("API key required (Authorization: Bearer ...)")
    else:
        log.warning("No MCP_API_KEY set: anyone with the URL can use this server")
    log.info("Serving MCP over HTTP at http://%s:%s/mcp", host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    #   python tracking_server.py                      -> stdio (local Claude Desktop/Code)
    #   MCP_TRANSPORT=http python tracking_server.py   -> HTTP at http://HOST:PORT/mcp
    if os.getenv("MCP_TRANSPORT", "stdio").lower() in ("http", "streamable-http"):
        run_http()
    else:
        mcp.run()
