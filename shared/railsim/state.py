"""Redis state contract: static-state keys, epoch derivation, commit/hydrate."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, Optional, Tuple

KEY_EPOCH = "rail:epoch"
KEY_TOPOLOGY = "rail:network_topology"
KEY_FLEET = "rail:fleet"

CONTROL_STREAM = "control_stream"

HYDRATION_POLL_SECONDS = 1.0


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_epoch(network: dict, scenario: dict) -> str:
    digest = hashlib.sha1(_canonical(network) + b"|" + _canonical(scenario)).hexdigest()[:10]
    return f"{int(time.time())}-{digest}"


def fleet_entry(raw: dict) -> dict:
    entry = {
        "train_id": str(raw["train_id"]),
        "train_name": raw["train_name"],
        "train_type": raw["train_type"],
        "priority_weight": float(raw["priority_weight"]),
        "max_speed_kmh": float(raw["max_speed_kmh"]),
        "scheduled_speed_kmh": float(raw["scheduled_speed_kmh"]),
        "route": list(raw["route"]),
    }
    if "train_length_m" in raw:
        entry["train_length_m"] = float(raw["train_length_m"])
    return entry


def commit_static_state(client, network: dict, scenario: dict, epoch: str) -> None:
    envelope = json.dumps({"epoch": epoch, "network": network}, separators=(",", ":"))
    fleet = {
        str(t["train_id"]): json.dumps(fleet_entry(t), separators=(",", ":"))
        for t in scenario["trains"]
    }
    pipe = client.pipeline(transaction=True)
    pipe.delete(KEY_FLEET)
    pipe.set(KEY_TOPOLOGY, envelope)
    pipe.hset(KEY_FLEET, mapping=fleet)
    pipe.set(KEY_EPOCH, epoch)
    pipe.execute()


def read_static_state(client) -> Optional[Tuple[str, dict, Dict[str, dict]]]:
    raw = client.get(KEY_TOPOLOGY)
    if not raw:
        return None
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        return None

    epoch = envelope.get("epoch")
    network = envelope.get("network")
    if not epoch or not network:
        return None

    fleet_raw = client.hgetall(KEY_FLEET)
    if not fleet_raw:
        return None

    if client.get(KEY_EPOCH) != epoch:
        return None

    try:
        fleet = {tid: json.loads(blob) for tid, blob in fleet_raw.items()}
    except json.JSONDecodeError:
        return None

    return epoch, network, fleet


def current_epoch(client) -> Optional[str]:
    return client.get(KEY_EPOCH)