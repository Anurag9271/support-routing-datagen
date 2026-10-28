"""Independent Phase 3a verification. Reads CSVs only; shares no code with generator/."""
import sys, bisect
from collections import defaultdict
from pathlib import Path
import numpy as np
import pandas as pd

CUT_STR = sys.argv[1] if len(sys.argv) > 1 else "2026-04-05T00:00:00Z"
raw, stg = Path("data/raw"), Path("data/staging")
rd = lambda p: pd.read_csv(p, keep_default_na=False, dtype=str)
EPOCH = pd.Timestamp("1970-01-01", tz="UTC")
def ep(s): return ((pd.to_datetime(s, utc=True) - EPOCH) // pd.Timedelta(seconds=1)).astype("int64")
def epday(s): return int((pd.Timestamp(s, tz="UTC") - EPOCH).days)
def check(name, ok, extra=""): print(("PASS  " if ok else "FAIL  ") + name + (f"  [{extra}]" if extra else ""))
def warn(name, extra): print(f"WARN  {name}  [{extra}]")
def show(title, x): print(f"\n== {title} ==\n{x}")

T, S, A, O = (rd(stg / f) for f in ("ticket_draft.csv", "ticket_status_event_draft.csv",
                                    "assignment_event_draft.csv", "ticket_outcome_draft.csv"))
AG, AS, AL, SH, QM, Q, I = (rd(raw / f) for f in ("agent.csv", "agent_skill.csv", "agent_language.csv",
                                                  "agent_shift.csv", "queue_member.csv", "queue.csv", "intent.csv"))
CUT = int(ep(pd.Series([CUT_STR]))[0])
START = epday("2026-01-05") * 86400
S["t"], A["t"], T["t"] = ep(S.event_at), ep(A.event_at), ep(T.created_at)
S["seq_no"] = S.seq_no.astype(int); A["seq_no"] = A.seq_no.astype(int)
A["load_before"] = A.agent_load_before.astype(int); A["load_after"] = A.agent_load_after.astype(int)
A["limit"] = A.concurrency_limit.astype(int); A["wait"] = A.wait_minutes.astype(float)
S = S.sort_values(["ticket_id", "seq_no"]).reset_index(drop=True)
A = A.sort_values(["ticket_id", "seq_no"]).reset_index(drop=True)

tk = T.set_index("ticket_id")
prio, lang, tq, intent = (tk[c].to_dict() for c in ("priority_level", "language_code", "queue_id", "intent_id"))
skill_of = I.set_index("intent_id").required_skill_id.to_dict()
minTier = Q.set_index("queue_id").min_tier.to_dict()
TIER = {"T1": 1, "T2": 2, "T3": 3}; PROF = {"BASIC": 1, "PROFICIENT": 2, "EXPERT": 3}
TH = {"P1": 5, "P2": 15, "P3": 30, "P4": 60}
ag = AG.set_index("agent_id")
tier = ag.tier.map(TIER).to_dict(); limit = ag.concurrency_limit.astype(int).to_dict(); astatus = ag.status.to_dict()
skills, qmem, speaks, shifts = defaultdict(list), defaultdict(list), set(), defaultdict(dict)
for r in AS.itertuples():
    skills[(r.agent_id, r.skill_id)].append((epday(r.effective_from), epday(r.effective_to) if r.effective_to else 10**9, PROF[r.proficiency]))
for r in QM.itertuples():
    qmem[(r.agent_id, r.queue_id)].append((epday(r.effective_from), epday(r.effective_to) if r.effective_to else 10**9))
for r in AL.itertuples():
    if r.fluency in ("FLUENT", "NATIVE"): speaks.add((r.agent_id, r.language_code))
for r in SH.itertuples():
    h, m = map(int, r.start_time.split(":")); eh, em = map(int, r.end_time.split(":"))
    st, en = h * 3600 + m * 60, eh * 3600 + em * 60
    shifts[r.agent_id][int(r.day_of_week)] = (st, (en - st) % 86400 or 86400)

def shift_window(a, t):
    ds = t - t % 86400
    for off in (0, 1):
        d0 = ds - off * 86400
        w = shifts[a].get(((d0 // 86400) + 3) % 7 + 1)
        if w:
            s = d0 + w[0]
            if s >= START and s <= t < s + w[1]:
                return s, s + w[1]
    return None

def static_ok(a, k, t, waived):
    if astatus[a] != "ACTIVE": return "H1"
    if shift_window(a, t) is None: return "H2"
    day = t // 86400
    best = max((p for f, to, p in skills.get((a, skill_of[intent[k]]), []) if f <= day <= to), default=0)
    if best == 0: return "H3"
    if prio[k] in ("P1", "P2") and best < 2: return "H3b"
    if not waived and (a, lang[k]) not in speaks: return "H4"
    q = tq[k]
    if not any(f <= day <= to for f, to in qmem.get((a, q), [])): return "H6-membership"
    if tier[a] < TIER[minTier[q]]: return "H6-tier"
    return None

# queued spells per ticket
spells, openq = defaultdict(list), {}
for r in S.itertuples():
    if r.to_status == "QUEUED": openq[r.ticket_id] = r.t
    elif r.to_status == "ASSIGNED" and r.from_status == "QUEUED": spells[r.ticket_id].append((openq.pop(r.ticket_id), r.t))
for k, s in openq.items(): spells[k].append((s, CUT))
def queued_before(k, t): return sum(max(0, min(e, t) - s) for s, e in spells[k] if s < t)
def waiver_active(k, t):
    return any(min(e, t) - s >= TH[prio[k]] * 60 for s, e in spells[k] if s < t)
def waiver_active(k, t):
   return any(min(e, t) - s >= TH[prio[k]] * 60 for s, e in spells[k] if s < t)

# ownership intervals (agent, start, end, ticket)
st_by = defaultdict(list)
for r in S.itertuples(): st_by[r.ticket_id].append((r.t, r.to_status))
arows = defaultdict(list)
for r in A.itertuples(): arows[r.ticket_id].append(r)
iv = []
for k, rows in arows.items():
    for i, r in enumerate(rows):
        c = [t for t, s in st_by[k] if s in ("QUEUED", "RESOLVED") and t > r.t]
        end = min(c[0] if c else CUT, rows[i + 1].t if i + 1 < len(rows) else CUT)
        iv.append((r.agent_id, r.t, end, k))
starts, ends = defaultdict(list), defaultdict(list)
for a, s, e, k in iv: starts[a].append(s); ends[a].append(e)
for a in starts: starts[a].sort(); ends[a].sort()
load_at = lambda a, t: bisect.bisect_right(starts[a], t) - bisect.bisect_right(ends[a], t)

print(f"tickets {len(T)}, status events {len(S)}, assignment events {len(A)}, ownership intervals {len(iv)}")

# 1. load replay
grp = defaultdict(list)
for r in A.itertuples(): grp[(r.agent_id, r.t)].append(r.load_before)
bad = sum(1 for (a, t), v in grp.items()
          if sorted(v) != list(range(bisect.bisect_left(starts[a], t) - bisect.bisect_right(ends[a], t),
                                     bisect.bisect_left(starts[a], t) - bisect.bisect_right(ends[a], t) + len(v))))
check("load_before matches my replay (releases before assignments)", bad == 0, f"bad groups {bad}")
check("load_after == load_before + 1", (A.load_after == A.load_before + 1).all())
check("load_after <= concurrency_limit", (A.load_after <= A.limit).all(), f"violations {(A.load_after > A.limit).sum()}")
check("concurrency_limit equals Agent table", (A.limit == A.agent_id.map(limit)).all())

# 2. eligibility for every assignment
fails, prev, badfrom = defaultdict(int), defaultdict(list), 0
for r in A.itertuples():
    k = r.ticket_id
    waived = waiver_active(k, r.t)
    why = static_ok(r.agent_id, k, r.t, waived)
    if why: fails[why] += 1
    if r.agent_id in prev[k]: fails["H7-repeat"] += 1
    if (r.language_match == "true") != ((r.agent_id, lang[k]) in speaks): fails["language_match flag wrong"] += 1
    if r.required_skill_id != skill_of[intent[k]]: fails["required_skill"] += 1
    if r.queue_id != tq[k]: fails["queue_id"] += 1
    if r.event_type == "ASSIGN" and (r.from_agent_id != "" or prev[k]): badfrom += 1
    if r.event_type == "TRANSFER" and (not prev[k] or r.from_agent_id != prev[k][-1]): badfrom += 1
    prev[k].append(r.agent_id)
check("every assignment satisfies H1-H7 (my recomputation)", not fails, dict(fails))
check("from_agent_id chain correct", badfrom == 0, badfrom)
check("only ASSIGN/TRANSFER; TRANSFER only SHIFT_HANDOVER",
      set(A.event_type) <= {"ASSIGN", "TRANSFER"} and set(A[A.event_type == "TRANSFER"].reason_code) <= {"SHIFT_HANDOVER"})

# 3. shifts
bs = sum(1 for a, s, e, k in iv if (shift_window(a, s) is None) or e > shift_window(a, s)[1])
check("no ticket owned past the end of the owner's shift", bs == 0, f"violations {bs}")

# 4. work-conserving invariant (sampled)
rng = np.random.default_rng(1)
allsp = [(k, s, e) for k, v in spells.items() for s, e in v if e - s >= 120]
sample = [allsp[i] for i in rng.choice(len(allsp), size=min(4000, len(allsp)), replace=False)] if allsp else []
viol = []
for k, s, e in sample:
    tm = (s + e) // 2
    waived = waiver_active(k, tm)
    owners = {r.agent_id for r in arows[k] if r.t <= tm}
    for a in AG.agent_id:
        if a not in owners and static_ok(a, k, tm, waived) is None and load_at(a, tm) < limit[a]:
            viol.append((k, a, tm)); break
check(f"no idle eligible agent while a ticket waits ({len(sample)} sampled waits)", len(viol) == 0, f"violations {len(viol)}; e.g. {viol[:3]}")

for k, a, tm in viol[:5]:
   print("  detail", k, a, "prio", prio[k], "lang", lang[k], "spells", spells[k], "t", tm,
        "load", load_at(a, tm), "limit", limit[a], "tier", tier[a],
        "skill", skills[(a, skill_of[intent[k]])])


# 5. outcomes vs events
fr = S[S.to_status == "IN_PROGRESS"].groupby("ticket_id").event_at.first()
rs = S[S.to_status == "RESOLVED"].groupby("ticket_id").event_at.first()
rsa = S[S.to_status == "RESOLVED"].groupby("ticket_id").actor_agent_id.first()
last = A.groupby("ticket_id").agent_id.last()
Oi = O.set_index("ticket_id")
res = Oi[Oi.resolved_at != ""]
check("first_response_at == first IN_PROGRESS event", (Oi.first_response_at[Oi.first_response_at != ""] == fr.reindex(Oi.index[Oi.first_response_at != ""])).all())
check("resolved_at == RESOLVED event", (res.resolved_at == rs.reindex(res.index)).all())
check("resolved_by == RESOLVED actor == last assigned owner",
      (res.resolved_by_agent_id == rsa.reindex(res.index)).all() and (res.resolved_by_agent_id == last.reindex(res.index)).all())
check("no event after cutoff", S.t.max() <= CUT and A.t.max() <= CUT)

# 6. reports
tr = A[A.event_type == "TRANSFER"].groupby("ticket_id").size()
show("handovers per ticket: max / tickets > 3", f"{int(tr.max())} / {int((tr > 3).sum())}")
cnt = S.groupby("ticket_id").to_status.last().value_counts()
show("status at cutoff", cnt.to_dict())

tot, cap = defaultdict(int), {}
for a, s, e, k in iv: tot[a] += e - s
d0, dN = START // 86400, CUT // 86400
for a in AG.agent_id[AG.status == "ACTIVE"]:
    sec = 0
    for d in range(d0, dN + 1):
        w = shifts[a].get(((d + 3) % 7) + 1)
        if w:
            s = d * 86400 + w[0]; sec += max(0, min(s + w[1], CUT) - max(s, START))
    cap[a] = sec * limit[a]
act = AG[AG.status == "ACTIVE"]
u = pd.DataFrame({"tier": act.tier.values, "load": [tot[a] for a in act.agent_id], "cap": [cap[a] for a in act.agent_id]})
g = u.groupby("tier").sum(); g["utilisation"] = (g.load / g.cap).round(3)
show(f"time-weighted utilisation (overall {u.load.sum() / u.cap.sum():.3f})", g.utilisation)

fb = A[A.reason_code == "LANGUAGE_FALLBACK"]
first = A[A.event_type == "ASSIGN"].copy()
first["lang"] = first.ticket_id.map(lang); first["prio"] = first.ticket_id.map(prio)
first["inc"] = first.ticket_id.map(tk.incident_id) != ""
first["fb"] = first.reason_code == "LANGUAGE_FALLBACK"
show("fallback share by ticket language", first.groupby("lang").fb.agg(["sum", "mean"]).round(3))
show("fallback share by priority / incident", first.groupby(["prio", "inc"]).fb.agg(["sum", "mean", "size"]).round(3))
show("share of fallbacks released exactly at threshold",
     round(float(np.mean([r.wait == TH[prio[r.ticket_id]] for r in fb.itertuples()])), 3) if len(fb) else "n/a")
first["wait"] = first.wait
show("wait minutes by priority and incident", first.groupby(["prio", "inc"]).wait.describe(percentiles=[.5, .95]).round(1)[["count", "mean", "50%", "95%", "max"]])

own = defaultdict(int)
for a, s, e, k in iv: own[k] += e - s
H = pd.DataFrame([(k, intent[k], tk.channel_id[k], tk.incident_id[k] != "", own[k] / 60) for k in own],
                 columns=["k", "intent", "channel", "inc", "own"])
med = I.set_index("intent_id").median_handle_minutes.astype(float)
H["exp"] = np.where(H.inc, 15.0, H.intent.map(med) * H.channel.map({"EMAIL": 3.0, "CHAT": 1.0, "PHONE": 0.8}))
gm = H.groupby(["intent", "channel", "inc"]).agg(n=("own", "size"), med=("own", "median"), exp=("exp", "first"))
gm = gm[gm.n >= 60]; gm["ratio"] = (gm.med / gm.exp).round(2)
check("median total owned time within 0.75-1.30 of spec median (every intent x channel x incident group, n>=60)",
      gm.ratio.between(0.75, 1.30).all(), f"{len(gm)} groups, ratio range {gm.ratio.min()}-{gm.ratio.max()}")
show("handle-time ratio table (actual median / spec median)", gm.ratio.unstack(level=[1, 2]))
firstA = A.groupby("ticket_id").t.first()
pk = pd.DataFrame({"d": (ep(Oi.first_response_at.where(Oi.first_response_at != "", other=None).dropna()) - firstA.reindex(Oi.index[Oi.first_response_at != ""])) / 60,
                   "ch": tk.channel_id.reindex(Oi.index[Oi.first_response_at != ""]), "p": tk.priority_level.reindex(Oi.index[Oi.first_response_at != ""])})
show("pickup delay minutes, P3/P4 (spec medians: CHAT 1.5, EMAIL 15, PHONE ~0.5)", pk[pk.p.isin(["P3", "P4"])].groupby("ch").d.median().round(2))