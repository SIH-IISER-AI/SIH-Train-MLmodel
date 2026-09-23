import csv
import sys
import statistics as st

if len(sys.argv) < 3:
    print("Usage: python extract_premier_metrics.py <global_csv_path> <enum_csv_path>")
    sys.exit(1)

global_path = sys.argv[1]
enum_path = sys.argv[2]

def load_arm(path, arm_label):
    try:
        return {r['seed']: r for r in csv.DictReader(open(path)) if r['arm'] == arm_label}
    except FileNotFoundError:
        print(f"Missing {path}.")
        sys.exit(1)

global_runs = load_arm(global_path, 'A')
enum_runs = load_arm(enum_path, 'A')
seeds = [s for s in global_runs.keys() if s in enum_runs.keys()]

if not seeds:
    print("No matching seeds found between the two datasets.")
    sys.exit(1)

print(f"Comparing Global: {global_path} | Enum: {enum_path}")
print("Seed | Premier Delay (Enum) | Premier Delay (Global) | Improvement (s)")
print("-" * 80)

improvements = []
for s in sorted(seeds, key=int):
    enum_delay = float(enum_runs[s]['premier_delay_s'])
    global_delay = float(global_runs[s]['premier_delay_s'])
    diff = enum_delay - global_delay
    improvements.append(diff)
    print(f"{s:>4} | {enum_delay:>20.1f} | {global_delay:>22.1f} | {diff:>15.1f}")

print("-" * 80)
print(f"Average Premier Delay Reduction: {st.mean(improvements):.1f}s")
print(f"Maximized Output (Best Run): {max(improvements):.1f}s")