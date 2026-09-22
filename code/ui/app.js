const views = {
  home: document.getElementById("view-home"),
  decision: document.getElementById("view-decision"),
  whatif: document.getElementById("view-whatif"),
};

const els = {
  form: document.getElementById("check-form"),
  amount: document.getElementById("amount"),
  purpose: document.getElementById("purpose"),
  purchaseDate: document.getElementById("purchase-date"),
  currentBalance: document.getElementById("current-balance"),
  minimumBalance: document.getElementById("minimum-balance"),
  essentialExpenses: document.getElementById("essential-expenses"),
  nextIncome: document.getElementById("next-income"),
  nextIncomeDate: document.getElementById("next-income-date"),
  partialAmount: document.getElementById("partial-amount"),
  partialPaymentField: document.getElementById("partial-payment-field"),
  error: document.getElementById("form-error"),
  whatIfError: document.getElementById("whatif-error"),
  checkBtn: document.getElementById("check-btn"),
  busy: document.getElementById("busy"),
  live: document.getElementById("live-status"),
  savings: document.getElementById("savings-amount"),
  reduce: document.getElementById("reduce-level"),
  waitDate: document.getElementById("wait-date"),
  whatIfPartialAmount: document.getElementById("whatif-partial-amount"),
  recalc: document.getElementById("recalc-btn"),
};

let currentPayload = null;
let lastDecision = null;

const STATUS_COPY = {
  affordable_now: { title: "YES, YOU CAN BUY THIS", tone: "safe", label: "Safe today" },
  affordable_with_plan: { title: "YES, BUT USE A PLAN", tone: "caution", label: "Use a plan" },
  affordable_later: { title: "WAIT BEFORE BUYING", tone: "caution", label: "Wait for now" },
  not_affordable: { title: "WAIT BEFORE BUYING", tone: "unsafe", label: "Not safe yet" },
};

const METHOD_COPY = {
  full_payment: "Pay everything now",
  partial_payment: "Pay part now, then the rest",
  installments: "Use installments",
  wait: "Wait, then pay in full",
  not_recommended: "No safe payment method found",
};

function show(name) {
  Object.entries(views).forEach(([key, node]) => {
    node.hidden = key !== name;
  });
  document.querySelectorAll("[data-nav]").forEach((button) => {
    button.classList.toggle("on", button.dataset.nav === name);
    button.disabled = button.dataset.nav !== "home" && !lastDecision;
  });
}

function setBusy(on) {
  els.busy.hidden = !on;
  els.checkBtn.disabled = on;
  els.recalc.disabled = on;
}

function api(path, body) {
  return fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  }).then(async (response) => {
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "The check could not be completed.");
    return data;
  });
}

function selectedPaymentPreference() {
  return document.querySelector('input[name="payment-preference"]:checked').value;
}

function payloadFromForm() {
  return {
    amount: els.amount.value.trim(),
    purpose: els.purpose.value.trim(),
    purchase_date: els.purchaseDate.value,
    current_balance: els.currentBalance.value.trim(),
    minimum_balance: els.minimumBalance.value.trim(),
    essential_expenses: els.essentialExpenses.value.trim(),
    next_income: els.nextIncome.value.trim(),
    next_income_date: els.nextIncomeDate.value,
    payment_preference: selectedPaymentPreference(),
    partial_amount: els.partialAmount.value.trim(),
  };
}

function indianMoney(amount) {
  if (amount === "" || amount == null) return "—";
  const number = Number(String(amount).replace(/,/g, ""));
  if (Number.isNaN(number)) return `₹${amount}`;
  return `₹${new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 }).format(number)}`;
}

function formatDate(value) {
  if (!value) return "Not available";
  const parsed = new Date(`${value}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return value;
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${String(parsed.getDate()).padStart(2, "0")} ${months[parsed.getMonth()]} ${parsed.getFullYear()}`;
}

function formatResultText(text) {
  return String(text || "")
    .replace(/(?:\b[A-Z]{3}\s*|₹\s*)([0-9][0-9,]*(?:\.[0-9]+)?)/g, (_, amount) => indianMoney(amount))
    .replace(/\b20\d{2}-\d{2}-\d{2}\b/g, (value) => formatDate(value));
}

function humanPlan(plan) {
  if (!plan || plan === "none") return "No payment plan";
  return plan.split("|").map((part) => {
    const [when, amount] = part.split(":");
    return `${formatDate(when)}: ${indianMoney(amount)}`;
  }).join(" → ");
}

function humanChanges(value) {
  if (!value || value === "none") return "No spending changes";
  return value.split("|").map((item) => {
    const parts = item.split(":");
    if (parts[0] === "stop") return `Stop ${parts[1]}`;
    if (parts[0] === "reduce_to") return `Reduce ${indianMoney(parts[2])} for ${parts[1]}`;
    return item;
  }).join(", ");
}

function metaFor(decision) {
  return STATUS_COPY[decision.affordability_status] || STATUS_COPY.not_affordable;
}

function fillFacts(node, decision) {
  const rows = [
    ["Payment plan", humanPlan(decision.payment_plan)],
    ["Full payment date", formatDate(decision.earliest_date_for_full_payment)],
    ["Spending changes", humanChanges(decision.spending_changes_needed)],
  ];
  node.innerHTML = rows.map(([dt, dd]) => `<div><dt>${dt}</dt><dd>${dd}</dd></div>`).join("");
}

function paintActionable(decision) {
  const box = document.getElementById("actionable");
  const list = document.getElementById("actionable-list");
  const steps = decision.actionable_steps || [];
  box.hidden = steps.length === 0;
  list.innerHTML = steps.map((step) => `<li>${formatResultText(step)}</li>`).join("");
}

function paintHero(data) {
  const decision = data.decision;
  const meta = metaFor(decision);
  const card = document.getElementById("decision-card");
  card.className = `decision-hero tone-${meta.tone}`;
  document.getElementById("decision-eyebrow").textContent = meta.label;
  document.getElementById("decision-title").textContent = meta.title;
  document.getElementById("decision-safe").textContent = indianMoney(decision.amount_safe_to_pay);
  document.getElementById("decision-method").textContent =
    METHOD_COPY[decision.recommended_payment_method] || decision.recommended_payment_method;
  document.getElementById("decision-explain").textContent = formatResultText(decision.decision_explanation || "SmartSpend checked the available plan.");
  fillFacts(document.getElementById("decision-facts"), decision);
  paintActionable(decision);
}

function selectedChanges() {
  return [...document.querySelectorAll("[data-change]:checked")].map((input) => input.value);
}

function syncPaymentField() {
  els.partialPaymentField.hidden = selectedPaymentPreference() !== "partial_payment";
}

function syncWhatIfControls() {
  const changes = new Set(selectedChanges());
  document.getElementById("whatif-spend-less").hidden = !changes.has("spend_less");
  document.getElementById("whatif-savings").hidden = !changes.has("use_savings");
  document.getElementById("whatif-wait").hidden = !changes.has("wait_income");
  document.getElementById("whatif-partial").hidden = !changes.has("pay_part_now");
}

function whatIfPayload() {
  return {
    ...currentPayload,
    changes: selectedChanges(),
    reduce_level: els.reduce.value,
    savings_amount: els.savings.value.trim(),
    wait_date: els.waitDate.value,
    partial_amount: els.whatIfPartialAmount.value.trim(),
  };
}

function paintWhatIf(data) {
  const before = lastDecision.decision;
  const after = data.decision;
  const beforeMeta = metaFor(before);
  const afterMeta = metaFor(after);
  document.getElementById("before-after").hidden = false;
  document.querySelector(".ba.before").className = `ba before tone-${beforeMeta.tone}`;
  document.getElementById("whatif-card").className = `ba after tone-${afterMeta.tone}`;
  document.getElementById("before-status").textContent = beforeMeta.label;
  document.getElementById("before-safe").textContent = `Safe now: ${indianMoney(before.amount_safe_to_pay)}`;
  document.getElementById("before-method").textContent = METHOD_COPY[before.recommended_payment_method] || "—";
  document.getElementById("whatif-eyebrow").textContent = afterMeta.label;
  document.getElementById("whatif-title").textContent = afterMeta.title;
  document.getElementById("after-safe").textContent = `Safe now: ${indianMoney(after.amount_safe_to_pay)}`;
  document.getElementById("after-method").textContent = `Recommended: ${METHOD_COPY[after.recommended_payment_method] || "—"}`;
  document.getElementById("after-date").textContent = `Full payment date: ${formatDate(after.earliest_date_for_full_payment)}`;
  document.getElementById("after-changes").textContent = `What changed: ${humanChanges(after.spending_changes_needed)}`;
  document.getElementById("whatif-explain").textContent = formatResultText(after.decision_explanation || "The combined changes were checked together.");
  document.getElementById("whatif-actions").innerHTML = (after.actionable_steps || []).map((step) => `<li>${formatResultText(step)}</li>`).join("");
}

document.querySelectorAll("[data-nav]").forEach((button) => {
  button.addEventListener("click", () => show(button.dataset.nav));
});
document.querySelectorAll("[data-go]").forEach((button) => {
  button.addEventListener("click", () => show(button.dataset.go));
});
document.querySelectorAll('input[name="payment-preference"]').forEach((input) => {
  input.addEventListener("change", syncPaymentField);
});
document.querySelectorAll("[data-change]").forEach((input) => {
  input.addEventListener("change", syncWhatIfControls);
});
els.reduce.addEventListener("input", () => {
  document.getElementById("reduce-readout").textContent = `${els.reduce.value}%`;
});

els.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  els.error.hidden = true;
  const payload = payloadFromForm();
  if (!payload.amount || Number(payload.amount) <= 0) {
    els.error.hidden = false;
    els.error.textContent = "Enter a purchase amount greater than zero.";
    return;
  }
  const requiredInputs = [
    [payload.purpose, "what it is for"],
    [payload.purchase_date, "when you want to buy it"],
    [payload.current_balance, "the money you have now"],
    [payload.minimum_balance, "the minimum money you need to keep"],
    [payload.essential_expenses, "your essential monthly expenses"],
  ];
  const missing = requiredInputs.find(([value]) => !value);
  if (missing) {
    els.error.hidden = false;
    els.error.textContent = `Enter ${missing[1]}.`;
    return;
  }
  if (payload.payment_preference === "partial_payment" && (!payload.partial_amount || Number(payload.partial_amount) <= 0)) {
    els.error.hidden = false;
    els.error.textContent = "Enter how much you want to pay now.";
    return;
  }
  if (payload.next_income && !payload.next_income_date) {
    els.error.hidden = false;
    els.error.textContent = "Add the next income date.";
    return;
  }
  if (!payload.next_income && payload.next_income_date) {
    els.error.hidden = false;
    els.error.textContent = "Add the next income amount.";
    return;
  }
  setBusy(true);
  try {
    const data = await api("/api/decide", payload);
    currentPayload = payload;
    lastDecision = data;
    paintHero(data);
    show("decision");
  } catch (error) {
    els.error.hidden = false;
    els.error.textContent = error.message;
  } finally {
    setBusy(false);
  }
});

document.getElementById("explore-btn").addEventListener("click", () => {
  show("whatif");
  syncWhatIfControls();
});

els.recalc.addEventListener("click", async () => {
  if (!currentPayload) return;
  els.whatIfError.hidden = true;
  const changes = selectedChanges();
  if (!changes.length) {
    els.whatIfError.hidden = false;
    els.whatIfError.textContent = "Choose at least one change.";
    return;
  }
  if (changes.includes("wait_income") && !els.waitDate.value) {
    els.whatIfError.hidden = false;
    els.whatIfError.textContent = "Choose the income date.";
    return;
  }
  if (changes.includes("use_savings") && (!els.savings.value || Number(els.savings.value) <= 0)) {
    els.whatIfError.hidden = false;
    els.whatIfError.textContent = "Enter how much savings to use.";
    return;
  }
  if (changes.includes("pay_part_now") && (!els.whatIfPartialAmount.value || Number(els.whatIfPartialAmount.value) <= 0)) {
    els.whatIfError.hidden = false;
    els.whatIfError.textContent = "Enter how much to pay now.";
    return;
  }
  setBusy(true);
  try {
    const data = await api("/api/whatif", whatIfPayload());
    paintWhatIf(data);
  } catch (error) {
    els.whatIfError.hidden = false;
    els.whatIfError.textContent = error.message;
  } finally {
    setBusy(false);
  }
});

syncPaymentField();
syncWhatIfControls();
show("home");
api("/health").catch(() => {
  els.live.textContent = "Server unavailable";
});