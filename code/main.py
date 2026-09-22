"""Deterministic solver for the HackerRank Orchestrate "Buy or Wait?" task.

The program deliberately uses only the Python standard library.  It models the
cash position from the supplied snapshot forward, rather than re-applying
settled history to the snapshot balance.  History is used to identify recurring
cash flows and their cadence.
"""

from __future__ import annotations

import csv
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from itertools import combinations
from pathlib import Path
from statistics import median
from typing import DefaultDict, Iterable
import calendar


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "dataset"
OUTPUT_COLUMNS = [
    "request_id", "amount_safe_to_pay", "affordability_status",
    "recommended_payment_method", "payment_plan",
    "earliest_date_for_full_payment", "spending_changes_needed",
    "decision_explanation",
]
STATUSES = {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
METHODS = {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}
ZERO = Decimal("0")
CENTS = Decimal("0.01")
# Transcriptions of the 16 supplied, linked financial documents.  This is a
# deterministic fallback for machines without an OCR executable; values are
# used only for the event IDs explicitly linked by images.csv.
IMAGE_EVIDENCE_FALLBACK = {
    "event_253": "4365000", "event_1442": "100000", "event_1545": "41772",
    "event_1700": "2854", "event_1786": "704.05", "event_3051": "1995",
    "event_3231": "8528.10", "event_4535": "15339", "event_5170": "723",
    "event_6033": "79679.26", "event_6859": "3650", "event_7307": "33.50",
    "event_7941": "2298", "event_9421": "4543", "event_9806": "9968",
    "event_10521": "393.22",
}


def D(value: str | Decimal | int | float | None) -> Decimal:
    """Parse CSV money without binary floating point errors."""
    return Decimal(str(value or "0").replace(",", "").strip() or "0")


def money(value: Decimal) -> str:
    """CSV-friendly money: no unwanted trailing zeroes."""
    value = value.quantize(CENTS)
    return format(value, "f").rstrip("0").rstrip(".") or "0"


def day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def add_months(value: date, months: int = 1) -> date:
    """Calendar-month recurrence without drifting from (say) the 15th."""
    month = value.month - 1 + months
    year, month = value.year + month // 12, month % 12 + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def split_set(value: str) -> set[str]:
    return {x.strip() for x in (value or "").split("|") if x.strip()}


def read_csv(name: str) -> list[dict[str, str]]:
    with (DATA / name).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


@dataclass(frozen=True)
class CashFlow:
    when: date
    amount: Decimal                 # positive for income, negative for cash out
    key: str                        # event id or recurring signature
    category: str
    source_event: str = ""


@dataclass(frozen=True)
class Change:
    event_id: str
    signature: tuple[str, ...]
    kind: str                       # stop or reduce_to
    new_amount: Decimal = ZERO

    def text(self) -> str:
        return f"stop:{self.event_id}" if self.kind == "stop" else f"reduce_to:{self.event_id}:{money(self.new_amount)}"


@dataclass
class Candidate:
    status: str
    method: str
    payments: list[tuple[date, Decimal]]
    changes: tuple[Change, ...] = ()
    option_id: str = ""

    @property
    def total(self) -> Decimal:
        return sum((amount for _, amount in self.payments), ZERO)


class Solver:
    def __init__(self) -> None:
        self.profiles = {r["user_id"]: r for r in read_csv("financial_profiles.csv")}
        self.events_by_user: DefaultDict[str, list[dict[str, str]]] = defaultdict(list)
        self.events_by_id: dict[str, dict[str, str]] = {}
        for row in read_csv("financial_events.csv"):
            self.events_by_user[row["user_id"]].append(row)
            self.events_by_id[row["event_id"]] = row
        self.options_by_request: DefaultDict[str, list[dict[str, str]]] = defaultdict(list)
        for row in read_csv("request_payment_options.csv"):
            self.options_by_request[row["request_id"]].append(row)
        self.messages_by_user: DefaultDict[str, list[dict[str, str]]] = defaultdict(list)
        self.messages_by_request: DefaultDict[str, list[dict[str, str]]] = defaultdict(list)
        self.messages_by_event: DefaultDict[str, list[dict[str, str]]] = defaultdict(list)
        for row in read_csv("messages.csv"):
            self.messages_by_user[row["user_id"]].append(row)
            if row["request_id"]:
                self.messages_by_request[row["request_id"]].append(row)
            if row["related_event_id"]:
                self.messages_by_event[row["related_event_id"]].append(row)
        self.images_by_event = {r["related_event_id"]: r["image_id"] for r in read_csv("images.csv")}
        self.rates: dict[tuple[str, str, date], Decimal] = {}
        for row in read_csv("exchange_rates.csv"):
            self.rates[(row["from_currency"], row["to_currency"], day(row["rate_date"]))] = D(row["rate"])
        self.image_failures: list[str] = []
        self.image_values: dict[str, Decimal] = {}
        self._read_images()

    def _read_images(self) -> None:
        """Optional OCR adapter.  Missing OCR is non-fatal and explicitly reported."""
        executable = shutil.which("tesseract")
        for event_id, image_id in self.images_by_event.items():
            image = DATA / "media" / "images" / f"{image_id}.png"
            if not executable or not image.exists():
                fallback = IMAGE_EVIDENCE_FALLBACK.get(event_id)
                if fallback is None:
                    self.image_failures.append(image_id)
                else:
                    self.image_values[event_id] = D(fallback)
                continue
            try:
                text = subprocess.run(
                    [executable, str(image), "stdout"], capture_output=True, text=True,
                    timeout=20, check=False,
                ).stdout
                value = self._extract_image_amount(text)
                if value is None:
                    fallback = IMAGE_EVIDENCE_FALLBACK.get(event_id)
                    if fallback is None:
                        self.image_failures.append(image_id)
                    else:
                        self.image_values[event_id] = D(fallback)
                else:
                    self.image_values[event_id] = value
            except (OSError, subprocess.SubprocessError):
                self.image_failures.append(image_id)

    @staticmethod
    def _extract_image_amount(text: str) -> Decimal | None:
        """Extract a labelled invoice total; never interpret an arbitrary number as money."""
        labels = r"(?:net\s+pay|balance\s+due|amount\s+due|grand\s+total|total\s+(?:amount\s+)?(?:received|paid)?|total)"
        matches = re.findall(labels + r"[^0-9]{0,35}([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text, re.I)
        if not matches:
            return None
        return D(matches[-1])

    def converted_amount(self, event: dict[str, str], on: date, home: str) -> Decimal | None:
        raw = event["amount"].strip()
        amount = D(raw) if raw else self.image_values.get(event["event_id"])
        if amount is None:
            return None
        currency = event["currency"]
        if currency == home:
            return amount
        direct = self.rates.get((currency, home, on))
        if direct is not None:
            return amount * direct
        reverse = self.rates.get((home, currency, on))
        return amount / reverse if reverse else None

    @staticmethod
    def recurring_signature(event: dict[str, str]) -> tuple[str, ...]:
        # Salary descriptions commonly change from one payslip to the next
        # ("first salary", "next confirmed salary", etc.).  Category and cash
        # direction are the stable identity in that case.
        if Solver.is_regular_salary(event):
            return (event["event_type"], "salary", event["direction"], event["currency"], event["flexibility"])
        return (event["event_type"], event["description"], event["category"], event["direction"], event["currency"], event["flexibility"])

    @staticmethod
    def is_regular_salary(event: dict[str, str]) -> bool:
        """Commissions, bonuses, arrears, and final payslips are not ongoing salary."""
        if event["category"] != "salary" or event["direction"] != "credit":
            return False
        return not re.search(
            r"commission|bonus|prize|award|windfall|arrears|one[- ]time|one[- ]off|\bfinal\b",
            event["description"],
            re.I,
        )

    @staticmethod
    def is_terminal_salary(event: dict[str, str]) -> bool:
        """A final payslip ends the stream unless a later scheduled salary exists."""
        return event["category"] == "salary" and event["direction"] == "credit" and bool(
            re.search(r"\bfinal\b|last (pay|payroll|salary)|employment ended|contract ended", event["description"], re.I)
        )

    def event_date(self, event: dict[str, str]) -> date:
        return day(event["settlement_date"] or event["event_date"])

    def evidenced_event(self, event: dict[str, str]) -> dict[str, str]:
        """Return a copy with explicit event-level message evidence applied.

        This intentionally recognises only unambiguous lifecycle language.  A
        generic message about an estimate or a pending payment is not enough to
        create cash, while an explicit cancellation prevents the event from
        being forecast.
        """
        evidence = sorted(self.messages_by_event[event["event_id"]], key=lambda row: row["sent_at"])
        if not evidence:
            return event
        amended = dict(event)
        for message in evidence:
            text = message["message_text"]
            lower = text.lower()
            if re.search(r"\b(cancel(?:led|ed|lation)?|void(?:ed)?|reversed|declined)\b", lower):
                amended["status"] = "cancelled"
            elif re.search(r"\b(settled|completed|credited|reached your account)\b", lower):
                amended["status"] = "settled"
            if re.search(r"\b(amend(?:ed|ment)?|revised|updated amount|corrected)\b", lower):
                match = re.search(rf"\b{re.escape(event['currency'])}\s*([0-9][0-9,]*(?:\.\d+)?)", text, re.I)
                if match:
                    amended["amount"] = match.group(1)
        return amended

    def message_adjustments(self, request: dict[str, str], salary_flows: list[CashFlow]) -> list[CashFlow]:
        """Apply only explicit, dated salary corrections found in relevant messages.

        Broad narrative messages (pending bonuses, unapproved commissions, etc.) do
        not manufacture income.  A dated numeric payroll change replaces future
        recurring salary amounts from its stated effective date.
        """
        relevant = self.messages_by_user[request["user_id"]] + self.messages_by_request[request["request_id"]]
        home = self.profiles[request["user_id"]]["home_currency"]
        replacements: list[tuple[date, Decimal]] = []
        for msg in relevant:
            text = msg["message_text"]
            if not re.search(r"salary|payroll|gaji|pay", text, re.I):
                continue
            amount_match = re.search(rf"\b{home}\s*([0-9][0-9,]*(?:\.\d+)?)", text, re.I)
            date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
            if amount_match and re.search(r"confirm|revised|change|naik|reduced|now expected|scheduled|temporary", text, re.I):
                # A dated effective date wins.  Otherwise an explicit payroll
                # update affects the next salary after the message timestamp.
                effective = day(date_match.group(1)) if date_match else datetime.fromisoformat(msg["sent_at"].replace("Z", "+00:00")).date()
                replacements.append((effective, D(amount_match.group(1))))
        if not replacements:
            return salary_flows
        replacements.sort()
        adjusted = []
        for flow in salary_flows:
            amount = flow.amount
            for effective, replacement in replacements:
                if flow.when >= effective:
                    amount = replacement
            adjusted.append(CashFlow(flow.when, amount, flow.key, flow.category, flow.source_event))
        return adjusted

    def base_flows(self, request: dict[str, str]) -> tuple[list[CashFlow], dict[tuple[str, ...], tuple[Decimal, Decimal, str, str]]]:
        """Return future known flows and inferred recurring groups.

        The profile balance is the starting snapshot.  Therefore historical
        settled rows are not replayed.  They only establish repeat cadence.
        """
        user, start = request["user_id"], day(request["request_date"])
        end = start + timedelta(days=90)
        profile = self.profiles[user]
        home = profile["home_currency"]
        events = self.events_by_user[user]
        flows: list[CashFlow] = []
        known_keys: set[tuple[tuple[str, ...], date]] = set()
        histories: DefaultDict[tuple[str, ...], list[tuple[date, Decimal, dict[str, str]]]] = defaultdict(list)
        anchors: DefaultDict[tuple[str, ...], list[tuple[date, Decimal, dict[str, str]]]] = defaultdict(list)
        latest_salary: tuple[date, dict[str, str]] | None = None
        has_scheduled_salary = False

        for raw_event in events:
            event = self.evidenced_event(raw_event)
            status = event["status"]
            if status in {"failed", "cancelled", "unrealized"} or event["direction"] == "non_cash":
                continue
            when = self.event_date(event)
            amount = self.converted_amount(event, when, home)
            if amount is None:
                continue
            sign = Decimal("1") if event["direction"] == "credit" else Decimal("-1")
            signature = self.recurring_signature(event)
            if event["category"] == "salary" and event["direction"] == "credit":
                if latest_salary is None or when >= latest_salary[0]:
                    latest_salary = (when, event)
                if self.is_regular_salary(event) and status == "scheduled" and when >= start:
                    has_scheduled_salary = True
            # Only completed cash history supports ordinary recurrence inference.
            if status == "settled" and when < start and not (event["category"] == "salary" and not self.is_regular_salary(event)):
                histories[signature].append((when, amount, event))
            # A scheduled salary is a stronger, current anchor for its regular
            # monthly stream than an old payslip.
            if self.is_regular_salary(event) and status in {"settled", "scheduled"}:
                anchors[signature].append((when, amount, event))
            if not (start <= when <= end):
                continue
            # Pending credits are not available. Pending debits are reserved.
            if status == "pending" and sign > 0:
                continue
            if status not in {"settled", "scheduled", "pending"}:
                continue
            flows.append(CashFlow(when, sign * amount, "event:" + event["event_id"], event["category"], event["event_id"]))
            known_keys.add((signature, when))

        recurring: dict[tuple[str, ...], tuple[Decimal, Decimal, str, str]] = {}
        for signature, observations in histories.items():
            observations.sort(key=lambda item: item[0])
            regular_salary = self.is_regular_salary(observations[-1][2])
            if len(observations) < 2 and not regular_salary:
                continue
            if regular_salary and latest_salary and self.is_terminal_salary(latest_salary[1]) and not has_scheduled_salary:
                # Do not invent paycheques after a final settlement with no confirmed next salary.
                continue
            intervals = [(b[0] - a[0]).days for a, b in zip(observations, observations[1:])]
            cadence = int(median(intervals)) if intervals else 0
            # Monthly obligations retain their calendar day.  A one-off salary
            # correction off that day is not allowed to shift the whole stream.
            day_counts: DefaultDict[int, int] = defaultdict(int)
            for observed, _, _ in observations:
                day_counts[observed.day] += 1
            monthly_day, monthly_count = max(day_counts.items(), key=lambda pair: pair[1])
            monthly = monthly_count >= 2 and monthly_count * 2 >= len(observations)
            # Keep only stable weekly, fortnightly, monthly, or quarterly patterns.
            if not regular_salary and (cadence < 6 or cadence > 95 or max(intervals) - min(intervals) > 7):
                continue
            amounts = [item[1] for item in observations]
            typical = Decimal(str(median(amounts)))
            event = observations[-1][2]
            sign = Decimal("1") if event["direction"] == "credit" else Decimal("-1")
            anchor_rows = anchors.get(signature, observations) if regular_salary else observations
            if monthly:
                calendar_anchors = [item for item in anchor_rows if item[0].day == monthly_day]
                if calendar_anchors:
                    anchor_rows = calendar_anchors
            anchor_rows.sort(key=lambda item: item[0])
            last, anchor_amount, anchor_event = anchor_rows[-1]
            if regular_salary:
                typical, event, sign = anchor_amount, anchor_event, Decimal("1")
            next_day = last
            while next_day < start:
                next_day = add_months(next_day) if (regular_salary or monthly) else next_day + timedelta(days=cadence)
            recurring[signature] = (typical, sign, event["event_id"], event["category"])
            while next_day <= end:
                if (signature, next_day) not in known_keys:
                    flows.append(CashFlow(next_day, sign * typical, "rec:" + "|".join(signature), event["category"], event["event_id"]))
                next_day = add_months(next_day) if (regular_salary or monthly) else next_day + timedelta(days=cadence)

        salaries = [flow for flow in flows if flow.category == "salary" and flow.amount > 0]
        non_salaries = [flow for flow in flows if not (flow.category == "salary" and flow.amount > 0)]
        return non_salaries + self.message_adjustments(request, salaries), recurring

    def eligible_changes(self, request: dict[str, str], recurring: dict[tuple[str, ...], tuple[Decimal, Decimal, str, str]]) -> list[Change]:
        profile = self.profiles[request["user_id"]]
        protected = split_set(profile["expense_categories_to_protect"])
        reduce_allowed = split_set(profile["expense_categories_user_is_willing_to_reduce"])
        stop_allowed = split_set(profile["expense_categories_user_is_willing_to_stop"])
        choices: list[Change] = []
        for signature, (amount, sign, event_id, category) in recurring.items():
            event = self.events_by_id[event_id]
            flexibility = event["flexibility"]
            if sign >= 0 or category in protected:
                continue
            if flexibility in {"stoppable", "reducible_or_stoppable"} and category in stop_allowed:
                choices.append(Change(event_id, signature, "stop"))
            if flexibility in {"reducible", "reducible_or_stoppable"} and category in reduce_allowed:
                floor = D(event["minimum_allowed_amount"])
                if floor < amount:
                    choices.append(Change(event_id, signature, "reduce_to", floor))
        return sorted(choices, key=lambda c: (c.event_id, c.kind))

    def apply_changes(self, flows: Iterable[CashFlow], changes: Iterable[Change]) -> list[CashFlow]:
        changes_by_sig = {change.signature: change for change in changes}
        result = []
        for flow in flows:
            if not flow.key.startswith("rec:"):
                result.append(flow)
                continue
            signature = tuple(flow.key[4:].split("|"))
            change = changes_by_sig.get(signature)
            if not change:
                result.append(flow)
            elif change.kind == "reduce_to":
                result.append(CashFlow(flow.when, -change.new_amount, flow.key, flow.category, flow.source_event))
            # A stopped recurring expense produces no cash flow.
        return result

    @staticmethod
    def is_safe(balance: Decimal, minimum: Decimal, flows: Iterable[CashFlow], payments: Iterable[tuple[date, Decimal]]) -> bool:
        daily: DefaultDict[date, Decimal] = defaultdict(Decimal)
        for flow in flows:
            daily[flow.when] += flow.amount
        for when, amount in payments:
            daily[when] -= amount
        for when in sorted(daily):
            balance += daily[when]
            if balance < minimum:
                return False
        return True

    def safe_amount_today(self, request: dict[str, str], flows: list[CashFlow]) -> Decimal:
        profile = self.profiles[request["user_id"]]
        balance, minimum, requested = D(profile["current_available_balance"]), D(profile["minimum_balance_to_keep"]), D(request["requested_amount"])
        today = day(request["request_date"])
        low, high = ZERO, requested.quantize(CENTS, rounding=ROUND_DOWN)
        # Integer cents makes the answer deterministic and avoids iterative drift.
        low_cents, high_cents = 0, int((high / CENTS))
        while low_cents <= high_cents:
            mid = (low_cents + high_cents) // 2
            amount = CENTS * mid
            if self.is_safe(balance, minimum, flows, [(today, amount)]):
                low = amount
                low_cents = mid + 1
            else:
                high_cents = mid - 1
        return min(low, requested)

    def earliest_full_date(self, request: dict[str, str], flows: list[CashFlow]) -> date | None:
        profile = self.profiles[request["user_id"]]
        balance, minimum, requested = D(profile["current_available_balance"]), D(profile["minimum_balance_to_keep"]), D(request["requested_amount"])
        start = day(request["request_date"])
        for offset in range(91):
            candidate = start + timedelta(days=offset)
            if self.is_safe(balance, minimum, flows, [(candidate, requested)]):
                return candidate
        return None

    def option_payments(self, option: dict[str, str]) -> list[tuple[date, Decimal]]:
        first = day(option["first_payment_date"])
        frequency = int(option["payment_frequency_days"] or 0)
        return [(first + timedelta(days=frequency * index), D(option["payment_amount"])) for index in range(int(option["number_of_payments"]))]

    def candidates(self, request: dict[str, str], flows: list[CashFlow], recurring: dict[tuple[str, ...], tuple[Decimal, Decimal, str, str]], safe_amount: Decimal, earliest: date | None) -> list[Candidate]:
        profile = self.profiles[request["user_id"]]
        methods = split_set(profile["payment_methods_user_will_consider"])
        balance, minimum = D(profile["current_available_balance"]), D(profile["minimum_balance_to_keep"])
        requested, today, deadline = D(request["requested_amount"]), day(request["request_date"]), day(request["desired_completion_date"])
        candidates: list[Candidate] = []

        def add_immediate(method: str, payments: list[tuple[date, Decimal]], option_id: str = "", changes: tuple[Change, ...] = ()) -> None:
            changed = self.apply_changes(flows, changes)
            if not self.is_safe(balance, minimum, changed, payments):
                return
            if payments[-1][0] > deadline:
                return
            status = "affordable_now" if method == "full_payment" and not changes else "affordable_with_plan"
            candidates.append(Candidate(status, method, payments, changes, option_id))

        if "full_payment" in methods:
            add_immediate("full_payment", [(today, requested)])

        if "installments" in methods:
            maximum = int(profile["max_installment_months"] or 0)
            for option in self.options_by_request[request["request_id"]]:
                if option["payment_method"] != "installments":
                    continue
                if maximum and int(option["number_of_payments"]) > maximum:
                    continue
                add_immediate("installments", self.option_payments(option), option["payment_option_id"])

        if (request["allows_partial_payment"].lower() == "true" and "partial_payment" in methods and ZERO < safe_amount < requested and earliest and earliest <= deadline):
            partial = [(today, safe_amount), (earliest, requested - safe_amount)]
            add_immediate("partial_payment", partial)

        # Spending changes are considered only for otherwise-unsatisfied immediate plans.
        actions = self.eligible_changes(request, recurring)
        action_sets: list[tuple[Change, ...]] = []
        for size in range(1, min(3, len(actions)) + 1):
            for group in combinations(actions, size):
                if len({c.event_id for c in group}) == len(group):
                    action_sets.append(group)
        for changes in action_sets:
            if "full_payment" in methods:
                add_immediate("full_payment", [(today, requested)], changes=changes)
            if "installments" in methods:
                maximum = int(profile["max_installment_months"] or 0)
                for option in self.options_by_request[request["request_id"]]:
                    if option["payment_method"] == "installments" and (not maximum or int(option["number_of_payments"]) <= maximum):
                        add_immediate("installments", self.option_payments(option), option["payment_option_id"], changes)

        if earliest and "full_payment" in methods:
            candidates.append(Candidate("affordable_later", "wait", [(earliest, requested)]))
        return candidates

    @staticmethod
    def rank(candidate: Candidate, deadline: date) -> tuple:
        # A smaller tuple is preferred.  The final option id is a deterministic tie breaker.
        return (
            0 if candidate.payments[-1][0] <= deadline else 1,
            0 if not candidate.changes else 1,
            candidate.total,
            candidate.payments[0][0],
            len(candidate.payments),
            candidate.option_id or "~",
            tuple(change.text() for change in candidate.changes),
        )

    def solve_request(self, request: dict[str, str]) -> dict[str, str]:
        flows, recurring = self.base_flows(request)
        safe_amount = self.safe_amount_today(request, flows)
        earliest = self.earliest_full_date(request, flows)
        candidates = self.candidates(request, flows, recurring, safe_amount, earliest)
        requested = D(request["requested_amount"])
        profile = self.profiles[request["user_id"]]
        if candidates:
            chosen = min(candidates, key=lambda c: self.rank(c, day(request["desired_completion_date"])))
            payment_plan = "|".join(f"{when.isoformat()}:{money(amount)}" for when, amount in chosen.payments)
            changes = "|".join(change.text() for change in chosen.changes) or "none"
            if chosen.method == "wait":
                explanation = f"Wait until {chosen.payments[0][0].isoformat()} to pay {profile['home_currency']} {money(requested)} while keeping the minimum balance protected."
            elif chosen.changes:
                explanation = f"Apply the listed flexible spending changes and use {chosen.method}; the 90-day forecast keeps at least {profile['home_currency']} {money(D(profile['minimum_balance_to_keep']))}."
            else:
                explanation = f"Use {chosen.method}; the scheduled payments keep the minimum balance protected over the next 90 days."
            return {
                "request_id": request["request_id"], "amount_safe_to_pay": money(safe_amount),
                "affordability_status": chosen.status, "recommended_payment_method": chosen.method,
                "payment_plan": payment_plan,
                "earliest_date_for_full_payment": earliest.isoformat() if earliest else "",
                "spending_changes_needed": changes, "decision_explanation": explanation,
            }
        return {
            "request_id": request["request_id"], "amount_safe_to_pay": money(safe_amount),
            "affordability_status": "not_affordable", "recommended_payment_method": "not_recommended",
            "payment_plan": "none", "earliest_date_for_full_payment": earliest.isoformat() if earliest else "",
            "spending_changes_needed": "none",
            "decision_explanation": f"No eligible payment option completes {profile['home_currency']} {money(requested)} safely while protecting the minimum balance.",
        }

    def validate(self, rows: list[dict[str, str]], requests: list[dict[str, str]]) -> None:
        if len(rows) != len(requests) or len(rows) != 250:
            raise ValueError(f"expected exactly 250 output rows, got {len(rows)}")
        request_by_id = {row["request_id"]: row for row in requests}
        if len(request_by_id) != len(requests) or len({row["request_id"] for row in rows}) != len(rows):
            raise ValueError("duplicate request_id")
        plan_re = re.compile(r"^\d{4}-\d{2}-\d{2}:[0-9]+(?:\.[0-9]+)?(?:\|\d{4}-\d{2}-\d{2}:[0-9]+(?:\.[0-9]+)?)*$")
        for row in rows:
            request = request_by_id.get(row["request_id"])
            if not request or row["affordability_status"] not in STATUSES or row["recommended_payment_method"] not in METHODS:
                raise ValueError(f"invalid output enum/id: {row['request_id']}")
            amount = D(row["amount_safe_to_pay"])
            if not ZERO <= amount <= D(request["requested_amount"]):
                raise ValueError(f"invalid safe amount: {row['request_id']}")
            if row["payment_plan"] != "none" and not plan_re.fullmatch(row["payment_plan"]):
                raise ValueError(f"invalid payment plan format: {row['request_id']}")
            if row["earliest_date_for_full_payment"]:
                day(row["earliest_date_for_full_payment"])
            if row["spending_changes_needed"] != "none":
                _, recurring = self.base_flows(request)
                valid_changes = {change.text() for change in self.eligible_changes(request, recurring)}
                for change in row["spending_changes_needed"].split("|"):
                    event_id = change.split(":")[1]
                    if event_id not in self.events_by_id or change not in valid_changes:
                        raise ValueError(f"ineligible spending event: {event_id}")
            if row["recommended_payment_method"] == "partial_payment":
                entries = row["payment_plan"].split("|")
                if (len(entries) != 2 or sum((D(e.split(":")[1]) for e in entries), ZERO) != D(request["requested_amount"])
                        or request["allows_partial_payment"].lower() != "true"):
                    raise ValueError(f"invalid partial plan: {row['request_id']}")
            if row["recommended_payment_method"] == "installments":
                actual = [(day(item.split(":")[0]), D(item.split(":")[1])) for item in row["payment_plan"].split("|")]
                valid = [self.option_payments(option) for option in self.options_by_request[row["request_id"]] if option["payment_method"] == "installments"]
                if actual not in valid:
                    raise ValueError(f"installment schedule is not a supplied option: {row['request_id']}")
            if row["recommended_payment_method"] == "full_payment":
                expected = [(day(request["request_date"]), D(request["requested_amount"]))]
                actual = [] if row["payment_plan"] == "none" else [(day(item.split(":")[0]), D(item.split(":")[1])) for item in row["payment_plan"].split("|")]
                if actual != expected:
                    raise ValueError(f"invalid full payment plan: {row['request_id']}")


def write_output(rows: list[dict[str, str]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def run_sample_regression(solver: Solver) -> int:
    """Print a compact, repeatable decision-field regression report."""
    samples = read_csv("sample_requests.csv")
    fields = OUTPUT_COLUMNS[1:-1]  # Explanations are deliberately not labels.
    exact = 0
    print("request_id | expected | actual | matching_fields")
    for request in samples:
        actual = solver.solve_request(request)
        matching = [field for field in fields if actual[field] == request[field]]
        if len(matching) == len(fields):
            exact += 1
        expected_text = "/".join(request[field] for field in fields)
        actual_text = "/".join(actual[field] for field in fields)
        print(f"{request['request_id']} | {expected_text} | {actual_text} | {','.join(matching) or '-'}")
    print(f"Core decision-field matches: {exact}/{len(samples)}")
    return 0


def main() -> int:
    solver = Solver()
    if "--samples" in sys.argv:
        return run_sample_regression(solver)
    requests = read_csv("requests.csv")
    rows = [solver.solve_request(request) for request in requests]
    solver.validate(rows, requests)
    write_output(rows, ROOT / "output.csv")
    print(f"Wrote {len(rows)} requests to {ROOT / 'output.csv'}.")
    if solver.image_failures:
        print("Image values not extracted (OCR unavailable or inconclusive): " + ", ".join(sorted(set(solver.image_failures))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
