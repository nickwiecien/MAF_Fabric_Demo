"""CopilotKit AG-UI Backend — Fabric Data Agents Orchestrator.

Exposes the Fabric orchestrator agent via AG-UI protocol for CopilotKit consumption.
"""

import os
import time
from pathlib import Path

import json

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from azure.identity import DefaultAzureCredential
from agent_framework.azure import AzureOpenAIResponsesClient
from agent_framework_ag_ui import AgentFrameworkAgent, add_agent_framework_fastapi_endpoint
from ag_ui.encoder import EventEncoder

# Load .env from agents/ directory (shared config)
_agents_env = Path(__file__).resolve().parent.parent.parent / "agents" / ".env"
load_dotenv(_agents_env)

# ---------------------------------------------------------------------------
# Load system prompt
# ---------------------------------------------------------------------------
_prompt_path = (
    Path(__file__).resolve().parent.parent.parent
    / "agents"
    / "orchestrator_agent"
    / "prompts"
    / "orchestrator_agent.md"
)
_instructions = _prompt_path.read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Entra ID authentication for Fabric MCP
# ---------------------------------------------------------------------------
FABRIC_DATA_AGENT_SCOPE = "https://api.fabric.microsoft.com/.default"

credential = DefaultAzureCredential()
_cached_token = credential.get_token(FABRIC_DATA_AGENT_SCOPE)
_TOKEN_REFRESH_BUFFER_SECS = 300

fabric_headers = {
    "Authorization": f"Bearer {_cached_token.token}",
    "Content-Type": "application/json",
}


def refresh_fabric_headers() -> None:
    """Refresh the Fabric bearer token when near expiry."""
    global _cached_token
    if time.time() >= _cached_token.expires_on - _TOKEN_REFRESH_BUFFER_SECS:
        _cached_token = credential.get_token(FABRIC_DATA_AGENT_SCOPE)
        fabric_headers["Authorization"] = f"Bearer {_cached_token.token}"


# ---------------------------------------------------------------------------
# Azure OpenAI client
# ---------------------------------------------------------------------------
client = AzureOpenAIResponsesClient(
    base_url=os.environ["AOAI_ENDPOINT"],
    api_key=os.environ["AOAI_KEY"],
    deployment_name=os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
)

# ---------------------------------------------------------------------------
# Fabric Data Agent MCP tools
# ---------------------------------------------------------------------------
sales_tool = client.get_mcp_tool(
    name="Sales Agent",
    url=os.environ["FABRIC_SALES_AGENT_MCP_URL"],
    headers=fabric_headers,
    approval_mode="never_require",
)

customer_tool = client.get_mcp_tool(
    name="Customer Agent",
    url=os.environ["FABRIC_CUSTOMER_AGENT_MCP_URL"],
    headers=fabric_headers,
    approval_mode="never_require",
)

product_tool = client.get_mcp_tool(
    name="Product Agent",
    url=os.environ["FABRIC_PRODUCT_AGENT_MCP_URL"],
    headers=fabric_headers,
    approval_mode="never_require",
)

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
agent = client.as_agent(
    name="Fabric Data Agents Orchestrator",
    description=(
        "Orchestrates three Fabric data agent MCP tools (Sales, Customer, Product) "
        "to answer business questions across orders, customers, addresses, products, "
        "categories, and more."
    ),
    instructions=_instructions,
    tools=[sales_tool, customer_tool, product_tool],
)

# ---------------------------------------------------------------------------
# FastAPI + AG-UI
# ---------------------------------------------------------------------------
app = FastAPI(title="Fabric Orchestrator — CopilotKit Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Token refresh middleware
@app.middleware("http")
async def refresh_token_middleware(request, call_next):
    refresh_fabric_headers()
    return await call_next(request)


# ---------------------------------------------------------------------------
# AG-UI endpoint with tool-message stripping
# ---------------------------------------------------------------------------
# Azure OpenAI Responses API with hosted MCP tools cannot replay previous
# tool call/result messages. CopilotKit sends the full conversation history
# on each turn, which causes 400 errors. We strip tool-related messages and
# only send the latest user message + system context.

_wrapped_agent = AgentFrameworkAgent(agent=agent)


def _strip_tool_messages(messages: list[dict]) -> list[dict]:
    """Keep only user/assistant text messages, drop tool calls and results."""
    cleaned = []
    for msg in messages:
        role = msg.get("role", "")
        # Skip tool result messages entirely
        if role == "tool":
            continue
        # For assistant messages, check if it's a tool call only
        if role == "assistant":
            content = msg.get("content")
            tool_calls = msg.get("tool_calls") or msg.get("toolCalls")
            # Skip assistant messages that are only tool calls (no text)
            if tool_calls and not content:
                continue
            # If it has both text and tool calls, keep only the text
            if tool_calls and content:
                msg = {k: v for k, v in msg.items() if k not in ("tool_calls", "toolCalls")}
        cleaned.append(msg)
    return cleaned


@app.post("/fabric_orchestrator")
async def fabric_orchestrator_endpoint(request: Request):
    input_data = await request.json()

    # Strip tool messages to prevent Azure OpenAI Responses API errors
    if "messages" in input_data:
        input_data["messages"] = _strip_tool_messages(input_data["messages"])

    async def event_generator():
        from ag_ui.core import RunErrorEvent, MessagesSnapshotEvent
        encoder = EventEncoder()
        try:
            async for event in _wrapped_agent.run(input_data):
                # Skip MessagesSnapshotEvent — it resets CopilotKit's message
                # list and drops tool call renders from previous turns
                if isinstance(event, MessagesSnapshotEvent):
                    continue
                yield encoder.encode(event)
        except Exception as e:
            # Emit AG-UI error events so the frontend shows the error gracefully
            import traceback
            traceback.print_exc()
            try:
                yield encoder.encode(RunErrorEvent(message=str(e), code="AGENT_ERROR"))
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def main():
    port = int(os.getenv("BACKEND_PORT", "8888"))
    print(f"Starting AG-UI backend on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
