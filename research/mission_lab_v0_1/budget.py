"""Profile-supplied model budgets.

A budget is a controller reservation, in the same sense as a read-buffer
reservation. Planning records the worst case for each task. The scheduler
adds that hold before launch and refuses the launch when the hold would
exceed a declared cap. A receipt whose reported usage fits the hold settles
the hold down to that usage. A launched task that does not settle keeps the
hold. Arithmetic is integer only. Prices come from the profile; this module
has no price table of its own.

The unit label is not a market rate, an invoice, or a measurement of energy.
"""
from __future__ import annotations

import re

from .core import LabError, fields, integer


COST_KEYS = ("requests", "input_tokens", "output_tokens", "total_tokens", "spend")
ACCOUNT_STATES = ("settled", "charged", "released", "refused", "unlaunched")
TERMINAL_KINDS = ("completed", "refused", "launch_failed", "blocked", "aborted")
MILLION = 1_000_000
MAX_PRICE = 10**12
MAX_SPEND = 10**18
MAX_REQUESTS = 10_000
MAX_TOKEN_CAP = 10**12
MAX_PRICES = 32
UNIT_LABEL = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def zero_cost():
    return {key: 0 for key in COST_KEYS}


def add_cost(left, right):
    return {key: left[key] + right[key] for key in COST_KEYS}


def reservation_numbers(reservation):
    return {key: reservation[key] for key in COST_KEYS}


def _priced(tokens, price, per):
    integer(tokens, 0, MAX_TOKEN_CAP, "priced tokens")
    integer(price, 0, MAX_PRICE, "token price")
    if tokens == 0 or price == 0:
        return 0
    if per == "token":
        amount = tokens * price
    elif per == "million":
        amount = (tokens * price + MILLION - 1) // MILLION
    else:
        raise LabError("model price per must be token or million")
    return integer(amount, 0, MAX_SPEND, "model spend")


def spend_for(input_tokens, output_tokens, price):
    """Integer spend for one call. Per-million prices round away from zero."""
    fields(price, ("input", "output", "per"), label="model price")
    total = _priced(input_tokens, price["input"], price["per"]) + _priced(output_tokens, price["output"], price["per"])
    return integer(total, 0, MAX_SPEND, "model spend")


def limit_view(budget):
    view = {"max_requests": budget["max_requests"], "max_spend": budget["max_spend"], "unit": budget["unit"]}
    for key in ("max_output_tokens", "max_total_tokens"):
        if key in budget:
            view[key] = budget[key]
    return view


def exceeds(used, budget):
    """True when a numeric cost vector is outside a declared cap. Equality fits."""
    if used["requests"] > budget["max_requests"] or used["spend"] > budget["max_spend"]:
        return True
    if "max_output_tokens" in budget and used["output_tokens"] > budget["max_output_tokens"]:
        return True
    if "max_total_tokens" in budget and used["total_tokens"] > budget["max_total_tokens"]:
        return True
    return False


def validate_reservation(value):
    fields(value, COST_KEYS + ("input_bound",), label="model reservation")
    for key in COST_KEYS:
        integer(value[key], 0, MAX_SPEND, "model reservation " + key)
    if value["requests"] not in (0, 1):
        raise LabError("model reservation requests must be 0 or 1")
    if value["input_bound"] not in ("declared", "prompt_bytes", "none"):
        raise LabError("model reservation input_bound is unknown")
    if value["total_tokens"] != value["input_tokens"] + value["output_tokens"]:
        raise LabError("model reservation total_tokens is not the sum of its bounds")
    if value["requests"] == 0 and any(value[key] != 0 for key in COST_KEYS):
        raise LabError("model reservation without a request must be zero")
    if value["requests"] == 0 and value["input_bound"] != "none":
        raise LabError("model reservation without a request has an input bound")
    if value["requests"] == 1 and value["input_bound"] == "none":
        raise LabError("model reservation for a request is missing its input bound")
    return {key: value[key] for key in COST_KEYS + ("input_bound",)}


def validate_actual(value):
    fields(value, COST_KEYS, label="model actual")
    for key in COST_KEYS:
        integer(value[key], 0, MAX_SPEND, "model actual " + key)
    if value["requests"] not in (0, 1):
        raise LabError("model actual requests must be 0 or 1")
    return {key: value[key] for key in COST_KEYS}


def within_reservation(actual, reservation):
    validate_actual(actual)
    numbers = reservation_numbers(validate_reservation(reservation))
    for key in COST_KEYS:
        if actual[key] > numbers[key]:
            raise LabError("Provider usage exceeded the model reservation")
    return actual


def validate_model_budget(value):
    """Closed profile object. Prices are integers in the declared unit."""
    fields(value, ("max_requests", "max_spend", "unit", "prices"),
           ("max_output_tokens", "max_total_tokens"), "model_budget")
    integer(value["max_requests"], 0, MAX_REQUESTS, "max_requests")
    integer(value["max_spend"], 0, MAX_SPEND, "max_spend")
    if "max_output_tokens" in value:
        integer(value["max_output_tokens"], 0, MAX_TOKEN_CAP, "max_output_tokens")
    if "max_total_tokens" in value:
        integer(value["max_total_tokens"], 0, MAX_TOKEN_CAP, "max_total_tokens")
    unit = value["unit"]
    if not isinstance(unit, str) or UNIT_LABEL.fullmatch(unit) is None:
        raise LabError("model_budget unit must be a fixed-point label")
    prices = value["prices"]
    if not isinstance(prices, dict) or not 1 <= len(prices) <= MAX_PRICES:
        raise LabError("model_budget prices must contain 1..32 models")
    from .grok import MODEL_ID
    normalized = {}
    for model, row in prices.items():
        if not isinstance(model, str) or MODEL_ID.fullmatch(model) is None:
            raise LabError("model_budget price model id is malformed")
        fields(row, ("input", "output", "per"), label="model price")
        integer(row["input"], 0, MAX_PRICE, "input price")
        integer(row["output"], 0, MAX_PRICE, "output price")
        if row["per"] not in ("token", "million"):
            raise LabError("model price per must be token or million")
        normalized[model] = {"input": row["input"], "output": row["output"], "per": row["per"]}
    result = {
        "max_requests": value["max_requests"],
        "max_spend": value["max_spend"],
        "unit": unit,
        "prices": {model: normalized[model] for model in sorted(normalized)},
    }
    for key in ("max_output_tokens", "max_total_tokens"):
        if key in value:
            result[key] = value[key]
    return result


def _prompt_bytes(node, inputs):
    if "prompt" in node:
        text = node["prompt"]
        if not isinstance(text, str):
            raise LabError("Grok prompt must be text without NUL")
        return len(text.encode("utf-8"))
    artifact = inputs.get(node.get("input_id")) if isinstance(inputs, dict) else None
    if not isinstance(artifact, dict) or type(artifact.get("size_bytes")) is not int:
        raise LabError("Grok prompt file is missing its size")
    return artifact["size_bytes"]


def reserve_task(node, inputs, budget):
    """Worst-case hold for one node. Non-model nodes reserve zeros."""
    if not isinstance(node, dict) or node.get("operation") != "llm:grok":
        return validate_reservation({**zero_cost(), "input_bound": "none"})
    if not isinstance(budget, dict) or node.get("model") not in budget.get("prices", {}):
        model = node.get("model")
        raise LabError("Model budget has no price for " + str(model))
    from .grok import MAX_OUTPUT_TOKENS, MAX_PROMPT_BYTES, MAX_USAGE_TOKENS
    floor = integer(_prompt_bytes(node, inputs), 1, MAX_PROMPT_BYTES, "prompt bytes")
    if "max_input_tokens" in node:
        bound = integer(node["max_input_tokens"], 1, MAX_USAGE_TOKENS, "max_input_tokens")
        if bound < floor:
            raise LabError("max_input_tokens is below the prompt byte length")
        origin = "declared"
    else:
        bound = floor
        origin = "prompt_bytes"
    output_tokens = integer(node["max_output_tokens"], 1, MAX_OUTPUT_TOKENS, "max_output_tokens")
    price = budget["prices"][node["model"]]
    reservation = {
        "requests": 1,
        "input_tokens": bound,
        "output_tokens": output_tokens,
        "total_tokens": bound + output_tokens,
        "spend": spend_for(bound, output_tokens, price),
        "input_bound": origin,
    }
    return validate_reservation(reservation)


def reservation_for(node, inputs, resources):
    """None when the profile declares no model budget."""
    if not isinstance(resources, dict) or "model_budget" not in resources:
        return None
    return reserve_task(node, inputs, resources["model_budget"])


def settled_actual(node, output, budget):
    """Usage that counts after a successful task. Non-model tasks settle at zero."""
    if not isinstance(node, dict) or node.get("operation") != "llm:grok":
        return validate_actual(zero_cost())
    if not isinstance(output, dict):
        raise LabError("Grok receipt is missing")
    usage = output.get("usage")
    if not isinstance(usage, dict):
        raise LabError("Grok receipt usage is missing")
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if type(usage.get(key)) is not int:
            raise LabError("Grok receipt usage is missing")
    price = budget["prices"].get(node.get("model"))
    if price is None:
        raise LabError("Model budget has no price for " + str(node.get("model")))
    actual = {
        "requests": 1,
        "input_tokens": usage["prompt_tokens"],
        "output_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
        "spend": spend_for(usage["prompt_tokens"], usage["completion_tokens"], price),
    }
    return validate_actual(actual)


def account_record(reservation, state, actual):
    if state not in ACCOUNT_STATES:
        raise LabError("Unknown model account state")
    reserved = validate_reservation(reservation)
    if state == "settled":
        recorded = within_reservation(actual, reserved)
    elif actual is not None:
        raise LabError("Only a settled model account records usage")
    else:
        recorded = None
    return {"state": state, "reserved": reserved, "actual": recorded}


def _remaining(accounted, budget):
    remaining = {
        "requests": budget["max_requests"] - accounted["requests"],
        "spend": budget["max_spend"] - accounted["spend"],
    }
    if "max_output_tokens" in budget:
        remaining["output_tokens"] = budget["max_output_tokens"] - accounted["output_tokens"]
    if "max_total_tokens" in budget:
        remaining["total_tokens"] = budget["max_total_tokens"] - accounted["total_tokens"]
    if any(type(value) is not int or value < 0 for value in remaining.values()):
        raise LabError("Model budget ledger exceeds its limit")
    return remaining


def ledger_for(budget, tasks):
    """Run totals from per-task accounts. Charged holds stay at the reservation."""
    if not isinstance(tasks, list):
        raise LabError("Model budget tasks must be a list")
    reserved = zero_cost()
    actual = zero_cost()
    accounted = zero_cost()
    rows = []
    seen = set()
    for task in sorted(tasks, key=lambda item: item.get("task_id", "")):
        if not isinstance(task, dict) or not isinstance(task.get("task_id"), str):
            raise LabError("Model budget task identity is missing")
        if task["task_id"] in seen:
            raise LabError("Duplicate model budget task")
        seen.add(task["task_id"])
        account = task.get("model_account")
        if not isinstance(account, dict):
            raise LabError("Model account is missing")
        account = account_record(account.get("reserved"), account.get("state"), account.get("actual"))
        numbers = reservation_numbers(account["reserved"])
        if account["state"] in ("settled", "charged"):
            reserved = add_cost(reserved, numbers)
        if account["state"] == "settled":
            actual = add_cost(actual, account["actual"])
            accounted = add_cost(accounted, account["actual"])
        elif account["state"] == "charged":
            accounted = add_cost(accounted, numbers)
        rows.append({"task_id": task["task_id"], **account})
    if exceeds(accounted, budget):
        raise LabError("Model budget ledger exceeds its limit")
    return {
        "schema_version": 1,
        "unit": budget["unit"],
        "limits": limit_view(budget),
        "prices": budget["prices"],
        "tasks": rows,
        "totals": {
            "reserved": reserved,
            "actual": actual,
            "accounted": accounted,
            "remaining": _remaining(accounted, budget),
        },
    }


def _require_account_fields(event, *keys):
    for key in keys:
        if key not in event:
            raise LabError("Model budget event is missing " + key)


def _current(accounted, inflight):
    total = accounted
    for held in inflight.values():
        total = add_cost(total, held)
    return total


def verify_ledger(plan, semantic, observations, events):
    """Recompute the ledger from the plan, receipts, and events. No network."""
    budget = plan["resources"].get("model_budget") if isinstance(plan.get("resources"), dict) else None
    tasks = semantic.get("tasks") if isinstance(semantic, dict) else None
    if not isinstance(tasks, list) or not isinstance(events, list) or not isinstance(observations, list):
        raise LabError("Model budget evidence is malformed")
    if budget is None:
        if "model_budget" in semantic:
            raise LabError("Model budget ledger is present without a declared budget")
        for task in tasks:
            if isinstance(task, dict) and "model_account" in task:
                raise LabError("Model account is present without a declared budget")
        for row in observations:
            if isinstance(row, dict) and "model_account" in row:
                raise LabError("Model account is present without a declared budget")
        for event in events:
            if isinstance(event, dict) and any(str(key).startswith("model_") for key in event):
                raise LabError("Model budget event is present without a declared budget")
        return None
    inputs = {item["id"]: item for item in plan["mission"]["inputs"]}
    nodes = {node["id"]: node for node in plan["graph"]}
    by_task = {}
    for task in tasks:
        if not isinstance(task, dict) or task.get("task_id") not in nodes:
            raise LabError("Model budget task identity is missing")
        account = task.get("model_account")
        if not isinstance(account, dict):
            raise LabError("Model account is missing")
        fields(account, ("state", "reserved", "actual"), label="model account")
        expected = reserve_task(nodes[task["task_id"]], inputs, budget)
        if plan["bindings"][task["task_id"]].get("model_reservation") != expected:
            raise LabError("Model reservation does not match the declared budget")
        if account["reserved"] != expected:
            raise LabError("Recorded model reservation contradicts the plan")
        if account["state"] == "settled":
            if task.get("status") != "succeeded":
                raise LabError("Settled model account is not a successful task")
            recomputed = settled_actual(nodes[task["task_id"]], task.get("output"), budget)
            within_reservation(recomputed, expected)
            if account["actual"] != recomputed:
                raise LabError("Settled model usage contradicts the receipt")
        elif account["actual"] is not None or account["state"] not in ACCOUNT_STATES:
            raise LabError("Model account does not match its state")
        elif account["state"] == "charged" and task.get("status") == "succeeded":
            raise LabError("Charged model reservation was recorded as success")
        elif account["state"] in ("refused", "unlaunched", "released") and task.get("status") == "succeeded":
            raise LabError("Unsettled model reservation was recorded as success")
        by_task[task["task_id"]] = account
    if set(by_task) != set(nodes):
        raise LabError("Model budget ledger is missing a task")
    accounted = zero_cost()
    inflight = {}
    final = {}
    opened = False
    for event in events:
        if not isinstance(event, dict):
            raise LabError("Model budget event is malformed")
        kind = event.get("kind")
        task_id = event.get("task_id")
        if kind == "run_started":
            if opened or event.get("model_budget") != budget or event.get("model_accounted") != zero_cost():
                raise LabError("Model budget did not start from the declared profile")
            opened = True
            continue
        if not opened:
            raise LabError("Model budget event preceded the run")
        if kind == "launched":
            if task_id not in by_task:
                raise LabError("Unknown model budget event task")
            _require_account_fields(event, "model_reserved", "model_accounted")
            reserved = by_task[task_id]["reserved"]
            if event["model_reserved"] != reserved or task_id in inflight or task_id in final:
                raise LabError("Launch reservation contradicts the plan")
            numbers = reservation_numbers(reserved)
            projected = add_cost(_current(accounted, inflight), numbers)
            if exceeds(projected, budget) or event["model_accounted"] != projected:
                raise LabError("Launch exceeded the model budget")
            inflight[task_id] = numbers
            continue
        if kind not in TERMINAL_KINDS:
            if any(str(key).startswith("model_") for key in event) and kind != "run_completed":
                raise LabError("Unexpected model budget event field")
            if kind == "run_completed":
                if event.get("model_accounted") != _current(accounted, inflight) or inflight:
                    raise LabError("Terminal model ledger contradicts reservations")
            continue
        if task_id not in by_task:
            raise LabError("Unknown model budget event task")
        if kind == "completed":
            _require_account_fields(event, "model_state", "model_actual", "model_accounted")
            if task_id not in inflight:
                raise LabError("Completion without a model reservation")
            held = inflight.pop(task_id)
            state = event["model_state"]
            if state == "settled":
                actual = within_reservation(event["model_actual"], by_task[task_id]["reserved"])
                if actual != by_task[task_id]["actual"]:
                    raise LabError("Completion usage contradicts the model account")
                accounted = add_cost(accounted, actual)
            elif state == "charged" and event["model_actual"] is None:
                accounted = add_cost(accounted, held)
            else:
                raise LabError("Unknown model completion state")
            terminal = state
        elif kind == "refused":
            _require_account_fields(event, "model_reserved", "model_accounted")
            if task_id in inflight or event["model_reserved"] != by_task[task_id]["reserved"]:
                raise LabError("Refused model reservation contradicts the plan")
            projected = add_cost(_current(accounted, inflight), reservation_numbers(event["model_reserved"]))
            if not exceeds(projected, budget):
                raise LabError("Refused model reservation would have fit the budget")
            terminal = "refused"
        elif kind == "launch_failed":
            _require_account_fields(event, "model_state", "model_accounted")
            if task_id in inflight:
                inflight.pop(task_id)
                terminal = "released"
            else:
                terminal = "unlaunched"
            if event["model_state"] != terminal:
                raise LabError("Launch failure model state contradicts the reservation")
        elif kind == "aborted":
            _require_account_fields(event, "model_state", "model_accounted")
            if task_id in inflight:
                accounted = add_cost(accounted, inflight.pop(task_id))
                terminal = "charged"
            else:
                terminal = "unlaunched"
            if event["model_state"] != terminal:
                raise LabError("Aborted model state contradicts the reservation")
        else:
            _require_account_fields(event, "model_state", "model_accounted")
            if task_id in inflight or event["model_state"] != "unlaunched":
                raise LabError("Blocked model reservation contradicts the ledger")
            terminal = "unlaunched"
        if task_id in final:
            raise LabError("Repeated model budget terminal state")
        if event.get("model_accounted") != _current(accounted, inflight):
            raise LabError("Model budget event contradicts the running ledger")
        if by_task[task_id]["state"] != terminal:
            raise LabError("Model account state contradicts the event ledger")
        final[task_id] = terminal
    if set(final) != set(nodes) or inflight:
        raise LabError("Model budget ledger did not close")
    expected = ledger_for(budget, tasks)
    if semantic.get("model_budget") != expected:
        raise LabError("Model budget summary contradicts task accounts")
    observed = set()
    for row in observations:
        if not isinstance(row, dict) or not isinstance(row.get("task_id"), str):
            continue
        if "model_account" not in row:
            raise LabError("Observation is missing its model account")
        if row["task_id"] not in by_task or row["model_account"] != by_task[row["task_id"]]:
            raise LabError("Observation model account contradicts the task receipt")
        if row["task_id"] in observed:
            raise LabError("Duplicate observation model account")
        observed.add(row["task_id"])
    for task_id, account in by_task.items():
        if account["state"] == "settled" and task_id not in observed:
            raise LabError("Settled model account is missing its observation")
    return expected


def verify_telemetry(plan, semantic, validation):
    telemetry = validation.get("resource_telemetry") if isinstance(validation, dict) else None
    if not isinstance(telemetry, dict):
        raise LabError("Missing resource telemetry")
    budget = plan["resources"].get("model_budget") if isinstance(plan.get("resources"), dict) else None
    if budget is None:
        if "model_budget" in telemetry:
            raise LabError("Model budget telemetry is present without a declared budget")
        return
    ledger = semantic.get("model_budget")
    if not isinstance(ledger, dict):
        raise LabError("Model budget summary is missing")
    expected = {
        "accounted": ledger["totals"]["accounted"],
        "remaining": ledger["totals"]["remaining"],
        "within_limits": True,
    }
    if telemetry.get("model_budget") != expected:
        raise LabError("Model budget telemetry contradicts the ledger")
