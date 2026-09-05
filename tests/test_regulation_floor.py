"""Gate on the day-14 regulation floor.

emit_directives degrades an impossible stand to a REGULATE and passes the
FULL priced slack to regulated_speed_kmh. Slack exceeding absorbable_delay_s
is exactly the condition under which the model set stopped = 1, so the
emitter is handed a wait the physics module has already ruled out. Without
saturation the algebra returns whatever falls out: seed 3 produced 6.3 km/h
against a booked 95.

Assertion 2 is the gate. 1 and 3 are canaries. Assertion 4 proves the gate
discriminates, by running the same inputs with the saturation disabled.
"""
import sys
sys.path.insert(0, "shared")
import railsim.kinematics as kin

F = kin.MIN_REGULATION_FRACTION
CASES = [
    ("freight  45 km/h, 8 km approach",  8000.0, kin.kmh_to_ms(45.0)),
    ("express  95 km/h, 15 km approach", 15000.0, kin.kmh_to_ms(95.0)),
    ("premier 110 km/h, 30 km approach", 30000.0, kin.kmh_to_ms(110.0)),
]
OBSERVED = ("seed 3, 12626 at BLK-115D", 15000.0, kin.kmh_to_ms(95.0), 7959.0)

fail = []
print(f"MIN_REGULATION_FRACTION = {F}\n")

for label, d, v in CASES:
    a = kin.absorbable_delay_s(d, v, F)
    floor = kin.ms_to_kmh(v) * F
    print(f"  {label}: absorbable={a:.1f}s floor={floor:.3f} km/h")

    got = kin.regulated_speed_kmh(d, v, a)
    if abs(got - floor) > 1e-9:
        fail.append(f"[1 boundary] {label}: {got:.6f} != {floor:.6f}")

    for m in (1.5, 2.0, 10.0, 100.0):
        got = kin.regulated_speed_kmh(d, v, m * a)
        if got < floor - 1e-9:
            fail.append(f"[2 saturation] {label}: wait={m}x absorbable "
                        f"-> {got:.3f} km/h, below floor {floor:.3f}")

    for q in (0.25, 0.5, 0.9, 0.999):
        got = kin.regulated_speed_kmh(d, v, q * a)
        if got <= floor:
            fail.append(f"[3 early bind] {label}: wait={q}x absorbable "
                        f"-> {got:.3f} km/h, floor bound inside the "
                        f"regulating range")

label, d, v, wait = OBSERVED
floor = kin.ms_to_kmh(v) * F
got = kin.regulated_speed_kmh(d, v, wait)
unsat = kin.regulated_speed_kmh(d, v, wait, min_fraction=0.0)
print(f"\n  {label}: wait={wait:.0f}s -> {got:.3f} km/h "
      f"(floor {floor:.3f}, booked {kin.ms_to_kmh(v):.1f})")
print(f"  same input, saturation disabled -> {unsat:.3f} km/h "
      f"(the day-13 behaviour)")
if got < floor - 1e-9:
    fail.append(f"[2 saturation, observed] {label}: {got:.3f} km/h "
                f"below floor {floor:.3f}")
if unsat >= floor:
    fail.append(f"[4 discrimination] saturation disabled returned "
                f"{unsat:.3f} km/h, at or above the floor -- assertion 2 "
                f"cannot distinguish the two configurations")

print()
if fail:
    print("FLOOR-FAIL: " + "; ".join(fail))
    sys.exit(1)
print("FLOOR-PASS")