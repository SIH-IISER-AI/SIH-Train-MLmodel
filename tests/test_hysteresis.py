import sys, types

sys.path.insert(0, "shared")
sys.path.insert(0, "ai-engine")

try:
    import redis  # noqa: F401
except ImportError:
    _stub = types.ModuleType("redis")
    class _RedisError(Exception):
        pass
    _stub.RedisError = _RedisError
    _stub.Redis = object
    sys.modules["redis"] = _stub

from detector import SEVERITY_BANDS
from main import DEESCALATION_DWELL_S, Engine

CID = "CONF-TEST"
engine = Engine(None)
failures = []


def band(contested_at):
    for threshold, label in SEVERITY_BANDS:
        if contested_at <= threshold:
            return label
    return "LOW"


def observe(contested_at, now):
    """One evaluate() pass: stabilise, then commit as evaluate() does."""
    severity = engine._stable_severity(CID, band(contested_at), contested_at, now)
    engine.last_severity[CID] = (severity, "OPEN")
    return severity


def check(what, got, want):
    print(f"{'ok  ' if got == want else 'FAIL'} {what}: {got} (want {want})")
    if got != want:
        failures.append(what)


check("first sighting publishes raw", observe(400, 0.0), "HIGH")
check("flap across the band is absorbed by the margin", observe(430, 10.0), "HIGH")
check("flap back publishes nothing new", observe(405, 20.0), "HIGH")
check("real de-escalation starts the dwell", observe(500, 30.0), "HIGH")
check("still inside the dwell", observe(505, 30.0 + DEESCALATION_DWELL_S - 1), "HIGH")
check("de-escalates past the dwell", observe(505, 30.0 + DEESCALATION_DWELL_S + 1), "MEDIUM")
check("escalation is never delayed", observe(120, 40.0 + DEESCALATION_DWELL_S), "CRITICAL")

print("\nFAIL: " + "; ".join(failures) if failures else "\nPASS")