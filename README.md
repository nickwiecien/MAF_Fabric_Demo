# Orchestrating Fabric Data Agents

An orchestrator agent built with the **Microsoft Agent Framework (MAF)** that queries three Microsoft Fabric data agents — **Sales**, **Customer**, and **Product** — via hosted MCP endpoints. The Azure OpenAI Responses API executes MCP tools server-side, so no local MCP server is needed.

The DevUI version includes **persistent cross-session memory** powered by [Mem0](https://mem0.ai/) OSS with **Azure AI Search** as the vector store. The agent remembers user preferences (e.g., preferred output format, frequently queried customers) across conversations.

Two interfaces ship from the same codebase:

| Interface | Folder | Purpose |
|-----------|--------|---------|
| **DevUI** | `agents/` | Local development & testing via browser UI |
| **M365 Channels** | `m365_agents_orchestrator/` | Azure App Service bot for Teams, Outlook, Copilot |

Both share the same system prompt (`prompts/orchestrator_agent.md`) and the same agent wiring pattern.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        User Interfaces                              │
│                                                                     │
│  ┌──────────────┐            ┌─────────────────────────────────┐    │
│  │    DevUI     │            │  M365 Channels                  │    │
│  │  (localhost) │            │  (Teams / Outlook / Copilot)    │    │
│  └──────┬───────┘            └───────────────┬─────────────────┘    │
│         │                                    │                      │
│         │  agents/                           │  m365_agents_        │
│         │  orchestrator_agent/               │  orchestrator/       │
│         │  agent.py                          │  src/agent.py        │
└─────────┼────────────────────────────────────┼──────────────────────┘
          │                                    │
          │  DefaultAzureCredential            │  SSO → OBO token
          │  (az login)                        │  (Bot Service OAuth)
          ▼                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   Microsoft Agent Framework (MAF)                   │
│                                                                     │
│  AzureOpenAIResponsesClient                                         │
│    ├── get_mcp_tool("Sales Agent",    url, headers)                 │
│    ├── get_mcp_tool("Customer Agent", url, headers)                 │
│    ├── get_mcp_tool("Product Agent",  url, headers)                 │
│    ├── @tool GraphQL tools (search, orders, analytics, …)           │
│    ├── as_agent(tools=[...], instructions=prompt)                   │
│    └── context_providers=[Mem0ContextProvider]                      │
└──────────┬──────────────┬──────────────┬────────────────────────────┘
           │              │              │
           ▼              │              ▼
┌──────────────────────┐  │  ┌─────────────────────────────┐
│  Mem0 OSS Memory     │  │  │  Fabric GraphQL API         │
│  (Azure AI Search)   │  │  │  (httpx — async, direct)    │
│  - User preferences  │  │  │  Products, Customers,       │
│  - Cross-session     │  │  │  Orders, Addresses,         │
└──────────────────────┘  │  │  Analytics / Aggregations   │
                          │  └─────────────────────────────┘
──────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  Azure OpenAI Responses API                         │
│           (hosted MCP tool execution — server-side)                 │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                ┌────────────────┼────────────────┐
                ▼                ▼                ▼
       ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
       │ Sales Agent  │ │Customer Agent│ │Product Agent │
       │ (Fabric MCP) │ │ (Fabric MCP) │ │ (Fabric MCP) │
       └──────────────┘ └──────────────┘ └──────────────┘
```

---

## Prerequisites

| Requirement | Details |
|-------------|---------|
| **Python 3.11+** | Required runtime |
| **Azure CLI** | Authenticated via `az login` for local development |
| **Azure OpenAI resource** | Deployment supporting the Responses API (e.g., `gpt-4o`) |
| **Microsoft Fabric workspace** | With three data agents (Sales, Customer, Product) created and MCP endpoints enabled |
| **Entra ID permissions** | Your identity (or managed identity) must have access to the Fabric workspace |
| **Azure AI Search** | Any tier (Free works for dev) — used as the mem0 vector store for agent memory |

---

## Quick Start — DevUI (Local Development)

### 1. Create a virtual environment

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r agents/requirements.txt
```

### 3. Create `agents/.env`

```env
AOAI_ENDPOINT=https://<your-aoai-resource>.openai.azure.com/
AOAI_KEY=<your-api-key>
AZURE_OPENAI_DEPLOYMENT_NAME=<your-deployment-name>

# NOTE: Do NOT set AZURE_OPENAI_API_VERSION — the MAF SDK default ("preview")
# is correct. Setting an explicit dated version causes 400 errors.

AOAI_EMBEDDINGS_MODEL=<your-embedding-deployment-name>  # e.g. text-embedding-3-small

FABRIC_SALES_AGENT_MCP_URL=https://api.fabric.microsoft.com/v1/mcp/workspaces/<workspace-id>/dataagents/<sales-agent-id>/agent
FABRIC_CUSTOMER_AGENT_MCP_URL=https://api.fabric.microsoft.com/v1/mcp/workspaces/<workspace-id>/dataagents/<customer-agent-id>/agent
FABRIC_PRODUCT_AGENT_MCP_URL=https://api.fabric.microsoft.com/v1/mcp/workspaces/<workspace-id>/dataagents/<product-agent-id>/agent

FABRIC_GRAPHQL_API_URL=https://api.fabric.microsoft.com/v1/workspaces/<workspace-id>/graphqlapis/<graphql-api-id>/graphql

# Mem0 + Azure AI Search (memory store)
AZURE_AI_SEARCH_SERVICE_NAME=<your-search-service-name>
AZURE_AI_SEARCH_API_KEY=<your-search-admin-key>  # or leave empty for DefaultAzureCredential
AZURE_AI_SEARCH_INDEX_NAME=mem0-orchestrator-memories
```

### 4. Authenticate to Azure

```bash
az login
```

This provides `DefaultAzureCredential` with a Fabric token for MCP endpoint authentication.

### 5. Launch DevUI

```bash
python run.py
```

Opens a browser UI at `http://localhost:8080` where you can chat with the orchestrator agent.

---

## M365 Channels (Teams / Outlook / Copilot)

The M365 deployment requires Azure Bot Service, App Service, Entra ID app registration, and an OAuth connection for Fabric SSO/OBO.

See the full deployment walkthrough:

- **[Getting Started — Deployment Guide](docs/getting-started-deployment-guide.md)** — step-by-step setup from scratch
- **[M365 Implementation Overview](docs/m365-implementation-overview.md)** — architecture, auth flow, and code walkthrough

The environment variable template is at [`m365_agents_orchestrator/env.TEMPLATE`](m365_agents_orchestrator/env.TEMPLATE).

---

## Project Structure

```
agents/                              # DevUI version (local development)
├── .env                             # Environment variables (not committed)
├── requirements.txt                 # Python dependencies
└── orchestrator_agent/
    ├── __init__.py
    ├── agent.py                     # Agent definition + MCP tools + GraphQL tools + Mem0 memory
    ├── graphql_tools.py             # 6 purpose-driven GraphQL tools (httpx)
    └── prompts/
        └── orchestrator_agent.md    # Shared system prompt (incl. routing & memory instructions)

m365_agents_orchestrator/            # M365 Channels version (Teams, Outlook, Copilot)
├── .env                             # Local-only env vars (not committed)
├── env.TEMPLATE                     # Template for required environment variables
├── requirements.txt                 # Python dependencies (pinned)
├── startup.sh                       # App Service startup command
├── README.md
├── appPackage/
│   ├── manifest.json                # Teams app manifest
│   ├── color.png
│   └── outline.png
├── prompts/
│   └── orchestrator_agent.md        # Shared system prompt
└── src/
    ├── __init__.py
    ├── main.py                      # Entrypoint
    ├── agent.py                     # Core bot logic — MAF + SSO/OBO + handlers
    └── start_server.py              # aiohttp server bootstrap

docs/                                # Documentation
├── getting-started-deployment-guide.md   # Full deployment walkthrough
└── m365-implementation-overview.md       # Architecture & code deep-dive

run.py                               # DevUI launcher
start.ps1                            # PowerShell launcher
```

---

## How It Works

### Authentication

| Interface | Auth Method | Fabric Token Source |
|-----------|-------------|---------------------|
| **DevUI** | `DefaultAzureCredential` (`az login`) | Developer's own identity |
| **M365** | Bot Service SSO → OBO token exchange | Signed-in user's identity (delegated) |

Fabric data agents require **delegated (user) access** — app-only / managed-identity tokens are rejected by design.

### Tool Registration

Each Fabric data agent is registered as a hosted MCP tool via the MAF SDK:

```python
client = AzureOpenAIResponsesClient(
    endpoint=os.environ["AOAI_ENDPOINT"],
    api_key=os.environ["AOAI_KEY"],
    deployment_name=os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
)

sales_tool = client.get_mcp_tool(
    name="Sales Agent",
    url=os.environ["FABRIC_SALES_AGENT_MCP_URL"],
    headers=fabric_headers,  # Bearer token for Fabric API
    approval_mode="never_require",
)
```

### Agent Execution

The agent is created with `as_agent()` and invoked with `agent.run()` (DevUI) or within the M365 bot's turn handler:

```python
agent = client.as_agent(
    name="Fabric Data Agents Orchestrator",
    instructions=_instructions,
    tools=[sales_tool, customer_tool, product_tool],
)
```

Azure OpenAI's Responses API handles MCP tool execution server-side — the orchestrator sends the tool definitions and Fabric bearer token, and Azure OpenAI calls the MCP endpoints directly.

### Persistent Memory (DevUI)

The DevUI version integrates **Mem0 OSS** with **Azure AI Search** as the vector store to provide persistent cross-session memory:

- **How it works:** The `Mem0ContextProvider` hooks into the Agent Framework's context provider lifecycle. Before each agent run, it queries mem0 for relevant memories and injects them into the system prompt. After each run, it extracts new facts from the conversation and stores them.
- **Vector store:** Azure AI Search stores memory embeddings. The index (`mem0-orchestrator-memories`) is auto-created on first use.
- **LLM + Embedder:** Both use your Azure OpenAI resource — the chat model for memory extraction and the embedding model for vector encoding.
- **Scoping:** Memories are currently scoped to a fixed `user_id` (`devui-user`). For multi-user scenarios, this can be made dynamic per session.

```python
mem0_provider = Mem0ContextProvider(
    source_id="mem0",
    user_id="devui-user",
    mem0_client=mem0_client,  # AsyncMemory backed by Azure AI Search
)

agent = client.as_agent(
    ...
    context_providers=[mem0_provider],
)
```

### Sessions

- **DevUI** — each browser session is a separate conversation with its own history. Mem0 memories persist across sessions.
- **M365** — conversation state is managed per Teams conversation via `MemoryStorage`. The M365 version also handles `signin/tokenExchange`, `signin/verifystate`, and `signin/failure` invoke activities for SSO flow.

---

## Fabric Data Agents

| Agent | Data Domain | Key Tables |
|-------|-------------|------------|
| **Sales Agent** | Orders, order status, order totals, line items | SalesOrderHeader, SalesOrderDetail |
| **Customer Agent** | Customer identity, addresses (billing/shipping) | Customer, CustomerAddress, Address |
| **Product Agent** | Products, categories, models, descriptions | Product, ProductCategory, ProductModel, ProductDescription |

MCP endpoint pattern:
```
https://api.fabric.microsoft.com/v1/mcp/workspaces/<workspace-id>/dataagents/<agent-id>/agent
```

See each agent's Fabric workspace page for endpoint details and schemas.

---

## GraphQL API Integration

In addition to the MCP data agents, the orchestrator includes **six purpose-driven GraphQL tools** that execute structured queries directly against a Fabric GraphQL API endpoint. These tools use [`httpx`](https://www.python-httpx.org/) for async HTTP calls and are authenticated with the same Fabric bearer token used by the MCP tools.

### GraphQL Tools

| Tool | Purpose | Example Query |
|------|---------|---------------|
| `search_products` | Filter the product catalog by color, price range, size, category, or product number | "Show me all red products under $50" |
| `get_customer_info` | Look up customers by ID, email address, or company name | "Find customer with email containing @contoso" |
| `get_customer_addresses` | Retrieve all addresses linked to a customer (joins CustomerAddress → Address) | "What are customer 123's addresses?" |
| `get_sales_orders` | Query order headers with date range, customer, status, and total filters | "Orders for customer 29545 in 2024" |
| `get_order_line_items` | Get detailed line items (products, qty, price, discount) for a specific order | "What's in order 71774?" |
| `get_sales_analytics` | Run aggregations (sum, avg, count, min, max) grouped by any field | "Total revenue by ship method" |

### Routing: When to Use GraphQL vs MCP

The system prompt includes routing guidance so the agent selects the right tool type:

| Use GraphQL Tools When | Use MCP Data Agents When |
|------------------------|--------------------------|
| User provides exact filters, IDs, or date ranges | Question is open-ended or exploratory |
| Aggregations/analytics are needed (sum, avg, group-by) | Domain-specific reasoning or interpretation is needed |
| Drilling into a specific order's line items | Unsure what data exists — let the agent explore |
| Precise joins across entities (e.g., customer → addresses) | Complex questions benefiting from agent reasoning |

The agent can also **combine both** — e.g., use GraphQL for structured data retrieval and MCP for contextual interpretation in the same turn.

### Schema Notes

The GraphQL API exposes **flat entities** (no nested relationships). Cross-entity queries require multiple sequential calls — for example, `get_customer_addresses` chains `customerAddresses` → `addresses` in two hops. The schema supports filtering (`eq`, `gt`, `contains`, `in`, etc.), pagination (`first`, `after`), ordering, and `groupBy` aggregations (`sum`, `avg`, `min`, `max`, `count`).

---

## SDK Versions

> **Important:** The `openai` SDK version matters — different versions construct Azure OpenAI URLs differently, which can cause silent failures or 4xx errors.

| Package | Version | Notes |
|---------|---------|-------|
| `agent-framework-azure-ai` | `1.0.0rc6` | MAF Azure AI integration |
| `agent-framework-core` | `1.0.1` | MAF core runtime |
| `agent-framework-mem0` | `1.0.0b260409` | MAF Mem0 context provider |
| `agent-framework-devui` | `1.0.0b260414` | MAF DevUI server |
| `mem0ai` | `1.0.1` | Mem0 OSS memory layer |
| `azure-search-documents` | `11.5.2` | Azure AI Search SDK (mem0 vector store) |
| `httpx` | `≥0.27` | Async HTTP client for GraphQL API calls |
| `openai` | `2.30.0` | Azure OpenAI SDK |
| `microsoft-agents-hosting-aiohttp` | `0.8.0` | M365 Agents SDK |
| `microsoft-agents-hosting-core` | `0.8.0` | M365 Agents SDK |
| `microsoft-agents-authentication-msal` | `0.8.0` | M365 Agents SDK |
| `microsoft-agents-activity` | `0.8.0` | M365 Agents SDK |
| `azure-identity` | `≥1.19.0` | Entra ID / DefaultAzureCredential |

### AZURE_OPENAI_API_VERSION — Do Not Set

Do **not** set the `AZURE_OPENAI_API_VERSION` environment variable. The MAF SDK uses a default value (`"preview"`) that works correctly with the Responses API. Setting an explicit dated version (e.g., `2025-03-01-preview`) causes **400 errors**.

---

## Adapting for Your Own Fabric Data Agents

1. **Create Fabric data agents** in your workspace — each agent exposes a lakehouse, warehouse, or other Fabric data source through a natural-language query interface.
2. **Update `.env`** with the new MCP URLs for each agent (format: `https://api.fabric.microsoft.com/v1/mcp/workspaces/<workspace-id>/dataagents/<agent-id>/agent`).
3. **Add or remove `get_mcp_tool()` calls** in `agent.py` to match your agents, and update the `tools=[...]` list in `as_agent()`.
4. **Update the system prompt** in `prompts/orchestrator_agent.md` — describe each agent's capabilities so the orchestrator routes queries correctly.
5. **Configure auth** — ensure your identity (local dev via `az login`) or the bot's OAuth connection (M365) has access to the Fabric workspace.

---

## Modification Notes — DevUI Patches

Two files in the installed `agent-framework-devui` and `agent-framework` packages required local patches for correct MCP tool rendering in the DevUI. These fix gaps in the current RC releases and will likely be resolved in future versions.

### `agent_framework_devui/_mapper.py`

**Problem:** DevUI displayed `"Warning: Unknown content type: Content"` for every MCP tool call and result because the mapper's `content_mappers` dict had no entries for `mcp_server_tool_call` or `mcp_server_tool_result` content types.

**Changes:**
1. **Registered handlers** in `content_mappers` for `mcp_server_tool_call` and `mcp_server_tool_result`.
2. **`_map_mcp_server_tool_call_content`** — New method that maps MCP tool calls to the same `ResponseOutputItemAddedEvent` + `ResponseFunctionCallArgumentsDeltaEvent` events used by regular function calls. Includes deduplication logic: only emits the "added" event once per `call_id`, so streaming argument deltas don't create duplicate tool bubbles.
3. **`_map_mcp_server_tool_result_content`** — New method that maps MCP tool results to `ResponseFunctionResultComplete`. Returns `None` when output is empty (suppresses the premature in-progress result that arrives before the actual output).

### `agent_framework/openai/_responses_client.py`

**Problem:** MCP tool arguments and results were not captured from Azure OpenAI streaming events. The `response.output_item.added` event arrives with `McpCall.output=None` (in-progress state), and the actual data comes through separate streaming events that were unhandled.

**Changes:**
1. **MCP call ID tracking** — In the `response.output_item.added` → `mcp_call` handler, added registration to `function_call_ids` so argument delta events can look up the call by `output_index`.
2. **`response.output_item.done` handler** — New case that captures the completed `McpCall` (with `output` populated) and emits `Content.from_mcp_server_tool_result` with the actual result text.
3. **`response.mcp_call_arguments.delta` / `.done` handlers** — New cases that convert MCP argument streaming events into `Content.from_mcp_server_tool_call` with the argument data, so DevUI can display what was sent to each tool.

> **Note:** These patches live in `.venv/Lib/site-packages/` and will be lost on `pip install --force-reinstall`. They should be re-applied after dependency updates until the framework ships native support.

---

## Documentation

| Document | Description |
|----------|-------------|
| [Getting Started — Deployment Guide](docs/getting-started-deployment-guide.md) | Full step-by-step M365 deployment |
| [M365 Implementation Overview](docs/m365-implementation-overview.md) | Architecture, auth flow, code walkthrough |
| [Local Testing Guide](docs/local-testing-guide.md) | DevUI and Bot Framework Emulator setup |

---

## License

See [LICENSE](LICENSE).