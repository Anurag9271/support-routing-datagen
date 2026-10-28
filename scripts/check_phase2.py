import sys, re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd

raw = Path(sys.argv[1] if len(sys.argv) > 1 else "data/raw")
stg = Path(sys.argv[2] if len(sys.argv) > 2 else "data/staging")
gt = Path(sys.argv[3] if len(sys.argv) > 3 else "data/ground_truth")
rd = lambda p: pd.read_csv(p, keep_default_na=False)
T, D, SW = rd(stg / "ticket_draft.csv"), rd(gt / "ground_truth_decisions.csv"), rd(gt / "scenario_windows.csv")
C, I = rd(raw / "customer.csv"), rd(raw / "intent.csv")
for c in ("created_at",):
    T[c] = pd.to_datetime(T[c], utc=True)
N = len(T)

def check(name, ok):
    print(("PASS  " if ok else "FAIL  ") + name)

def show(title, x):
    print(f"\n== {title} ==\n{x}")

# ---------- basic integrity ----------
check("ticket count == 30000", N == 30000)
check("ids gapless TKT-2026-000001..N",
      list(T.ticket_id) == [f"TKT-2026-{i:06d}" for i in range(1, N + 1)])
check("sorted by created_at", T.created_at.is_monotonic_increasing)
check("inside arrival window",
      (T.created_at >= "2026-01-05T00:00:00Z").all() and (T.created_at < "2026-04-05T00:00:00Z").all())
check("decisions: one row per ticket", set(D.ticket_id) == set(T.ticket_id) and len(D) == N)
check("every customer_id exists", T.customer_id.isin(C.customer_id).all())
M = T.merge(C[["customer_id", "customer_type", "segment", "preferred_language"]], on="customer_id") \
     .merge(I[["intent_id", "default_priority", "is_safety_related", "allows_prospect"]], on="intent_id")

# ---------- business rules, recomputed independently ----------
pro = M[M.customer_type == "PROSPECT"]
check("prospects only hold allows_prospect intents", (pro.allows_prospect == "true").all())
saf = M[M.is_safety_related == "true"]
check("all safety intents are P1/SAFETY_OVERRIDE (or INCIDENT_OVERRIDE)",
      (saf.priority_level == "P1").all() and saf.priority_reason_code.isin(["SAFETY_OVERRIDE", "INCIDENT_OVERRIDE"]).all())
inc = M[M.incident_id != ""]
check("incident tickets: INT-005, P1, CUSTOMER type, flagged",
      (inc.intent_id == "INT-005").all() and (inc.priority_level == "P1").all()
      and (inc.customer_type == "CUSTOMER").all() and (inc.is_critical_incident == "true").all())
check("is_critical_incident iff incident_id", ((M.is_critical_incident == "true") == (M.incident_id != "")).all())

order = ["P4", "P3", "P2"]
def expect(r):
    if r.incident_id != "":
        return "P1", "INCIDENT_OVERRIDE"
    if r.is_safety_related == "true":
        return "P1", "SAFETY_OVERRIDE"
    p = r.default_priority
    if r.segment == "ENTERPRISE" and p in ("P3", "P4"):
        return ("P3" if p == "P4" else "P2"), "SEGMENT_UPGRADE"
    return p, "INTENT_DEFAULT"
exp = M.apply(expect, axis=1, result_type="expand")
check("priority + reason match section 5 (independent recompute)",
      ((exp[0] == M.priority_level) & (exp[1] == M.priority_reason_code)).all())
check("queue rule: P1 -> QUE-INCIDENT only",
      ((M.priority_level == "P1") == (M.queue_id == "QUE-INCIDENT")).all())
d = M.created_at.dt.strftime("%Y-%m-%d")
def rule(r, day):
    return {"P1": "PRL-001", "P3": "PRL-004", "P4": "PRL-005"}.get(r) or ("PRL-002" if day < "2026-02-16" else "PRL-003")
check("priority_rule_id (P2 switches 2026-02-16)",
      (M.apply(lambda r: rule(r.priority_level, d[r.name]), axis=1) == M.priority_rule_id).all())
check("language = preferred, except non-en customers using en",
      ((M.language_code == M.preferred_language) | ((M.language_code == "en") & (M.preferred_language != "en"))).all())
show("share of tickets where language != preferred (expect ~2%)", round((M.language_code != M.preferred_language).mean(), 4))

# ---------- calendars: independent day-walk implementation ----------
CAL = {"CAL-EMAIL": {d_: (480, 1200) for d_ in range(5)},
       "CAL-CHAT": {d_: (360, 1320) for d_ in range(7)},
       "CAL-PHONE": {d_: (540, 1080) for d_ in range(5)},
       "CAL-247": {d_: (0, 1440) for d_ in range(7)}}
CH = {"EMAIL": "CAL-EMAIL", "CHAT": "CAL-CHAT", "PHONE": "CAL-PHONE"}
def is_open(cal, t):
    w = CAL[cal].get(t.weekday())
    return bool(w) and w[0] * 60 <= t.hour * 3600 + t.minute * 60 + t.second < w[1] * 60
def add_bm(cal, start, minutes):
    rem, t = minutes * 60, start
    for _ in range(60):
        day = t.replace(hour=0, minute=0, second=0, microsecond=0)
        w = CAL[cal].get(day.weekday())
        if w:
            op, cl = day + timedelta(minutes=w[0]), day + timedelta(minutes=w[1])
            if t < cl:
                cur = max(t, op)
                avail = (cl - cur).total_seconds()
                if rem <= avail:
                    return cur + timedelta(seconds=rem)
                rem -= avail
        t = day + timedelta(days=1)
    raise RuntimeError("no result")
def first_open(cal, t):
    return add_bm(cal, t, 0) if is_open(cal, t) else None

ah = M.apply(lambda r: not is_open(CH[r.channel_id], r.created_at.to_pydatetime()), axis=1)
check("is_after_hours_arrival matches my calendars", (ah == (M.is_critical_incident.eq("true") & False) | (ah == (M.is_after_hours_arrival == "true"))).all()
      if False else (ah == (M.is_after_hours_arrival == "true")).all())
show("after-hours share overall (expect 12-25%)", round(ah.mean(), 3))
show("after-hours share by channel", ah.groupby(M.channel_id).mean().round(3))

MIN = {"P1": (15, 240, 60), "P2a": (60, 480, 240), "P2b": (45, 480, 240), "P3": (240, 1440, 720), "P4": (480, 2880, 1440)}
bad = {"fr": 0, "res": 0, "esc": 0, "cal": 0}
Dm = D.set_index("ticket_id")
for r in M.itertuples():
    p = r.priority_level
    key = p if p != "P2" else ("P2a" if r.created_at < pd.Timestamp("2026-02-16", tz="UTC") else "P2b")
    cal = "CAL-247" if p == "P1" else CH[r.channel_id]
    t0 = r.created_at.to_pydatetime()
    # clock start: first open instant >= created_at
    t = t0
    if not is_open(cal, t):
        for k in range(10):
            day = (t + timedelta(days=k if k else 0)).replace(hour=0, minute=0, second=0)
            w = CAL[cal].get(day.weekday())
            if w and day + timedelta(minutes=w[0]) >= t0:
                t = day + timedelta(minutes=w[0]); break
    row = Dm.loc[r.ticket_id]
    fr, rs, es = MIN[key]
    iso = lambda x: pd.Timestamp(x).to_pydatetime()
    if row.expected_calendar_code != cal: bad["cal"] += 1
    if iso(row.expected_first_response_due) != add_bm(cal, t, fr): bad["fr"] += 1
    if iso(row.expected_resolution_due) != add_bm(cal, t, rs): bad["res"] += 1
    if iso(row.expected_escalation_due_at) != add_bm(cal, t, es): bad["esc"] += 1
check(f"SLA due times match my independent calendar math (mismatches: {bad})", sum(bad.values()) == 0)
p1 = M[M.priority_level == "P1"].ticket_id
x = Dm.loc[p1]
check("P1: due = created + 15/240/60 wall minutes (24x7)",
      ((pd.to_datetime(x.expected_first_response_due, utc=True) - M.set_index("ticket_id").loc[p1].created_at) == pd.Timedelta(minutes=15)).all())

# ---------- distributions ----------
show("priority share (P1 expect ~6%)", M.priority_level.value_counts(normalize=True).round(3).sort_index())
show("priority_reason_code", M.priority_reason_code.value_counts())
show("channel share (expect ~EMAIL 47, CHAT 39, PHONE 14)", M.channel_id.value_counts(normalize=True).round(3))
show("channel by segment", pd.crosstab(M.segment, M.channel_id, normalize="index").round(2))
sh = (M.intent_id.value_counts(normalize=True) * 100).round(1).sort_index()
wt = I.set_index("intent_id").volume_weight_pct.astype(float)
ex = wt.copy()
for i_ in ex.index:
    allows = I.set_index("intent_id").allows_prospect[i_] == "true"
    ex[i_] = 100 * 0.96 * (0.9 * wt[i_] / 100 + (0.1 * 0.5 if allows else 0))
ex["INT-005"] += 4
show("intent share % (actual vs rough expected; spike skew shifts BILLING/TECH slightly)",
     pd.DataFrame({"actual": sh, "expected": ex.round(1)}))
tpc = T.customer_id.value_counts()
show("tickets per customer (mean ~2.5, heavy tail)", tpc.describe().round(2))
show("customers with 0 tickets (share)", round(1 - len(tpc) / len(C), 3))
show("dow share (Mon=0)", M.created_at.dt.dayofweek.value_counts(normalize=True).sort_index().round(3))
show("hour-of-day share (UTC)", M.created_at.dt.hour.value_counts(normalize=True).sort_index().round(3).to_dict())

# ---------- scenario windows ----------
show("scenario_windows.csv", SW)
SW["start"], SW["end"] = pd.to_datetime(SW.start, utc=True), pd.to_datetime(SW.end, utc=True)
base = M[M.incident_id == ""]
for w in SW.itertuples():
    k = str(w.kind).upper()
    if "INC" in k:
        n = (M.incident_id == w.id).sum()
        inside = M[(M.incident_id == w.id) & (M.created_at >= w.start) & (M.created_at < w.end)]
        check(f"{w.id}: {n} tickets, all inside its window", len(inside) == n)
    else:
        n = ((base.created_at >= w.start) & (base.created_at < w.end)).sum()
        day = w.start.normalize()
        others = []
        for dd in pd.date_range("2026-01-05", "2026-04-04", tz="UTC"):
            if dd.dayofweek < 5 and dd != day:
                a, b = dd + (w.start - day), dd + (w.end - day)
                others.append(((base.created_at >= a) & (base.created_at < b)).sum())
        avg = sum(others) / len(others)
        show(f"spike {w.id}", f"{n} tickets vs {avg:.1f} typical -> ratio {n / avg:.2f} (expect ~3.5-4.3)")

# ---------- leakage guard ----------
hits = [str(p) for p in Path("generator").glob("*.py") if "read_csv" in p.read_text() and "ground_truth" in p.read_text()]
show("generator modules mentioning both read_csv and ground_truth (review by hand; expect none)", hits)