/* SmartSpend AI - client logic
 *
 * Key changes from original:
 *  - Demo account connect/disconnect: fetches /api/demo-profile and
 *    auto-populates financial fields from the existing dataset.
 *  - payment_preference is always 'auto' -- SmartSpend decides the method.
 *  - Payment-choice radio buttons removed from form.
 *  - What-If functionality preserved unchanged.
 */

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
  // Demo account elements
  connectBtn: document.getElementById("connect-btn"),
  disconnectBtn: document.getElementById("disconnect-btn"),
  connectArea: document.getElementById("connect-area"),
  accountConnected: document.getElementById("account-connected"),
  accountLabel: document.getElementById("account-label"),
  connectedSummary: document.getElementById("connected-summary"),
  connectedFacts: document.getElementById("connected-facts"),
  manualFinancial: document.getElementById("manual-financial"),
  currencyPrefix: document.getElementById("currency-prefix"),
};

let currentPayload = null;
let lastDecision = null;
// Holds the demo profile loaded from the dataset; null when not connected.
let demoProfile = null;
let activeCurrencySymbol = "₹";

const STATUS_COPY = {
  affordable_now: { title: "Safe to buy now", tone: "safe", label: "Safe today" },
  affordable_with_plan: { title: "Safer payment plan", tone: "caution", label: "Use a plan" },
  affordable_later: { title: "Wait before buying", tone: "caution", label: "Wait for now" },
  not_affordable: { title: "Wait before buying", tone: "unsafe", label: "Not affordable yet" },
};

const METHOD_COPY = {
  full_payment: "Pay in full now",
  partial_payment: "Pay partially now and the rest later",
  installments: "Use installments",
  wait: "Wait until a safer date",
  not_recommended: "Do not proceed yet / not affordable",
};

function show(name) {
  Object.entries(views).forEach(([key, node]) => { node.hidden = key !== name; });
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

// -- Demo account connect / disconnect ----------------------------------------

function currencySymbol(code) {
  const map = { INR: "\u20b9", EUR: "\u20ac", ZAR: "R", IDR: "Rp", USD: "$", GBP: "\u00a3" };
  return map[code] || code;
}

function formatDate(value) {
  if (!value) return "Not available";
  const parsed = new Date(value + "T00:00:00");
  if (Number.isNaN(parsed.getTime())) return value;
  const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  return String(parsed.getDate()).padStart(2, "0") + " " + months[parsed.getMonth()] + " " + parsed.getFullYear();
}

function applyDemoProfile(profile) {
  demoProfile = profile;
  const sym = currencySymbol(profile.home_currency);
  activeCurrencySymbol = sym;
  if (els.currencyPrefix) els.currencyPrefix.textContent = sym;
  els.connectArea.hidden = true;
  els.accountConnected.hidden = false;
  els.accountLabel.textContent = "Account connected";
  els.manualFinancial.hidden = true;
  els.connectedSummary.hidden = false;
  function cfItem(label, value) {
    return "<div class=\"cf-item\"><span class=\"cf-label\">" + label + "</span><span class=\"cf-value\">" + value + "</span></div>";
  }
  const bal = Number(profile.current_balance).toLocaleString("en-IN", { maximumFractionDigits: 2 });
  const mn  = Number(profile.minimum_balance).toLocaleString("en-IN", { maximumFractionDigits: 2 });
  const exp = Number(profile.essential_expenses).toLocaleString("en-IN", { maximumFractionDigits: 2 });
  let html = cfItem("Available balance", sym + "\u00a0" + bal)
           + cfItem("Minimum to keep", sym + "\u00a0" + mn)
           + cfItem("Est. essential/mo", sym + "\u00a0" + exp);
  if (profile.next_income && profile.next_income_date) {
    const inc = Number(profile.next_income).toLocaleString("en-IN", { maximumFractionDigits: 2 });
    html += cfItem("Next income", sym + "\u00a0" + inc + " on " + formatDate(profile.next_income_date));
  }
  els.connectedFacts.innerHTML = html;
  if (!els.purchaseDate.value && profile.request_date) {
    els.purchaseDate.value = profile.request_date;
  }
}

function disconnectDemoProfile() {
  demoProfile = null;
  activeCurrencySymbol = "\u20b9";
  els.connectArea.hidden = false;
  els.accountConnected.hidden = true;
  els.manualFinancial.hidden = false;
  els.connectedSummary.hidden = true;
  els.currentBalance.value = "";
  els.minimumBalance.value = "";
  els.essentialExpenses.value = "";
  els.nextIncome.value = "";
  els.nextIncomeDate.value = "";
  if (els.currencyPrefix) els.currencyPrefix.textContent = "\u20b9";
}

els.connectBtn.addEventListener("click", async () => {
  els.connectBtn.disabled = true;
  els.connectBtn.textContent = "Connecting\u2026";
  try {
    const profile = await api("/api/demo-profile");
    applyDemoProfile(profile);
  } catch (err) {
    els.error.hidden = false;
    els.error.textContent = "Could not connect account: " + err.message;
  } finally {
    els.connectBtn.disabled = false;
    els.connectBtn.innerHTML = "<span aria-hidden=\"true\">\u26a1</span> Connect Account";
  }
});

els.disconnectBtn.addEventListener("click", () => { disconnectDemoProfile(); });

// -- Form helpers --------------------------------------------------------------

function payloadFromForm() {
  if (demoProfile) {
    return {
      connected_account: true,
      request_id: demoProfile.request_id,
      amount: els.amount.value.trim(),
      purpose: els.purpose.value.trim(),
      purchase_date: els.purchaseDate.value,
      payment_preference: "auto",
    };
  }
  return {
    amount: els.amount.value.trim(),
    purpose: els.purpose.value.trim(),
    purchase_date: els.purchaseDate.value,
    payment_preference: "auto",
    current_balance: els.currentBalance.value.trim(),
    minimum_balance: els.minimumBalance.value.trim(),
    essential_expenses: els.essentialExpenses.value.trim(),
    next_income: els.nextIncome.value.trim(),
    next_income_date: els.nextIncomeDate.value,
  };
}

function indianMoney(amount) {
  if (amount === "" || amount == null) return "\u2014";
  const number = Number(String(amount).replace(/,/g, ""));
  const sym = activeCurrencySymbol || "\u20b9";
  if (Number.isNaN(number)) return sym + "\u00a0" + amount;
  return sym + "\u00a0" + new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 }).format(number);
}

function formatResultText(text) {
  return String(text || "")
    .replace(/(?:\b[A-Z]{3}\s*|[\u20b9\$\u20ac\u00a3R]\s*|Rp\s*)([0-9][0-9,]*(?:\.[0-9]+)?)/g, (_, a) => indianMoney(a))
    .replace(/\b20\d{2}-\d{2}-\d{2}\b/g, (v) => formatDate(v));
}

function humanPlan(plan) {
  if (!plan || plan === "none") return "No payment plan";
  return plan.split("|").map((part) => {
    const [when, amount] = part.split(":");
    return formatDate(when) + ": " + indianMoney(amount);
  }).join(" \u2192 ");
}

function humanChanges(value) {
  if (!value || value === "none") return "No spending changes";
  return value.split("|").map((item) => {
    const parts = item.split(":");
    if (parts[0] === "stop") return "Stop " + parts[1];
    if (parts[0] === "reduce_to") return "Reduce " + indianMoney(parts[2]) + " for " + parts[1];
    return item;
  }).join(", ");
}

function metaFor(decision) {
  return STATUS_COPY[decision.affordability_status] || STATUS_COPY.not_affordable;
}

function fillFacts(node, decision, request) {
  const purchaseAmount = (request && request.requested_amount) || (currentPayload && currentPayload.amount) || "";
  const dateLabel = (decision.recommended_payment_method === "wait" || decision.affordability_status === "affordable_later")
    ? "Next safe date"
    : "Full payment date";
  const rows = [
    ["Purchase amount", indianMoney(purchaseAmount)],
    ["Payment plan", humanPlan(decision.payment_plan)],
    [dateLabel, formatDate(decision.earliest_date_for_full_payment)],
    ["Spending changes", humanChanges(decision.spending_changes_needed)],
  ];
  node.innerHTML = rows.map(([dt, dd]) => "<div><dt>" + dt + "</dt><dd>" + dd + "</dd></div>").join("");
}

function paintActionable(decision) {
  const box = document.getElementById("actionable");
  const list = document.getElementById("actionable-list");
  const steps = decision.actionable_steps || [];
  box.hidden = steps.length === 0;
  list.innerHTML = steps.map((step) => "<li>" + formatResultText(step) + "</li>").join("");
}

function paintHero(data) {
  const decision = data.decision;
  const meta = metaFor(decision);
  const card = document.getElementById("decision-card");
  card.className = "decision-hero tone-" + meta.tone;
  document.getElementById("decision-eyebrow").textContent = meta.label;
  document.getElementById("decision-title").textContent = meta.title;
  document.getElementById("decision-safe").textContent = indianMoney(decision.amount_safe_to_pay);
  document.getElementById("decision-method").textContent =
    METHOD_COPY[decision.recommended_payment_method] || decision.recommended_payment_method;
  document.getElementById("decision-explain").textContent =
    formatResultText(decision.decision_explanation || "SmartSpend checked the available plan.");
  fillFacts(document.getElementById("decision-facts"), decision, data.request);
  paintActionable(decision);
}

function selectedChanges() {
  return [...document.querySelectorAll("[data-change]:checked")].map((input) => input.value);
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
  document.querySelector(".ba.before").className = "ba before tone-" + beforeMeta.tone;
  document.getElementById("whatif-card").className = "ba after tone-" + afterMeta.tone;
  document.getElementById("before-status").textContent = beforeMeta.label;
  document.getElementById("before-safe").textContent = "Safe now: " + indianMoney(before.amount_safe_to_pay);
  document.getElementById("before-method").textContent = METHOD_COPY[before.recommended_payment_method] || "\u2014";
  document.getElementById("whatif-eyebrow").textContent = afterMeta.label;
  document.getElementById("whatif-title").textContent = afterMeta.title;
  document.getElementById("after-safe").textContent = "Safe now: " + indianMoney(after.amount_safe_to_pay);
  document.getElementById("after-method").textContent =
    "Recommended: " + (METHOD_COPY[after.recommended_payment_method] || "\u2014");
  const afterDateLabel = (after.recommended_payment_method === "wait" || after.affordability_status === "affordable_later")
    ? "Next safe date: "
    : "Full payment date: ";
  document.getElementById("after-date").textContent =
    afterDateLabel + formatDate(after.earliest_date_for_full_payment);
  document.getElementById("after-changes").textContent =
    "What changed: " + humanChanges(after.spending_changes_needed);
  document.getElementById("whatif-explain").textContent =
    formatResultText(after.decision_explanation || "The combined changes were checked together.");
  document.getElementById("whatif-actions").innerHTML =
    (after.actionable_steps || []).map((step) => "<li>" + formatResultText(step) + "</li>").join("");
}

// -- Nav ----------------------------------------------------------------------

document.querySelectorAll("[data-nav]").forEach((button) => {
  button.addEventListener("click", () => show(button.dataset.nav));
});
document.querySelectorAll("[data-go]").forEach((button) => {
  button.addEventListener("click", () => show(button.dataset.go));
});
document.querySelectorAll("[data-change]").forEach((input) => {
  input.addEventListener("change", syncWhatIfControls);
});
els.reduce.addEventListener("input", () => {
  document.getElementById("reduce-readout").textContent = els.reduce.value + "%";
});

// -- Form submit --------------------------------------------------------------

els.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  els.error.hidden = true;
  const payload = payloadFromForm();
  if (!payload.amount || Number(payload.amount) <= 0) {
    els.error.hidden = false;
    els.error.textContent = "Enter a purchase amount greater than zero.";
    return;
  }
  if (!payload.purpose) {
    els.error.hidden = false;
    els.error.textContent = "Enter what the purchase is for.";
    return;
  }
  if (!payload.purchase_date) {
    els.error.hidden = false;
    els.error.textContent = "Enter when you want to buy it.";
    return;
  }
  if (!demoProfile) {
    const requiredFinancial = [
      [payload.current_balance, "the money you have now"],
      [payload.minimum_balance, "the minimum money you need to keep"],
      [payload.essential_expenses, "your essential monthly expenses"],
    ];
    const missing = requiredFinancial.find(([value]) => !value);
    if (missing) {
      els.error.hidden = false;
      els.error.textContent = "Enter " + missing[1] + ".";
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

// -- Init --------------------------------------------------------------------

syncWhatIfControls();
show("home");
api("/health").catch(() => { els.live.textContent = "Server unavailable"; });
