"""Independent Phase 3b verification. Reads CSVs only; shares no code with generator/.
Usage: python scripts/check_phase3b.py [cutoff]      (default 2026-04-05T00:00:00Z)"""
import sys, bisect
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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
def show(title, x): print(f"\n== {title} ==\n{x}")

T, S, A, O, CY = (rd(stg / f) for f in ("ticket_draft.csv", "ticket_status_event_draft.csv", "assignment_event_draft.csv",
                                        "ticket_outcome_draft.csv", "ticket_cycle_outcome_draft.csv"))
AG, AS, AL, SH, QM, Q, I, C = (rd(raw / f) for f in ("agent.csv", "agent_skill.csv", "agent_language.csv", "agent_shift.csv",
                                                    "queue_member.csv", "queue.csv", "intent.csv", "customer.csv"))
CUT = int(ep(pd.Series([CUT_STR]))[0]); START = epday("2026-01-05") * 86400
S["t"], S["seq"], S["cyc"] = ep(S.event_at), S.seq_no.astype(int), S.cycle_no.astype(int)
A["t"], A["seq"], A["cyc"] = ep(A.event_at), A.seq_no.astype(int), A.cycle_no.astype(int)
A["lb"], A["la"], A["limit"], A["wait"] = A.agent_load_before.astype(int), A.agent_load_after.astype(int), A.concurrency_limit.astype(int), A.wait_minutes.astype(float)
T["t"] = ep(T.created_at)
S = S.sort_values(["ticket_id", "seq"]).reset_index(drop=True)
A = A.sort_values(["ticket_id", "seq"]).reset_index(drop=True)

tk = T.set_index("ticket_id")
prio, lang, tq, intent, chan = (tk[c].to_dict() for c in ("priority_level", "language_code", "queue_id", "intent_id", "channel_id"))
incid = tk.incident_id.to_dict(); created = tk.t.to_dict()
seg = C.set_index("customer_id").segment.to_dict(); cseg = {k: seg[c] for k, c in tk.customer_id.items()}
skill_of = I.set_index("intent_id").required_skill_id.to_dict()
minTier = Q.set_index("queue_id").min_tier.to_dict()
TIER = {"T1": 1, "T2": 2, "T3": 3}; PROF = {"BASIC": 1, "PROFICIENT": 2, "EXPERT": 3}
TH = {"P1": 5, "P2": 15, "P3": 30, "P4": 60}
ESC = {"P1": 60, "P2": 240, "P3": 720, "P4": 1440}; RES = {"P1": 240, "P2": 480, "P3": 1440, "P4": 2880}
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
    st_, en_ = h * 3600 + m * 60, eh * 3600 + em * 60
    shifts[r.agent_id][int(r.day_of_week)] = (st_, (en_ - st_) % 86400 or 86400)
def shift_window(a, t):
    ds = t - t % 86400
    for off in (0, 1):
        d0 = ds - off * 86400
        w = shifts[a].get(((d0 // 86400) + 3) % 7 + 1)
        if w:
            s = d0 + w[0]
            if s >= START and s <= t < s + w[1]: return s, s + w[1]
    return None

# ---------- independent calendar math ----------
CALD = {"CAL-EMAIL": {d: (480, 1200) for d in range(5)}, "CAL-CHAT": {d: (360, 1320) for d in range(7)},
        "CAL-PHONE": {d: (540, 1080) for d in range(5)}, "CAL-247": {d: (0, 1440) for d in range(7)}}
CH = {"EMAIL": "CAL-EMAIL", "CHAT": "CAL-CHAT", "PHONE": "CAL-PHONE"}
dt_ = lambda s: datetime.fromtimestamp(s, tz=timezone.utc)
def first_open(cal, t0):
    for k in range(9):
        day = (t0 + timedelta(days=k)).replace(hour=0, minute=0, second=0, microsecond=0)
        w = CALD[cal].get(day.weekday())
        if not w: continue
        op, cl = day + timedelta(minutes=w[0]), day + timedelta(minutes=w[1])
        if t0 < cl: return max(t0, op)
    raise RuntimeError("no open time")
def add_bm(cal, start, minutes):
    rem, t = minutes * 60, start
    for _ in range(80):
        day = t.replace(hour=0, minute=0, second=0, microsecond=0)
        w = CALD[cal].get(day.weekday())
        if w:
            op, cl = day + timedelta(minutes=w[0]), day + timedelta(minutes=w[1])
            if t < cl:
                cur = max(t, op); avail = (cl - cur).total_seconds()
                if rem <= avail: return cur + timedelta(seconds=rem)
                rem -= avail
        t = day + timedelta(days=1)
    raise RuntimeError("no result")
def due_epoch(k, start_sec, minutes):
    cal = "CAL-247" if prio[k] == "P1" else CH[chan[k]]
    return int(add_bm(cal, first_open(cal, dt_(start_sec)), minutes).timestamp())

# ---------- per-ticket structures ----------
st = defaultdict(list)
for r in S.itertuples(index=False):
    st[r.ticket_id].append((r.t, r.seq, r.cyc, r.from_status, r.to_status, r.actor_type, r.actor_agent_id))
arows = defaultdict(list)
for r in A.itertuples(index=False): arows[r.ticket_id].append(r)
print(f"tickets {len(T)}, status events {len(S)}, assignment events {len(A)}, cycle rows {len(CY)}")

# ---------- 1. lifecycle ----------
OK_TR = {("", "NEW"), ("NEW", "QUEUED"), ("QUEUED", "ASSIGNED"), ("ASSIGNED", "IN_PROGRESS"), ("ASSIGNED", "ASSIGNED"),
         ("ASSIGNED", "QUEUED"), ("ASSIGNED", "ESCALATED"), ("IN_PROGRESS", "PENDING_CUSTOMER"), ("IN_PROGRESS", "RESOLVED"),
         ("IN_PROGRESS", "ESCALATED"), ("IN_PROGRESS", "ASSIGNED"), ("IN_PROGRESS", "QUEUED"), ("PENDING_CUSTOMER", "IN_PROGRESS"),
         ("PENDING_CUSTOMER", "RESOLVED"), ("ESCALATED", "ASSIGNED"), ("RESOLVED", "CLOSED"), ("RESOLVED", "REOPENED"), ("REOPENED", "QUEUED")}
S["prev_to"] = S.groupby("ticket_id").to_status.shift().fillna("")
check("from_status equals previous to_status", (S.from_status == S.prev_to).all())
badpairs = {p for p in zip(S.from_status, S.to_status) if p not in OK_TR}
check("every status transition allowed by 7.1", not badpairs, badpairs)
S["prev_cyc"] = S.groupby("ticket_id").cyc.shift().fillna(0).astype(int)
exp_cyc = np.where(S.prev_cyc == 0, 1, S.prev_cyc + (S.to_status == "REOPENED").astype(int))
check("cycle_no increases only at REOPENED (and REOPENED carries the new cycle)", (S.cyc == exp_cyc).all())
check("at most 2 reopens per ticket", S[S.to_status == "REOPENED"].groupby("ticket_id").size().max() <= 2)
check("status event times non-decreasing per ticket", (S.groupby("ticket_id").t.diff().fillna(0) >= 0).all())
check("no event after cutoff", S.t.max() <= CUT and A.t.max() <= CUT)

# ---------- 2. spells, ownership, load ----------
spells = defaultdict(list)
for k, v in st.items():
    cur = None
    for (t, seq, cyc, frm, to, act, aag) in v:
        if to in ("QUEUED", "ESCALATED"): cur = (t, cyc)
        elif to == "ASSIGNED" and frm in ("QUEUED", "ESCALATED") and cur:
            spells[k].append((cur[0], t, cur[1], True)); cur = None
    if cur: spells[k].append((cur[0], CUT, cur[1], False))
def waiver_active(k, t, cyc):
    return any(min(e, t) - s >= TH[prio[k]] * 60 for s, e, c, closed in spells[k] if c == cyc and s < t)
def owner_at(k, cyc, t):
    o = None
    for r in arows[k]:
        if r.cyc == cyc and r.t <= t: o = r.agent_id
    return o
iv = []
for k, rows in arows.items():
    rl = [t for (t, seq, cyc, frm, to, act, aag) in st[k] if to in ("QUEUED", "ESCALATED", "RESOLVED")]
    for i, r in enumerate(rows):
        j = bisect.bisect_right(rl, r.t)
        end = rl[j] if j < len(rl) else CUT
        if i + 1 < len(rows): end = min(end, rows[i + 1].t)
        iv.append((r.agent_id, r.t, end, k, r.cyc))
starts, ends = defaultdict(list), defaultdict(list)
for a, s, e, k, c in iv: starts[a].append(s); ends[a].append(e)
for a in starts: starts[a].sort(); ends[a].sort()
load_at = lambda a, t: bisect.bisect_right(starts[a], t) - bisect.bisect_right(ends[a], t)
grp = defaultdict(list)
for r in A.itertuples(): grp[(r.agent_id, r.t)].append(r.lb)
bad = 0
for (a, t), v in grp.items():
    base = bisect.bisect_left(starts[a], t) - bisect.bisect_right(ends[a], t)
    if sorted(v) != list(range(base, base + len(v))): bad += 1
check("load_before matches my replay (pending counts; releases before assignments)", bad == 0, f"bad groups {bad}")
check("load_after == load_before + 1 and <= limit", (A.la == A.lb + 1).all() and (A.la <= A.limit).all(), f"over limit {(A.la > A.limit).sum()}")
bs = sum(1 for a, s, e, k, c in iv if shift_window(a, s) is None or e > shift_window(a, s)[1])
check("no ticket owned past the end of the owner's shift (incl. pending)", bs == 0, f"violations {bs}")

# ---------- 3. assignment rules: active skill/queue, H1-H7, chains ----------
SK_Q = lambda s: s.replace("SKL-", "QUE-")
REROUTE = {"SKL-BILLING": {"SKL-ACCOUNT"}, "SKL-TECH": {"SKL-ACCOUNT"}, "SKL-ACCOUNT": {"SKL-BILLING", "SKL-TECH"},
           "SKL-GENERAL": {"SKL-BILLING", "SKL-TECH", "SKL-ACCOUNT", "SKL-SALES"}}
VALID = {("ASSIGN", "INITIAL_ROUTING"), ("ASSIGN", "LANGUAGE_FALLBACK"), ("TRANSFER", "SHIFT_HANDOVER"), ("TRANSFER", "SKILL_REROUTE"),
         ("ESCALATE", "SLA_ESCALATION"), ("ESCALATE", "MANUAL_ESCALATION"), ("REOPEN_ASSIGN", "REOPEN_ROUTING")}
def elig(a, k, t, waived, skill, queue, need_tier=None):
    if astatus[a] != "ACTIVE": return "H1"
    if shift_window(a, t) is None: return "H2"
    day = t // 86400
    best = max((p for f, to, p in skills.get((a, skill), []) if f <= day <= to), default=0)
    if best == 0: return "H3"
    if prio[k] in ("P1", "P2") and best < 2: return "H3b"
    if not waived and (a, lang[k]) not in speaks: return "H4"
    if not any(f <= day <= to for f, to in qmem.get((a, queue), [])): return "H6-membership"
    if tier[a] < TIER[minTier[queue]]: return "H6-tier"
    if need_tier is not None and tier[a] != need_tier: return "H6-exact-tier"
    return None
fails, reroutes_seen = defaultdict(int), defaultdict(int)
esc_rows = defaultdict(list)
for k, rows in arows.items():
    isk, cur_c, askill, aq, owners, prev_agent = skill_of[intent[k]], None, None, None, [], None
    for r in rows:
        if r.cyc != cur_c: cur_c, askill, aq, owners, prev_agent = r.cyc, isk, tq[k], [], None
        if (r.event_type, r.reason_code) not in VALID: fails["event_type/reason combination"] += 1
        first = prev_agent is None
        if first and r.event_type not in ("ASSIGN", "REOPEN_ASSIGN"): fails["first event of cycle must be ASSIGN/REOPEN_ASSIGN"] += 1
        if first and (r.event_type == "ASSIGN") != (r.cyc == 1): fails["ASSIGN only in cycle 1, REOPEN_ASSIGN only later"] += 1
        if not first and r.event_type in ("ASSIGN", "REOPEN_ASSIGN"): fails["extra ASSIGN in cycle"] += 1
        if r.event_type in ("ASSIGN", "REOPEN_ASSIGN") and r.from_agent_id != "": fails["from_agent_id must be null"] += 1
        if r.event_type in ("TRANSFER", "ESCALATE") and r.from_agent_id != prev_agent: fails["from_agent_id chain"] += 1
        is_rr = r.event_type == "TRANSFER" and r.reason_code == "SKILL_REROUTE"
        if is_rr:
            reroutes_seen[(k, r.cyc)] += 1
            if askill != isk or r.required_skill_id not in REROUTE.get(isk, set()): fails["reroute target not allowed"] += 1
            if prio[k] == "P1" or incid[k] != "": fails["reroute on P1/incident ticket"] += 1
            exp_sk, exp_q = r.required_skill_id, SK_Q(r.required_skill_id)
        else:
            exp_sk, exp_q = askill, ("QUE-ESCALATION" if r.event_type == "ESCALATE" else aq)
        if r.required_skill_id != exp_sk: fails["required_skill_id != active skill"] += 1
        if r.queue_id != exp_q: fails["queue_id != active queue"] += 1
        need = tier[prev_agent] + 1 if r.event_type == "ESCALATE" and prev_agent else None
        why = elig(r.agent_id, k, r.t, waiver_active(k, r.t, r.cyc), exp_sk, exp_q, need)
        if why: fails[why] += 1
        if r.agent_id in owners: fails["H7 agent repeated in cycle"] += 1
        if (r.language_match == "true") != ((r.agent_id, lang[k]) in speaks): fails["language_match flag"] += 1
        if r.event_type == "ESCALATE": esc_rows[k].append(r)
        owners.append(r.agent_id); prev_agent = r.agent_id
        if is_rr: askill, aq = r.required_skill_id, SK_Q(r.required_skill_id)
check("every assignment: active skill/queue, event combos, chains, H1-H7 (my recomputation)", not fails, dict(fails))
check("at most one skill reroute per cycle", all(v == 1 for v in reroutes_seen.values()), f"{len(reroutes_seen)} reroutes")

# ---------- 4. escalation rows vs status events ----------
bad_esc = defaultdict(int)
for k, rows in esc_rows.items():
    escs = [(t, act) for (t, seq, cyc, frm, to, act, aag) in st[k] if to == "ESCALATED"]
    for r in rows:
        prior = [e for e in escs if e[0] <= r.t]
        if not prior: bad_esc["no preceding ESCALATED status"] += 1; continue
        t0, act = prior[-1]
        if (act == "SYSTEM") != (r.reason_code == "SLA_ESCALATION"): bad_esc["reason vs actor"] += 1
        if abs(r.wait - (r.t - t0) / 60) > 0.06: bad_esc["wait_minutes"] += 1
check("ESCALATE events: preceded by ESCALATED status, reason matches actor, wait_minutes correct", not bad_esc, dict(bad_esc))
per_cycle = defaultdict(lambda: defaultdict(int))
for k, rows in esc_rows.items():
    for r in rows: per_cycle[(k, r.cyc)][r.reason_code] += 1
check("at most one SLA and one manual escalation per cycle", all(v <= 1 for d in per_cycle.values() for v in d.values()))

# ---------- 5. pending episodes ----------
pend_bad, pend_n, silent_n, pend_dur = defaultdict(int), defaultdict(int), 0, []
for k, v in st.items():
    for i, (t, seq, cyc, frm, to, act, aag) in enumerate(v):
        if to != "PENDING_CUSTOMER": continue
        pend_n[(k, cyc)] += 1
        o = owner_at(k, cyc, t); w = shift_window(o, t) if o else None
        if o is None or aag != o or act != "AGENT": pend_bad["actor/owner"] += 1
        if w is None: pend_bad["owner off shift"] += 1; continue
        if w[1] - t < 600: pend_bad["started with < 10 min of shift left"] += 1
        nx = v[i + 1] if i + 1 < len(v) else None
        if nx is None: continue
        if nx[4] not in ("IN_PROGRESS", "RESOLVED"): pend_bad["episode ends in wrong state"] += 1; continue
        if nx[4] == "IN_PROGRESS" and nx[5] != "CUSTOMER": pend_bad["reply actor"] += 1
        if nx[4] == "RESOLVED": silent_n += 1
        if nx[0] > w[1] - 60: pend_bad["reply after shift end - 60 s"] += 1
        if nx[0] - t < 60: pend_bad["episode shorter than 1 min"] += 1
        if owner_at(k, cyc, nx[0] - 1) != o: pend_bad["owner changed during pending"] += 1
        pend_dur.append((nx[0] - t) / 60)
check("pending episodes obey shift rules (7.8 item 4)", not pend_bad, dict(pend_bad))
check("at most one pending episode per cycle", all(v == 1 for v in pend_n.values()))
mid = defaultdict(int)
for (k, c), n in pend_n.items(): mid[(k, c)] += n
for (k, c), n in reroutes_seen.items(): mid[(k, c)] += n
for (k, c), d in per_cycle.items(): mid[(k, c)] += d.get("MANUAL_ESCALATION", 0)
check("at most one mid-handling event (pending / reroute / manual escalation) per cycle", all(v <= 1 for v in mid.values()))

# ---------- 6. cycle table, SLA escalation timing, reopens ----------
cstart, esc_due, res_due = {}, {}, {}
rop = defaultdict(dict)
for k, v in st.items():
    for (t, seq, cyc, frm, to, act, aag) in v:
        if to == "REOPENED": rop[k][cyc] = (t, act)
for k in tk.index:
    cs = {1: created[k]}; cs.update({c: x[0] for c, x in rop.get(k, {}).items()})
    for c, s0 in cs.items():
        cstart[(k, c)] = s0
        esc_due[(k, c)] = due_epoch(k, s0, ESC[prio[k]])
        rm = RES[prio[k]] if c == 1 else -(-RES[prio[k]] // 2)
        res_due[(k, c)] = due_epoch(k, s0, rm)
exp_fire, actual_fire, why_not = {}, {}, defaultdict(int)
res_t, nxt_cl = {}, {}
for k, v in st.items():
    byc = defaultdict(list)
    for e in v: byc[e[2]].append(e)
    for c, evs in byc.items():
        due = esc_due[(k, c)]
        rt = next((e[0] for e in evs if e[4] == "RESOLVED"), None); res_t[(k, c)] = rt
        sysesc = [e[0] for e in evs if e[4] == "ESCALATED" and e[5] == "SYSTEM"]
        if sysesc: actual_fire[(k, c)] = sysesc
        if due > CUT: why_not["due after cutoff"] += 1; continue
        if rt is not None and rt <= due: why_not["resolved at/before due"] += 1; continue
        se_at_due = [e for e in evs if e[0] == due and e[4] == "ESCALATED" and e[5] == "SYSTEM"]
        if se_at_due:
            s = se_at_due[0][3]
            o = owner_at(k, c, due - 1)
            if s in ("ASSIGNED", "IN_PROGRESS"):
                if tier[o] <= 2: exp_fire[(k, c)] = due; why_not["FIRES: owned by T1/T2"] += 1
                else: why_not["owner is T3: no escalation"] += 1
            else:
                why_not[f"in {s} at due: never escalates"] += 1
            continue

        idx = max((i for i, e in enumerate(evs) if e[0] <= due), default=None)
        if idx is None: why_not["no state at due"] += 1; continue
        s = evs[idx][4]
        if s in ("ASSIGNED", "IN_PROGRESS"):
            o = owner_at(k, c, due - 1)
            if o is None:
                why_not["in QUEUED at due: never escalates"] += 1
            elif tier[o] <= 2: exp_fire[(k, c)] = due; why_not["FIRES: owned by T1/T2"] += 1
            else: why_not["owner is T3: no escalation"] += 1
        elif s == "PENDING_CUSTOMER":
            nx = evs[idx + 1] if idx + 1 < len(evs) else None
            if nx and nx[4] == "IN_PROGRESS":
                o = owner_at(k, c, nx[0] - 1)
                if tier[o] <= 2: exp_fire[(k, c)] = nx[0]; why_not["FIRES: deferred past pending"] += 1
                else: why_not["pending then T3 owner: none"] += 1
            elif nx: why_not["pending ended in silent resolve: none"] += 1
            else: why_not["still pending at cutoff: none"] += 1
        else: why_not[f"in {s} at due: never escalates"] += 1
miss = [x for x in exp_fire if x not in actual_fire]
extra = [x for x in actual_fire if x not in exp_fire]
wrong = [x for x in exp_fire if x in actual_fire and actual_fire[x] != [exp_fire[x]]]
check("SLA escalation fires exactly when my reading of 7.8(6) says, at exactly the right instant",
      not (miss or extra or wrong), f"expected {len(exp_fire)}, actual {len(actual_fire)}; missing {len(miss)}, unexpected {len(extra)}, wrong time {len(wrong)}")
for lab, lst in (("missing", miss), ("unexpected", extra), ("wrong time", wrong)):
    for x in lst[:3]:
        print(f"  detail {lab}: {x} prio {prio[x[0]]} due {esc_due[x]} expected {exp_fire.get(x)} actual {actual_fire.get(x)}")
show("why cycles did / did not escalate", pd.Series(why_not).sort_values(ascending=False).to_string())

CYi = CY.copy()
for c in ("cycle_start_at", "first_response_at", "resolved_at", "escalation_due_at", "sla_escalation_fired_at"):
    CYi[c + "_s"] = -1
    m = (CYi[c] != "") & CYi[c].notna()
    CYi.loc[m, c + "_s"] = ep(CYi.loc[m, c]).values
CYi["cyc"] = CYi.cycle_no.astype(int)
key = list(zip(CYi.ticket_id, CYi.cyc))
nreopen = int((S.to_status == "REOPENED").sum())
check("cycle rows == tickets + reopens, keys unique", len(CY) == len(T) + nreopen and len(set(key)) == len(key), f"{len(CY)} vs {len(T) + nreopen}")
cnt = lambda pred: defaultdict(int, {kk: sum(1 for r in rows if pred(r)) for kk, rows in
                                     {(k, c): [r for r in arows[k] if r.cyc == c] for (k, c) in key}.items()})
tr_n, es_n = defaultdict(int), defaultdict(int)
for k, rows in arows.items():
    for r in rows:
        if r.event_type == "TRANSFER": tr_n[(k, r.cyc)] += 1
        if r.event_type == "ESCALATE": es_n[(k, r.cyc)] += 1
firstip, resrow = {}, {}
for k, v in st.items():
    for (t, seq, cyc, frm, to, act, aag) in v:
        if to == "IN_PROGRESS": firstip.setdefault((k, cyc), t)
        if to == "RESOLVED": resrow.setdefault((k, cyc), (t, aag, frm))
cb = defaultdict(int)
for r in CYi.itertuples():
    kk = (r.ticket_id, r.cyc)
    if r.cycle_start_at_s != cstart.get(kk): cb["cycle_start_at"] += 1
    if r.first_response_at_s != firstip.get(kk, -1): cb["first_response_at"] += 1
    rr = resrow.get(kk)
    if r.resolved_at_s != (rr[0] if rr else -1): cb["resolved_at"] += 1
    if rr and (r.resolved_by_agent_id != rr[1] or r.resolved_by_agent_id != owner_at(*kk, rr[0])): cb["resolved_by"] += 1
    if rr and (r.resolution_kind == "NO_CUSTOMER_RESPONSE") != (rr[2] == "PENDING_CUSTOMER"): cb["resolution_kind"] += 1
    if int(r.transfer_count) != tr_n[kk]: cb["transfer_count"] += 1
    if int(r.escalation_count) != es_n[kk]: cb["escalation_count"] += 1
    if int(r.pending_episodes) != pend_n[kk]: cb["pending_episodes"] += 1
    if r.escalation_due_at_s != esc_due[kk]: cb["escalation_due_at (my calendar math)"] += 1
    if r.sla_escalation_fired_at_s != (actual_fire[kk][0] if kk in actual_fire else -1): cb["sla_escalation_fired_at"] += 1
    if pend_n[kk] and r.mid_event_type != "PENDING": cb["mid_event_type vs pending"] += 1
    if reroutes_seen.get(kk) and r.mid_event_type != "SKILL_REROUTE": cb["mid_event_type vs reroute"] += 1
    if per_cycle[kk].get("MANUAL_ESCALATION") and r.mid_event_type != "MANUAL_ESCALATION": cb["mid_event_type vs manual"] += 1
check("ticket_cycle_outcome matches events (incl. my escalation_due_at)", not cb, dict(cb))
Oi = O.set_index("ticket_id")
rc = pd.Series({k: len(rop.get(k, {})) for k in tk.index})
check("ticket_outcome.reopen_count == number of REOPENED events", (Oi.reopen_count.astype(int) == rc.reindex(Oi.index)).all())

rb, rdelay, expp, actual_re, eligible_n = defaultdict(int), [], 0.0, 0, 0
for (k, c), rt in res_t.items():
    if rt is None: continue
    if c in rop.get(k, {}):
        d = rop[k][c][0]; pass
    nxt = rop.get(k, {}).get(c + 1)
    if nxt:
        dly = nxt[0] - rt; rdelay.append(dly / 3600)
        if not (0 < dly <= 72 * 3600): rb["delay outside (0,72h]"] += 1
        if nxt[1] != "CUSTOMER": rb["REOPENED actor"] += 1
    if c <= 2 and rt + 72 * 3600 <= CUT:
        breach = rt > res_due[(k, c)]
        expp += 0.04 + 0.03 * breach + 0.015 * min(tr_n[(k, c)], 3) + 0.02 * (es_n[(k, c)] > 0)
        actual_re += 1 if nxt else 0; eligible_n += 1
check("reopen delays in (0,72h], actor CUSTOMER", not rb, dict(rb))
check("reopen count matches 7.7 probability formula (my breach/transfer/escalation recompute)",
      abs(actual_re / expp - 1) < 0.12, f"expected {expp:.0f}, actual {actual_re}, ratio {actual_re / expp:.3f} over {eligible_n} cycles")
ra = [r for k, rows in arows.items() for r in rows if r.event_type == "REOPEN_ASSIGN"]
check("every reopened cycle that got an agent used REOPEN_ASSIGN/REOPEN_ROUTING", all(r.reason_code == "REOPEN_ROUTING" for r in ra))

# ---------- 7. work-conserving (sampled) ----------
rng = np.random.default_rng(7); allsp = []
for k, v in spells.items():
    for s, e, c, closed in v:
        if closed and e - s >= 120:
            end = next((r for r in arows[k] if r.t == e and r.cyc == c), None)
            if end is not None: allsp.append((k, s, e, c, end))
sample = [allsp[i] for i in rng.choice(len(allsp), size=min(4000, len(allsp)), replace=False)]
viol = []
for k, s, e, c, endr in sample:
    tm = (s + e) // 2; waived = waiver_active(k, tm, c)
    owners = {r.agent_id for r in arows[k] if r.cyc == c and r.t <= tm}
    need = tier[endr.from_agent_id] + 1 if endr.event_type == "ESCALATE" and endr.from_agent_id else None
    for a in AG.agent_id:
        if a not in owners and elig(a, k, tm, waived, endr.required_skill_id, endr.queue_id, need) is None and load_at(a, tm) < limit[a]:
            viol.append((k, a, tm, endr.event_type)); break
check(f"no idle eligible agent while a ticket waits ({len(sample)} sampled QUEUED/ESCALATED spells)", not viol, f"violations {len(viol)}; e.g. {viol[:3]}")

# ---------- 8. reports (judgment, not pass/fail) ----------
show("reopen delay hours (median / p95; spec median ~6)", f"{np.median(rdelay):.1f} / {np.percentile(rdelay, 95):.1f}" if rdelay else "n/a")
show("pending duration minutes: replied episodes (median, spec 45 before shift truncation)", f"{np.median(pend_dur):.1f} (n={len(pend_dur)})")
show("silent share of pending episodes (spec 3%)", f"{silent_n / max(1, sum(pend_n.values())):.3f}")
c1 = CYi[CYi.cyc == 1].copy(); c1["intent"] = c1.ticket_id.map(intent); c1["seg"] = c1.ticket_id.map(cseg)
c1["pend"] = c1.pending_episodes.astype(int) > 0
spec = {"INT-004": .25, "INT-002": .20, "INT-008": .20, "INT-006": .15}
pi = c1.groupby("intent").pend.mean().round(3).to_frame("observed"); pi["spec(before skips)"] = [spec.get(i, .08) for i in pi.index]
show("cycle-1 pending share by intent", pi)
c1["man"] = [bool(per_cycle[(k, 1)].get("MANUAL_ESCALATION")) for k in c1.ticket_id]
show("cycle-1 manual escalation share by segment (spec 6% ENTERPRISE / 2% others, before earlier draws)", c1.groupby("seg").man.mean().round(3))
c1["sla"] = [(k, 1) in actual_fire for k in c1.ticket_id]; c1["prio"] = c1.ticket_id.map(prio)
show("cycle-1 SLA escalation share by priority", c1.groupby("prio").sla.agg(["sum", "mean"]).round(3))
elg = c1[(c1.prio != "P1") & (c1.ticket_id.map(incid) == "") & (c1.intent.map(skill_of).isin(list(REROUTE)))]
show("cycle-1 reroute share among eligible tickets (spec 4% x P(no pending))", round(float(np.mean([(k, 1) in reroutes_seen for k in elg.ticket_id])), 3))
tot = defaultdict(int)
for a, s, e, k, c in iv: tot[a] += e - s
cap = {}
for a in AG.agent_id[AG.status == "ACTIVE"]:
    sec = 0
    for d in range(START // 86400, CUT // 86400 + 1):
        w = shifts[a].get(((d + 3) % 7) + 1)
        if w:
            s0 = d * 86400 + w[0]; sec += max(0, min(s0 + w[1], CUT) - max(s0, START))
    cap[a] = sec * limit[a]
act = AG[AG.status == "ACTIVE"]
u = pd.DataFrame({"tier": act.tier.values, "load": [tot[a] for a in act.agent_id], "cap": [cap[a] for a in act.agent_id]})
g = u.groupby("tier").sum(); g["utilisation"] = (g.load / g.cap).round(3)
show(f"time-weighted utilisation incl. pending (overall {u.load.sum() / u.cap.sum():.3f})", g.utilisation)