"""Diff two harness CSVs row by row on (seed, arm). Read-only."""
import argparse, csv, sys
from collections import Counter


def load(path):
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SystemExit(f"{path}: no data rows")
    return {(r["seed"], r["arm"]): r for r in rows}, list(rows[0])


def equal(a, b, tol):
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    return abs(fa - fb) <= tol


def main():
    p = argparse.ArgumentParser()
    p.add_argument("reference")
    p.add_argument("candidate")
    p.add_argument("--exclude", action="append", default=["max_solve_ms"],
                   help="column excluded on every row; repeatable")
    p.add_argument("--exclude-for", action="append", default=[],
                   metavar="SEED:COLUMN",
                   help="column excluded on one seed only; repeatable")
    p.add_argument("--seeds", default="", help="space-separated; default all")
    p.add_argument("--arms", default="", help="space-separated; default all")
    p.add_argument("--tol", type=float, default=0.0)
    args = p.parse_args()

    ref, ref_cols = load(args.reference)
    cand, cand_cols = load(args.candidate)

    if ref_cols != cand_cols:
        print(f"HEADER MISMATCH\n  {args.reference}: {len(ref_cols)} columns"
              f"\n  {args.candidate}: {len(cand_cols)} columns")
        only_r = [c for c in ref_cols if c not in cand_cols]
        only_c = [c for c in cand_cols if c not in ref_cols]
        if only_r:
            print(f"  only in reference: {', '.join(only_r)}")
        if only_c:
            print(f"  only in candidate: {', '.join(only_c)}")
        return 1

    per_seed = {}
    for spec in args.exclude_for:
        seed, _, col = spec.partition(":")
        if not col:
            raise SystemExit(f"--exclude-for {spec!r}: want SEED:COLUMN")
        per_seed.setdefault(seed, set()).add(col)

    want_seeds = set(args.seeds.split()) or None
    want_arms = set(a.upper() for a in args.arms.split()) or None

    def wanted(k):
        return ((want_seeds is None or k[0] in want_seeds)
                and (want_arms is None or k[1] in want_arms))

    def rows(pool):
        return sorted((k for k in pool if wanted(k)),
                      key=lambda k: (int(k[0]), k[1]))

    keys = rows(set(ref) & set(cand))
    missing = rows(set(ref) - set(cand))
    extra = rows(set(cand) - set(ref))

    if not keys:
        print("NO COMPARABLE ROWS after filtering")
        return 1

    hits = Counter()
    bad_rows = 0
    bad_cells = 0
    print(f"reference {args.reference}")
    print(f"candidate {args.candidate}")
    print(f"comparing {len(keys)} rows, excluding {', '.join(args.exclude)}"
          + (f" (+ per-seed: {args.exclude_for})" if args.exclude_for else ""))
    print()
    for key in keys:
        skip = set(args.exclude) | per_seed.get(key[0], set())
        cols = [c for c in ref_cols if c not in skip]
        diff = [c for c in cols if not equal(ref[key][c], cand[key][c], args.tol)]
        if diff:
            bad_rows += 1
            bad_cells += len(diff)
            for c in diff:
                hits[c] += 1
            print(f"seed {key[0]:>2} arm {key[1]}  {len(diff)} of {len(cols)}")
            for c in diff:
                print(f"    {c:<40} ref={ref[key][c]:>14} "
                      f"cand={cand[key][c]:>14}")
    for key in missing:
        print(f"seed {key[0]:>2} arm {key[1]}  MISSING from candidate")
    for key in extra:
        print(f"seed {key[0]:>2} arm {key[1]}  EXTRA in candidate")

    print()
    print(f"rows compared      {len(keys)}")
    print(f"rows differing     {bad_rows}")
    print(f"cells differing    {bad_cells}")
    if hits:
        print("columns by frequency:")
        for c, n in hits.most_common():
            print(f"  {n:>3}/{len(keys):<3} {c}")
    if bad_rows or missing or extra:
        print(f"\nBASELINE-DIFF-FAIL: {bad_rows} rows, {bad_cells} cells, "
              f"{len(missing)} missing, {len(extra)} extra")
        return 1
    print("\nBASELINE-DIFF-PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())