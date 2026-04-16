"""Fabric Data Agents Orchestrator — single agent wired to 3 Fabric data agent MCP tools.

This module creates an Agent that can query Sales, Customer, and Product
data through Fabric data agent MCP endpoints, authenticated via Entra ID.

DevUI discovers this agent via the __init__.py that exports `agent`.
"""

import os
import time
from pathlib import Path

from azure.identity import DefaultAzureCredential
from agent_framework.openai import OpenAIChatClient
from agent_framework.mem0 import Mem0ContextProvider
from dotenv import load_dotenv

from .graphql_tools import ALL_GRAPHQL_TOOLS
from mem0 import AsyncMemory
from mem0.configs.base import MemoryConfig

# Load shared .env from agents/ directory, then any local .env
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

# ---------------------------------------------------------------------------
# Load system prompt from markdown
# ---------------------------------------------------------------------------
_prompt_path = Path(__file__).resolve().parent / "prompts" / "orchestrator_agent.md"
_instructions = _prompt_path.read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Entra ID authentication
# ---------------------------------------------------------------------------
# DefaultAzureCredential supports az login (local dev) and managed identity
# (production). A Fabric token is requested explicitly for MCP headers.
FABRIC_DATA_AGENT_SCOPE = "https://api.fabric.microsoft.com/.default"

credential = DefaultAzureCredential()
_cached_token = credential.get_token(FABRIC_DATA_AGENT_SCOPE)

# Refresh the token if it expires within this many seconds
_TOKEN_REFRESH_BUFFER_SECS = 300  # 5 minutes

fabric_headers = {
    "Authorization": f"Bearer {_cached_token.token}",
    "Content-Type": "application/json",
}


def refresh_fabric_headers() -> None:
    """Refresh the Fabric bearer token in-place when it is expired or near expiry.

    The ``fabric_headers`` dict is shared by reference with all MCP tool
    definitions.  Mutating it in-place ensures that subsequent Azure OpenAI
    Responses API calls include a valid token without recreating tools or the
    agent.
    """
    global _cached_token

    if time.time() >= _cached_token.expires_on - _TOKEN_REFRESH_BUFFER_SECS:
        _cached_token = credential.get_token(FABRIC_DATA_AGENT_SCOPE)
        fabric_headers["Authorization"] = f"Bearer {_cached_token.token}"

# ---------------------------------------------------------------------------
# Azure OpenAI client (API key auth)
# ---------------------------------------------------------------------------
client = OpenAIChatClient(
    model=os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
    base_url=os.environ["AOAI_ENDPOINT"],
    api_key=os.environ["AOAI_KEY"],
)

# ---------------------------------------------------------------------------
# Fabric Data Agent MCP tools (hosted — executed server-side by Azure OpenAI)
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
# Mem0 memory store — backed by Azure AI Search
# ---------------------------------------------------------------------------
# Mem0's AzureConfig expects the bare Azure endpoint (no /openai/v1/ suffix)
_aoai_base = os.environ["AOAI_ENDPOINT"].rstrip("/").removesuffix("/openai/v1").removesuffix("/openai")

mem0_config = {
    "vector_store": {
        "provider": "azure_ai_search",
        "config": {
            "service_name": os.environ["AZURE_AI_SEARCH_SERVICE_NAME"],
            "api_key": os.environ.get("AZURE_AI_SEARCH_API_KEY", ""),
            "collection_name": os.environ.get(
                "AZURE_AI_SEARCH_INDEX_NAME", "mem0-orchestrator-memories"
            ),
            "embedding_model_dims": 1536,  # text-embedding-3-small
        },
    },
    "llm": {
        "provider": "azure_openai",
        "config": {
            "model": os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
            "azure_kwargs": {
                "azure_deployment": os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
                "azure_endpoint": _aoai_base,
                "api_key": os.environ["AOAI_KEY"],
                "api_version": "2025-04-01-preview",
            },
        },
    },
    "embedder": {
        "provider": "azure_openai",
        "config": {
            "model": os.environ["AOAI_EMBEDDINGS_MODEL"],
            "azure_kwargs": {
                "azure_deployment": os.environ["AOAI_EMBEDDINGS_MODEL"],
                "azure_endpoint": _aoai_base,
                "api_key": os.environ["AOAI_KEY"],
                "api_version": "2025-04-01-preview",
            },
        },
    },
}

mem0_client = AsyncMemory(MemoryConfig(
    vector_store=mem0_config["vector_store"],
    llm=mem0_config["llm"],
    embedder=mem0_config["embedder"],
))

mem0_provider = Mem0ContextProvider(
    source_id="mem0",
    user_id="devui-user",
    mem0_client=mem0_client,
)

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
agent = client.as_agent(
    name="Fabric Data Agents Orchestrator",
    description=(
        "Orchestrates three Fabric data agent MCP tools (Sales, Customer, Product) "
        "to answer business questions across orders, customers, addresses, products, "
        "categories, and more. Remembers user preferences across sessions."
    ),
    instructions=_instructions,
    tools=[sales_tool, customer_tool, product_tool] + ALL_GRAPHQL_TOOLS,
    context_providers=[mem0_provider],
)
