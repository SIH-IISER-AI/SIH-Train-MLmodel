# tests/verify_day13.py  — throwaway, do not commit
import csv, statistics as st, math

def arm(path, a):
    return {r['seed']: r for r in csv.DictReader(open(path)) if r['arm'] == a}

g = arm('docs/baselines/ab-global.csv', 'A')
e = arm('docs/baselines/ab-enumerate.csv', 'A')
b = arm('docs/baselines/ab-enumerate.csv', 'B')
seeds = [str(i) for i in range(1, 16)]
TH = 'cleared_through_SEC-PWL-KSV_per_sim_hour'

def paired(col):
    dg = [float(g[s][col]) for s in seeds]
    de = [float(e[s][col]) for s in seeds]
    db = [float(b[s][col]) for s in seeds]
    d = [x - y for x, y in zip(dg, de)]
    m, sd = st.mean(d), st.stdev(d)
    se = sd / math.sqrt(len(d))
    print(f"{col}\n  global {st.mean(dg):.3f} | enum {st.mean(de):.3f} | armB {st.mean(db):.3f}")
    print(f"  delta {m:+.3f}  t {m/se:+.3f}  95% CI [{m-2.145*se:.3f}, {m+2.145*se:.3f}]")
    print(f"  better/worse/tied {sum(x>0 for x in d)}/{sum(x<0 for x in d)}/{sum(x==0 for x in d)}\n")
    return d

thd = paired(TH); fld = paired('total_fleet_delay_s'); paired('premier_delay_s')

def corr(x, y):
    mx, my = st.mean(x), st.mean(y)
    return (sum((a-mx)*(c-my) for a, c in zip(x, y))
            / math.sqrt(sum((a-mx)**2 for a in x) * sum((c-my)**2 for c in y)))

print("corr(throughput d, fleet d) =", round(corr(thd, fld), 4))
print("global >= armB 1.333 on", sum(1 for s in seeds if float(g[s][TH]) >= 1.333), "of 15")