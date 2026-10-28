import sys, pathlib
import pandas as pd

d = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "data/_check")
t = lambda n: pd.read_csv(d / f"{n}.csv", keep_default_na=False)
agent, askill, alang = t("agent"), t("agent_skill"), t("agent_language")
shift, cust, rule, qm = t("agent_shift"), t("customer"), t("priority_rule"), t("queue_member")

def check(name, ok):
    print(("PASS  " if ok else "FAIL  ") + name)

def show(title, x):
    print(f"\n== {title} ==\n{x}")

# Agents
check("tiers 70/22/8", agent.tier.value_counts().to_dict() == {"T1": 70, "T2": 22, "T3": 8})
inactive = agent[agent.status == "INACTIVE"]
check("3 INACTIVE, all T1", len(inactive) == 3 and set(inactive.tier) == {"T1"})
base = {"T1": 4, "T2": 5, "T3": 3}
new = agent.hire_date >= "2025-11-06"
exp = agent.tier.map(base) - new.astype(int)
check("limits = tier base minus 1 for new hires (floor 2)",
      (agent.concurrency_limit == exp.clip(lower=2)).all())
show("new-hire share (expect ~15%)", round(new.mean(), 3))

# Languages
en = alang[alang.language_code == "en"]
check("every agent has en FLUENT/NATIVE",
      set(en.agent_id) == set(agent.agent_id) and en.fluency.isin(["FLUENT", "NATIVE"]).all())
show("non-en agents (expect es 24, hi 20, fr 12, ar 10)",
     alang[alang.language_code != "en"].language_code.value_counts())

# Shifts
per_agent = shift.groupby("agent_id")
check("one shift_code per agent", (per_agent.shift_code.nunique() == 1).all())
show("agents per shift (expect EARLY 25, DAY 30, LATE 30, NIGHT 15)",
     per_agent.shift_code.first().value_counts())
show("work-day patterns (expect ~70/15/15)",
     per_agent.day_of_week.apply(lambda s: tuple(sorted(s))).value_counts())
show("shift times", shift.groupby("shift_code")[["start_time", "end_time"]].first())

# Skills
acq = askill[askill.effective_from > "2026-01-05"]
check("8 dated skill acquisitions", len(acq) == 8)
t3 = agent[agent.tier == "T3"].agent_id
sk = askill.groupby("agent_id").skill_id.apply(set)
check("all T3 hold TECH and SECURITY",
      all({"SKL-TECH", "SKL-SECURITY"} <= sk.get(a, set()) for a in t3))
dups = askill[askill.duplicated(["agent_id", "skill_id"], keep=False)]
show("agent+skill with multiple rows (acquisitions must not overlap)", dups.sort_values(["agent_id", "skill_id"]))
m = qm.merge(agent[["agent_id", "hire_date"]], on="agent_id")
check("QueueMember.effective_from >= hire_date", (m.effective_from >= m.hire_date).all())

# Rules
show("PriorityRule", rule[["rule_id", "rule_type", "priority_level", "first_response_min",
                           "effective_from", "effective_to"]])

# Customers
show("customer_type (expect ~10% PROSPECT)", cust.customer_type.value_counts(normalize=True).round(3))
show("segment (expect 70/22/8)", cust.segment.value_counts(normalize=True).round(3))
show("language (expect en 60, es 15, hi 12, fr 7, ar 6)", cust.preferred_language.value_counts(normalize=True).round(3))
check("emails end with @example.test", cust.email.str.endswith("@example.test").all())
check("phones start with +999 when present", cust.phone[cust.phone != ""].str.startswith("+999").all())
show("language x country", pd.crosstab(cust.preferred_language, cust.country_code))

# Line endings
bad = [f.name for f in d.glob("*.csv") if b"\r\n" in f.read_bytes()]
check("no CRLF in any CSV", not bad)