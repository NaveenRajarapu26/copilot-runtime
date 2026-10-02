import json
import os
import re
import uuid
from typing import Any, AsyncIterator

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from agents import DailyArPrioritiesWorker, DailyArWorkerError, resolve_worker_access
from flowone import (
    DailyArToolService,
    FlowOneApiError,
    FlowOneClient,
    FlowOneContextError,
    FlowOneRequestContext,
)


# =========================================================
# ENVIRONMENT
# =========================================================

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip()
OPENROUTER_URL = os.getenv(
    "OPENROUTER_URL",
    "https://openrouter.ai/api/v1/chat/completions",
).strip()


# =========================================================
# APP
# =========================================================

app = FastAPI(
    title="FlowOne Copilot Runtime",
    version="0.8.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "flowone-copilot-runtime",
        "mode": "local-poc",
        "model": OPENROUTER_MODEL,
        "openrouterConfigured": bool(OPENROUTER_API_KEY),
        "runtimeVersion": "0.8.0",
        "flowOneApiClient": "request-scoped",
    }


# =========================================================
# AG-UI HELPERS
# =========================================================


def encode_sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])

    return " ".join(parts)


def get_last_user_message(payload: dict[str, Any]) -> str:
    messages = payload.get("messages") or []

    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        return extract_text(message.get("content")).strip()

    return ""


def build_flowone_context(state: dict[str, Any]) -> str:
    if not state:
        return "No FlowOne application context was supplied."

    return json.dumps(state, indent=2, default=str)


def get_flowone_capabilities(payload: dict[str, Any]) -> dict[str, Any]:
    state = payload.get("state") or {}
    if not isinstance(state, dict):
        return {}

    permissions = state.get("permissions") or {}
    if not isinstance(permissions, dict):
        return {}

    capabilities = permissions.get("capabilities") or {}
    return capabilities if isinstance(capabilities, dict) else {}


def get_flowone_currency_code(payload: dict[str, Any]) -> str:
    state = payload.get("state") or {}
    if not isinstance(state, dict):
        return "USD"

    ar_context = state.get("accountsReceivable") or {}
    if not isinstance(ar_context, dict):
        return "USD"

    value = str(ar_context.get("currencyCode") or "USD").strip()
    return value or "USD"


# =========================================================
# OPENROUTER HELPERS
# =========================================================


def openrouter_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:3000",
        "X-Title": "FlowOne Copilot Local POC",
    }


async def call_openrouter_json(
    system_prompt: str,
    user_message: str,
) -> dict[str, Any]:
    if not OPENROUTER_API_KEY:
        return {"intent": "ANSWER"}

    body = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

    timeout = httpx.Timeout(
        connect=20.0,
        read=60.0,
        write=20.0,
        pool=20.0,
    )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                OPENROUTER_URL,
                headers=openrouter_headers(),
                json=body,
            )

        if response.status_code >= 400:
            return {"intent": "ANSWER"}

        data = response.json()
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )

        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else {"intent": "ANSWER"}

    except (
        httpx.HTTPError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        return {"intent": "ANSWER"}


# =========================================================
# FAST, DETERMINISTIC ROUTING FOR KNOWN CAPABILITIES
# =========================================================


def route_known_request(user_message: str) -> dict[str, Any] | None:
    """
    Keep high-confidence product commands deterministic so the demo does not
    depend on a model recognizing obvious supported FlowOne resources.

    This contains no tenant/customer/business data. It only maps language to
    registered Copilot capabilities.
    """

    text = " ".join(user_message.lower().strip().split())

    if not text:
        return None

    if text in {
        "validate daily ar backend",
        "validate daily ar tools",
        "check daily ar backend",
        "check daily ar tools",
    }:
        return {
            "intent": "VALIDATE_WORKER_TOOLS",
            "workerId": "daily_ar_priorities",
        }

    if text in {
        "run daily ar priorities",
        "run daily ar priority",
        "start daily ar priorities",
        "start daily ar priority",
    } or ("daily ar" in text and any(word in text for word in ("run", "start"))):
        return {
            "intent": "RUN_WORKER",
            "workerId": "daily_ar_priorities",
        }

    if "collection overview" in text or "collections overview" in text:
        return {"intent": "SHOW", "resource": "collection_overview"}

    if (
        "unpaid invoice" in text
        or "open invoice" in text
        or "outstanding invoice" in text
    ):
        return {"intent": "SHOW", "resource": "unpaid_invoices"}

    if "credit note" in text:
        return {"intent": "SHOW", "resource": "credit_notes"}

    if (
        "customer payment" in text
        or "recent payment" in text
        or "payments made" in text
        or text in {"payments", "payment activity"}
    ):
        return {"intent": "SHOW", "resource": "customer_payments"}

    if (
        "overdue customer" in text
        or "unpaid account" in text
        or "customers with unpaid" in text
        or "accounts with overdue" in text
    ):
        return {"intent": "SHOW", "resource": "overdue_customers"}

    if (
        "ar summary" in text
        or "receivables summary" in text
        or "ar metrics" in text
    ):
        return {"intent": "SHOW", "resource": "ar_summary"}

    navigation_verbs = ("open ", "go to ", "take me to ", "navigate to ")
    if text.startswith(navigation_verbs):
        if "collection" in text:
            return {
                "intent": "NAVIGATE",
                "targetType": "route",
                "route": "collections",
            }
        if "sales invoice" in text or text.endswith("invoices"):
            return {
                "intent": "NAVIGATE",
                "targetType": "route",
                "route": "sales_invoices",
            }
        if "payment" in text:
            return {
                "intent": "NAVIGATE",
                "targetType": "route",
                "route": "payments",
            }
        if "credit note" in text:
            return {
                "intent": "NAVIGATE",
                "targetType": "route",
                "route": "credit_notes",
            }
        if "ar dashboard" in text or "accounts receivable" in text:
            return {
                "intent": "NAVIGATE",
                "targetType": "route",
                "route": "ar_dashboard",
            }

    return None


# =========================================================
# INTENT CLASSIFICATION
# =========================================================


async def classify_intent(payload: dict[str, Any]) -> dict[str, Any]:
    user_message = get_last_user_message(payload)

    if not user_message:
        return {"intent": "ANSWER"}

    known = route_known_request(user_message)
    if known:
        return known

    system_prompt = """
You are the intent router for FlowOne Copilot.

Return ONLY one JSON object.
Do not use markdown.
Do not explain your answer.

SUPPORTED INTENTS:
ANSWER
SHOW
NAVIGATE
ACT
RUN_WORKER
VALIDATE_WORKER_TOOLS

RUN_WORKER:
Use when the user explicitly asks to run/start Daily AR Priorities.

{
  "intent": "RUN_WORKER",
  "workerId": "daily_ar_priorities"
}

VALIDATE_WORKER_TOOLS:
Use only when the user explicitly asks to validate/check the Daily AR backend/tools.
This is a local migration diagnostic and returns counts only.

{
  "intent": "VALIDATE_WORKER_TOOLS",
  "workerId": "daily_ar_priorities"
}

SHOW resources:

1. overdue_customers
Use for overdue customers, unpaid customer accounts, customers with overdue balances.

2. ar_summary
Use for AR summary, Accounts Receivable summary, DSO/CEI/receivables metrics.

3. unpaid_invoices
Use for unpaid invoices, open invoices, invoices with remaining balances.

4. customer_payments
Use for recent payments, customer payments, payment activity.

5. credit_notes
Use for credit notes or credit-note activity.

6. collection_overview
Use for a combined collection overview or consolidated collections view.

SHOW response shape:
{
  "intent": "SHOW",
  "resource": "<resource>"
}

NAVIGATE routes:
ar_dashboard
collections
sales_invoices
payments
credit_notes

NAVIGATE route response shape:
{
  "intent": "NAVIGATE",
  "targetType": "route",
  "route": "<route>"
}

CUSTOMER NAVIGATION:
If the user asks to open a specific customer, extract the customer name.

Example:
Open Krishna Traders

{
  "intent": "NAVIGATE",
  "targetType": "customer",
  "customerName": "Krishna Traders"
}

ACT:
Currently supported action: collection_followup

Examples:
Follow up with Krishna Traders
Prepare a collection follow-up for Krishna Traders
Draft a reminder for Sri Ganga Agro Traders

{
  "intent": "ACT",
  "action": "collection_followup",
  "customerName": "Krishna Traders"
}

ACT only prepares work. It never means a message has already been sent.
The frontend must validate permissions, resolve the real customer, show human
approval, and keep production execution disabled in this POC.

If a collection follow-up is requested without a clear customer:
{
  "intent": "ACT",
  "action": "collection_followup",
  "customerName": ""
}

Use ANSWER for normal explanations, summaries, factual questions, and product
questions that do not directly request a supported UI/action.

If no supported intent applies:
{
  "intent": "ANSWER"
}
""".strip()

    result = await call_openrouter_json(system_prompt, user_message)
    intent = str(result.get("intent", "ANSWER")).upper()

    if intent == "SHOW":
        resource = str(result.get("resource", "")).strip()
        if resource in {
            "overdue_customers",
            "ar_summary",
            "unpaid_invoices",
            "customer_payments",
            "credit_notes",
            "collection_overview",
        }:
            return {"intent": "SHOW", "resource": resource}

    if intent == "NAVIGATE":
        target_type = str(result.get("targetType", "")).strip().lower()

        if target_type == "route":
            route = str(result.get("route", "")).strip()
            if route in {
                "ar_dashboard",
                "collections",
                "sales_invoices",
                "payments",
                "credit_notes",
            }:
                return {
                    "intent": "NAVIGATE",
                    "targetType": "route",
                    "route": route,
                }

        if target_type == "customer":
            customer_name = str(result.get("customerName", "")).strip()
            if customer_name:
                return {
                    "intent": "NAVIGATE",
                    "targetType": "customer",
                    "customerName": customer_name,
                }

    if intent == "ACT":
        action = str(result.get("action", "")).strip()
        if action == "collection_followup":
            return {
                "intent": "ACT",
                "action": "collection_followup",
                "customerName": str(result.get("customerName", "")).strip(),
            }

    return {"intent": "ANSWER"}


# =========================================================
# MODEL MESSAGES
# =========================================================


def build_model_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    state = payload.get("state") or {}
    flowone_context = build_flowone_context(state)

    system_prompt = f"""
You are FlowOne Copilot, embedded inside the FlowOne subscriber application.
You are running as a local proof of concept.

GROUNDING RULES:

1. The FLOWONE APPLICATION CONTEXT below is the source of truth for:
   - current user
   - company
   - legal entity
   - workspace
   - permissions
   - capabilities
   - available tool catalog
   - business metrics
   - recommendations
   - assigned tasks

2. Never invent customers, invoices, payments, credit notes, balances, DSO,
   CEI, tasks, permissions, roles, or business events.

3. If information is not present in context and no frontend tool returned it,
   say it is not currently available through the connected Copilot tools.

4. Never claim an application action succeeded unless the frontend confirms it.

5. Never claim an email, reminder, collection action, payment, approval,
   transaction, or customer contact occurred unless the application explicitly
   confirms that result.

6. Permission capabilities are authoritative. The toolCatalog tells you which
   Copilot tools are currently allowed for this logged-in user.

7. When asked "what can you do?", describe only tools marked allowed=true plus
   normal grounded Q&A. Do not claim unsupported FlowOne coverage.

8. When asked what needs attention / what to work on, use this order:
   A. live business signals
   B. recommended work supplied by FlowOne
   C. current focus supplied by FlowOne
   D. assigned tasks

9. Distinguish factual business signals from recommendations.

10. Do not invent thresholds, urgency levels, risk classifications, or targets.

11. Keep answers concise and useful for an enterprise application.

CURRENT FLOWONE APPLICATION CONTEXT:

{flowone_context}
""".strip()

    model_messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt}
    ]

    incoming_messages = payload.get("messages") or []
    for message in incoming_messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = extract_text(message.get("content"))
        if not content:
            continue
        model_messages.append({"role": role, "content": content})

    return model_messages


# =========================================================
# OPENROUTER STREAM
# =========================================================


async def stream_openrouter(
    model_messages: list[dict[str, str]],
) -> AsyncIterator[str]:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not configured.")

    body = {
        "model": OPENROUTER_MODEL,
        "messages": model_messages,
        "stream": True,
        "temperature": 0.2,
    }

    timeout = httpx.Timeout(
        connect=20.0,
        read=None,
        write=20.0,
        pool=20.0,
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            OPENROUTER_URL,
            headers=openrouter_headers(),
            json=body,
        ) as response:
            if response.status_code >= 400:
                error_body = await response.aread()
                raise RuntimeError(
                    "OpenRouter request failed "
                    f"with status {response.status_code}: "
                    f"{error_body.decode(errors='ignore')}"
                )

            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue

                data = line[5:].strip()
                if data == "[DONE]":
                    break

                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                choices = chunk.get("choices") or []
                if not choices:
                    continue

                delta = choices[0].get("delta", {}).get("content")
                if isinstance(delta, str) and delta:
                    yield delta


# =========================================================
# STATIC AG-UI MESSAGE
# =========================================================


async def stream_static_message(
    message_id: str,
    text: str,
) -> AsyncIterator[str]:
    yield encode_sse(
        {
            "type": "TEXT_MESSAGE_START",
            "messageId": message_id,
            "role": "assistant",
        }
    )
    yield encode_sse(
        {
            "type": "TEXT_MESSAGE_CONTENT",
            "messageId": message_id,
            "delta": text,
        }
    )
    yield encode_sse(
        {
            "type": "TEXT_MESSAGE_END",
            "messageId": message_id,
        }
    )


# =========================================================
# AG-UI ENDPOINT
# =========================================================


@app.post("/agent")
async def run_agent(request: Request) -> StreamingResponse:
    payload = await request.json()
    thread_id = payload.get("threadId") or str(uuid.uuid4())
    run_id = payload.get("runId") or str(uuid.uuid4())
    message_id = str(uuid.uuid4())

    async def event_stream() -> AsyncIterator[str]:
        yield encode_sse(
            {
                "type": "RUN_STARTED",
                "threadId": thread_id,
                "runId": run_id,
            }
        )

        try:
            yield encode_sse(
                {
                    "type": "STEP_STARTED",
                    "stepName": "Classifying request",
                }
            )

            intent_result = await classify_intent(payload)

            yield encode_sse(
                {
                    "type": "STEP_FINISHED",
                    "stepName": "Classifying request",
                }
            )

            # -------------------------------------------------
            # VALIDATE BACKEND WORKER TOOLS (LOCAL MIGRATION CHECK)
            # -------------------------------------------------
            if intent_result.get("intent") == "VALIDATE_WORKER_TOOLS":
                worker_id = str(intent_result.get("workerId", "")).strip()
                access = resolve_worker_access(
                    worker_id,
                    get_flowone_capabilities(payload),
                )

                if access is None:
                    async for event in stream_static_message(
                        message_id,
                        "That FlowOne worker is not registered in this runtime.",
                    ):
                        yield event
                    yield encode_sse(
                        {
                            "type": "RUN_FINISHED",
                            "threadId": thread_id,
                            "runId": run_id,
                        }
                    )
                    return

                if not access.allowed:
                    async for event in stream_static_message(
                        message_id,
                        access.reason or "This worker is not available to your current FlowOne permissions.",
                    ):
                        yield event
                    yield encode_sse(
                        {
                            "type": "RUN_FINISHED",
                            "threadId": thread_id,
                            "runId": run_id,
                        }
                    )
                    return

                try:
                    execution_context = FlowOneRequestContext.from_request(request)
                except FlowOneContextError as error:
                    async for event in stream_static_message(
                        message_id,
                        f"Daily AR backend validation could not start: {error}",
                    ):
                        yield event
                    yield encode_sse(
                        {
                            "type": "RUN_FINISHED",
                            "threadId": thread_id,
                            "runId": run_id,
                        }
                    )
                    return

                service = DailyArToolService(FlowOneClient(execution_context))
                counts: dict[str, int] = {}
                failures: list[str] = []

                for tool_id in access.permitted_tools:
                    yield encode_sse(
                        {
                            "type": "STEP_STARTED",
                            "stepName": f"Validating {tool_id}",
                        }
                    )
                    try:
                        result = await service.run_tool(tool_id)
                        counts[tool_id] = result.count
                    except (FlowOneApiError, KeyError) as error:
                        failures.append(f"{tool_id}: {error}")
                    yield encode_sse(
                        {
                            "type": "STEP_FINISHED",
                            "stepName": f"Validating {tool_id}",
                        }
                    )

                lines = [
                    "Daily AR backend FlowOne tool validation finished.",
                    *[f"{tool_id}: {count} records" for tool_id, count in counts.items()],
                ]
                if access.denied_tools:
                    lines.append(
                        "Permission-skipped tools: " + ", ".join(access.denied_tools)
                    )
                if failures:
                    lines.append("Failures: " + " | ".join(failures))
                else:
                    lines.append("All permitted backend tools completed successfully.")

                async for event in stream_static_message(
                    message_id,
                    "\n".join(lines),
                ):
                    yield event

                yield encode_sse(
                    {
                        "type": "RUN_FINISHED",
                        "threadId": thread_id,
                        "runId": run_id,
                    }
                )
                return

            # -------------------------------------------------
            # RUN WORKER
            # -------------------------------------------------
            if intent_result.get("intent") == "RUN_WORKER":
                worker_id = str(intent_result.get("workerId", "")).strip()
                capabilities = get_flowone_capabilities(payload)
                access = resolve_worker_access(worker_id, capabilities)

                if access is None:
                    async for event in stream_static_message(
                        message_id,
                        "That FlowOne worker is not registered in this runtime.",
                    ):
                        yield event

                    yield encode_sse(
                        {
                            "type": "RUN_FINISHED",
                            "threadId": thread_id,
                            "runId": run_id,
                        }
                    )
                    return

                if not access.allowed:
                    async for event in stream_static_message(
                        message_id,
                        access.reason
                        or "This worker is not available to your current FlowOne permissions.",
                    ):
                        yield event

                    yield encode_sse(
                        {
                            "type": "RUN_FINISHED",
                            "threadId": thread_id,
                            "runId": run_id,
                        }
                    )
                    return

                try:
                    execution_context = FlowOneRequestContext.from_request(request)
                except FlowOneContextError as error:
                    async for event in stream_static_message(
                        message_id,
                        f"Daily AR could not start through the backend worker path: {error}",
                    ):
                        yield event
                    yield encode_sse(
                        {
                            "type": "RUN_FINISHED",
                            "threadId": thread_id,
                            "runId": run_id,
                        }
                    )
                    return

                if worker_id != "daily_ar_priorities":
                    async for event in stream_static_message(
                        message_id,
                        "That worker is registered but does not yet have a backend executor.",
                    ):
                        yield event
                    yield encode_sse(
                        {
                            "type": "RUN_FINISHED",
                            "threadId": thread_id,
                            "runId": run_id,
                        }
                    )
                    return

                worker_run_id = str(uuid.uuid4())
                service = DailyArToolService(FlowOneClient(execution_context))
                worker = DailyArPrioritiesWorker(
                    tools=service,
                    access=access,
                    capabilities=capabilities,
                    currency_code=get_flowone_currency_code(payload),
                )

                yield encode_sse(
                    {
                        "type": "CUSTOM",
                        "name": "FLOWONE_WORKER_STARTED",
                        "value": {
                            "workerId": access.worker.id,
                            "label": access.worker.label,
                            "runId": worker_run_id,
                            "executionMode": access.worker.execution_mode,
                            "outputResource": access.worker.output_resource,
                            "permittedTools": list(access.permitted_tools),
                            "deniedTools": list(access.denied_tools),
                            "steps": worker.steps,
                        },
                    }
                )

                try:
                    async for worker_event in worker.stream_run(
                        run_id=worker_run_id,
                    ):
                        if worker_event.get("kind") == "step":
                            yield encode_sse(
                                {
                                    "type": "CUSTOM",
                                    "name": "FLOWONE_WORKER_STEP",
                                    "value": {
                                        "workerId": access.worker.id,
                                        "runId": worker_run_id,
                                        **{
                                            key: value
                                            for key, value in worker_event.items()
                                            if key != "kind"
                                        },
                                    },
                                }
                            )
                        elif worker_event.get("kind") == "result":
                            yield encode_sse(
                                {
                                    "type": "CUSTOM",
                                    "name": "FLOWONE_WORKER_RESULT",
                                    "value": {
                                        "workerId": access.worker.id,
                                        "runId": worker_run_id,
                                        "outputResource": access.worker.output_resource,
                                        "run": worker_event.get("run"),
                                    },
                                }
                            )
                except DailyArWorkerError as error:
                    yield encode_sse(
                        {
                            "type": "CUSTOM",
                            "name": "FLOWONE_WORKER_FAILED",
                            "value": {
                                "workerId": access.worker.id,
                                "runId": worker_run_id,
                                "message": str(error),
                            },
                        }
                    )
                    async for event in stream_static_message(
                        message_id,
                        str(error),
                    ):
                        yield event
                    yield encode_sse(
                        {
                            "type": "RUN_FINISHED",
                            "threadId": thread_id,
                            "runId": run_id,
                        }
                    )
                    return

                async for event in stream_static_message(
                    message_id,
                    "Daily AR Priorities completed in the FlowOne backend and the priority cases are ready.",
                ):
                    yield event

                yield encode_sse(
                    {
                        "type": "RUN_FINISHED",
                        "threadId": thread_id,
                        "runId": run_id,
                    }
                )
                return

            # -------------------------------------------------
            # NAVIGATE
            # -------------------------------------------------
            if intent_result.get("intent") == "NAVIGATE":
                yield encode_sse(
                    {
                        "type": "CUSTOM",
                        "name": "NAVIGATE_FLOWONE",
                        "value": intent_result,
                    }
                )
                yield encode_sse(
                    {
                        "type": "RUN_FINISHED",
                        "threadId": thread_id,
                        "runId": run_id,
                    }
                )
                return

            # -------------------------------------------------
            # SHOW
            # -------------------------------------------------
            if intent_result.get("intent") == "SHOW":
                resource = str(intent_result.get("resource", ""))

                event_map = {
                    "overdue_customers": "SHOW_OVERDUE_CUSTOMERS",
                    "ar_summary": "SHOW_AR_SUMMARY",
                    "unpaid_invoices": "SHOW_UNPAID_INVOICES",
                    "customer_payments": "SHOW_CUSTOMER_PAYMENTS",
                    "credit_notes": "SHOW_CREDIT_NOTES",
                    "collection_overview": "SHOW_COLLECTION_OVERVIEW",
                }

                message_map = {
                    "overdue_customers": (
                        "I've opened the overdue-customer work surface using "
                        "your current FlowOne scope."
                    ),
                    "ar_summary": (
                        "I've opened your current Accounts Receivable summary "
                        "in the FlowOne work surface."
                    ),
                    "unpaid_invoices": (
                        "I've opened unpaid invoices available in your current "
                        "FlowOne scope."
                    ),
                    "customer_payments": (
                        "I've opened recent customer payment activity available "
                        "in your current FlowOne scope."
                    ),
                    "credit_notes": (
                        "I've opened credit-note activity available in your "
                        "current FlowOne scope."
                    ),
                    "collection_overview": (
                        "I've composed a collection overview from the FlowOne "
                        "data and tools available to you."
                    ),
                }

                event_name = event_map.get(resource)
                if event_name:
                    yield encode_sse(
                        {
                            "type": "CUSTOM",
                            "name": event_name,
                            "value": {"resource": resource},
                        }
                    )

                    async for event in stream_static_message(
                        message_id,
                        message_map[resource],
                    ):
                        yield event

                    yield encode_sse(
                        {
                            "type": "RUN_FINISHED",
                            "threadId": thread_id,
                            "runId": run_id,
                        }
                    )
                    return

            # -------------------------------------------------
            # ACT
            # -------------------------------------------------
            if intent_result.get("intent") == "ACT":
                action = str(intent_result.get("action", ""))

                if action == "collection_followup":
                    customer_name = str(
                        intent_result.get("customerName", "")
                    ).strip()

                    if not customer_name:
                        async for event in stream_static_message(
                            message_id,
                            "Which customer would you like me to prepare the collection follow-up for?",
                        ):
                            yield event

                        yield encode_sse(
                            {
                                "type": "RUN_FINISHED",
                                "threadId": thread_id,
                                "runId": run_id,
                            }
                        )
                        return

                    yield encode_sse(
                        {
                            "type": "CUSTOM",
                            "name": "PREPARE_COLLECTION_FOLLOWUP",
                            "value": {
                                "action": "collection_followup",
                                "customerName": customer_name,
                            },
                        }
                    )

                    yield encode_sse(
                        {
                            "type": "RUN_FINISHED",
                            "threadId": thread_id,
                            "runId": run_id,
                        }
                    )
                    return

            # -------------------------------------------------
            # ANSWER
            # -------------------------------------------------
            yield encode_sse(
                {
                    "type": "STEP_STARTED",
                    "stepName": "Understanding FlowOne context",
                }
            )

            model_messages = build_model_messages(payload)

            yield encode_sse(
                {
                    "type": "STEP_FINISHED",
                    "stepName": "Understanding FlowOne context",
                }
            )

            yield encode_sse(
                {
                    "type": "TEXT_MESSAGE_START",
                    "messageId": message_id,
                    "role": "assistant",
                }
            )

            async for delta in stream_openrouter(model_messages):
                yield encode_sse(
                    {
                        "type": "TEXT_MESSAGE_CONTENT",
                        "messageId": message_id,
                        "delta": delta,
                    }
                )

            yield encode_sse(
                {
                    "type": "TEXT_MESSAGE_END",
                    "messageId": message_id,
                }
            )

            yield encode_sse(
                {
                    "type": "RUN_FINISHED",
                    "threadId": thread_id,
                    "runId": run_id,
                }
            )

        except Exception as error:
            yield encode_sse(
                {
                    "type": "RUN_ERROR",
                    "threadId": thread_id,
                    "runId": run_id,
                    "message": str(error),
                }
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
