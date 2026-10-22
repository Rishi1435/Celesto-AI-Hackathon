import json
from decimal import Decimal

import httpx

from app.fixtures import CONTRACT
from app.sandbox import GuestCommandError

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "inspect_csv",
            "description": "Inspect CSV column names and row counts in the guest; row values remain private.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Write and execute Python inside Celesto.",
            "parameters": {
                "type": "object",
                "properties": {"source": {"type": "string"}},
                "required": ["source"],
                "additionalProperties": False,
            },
        },
    },
]


class ModelProviderError(RuntimeError):
    def __init__(self, status_code, message):
        self.status_code = status_code
        self.side_effects = "none"
        super().__init__(f"Model provider HTTP {status_code}: {message[:1000]}")


def request_payload(settings, messages):
    settings.validate_model_policy()
    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "required",
        "temperature": 0,
        "max_tokens": 4000,
    }
    if settings.llm_free_only:
        # Provider-side caps remain zero even if a model's routing or pricing changes.
        payload["provider"] = {
            "max_price": {"prompt": 0, "completion": 0, "request": 0},
            "require_parameters": True,
        }
        payload["reasoning"] = {"enabled": False}
    return payload


class ReconciliationAgent:
    def __init__(self, settings, cancel, client=None):
        self.settings, self.cancel = settings, cancel
        self.client = client
        self.input_tokens = self.output_tokens = 0
        self.source = None
        self.reported_cost_usd = None
        self.models_used = []

    def run(self, sandbox, validate):
        messages = [
            {"role": "system", "content": "You are a reconciliation agent. " + CONTRACT},
            {
                "role": "user",
                "content": "The controller has placed and inspected the exports in the guest. "
                "Write a generic reconciliation program using the schema and rules in the contract, then run it. "
                "Inspect schema metadata only if necessary; do not print or transmit CSV row values.",
            },
        ]
        with httpx.Client(timeout=60) as default_client:
            client = self.client or default_client
            for _ in range(self.settings.max_model_turns):
                sandbox.check_cancelled()
                response = client.post(
                    self.settings.llm_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.settings.llm_key},
                    json=request_payload(self.settings, messages),
                )
                if not response.is_success:
                    try:
                        message = str(response.json().get("error", {}).get("message", "Request rejected"))
                    except (ValueError, AttributeError):
                        message = "Request rejected"
                    for secret in [
                        self.settings.llm_key,
                        self.settings.celesto_key,
                        self.settings.access_token,
                    ]:
                        if secret:
                            message = message.replace(secret, "[redacted]")
                    raise ModelProviderError(response.status_code, message)
                data = response.json()
                usage = data.get("usage", {})
                if usage.get("cost") is not None:
                    reported_cost = Decimal(str(usage["cost"]))
                    if not reported_cost.is_finite() or reported_cost < 0:
                        raise ValueError("Provider returned an invalid model cost")
                    if self.settings.llm_free_only and reported_cost != 0:
                        raise ValueError("Free-only policy violation: provider reported a nonzero model cost")
                    self.reported_cost_usd = float(Decimal(str(self.reported_cost_usd or 0)) + reported_cost)
                if data.get("model") and data["model"] not in self.models_used:
                    self.models_used.append(data["model"])
                self.input_tokens += usage.get("prompt_tokens", 0)
                self.output_tokens += usage.get("completion_tokens", 0)
                message = data["choices"][0]["message"]
                calls = message.get("tool_calls", [])
                if not calls or len(calls) > 4:
                    raise ValueError("Model must request between one and four structured tool calls")
                messages.append({k: v for k, v in message.items() if k in {"role", "content", "tool_calls"}})
                for call in calls:
                    name = call["function"]["name"]
                    try:
                        args = json.loads(call["function"]["arguments"])
                        if name == "inspect_csv" and args == {}:
                            output = sandbox.inspect()
                        elif (
                            name == "run_python"
                            and set(args) == {"source"}
                            and isinstance(args["source"], str)
                        ):
                            sandbox.execute_agent(args["source"])
                            result = sandbox.read_report()
                            validate(result)
                            self.source = args["source"]
                            return result
                        else:
                            output = "Unknown tool or invalid arguments; use inspect_csv or run_python."
                    except (ValueError, GuestCommandError) as error:
                        if "outcome unknown" in str(error):
                            raise
                        # Guest exceptions may contain CSV cells. Keep their full diagnostics local.
                        output = (
                            "Guest program failed; revise the Python code using the contract."
                            if isinstance(error, GuestCommandError)
                            else "Tool arguments or report validation failed. Fractional rupees are valid: "
                            "12.34 is 1234 cents; 12.345 is invalid. Recheck the exact contract."
                        )
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
        raise RuntimeError("Model exhausted the tool-call budget without a verified report")

    def cost(self):
        return (
            self.input_tokens * self.settings.llm_input_price
            + self.output_tokens * self.settings.llm_output_price
        ) / 1_000_000
