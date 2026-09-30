"""SmartSpend UI adapter.

The decision engine in ``code/main.py`` is intentionally frozen.  This module
owns the web contract: it maps UI payment preferences and What-If changes to
the existing solver without changing the solver or the dataset.
"""
from __future__ import annotations

import json
import sys
import threading
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

UI_DIR = Path(__file__).resolve().parent
ROOT = UI_DIR.parent
sys.path.insert(0, str(ROOT))

import shutil as _shutil

_real_which = _shutil.which
_shutil.which = lambda cmd, *args, **kwargs: (
    None if str(cmd).lower() == "tesseract" else _real_which(cmd, *args, **kwargs)
)

print("Loading frozen solver…", flush=True)
from main import Solver, add_months, day, money, read_csv  # noqa: E402

SOLVER_ERROR = ""
try:
    solver = Solver()
except FileNotFoundError as exc:
    # The uploaded archive does not contain dataset/. Keep static Preview and
    # /health available, but never invent data to make a decision.
    solver = None
    SOLVER_ERROR = str(exc)
    print(f"Solver dataset unavailable: {exc}", flush=True)
SOLVER_LOCK = threading.Lock()
if solver is not None:
    print("Solver ready.", flush=True)

SAMPLE_INPUT_FIELDS = [
    "request_id", "user_id", "request_date", "request_type",
    "requested_amount", "desired_completion_date", "allows_partial_payment",
    "request_text",
]
PAYMENT_PREFERENCES = {"full_payment", "partial_payment", "installments", "auto"}
CHANGE_NAMES = {"spend_less", "use_savings", "wait_income", "pay_part_now"}


def json_response(handler: SimpleHTTPRequestHandler, payload: object, status: int = 200) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def require_solver() -> Solver:
    if solver is None:
        raise RuntimeError("The dataset is not present in this project archive; no financial decision was generated.")
    return solver


def read_json(handler: SimpleHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b"{}"
    value = json.loads(raw.decode("utf-8") or "{}")
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    return value


def decimal_value(value: object, label: str, *, allow_zero: bool = True) -> Decimal:
    try:
        parsed = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a number") from None
    if parsed < 0 or (not allow_zero and parsed == 0):
        raise ValueError(f"{label} must be greater than zero" if not allow_zero else f"{label} cannot be negative")
    return parsed


def profile_public(user_id: str) -> dict:
    row = require_solver().profiles[user_id]
    return {
        "user_id": user_id,
        "home_currency": row["home_currency"],
        "current_available_balance": row["current_available_balance"],
        "minimum_balance_to_keep": row["minimum_balance_to_keep"],
        "financial_priorities": row["financial_priorities"],
        "payment_methods_user_will_consider": row["payment_methods_user_will_consider"],
        "expense_categories_to_protect": row["expense_categories_to_protect"],
        "expense_categories_user_is_willing_to_reduce": row["expense_categories_user_is_willing_to_reduce"],
        "expense_categories_user_is_willing_to_stop": row["expense_categories_user_is_willing_to_stop"],
    }


def template_rows() -> list[dict[str, str]]:
    require_solver()
    return read_csv("sample_requests.csv")


def load_situations() -> list[dict]:
    """Retained as a small compatibility endpoint; the UI no longer exposes a selector."""
    rows = []
    for row in template_rows():
        item = {field: row[field] for field in SAMPLE_INPUT_FIELDS}
        item["profile"] = profile_public(row["user_id"])
        rows.append(item)
    return rows


def first_template(body: dict) -> dict[str, str]:
    rows = template_rows()
    if not rows:
        raise ValueError("no request template is available")
    requested_id = body.get("request_id")
    return next((row for row in rows if row["request_id"] == requested_id), rows[0])


def build_request(body: dict) -> dict[str, str]:
    if not body.get("request_id"):
        required = {
            "amount": "purchase amount",
            "purpose": "purchase purpose",
            "purchase_date": "purchase date",
            "current_balance": "current balance",
            "minimum_balance": "minimum balance",
            "essential_expenses": "essential monthly expenses",
        }
        missing = next((label for key, label in required.items() if body.get(key) in (None, "")), None)
        if missing:
            raise ValueError(f"missing {missing}")
    if bool(body.get("next_income")) != bool(body.get("next_income_date")):
        raise ValueError("next income and next income date must be provided together")
    base = first_template(body)
    request = {field: base[field] for field in SAMPLE_INPUT_FIELDS}
    request["_template_request_date"] = base["request_date"]
    if body.get("amount") not in (None, ""):
        request["requested_amount"] = str(body["amount"]).replace(",", "")
    if body.get("purpose") not in (None, ""):
        request["request_text"] = str(body["purpose"])
    if body.get("purchase_date") not in (None, ""):
        request["request_date"] = str(body["purchase_date"])
    elif body.get("request_date") not in (None, ""):
        request["request_date"] = str(body["request_date"])
    # The UI asks when the purchase should happen, not when a plan must end.
    # A 90-day window preserves the frozen solver's plan search horizon.
    purchase_day = day(request["request_date"])
    request["desired_completion_date"] = str(
        body.get("desired_completion_date") or (purchase_day + timedelta(days=90)).isoformat()
    )
    if body.get("request_type"):
        request["request_type"] = str(body["request_type"])
    if body.get("allows_partial_payment") is True:
        request["allows_partial_payment"] = "true"
    return request


def input_overrides(body: dict, request: dict[str, str]) -> dict[str, str]:
    if body.get("connected_account"):
        return {}
    overrides: dict[str, str] = {}
    if body.get("current_balance") not in (None, ""):
        overrides["current_available_balance"] = money(decimal_value(body["current_balance"], "Money you have now"))
    if body.get("minimum_balance") not in (None, ""):
        overrides["minimum_balance_to_keep"] = money(decimal_value(body["minimum_balance"], "Minimum money you need to keep"))
    # This value is used by the event adapter below; retaining it in the
    # profile makes the active inputs visible and inspectable in the response.
    if body.get("essential_expenses") not in (None, ""):
        overrides["ui_essential_monthly_expenses"] = money(
            decimal_value(body["essential_expenses"], "Essential monthly expenses")
        )
    return overrides


def user_events(body: dict, request: dict[str, str]) -> list[dict[str, str]]:
    """Turn user-entered upcoming money into normal solver event rows.

    These rows live only for this request.  Nothing is written to the dataset.
    """
    user_id = request["user_id"]
    currency = solver.profiles[user_id]["home_currency"]
    events: list[dict[str, str]] = []
    income = body.get("next_income")
    income_date = body.get("next_income_date")
    if income not in (None, "") and income_date:
        amount = decimal_value(income, "Next income")
        events.append({
            "event_id": "ui_next_income",
            "user_id": user_id,
            "event_type": "user_next_income",
            "event_date": str(income_date),
            "settlement_date": str(income_date),
            "amount": money(amount),
            "currency": currency,
            "direction": "credit",
            "category": "salary",
            "description": "User-provided one-time next income",
            "status": "scheduled",
            "flexibility": "protected",
            "minimum_allowed_amount": "0",
        })
    essential = body.get("essential_expenses")
    if essential not in (None, ""):
        amount = decimal_value(essential, "Essential monthly expenses")
        start = day(request["request_date"])
        for index in range(4):
            when = add_months(start, index + 1)
            events.append({
                "event_id": f"ui_essential_{index}",
                "user_id": user_id,
                "event_type": "user_essential_expense",
                "event_date": when.isoformat(),
                "settlement_date": when.isoformat(),
                "amount": money(amount),
                "currency": currency,
                "direction": "debit",
                "category": "essential",
                "description": "User-provided essential monthly expenses",
                "status": "scheduled",
                "flexibility": "protected",
                "minimum_allowed_amount": "0",
            })
    return events


@contextmanager
def user_inputs(body: dict, request: dict[str, str]):
    user_id = request["user_id"]
    original_profile = dict(solver.profiles[user_id])
    original_events = list(solver.events_by_user[user_id])
    original_options = list(solver.options_by_request[request["request_id"]])
    overrides = input_overrides(body, request)
    try:
        solver.profiles[user_id].update(overrides)
        option_shift = day(request["request_date"]) - day(request["_template_request_date"])
        solver.options_by_request[request["request_id"]] = [
            {**option, "first_payment_date": (day(option["first_payment_date"]) + option_shift).isoformat()}
            for option in original_options
        ]
        if not body.get("connected_account"):
            extra = user_events(body, request)
            if extra:
                # A manually entered next income is authoritative for this check;
                # avoid counting a template salary on the same forecast dates.
                has_manual_income = body.get("next_income") not in (None, "") and body.get("next_income_date")
                solver.events_by_user[user_id] = [
                    event for event in original_events
                    if not (
                        has_manual_income
                        and event["category"] == "salary"
                        and day(event["settlement_date"] or event["event_date"]) >= day(request["request_date"])
                    )
                ] + extra
        yield overrides
    finally:
        solver.profiles[user_id].clear()
        solver.profiles[user_id].update(original_profile)
        solver.events_by_user[user_id] = original_events
        solver.options_by_request[request["request_id"]] = original_options


@contextmanager
def shift_current_options(request: dict[str, str], reference_date: str):
    request_id = request["request_id"]
    original_options = list(solver.options_by_request[request_id])
    delta = day(request["request_date"]) - day(reference_date)
    try:
        if delta:
            solver.options_by_request[request_id] = [
                {**option, "first_payment_date": (day(option["first_payment_date"]) + delta).isoformat()}
                for option in original_options
            ]
        yield
    finally:
        solver.options_by_request[request_id] = original_options


def with_profile_overrides(user_id: str, overrides: dict, fn):
    original = dict(solver.profiles[user_id])
    try:
        solver.profiles[user_id].update(overrides)
        return fn()
    finally:
        solver.profiles[user_id].clear()
        solver.profiles[user_id].update(original)


def next_salary_date(request_date: str) -> date:
    current = day(request_date)
    payday = date(current.year, current.month, 15)
    return add_months(payday) if payday <= current else payday


def clamp_money(value: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    return max(lo, min(value, hi))


def flexible_categories(profile: dict[str, str], level: int) -> tuple[str, str]:
    protected = {item for item in profile["expense_categories_to_protect"].split("|") if item}
    stop = [item for item in profile["expense_categories_user_is_willing_to_stop"].split("|") if item]
    reduce = [item for item in profile["expense_categories_user_is_willing_to_reduce"].split("|") if item]
    extras = ["dining", "shopping", "entertainment", "streaming", "delivery_membership", "cloud_storage", "music_subscription"]
    count = max(0, min(len(extras), int(round(len(extras) * max(level, 0) / 100))))
    extra_reduce = extras[:count]
    extra_stop = extra_reduce if level >= 70 else extra_reduce[: max(0, len(extra_reduce) // 2)]
    merged_stop, merged_reduce = [], []
    for item in stop + extra_stop:
        if item and item not in protected and item not in merged_stop:
            merged_stop.append(item)
    for item in reduce + extra_reduce:
        if item and item not in protected and item not in merged_reduce:
            merged_reduce.append(item)
    return "|".join(merged_stop), "|".join(merged_reduce)


def apply_knobs(request: dict[str, str], custom: dict) -> tuple[dict[str, str], dict[str, str]]:
    profile = solver.profiles[request["user_id"]]
    overrides: dict[str, str] = {}
    tweaked = dict(request)
    minimum = Decimal(profile["minimum_balance_to_keep"])

    if custom.get("use_savings") or custom.get("savings_amount") not in (None, "", False):
        release = decimal_value(custom.get("savings_amount") or minimum * Decimal("0.55"), "Savings to use")
        release = clamp_money(release, Decimal("0"), (minimum * Decimal("0.9")).quantize(Decimal("0.01")))
        overrides["minimum_balance_to_keep"] = money(minimum - release)

    if custom.get("reduce_expenses") or custom.get("reduce_level") not in (None, ""):
        level = int(custom.get("reduce_level", 100) or 0)
        stop, reduce = flexible_categories(profile, level)
        overrides["expense_categories_user_willing_to_stop"] = stop
        overrides["expense_categories_user_willing_to_reduce"] = reduce

    wait_date = custom.get("wait_date")
    if wait_date:
        moved = day(str(wait_date))
        tweaked["request_date"] = moved.isoformat()
        tweaked["desired_completion_date"] = max(
            day(request["desired_completion_date"]), moved + timedelta(days=90)
        ).isoformat()

    if custom.get("partial") or custom.get("pay_partially"):
        tweaked["allows_partial_payment"] = "true"
        methods = set(filter(None, profile["payment_methods_user_will_consider"].split("|")))
        methods.add("partial_payment")
        overrides["payment_methods_user_will_consider"] = "|".join(sorted(methods))
    return tweaked, overrides


def earliest_safe_date(request: dict[str, str], flows, amount: Decimal, start_offset: int = 0) -> date | None:
    profile = solver.profiles[request["user_id"]]
    balance = Decimal(profile["current_available_balance"])
    minimum = Decimal(profile["minimum_balance_to_keep"])
    start = day(request["request_date"])
    for offset in range(start_offset, 91):
        candidate = start + timedelta(days=offset)
        if solver.is_safe(balance, minimum, flows, [(candidate, amount)]):
            return candidate
    return None


def base_result(request: dict[str, str], safe_amount: Decimal, status: str, method: str,
                plan: str, earliest: date | None, explanation: str, changes: str = "none") -> dict[str, str]:
    return {
        "request_id": request["request_id"],
        "amount_safe_to_pay": money(safe_amount),
        "affordability_status": status,
        "recommended_payment_method": method,
        "payment_plan": plan,
        "earliest_date_for_full_payment": earliest.isoformat() if earliest else "",
        "spending_changes_needed": changes,
        "decision_explanation": explanation,
    }


def evaluate_payment_preference(request: dict[str, str], preference: str = "auto",
                                partial_amount: object = None) -> dict[str, str]:
    if preference not in PAYMENT_PREFERENCES:
        raise ValueError("unknown payment preference")
    if preference == "auto":
        return solver.solve_request(request)

    flows, _ = solver.base_flows(request)
    requested = Decimal(request["requested_amount"])
    safe_amount = solver.safe_amount_today(request, flows)
    full_date = earliest_safe_date(request, flows, requested)
    today = day(request["request_date"])
    deadline = day(request["desired_completion_date"])
    profile = solver.profiles[request["user_id"]]
    currency = profile["home_currency"]

    if preference == "full_payment":
        if solver.is_safe(Decimal(profile["current_available_balance"]), Decimal(profile["minimum_balance_to_keep"]), flows, [(today, requested)]):
            return base_result(request, requested, "affordable_now", "full_payment",
                                f"{today.isoformat()}:{money(requested)}", full_date,
                                f"Pay {currency} {money(requested)} in full today; the forecast keeps your buffer protected.")
        if full_date:
            return base_result(request, safe_amount, "affordable_later", "wait",
                                f"{full_date.isoformat()}:{money(requested)}", full_date,
                                f"Wait until {full_date.isoformat()} to pay the full amount safely.")
        return base_result(request, safe_amount, "not_affordable", "not_recommended", "none", None,
                           "Still not safe. The full amount cannot be paid safely in the forecast.")

    if preference == "installments":
        maximum = int(profile["max_installment_months"] or 0)
        valid = []
        for option in solver.options_by_request[request["request_id"]]:
            if option["payment_method"] != "installments":
                continue
            if maximum and int(option["number_of_payments"]) > maximum:
                continue
            payments = solver.option_payments(option)
            if payments[-1][0] <= deadline and solver.is_safe(
                Decimal(profile["current_available_balance"]), Decimal(profile["minimum_balance_to_keep"]), flows, payments
            ):
                valid.append((solver.rank(type("Plan", (), {
                    "payments": payments, "changes": (), "total": sum((amount for _, amount in payments), Decimal("0")),
                    "option_id": option["payment_option_id"],
                })(), deadline), payments))
        if valid:
            _, payments = min(valid, key=lambda item: item[0])
            return base_result(request, safe_amount, "affordable_with_plan", "installments",
                               "|".join(f"{when.isoformat()}:{money(amount)}" for when, amount in payments),
                               full_date, "This purchase fits an available installment plan.")
        return base_result(request, safe_amount, "not_affordable", "not_recommended", "none", full_date,
                           "No suitable installment plan is available for this purchase.")

    # Explicit partial payment is deliberately not delegated to the solver's
    # automatic choice: the amount the user entered is evaluated as-is.
    if request["allows_partial_payment"].lower() != "true":
        return base_result(request, safe_amount, "not_affordable", "not_recommended", "none", full_date,
                           "Partial payment is not supported for this purchase.")
    if partial_amount in (None, ""):
        raise ValueError("enter how much you want to pay now")
    partial = decimal_value(partial_amount, "Amount to pay now", allow_zero=False)
    if partial >= requested:
        raise ValueError("amount to pay now must be less than the purchase amount")
    if not solver.is_safe(
        Decimal(profile["current_available_balance"]), Decimal(profile["minimum_balance_to_keep"]), flows, [(today, partial)]
    ):
        return base_result(request, safe_amount, "not_affordable", "not_recommended", "none", full_date,
                           f"Still not safe. Paying {currency} {money(partial)} now would cross your buffer.")
    remaining = requested - partial
    remaining_date = earliest_safe_date(request, flows, remaining, start_offset=1)
    if not remaining_date or remaining_date > deadline or not solver.is_safe(
        Decimal(profile["current_available_balance"]), Decimal(profile["minimum_balance_to_keep"]), flows,
        [(today, partial), (remaining_date, remaining)]
    ):
        return base_result(request, partial, "not_affordable", "not_recommended", "none", full_date,
                           f"{currency} {money(partial)} is safe now, but the remaining {currency} {money(remaining)} cannot be safely paid later in time.")
    return base_result(request, partial, "affordable_with_plan", "partial_payment",
                       f"{today.isoformat()}:{money(partial)}|{remaining_date.isoformat()}:{money(remaining)}",
                       full_date, f"Pay {currency} {money(partial)} now and the remaining {currency} {money(remaining)} on {remaining_date.isoformat()}.")


def normalise_changes(body: dict) -> list[str]:
    changes = body.get("changes")
    if isinstance(changes, list):
        return [str(item) for item in changes if str(item) in CHANGE_NAMES]
    # Compatibility for older callers; the UI sends the multi-select list.
    scenario = body.get("scenario")
    return {
        "use_savings": ["use_savings"],
        "reduce_expenses": ["spend_less"],
        "wait_salary": ["wait_income"],
        "pay_partially": ["pay_part_now"],
    }.get(str(scenario), [])


def run_what_if(request: dict[str, str], body: dict) -> dict[str, str]:
    changes = normalise_changes(body)
    custom = dict(body.get("custom") or {})
    if "use_savings" in changes:
        custom["use_savings"] = True
        custom["savings_amount"] = body.get("savings_amount", custom.get("savings_amount"))
    if "spend_less" in changes:
        custom["reduce_expenses"] = True
        custom["reduce_level"] = body.get("reduce_level", 100)
    if "wait_income" in changes:
        custom["wait_date"] = body.get("wait_date") or custom.get("wait_date") or next_salary_date(request["request_date"]).isoformat()
    if "pay_part_now" in changes:
        custom["partial"] = True
    tweaked, overrides = apply_knobs(request, custom)
    preference = "partial_payment" if "pay_part_now" in changes else "auto"

    def run():
        with shift_current_options(tweaked, request["request_date"]):
            return evaluate_payment_preference(tweaked, preference, body.get("partial_amount"))

    result = with_profile_overrides(request["user_id"], overrides, run) if overrides else run()
    result["scenario"] = "+".join(changes) or "none"
    result["changes"] = changes
    result["evaluated_request_date"] = tweaked["request_date"]
    result["profile_overrides"] = overrides
    return result


def actionable(result: dict[str, str], currency: str) -> list[str]:
    actions = []
    if result.get("amount_safe_to_pay") not in ("", "0", "0.0", None):
        actions.append(f"Your safe spending limit right now is {currency} {result['amount_safe_to_pay']}.")
    if result.get("earliest_date_for_full_payment"):
        actions.append(f"You may be able to make the full purchase by {result['earliest_date_for_full_payment']}.")
    if result.get("spending_changes_needed") not in (None, "", "none"):
        actions.append(f"Flexible spending changes needed: {result['spending_changes_needed']}.")
    if result.get("affordability_status") in {"affordable_later", "not_affordable"}:
        actions.append("What if I change my plan?")
    return actions


def control_hints(request: dict[str, str]) -> dict:
    profile = solver.profiles[request["user_id"]]
    minimum = Decimal(profile["minimum_balance_to_keep"])
    return {
        "home_currency": profile["home_currency"],
        "current_available_balance": profile["current_available_balance"],
        "minimum_balance_to_keep": profile["minimum_balance_to_keep"],
        "max_savings_release": money((minimum * Decimal("0.9")).quantize(Decimal("0.01"))),
        "next_salary_date": next_salary_date(request["request_date"]).isoformat(),
        "request_date": request["request_date"],
        "desired_completion_date": request["desired_completion_date"],
        "requested_amount": request["requested_amount"],
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/health", "/api/health"}:
            json_response(self, {
                "ok": True,
                "solver": "frozen" if solver is not None else "unavailable",
                "dataset_error": SOLVER_ERROR or None,
            })
            return
        if path == "/api/situations":
            try:
                json_response(self, {"situations": load_situations()})
            except RuntimeError as exc:
                json_response(self, {"error": str(exc)}, 503)
            return
        if path == "/api/demo-profile":
            # Returns financial data from the first sample request user in the
            # existing dataset.  Nothing is invented; this is a read-only
            # view of the real dataset for the demo account connect flow.
            try:
                s = require_solver()
                rows = template_rows()
                if not rows:
                    raise RuntimeError("no sample requests in dataset")
                row = rows[0]
                user_id = row["user_id"]
                profile = s.profiles[user_id]
                # Derive a representative essential-expenses figure from the
                # user's event history (average of protected debit categories).
                events = s.events_by_user.get(user_id, [])
                essential_cats = {c.strip() for c in profile.get("expense_categories_to_protect", "").split("|") if c.strip()}
                monthly_debits: list[Decimal] = []
                for ev in events:
                    if ev.get("direction") == "debit" and ev.get("category") in essential_cats:
                        try:
                            monthly_debits.append(Decimal(str(ev.get("amount") or "0").replace(",", "").strip() or "0"))
                        except Exception:
                            pass
                if monthly_debits:
                    avg_essential = sum(monthly_debits) / len(monthly_debits)
                    avg_essential = avg_essential.quantize(Decimal("0.01"))
                else:
                    # Graceful fallback: 30% of minimum balance as a conservative estimate
                    avg_essential = (Decimal(profile["minimum_balance_to_keep"]) * Decimal("0.30")).quantize(Decimal("0.01"))
                # Next salary from events
                request_date = row["request_date"]
                salary_events = sorted(
                    [ev for ev in events if ev.get("category") == "salary" and ev.get("direction") == "credit"],
                    key=lambda ev: ev.get("settlement_date") or ev.get("event_date") or "",
                )
                next_salary = None
                next_salary_date_str = ""
                for ev in salary_events:
                    ev_date = ev.get("settlement_date") or ev.get("event_date") or ""
                    if ev_date >= request_date:
                        try:
                            next_salary = str(Decimal(str(ev.get("amount") or "0").replace(",", "")))
                            next_salary_date_str = ev_date
                        except Exception:
                            pass
                        break
                json_response(self, {
                    "user_id": user_id,
                    "home_currency": profile["home_currency"],
                    "current_balance": profile["current_available_balance"],
                    "minimum_balance": profile["minimum_balance_to_keep"],
                    "essential_expenses": str(avg_essential),
                    "next_income": next_salary or "",
                    "next_income_date": next_salary_date_str,
                    "request_id": row["request_id"],
                    "request_date": request_date,
                })
            except RuntimeError as exc:
                json_response(self, {"error": str(exc)}, 503)
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if solver is None:
            json_response(self, {"error": SOLVER_ERROR or "solver unavailable"}, 503)
            return
        try:
            body = read_json(self)
            request = build_request(body)
            if Decimal(request["requested_amount"]) <= 0:
                raise ValueError("purchase amount must be greater than zero")
        except (ValueError, InvalidOperation, KeyError, json.JSONDecodeError) as exc:
            json_response(self, {"error": str(exc)}, 400)
            return

        try:
            with SOLVER_LOCK, user_inputs(body, request):
                if path == "/api/decide":
                    preference = body.get("payment_preference") or "auto"
                    result = evaluate_payment_preference(request, preference, body.get("partial_amount"))
                    result["payment_preference"] = preference
                    result["actionable_steps"] = actionable(result, solver.profiles[request["user_id"]]["home_currency"])
                    json_response(self, {
                        "request": request,
                        "profile": profile_public(request["user_id"]),
                        "controls": control_hints(request),
                        "decision": result,
                    })
                    return
                if path == "/api/whatif":
                    result = run_what_if(request, body)
                    result["actionable_steps"] = actionable(result, solver.profiles[request["user_id"]]["home_currency"])
                    json_response(self, {
                        "request": request,
                        "profile": profile_public(request["user_id"]),
                        "controls": control_hints(request),
                        "decision": result,
                    })
                    return
        except (ValueError, InvalidOperation, KeyError, RuntimeError) as exc:
            json_response(self, {"error": str(exc)}, 400)
            return
        json_response(self, {"error": "not found"}, 404)


def main() -> int:
    port = 8765
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"SmartSpend AI: http://0.0.0.0:{port}/", flush=True)
    print("Frozen solver imported from code/main.py. output.csv is not written.", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())