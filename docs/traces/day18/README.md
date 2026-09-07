Hold-mutation traces, `SIM_TRACE_HOLDS`, arm A, 1080 ticks,
PYTHONHASHSEED=0 GLOBAL_TIER_BUDGET_S=1000, git 75fb342.

holds-sN-A.jsonl     reason == "retargeted"
holds-sN-A-v2.jsonl  reason == "retargeted from <station>"

Match with startswith("retargeted"). Do not glob both generations
into one analysis.
