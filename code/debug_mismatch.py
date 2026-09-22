"""Debug script for sample request mismatches."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from main import Solver, read_csv, D, day

s = Solver()
samples = {r["request_id"]: r for r in read_csv("sample_requests.csv")}
IDS = ["request_03", "request_05", "request_06", "request_13", "request_21"]

for rid in IDS:
    r = samples[rid]
    a = s.solve_request(r)
    flows, rec = s.base_flows(r)
    safe = s.safe_amount_today(r, flows)
    earliest = s.earliest_full_date(r, flows)
    print(f"\n{'='*60}\n{rid} user={r['user_id']} date={r['request_date']} amt={r['requested_amount']}")
    print(f"EXP: {r['affordability_status']}/{r['recommended_payment_method']} safe={r['amount_safe_to_pay']} earliest={r['earliest_date_for_full_payment']} changes={r['spending_changes_needed']}")
    print(f"ACT: {a['affordability_status']}/{a['recommended_payment_method']} safe={a['amount_safe_to_pay']} earliest={a['earliest_date_for_full_payment']} changes={a['spending_changes_needed']}")
    print(f"computed safe={safe} earliest={earliest} flows={len(flows)} recurring={len(rec)}")
    prof = s.profiles[r["user_id"]]
    print(f"balance={prof['current_available_balance']} min={prof['minimum_balance_to_keep']}")
    elig = [c.text() for c in s.eligible_changes(r, rec)]
    print(f"eligible_changes: {elig}")
    # salary flows
    salaries = [f for f in flows if f.category == "salary" and f.amount > 0]
    print(f"salary flows ({len(salaries)}):")
    for f in sorted(salaries, key=lambda x: x.when)[:15]:
        print(f"  {f.when} +{f.amount} key={f.key[:40]}")
    if len(salaries) > 15:
        print(f"  ... +{len(salaries)-15} more")
    # pending debits near start
    start = day(r["request_date"])
    debits = [f for f in flows if f.amount < 0 and start <= f.when <= start.replace(day=min(start.day+30,28))]
    print(f"debits in first ~30d: {len(debits)}")
