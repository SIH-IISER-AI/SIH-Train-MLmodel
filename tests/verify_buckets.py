import re
import json
import glob
import sys

log_path = "docs/traces/day18/per_train_delays.txt"
delays = {}  # (seed, arm, train_id) -> delay_seconds
current_seed = None
current_arm = None

# Step 1: Parse the stdout log for per-train delays
try:
    with open(log_path, "r") as f:
        for line in f:
            # Capture the seed and arm from the harness header
            m_header = re.search(r"harness seed=(\d+) arm=([AB])", line)
            if m_header:
                current_seed = int(m_header.group(1))
                current_arm = m_header.group(2)
                continue

            # Capture the train ID and delay seconds from the HARNESS_PER_TRAIN output
            m_train = re.search(r"^\s+(\d+)\s+\w+\s+class=\d+\s+delay=(\d+)s", line)
            if m_train and current_seed and current_arm:
                train_id = m_train.group(1)
                delay = int(m_train.group(2))
                delays[(current_seed, current_arm, train_id)] = delay
except FileNotFoundError:
    print(f"Log file {log_path} not found.")
    sys.exit(1)

# Step 2: Parse the JSONL traces for blocked releases in Arm A
blocked_trains = set()
trace_files = glob.glob("docs/traces/day18/holds-s*-A*.jsonl")
for filepath in trace_files:
    m_file = re.search(r"holds-s(\d+)-A", filepath)
    if not m_file:
        continue
    seed = int(m_file.group(1))
    
    with open(filepath, "r") as f:
        for line in f:
            try:
                event = json.loads(line)
                if event.get("writer") == "release_blocked":
                    blocked_trains.add((seed, str(event.get("train"))))
            except json.JSONDecodeError:
                continue

# Step 3: Pair data and sort into buckets
buckets = {
    "improved": [],
    "tied": [],
    "worse_not_stranded": [],
    "stranded": []
}

pairs = set((s, t) for s, a, t in delays.keys())

for seed, train_id in pairs:
    delay_a = delays.get((seed, 'A', train_id))
    delay_b = delays.get((seed, 'B', train_id))
    
    if delay_a is None or delay_b is None:
        continue
        
    delta = delay_a - delay_b
    
    if delta < 0:
        buckets["improved"].append((seed, train_id, delta))
    elif delta == 0:
        buckets["tied"].append((seed, train_id, delta))
    else:
        # delay_a > delay_b
        if (seed, train_id) in blocked_trains:
            buckets["stranded"].append((seed, train_id, delta))
        else:
            buckets["worse_not_stranded"].append((seed, train_id, delta))

# Step 4: Output the definitive count
print(f"Total paired train-runs: {len(pairs)}")
print("-" * 40)
print(f"Improved (Delta < 0)       : {len(buckets['improved'])}")
print(f"Tied (Delta == 0)          : {len(buckets['tied'])}")
print(f"Stranded (Worse + Blocked) : {len(buckets['stranded'])}")
print(f"Worse (Not Blocked)        : {len(buckets['worse_not_stranded'])}")
print("-" * 40)

if buckets["worse_not_stranded"]:
    print("THE MISSING MIDDLE BUCKET:")
    for seed, tid, delta in sorted(buckets["worse_not_stranded"], key=lambda x: (x[0], x[1])):
        print(f"  Seed {seed:>2} | Train {tid:>5} | Delta: +{delta}s")
else:
    print("The middle bucket is empty. Every worsened train was physically stranded by main-line contention.")