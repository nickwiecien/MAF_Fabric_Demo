# Fabric Data Agents Orchestrator — M365 Channels

An orchestrator agent built with the [Microsoft 365 Agents SDK](https://github.com/microsoft/Agents) that queries three Microsoft Fabric data agents (Sales, Customer, Product) via hosted MCP endpoints and exposes the experience through **M365 channels** — Teams, Outlook, Copilot, and more.

This is the M365-channel companion to the [DevUI-based orchestrator](../agents/orchestrator_agent/) in the same repository. Both share the same Fabric data agents and system prompt, but this version uses the M365 Agents SDK to serve responses through Azure Bot Service channels instead of DevUI.

## Architecture

```
        ┌──────────────────────────────────────────────┐
        │       M365 Channels (Teams / Outlook / …)    │
        └──────────────────┬───────────────────────────┘
                           │  Azure Bot Service
                           ▼
        ┌──────────────────────────────────────────────┐
        │  M365 Agents SDK (aiohttp — /api/messages)   │
        │  ┌────────────────────────────────────────┐  │
        │  │  Fabric Data Agents Orchestrator       │  │
        │  │  (AgentApplication + Azure OpenAI)     │  │
        │  └───────────────┬────────────────────────┘  │
        └──────────────────┼───────────────────────────┘
                           │
                           │ Azure OpenAI Responses API
                           │ (hosted MCP — server-side execution)
                           ▼
        ┌──────────────────────────────────────────────┐
        │        Azure OpenAI (Responses API)          │
        └──────┬──────────────┬──────────────┬─────────┘
               │              │              │
               ▼              ▼              ▼
     ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
     │ Sales Agent  │ │  Customer    │ │  Product     │
     │ (Fabric MCP) │ │ Agent (MCP)  │ │ Agent (MCP)  │
     └──────────────┘ └──────────────┘ └──────────────┘
```

The agent uses **hosted MCP execution** — Azure OpenAI calls the Fabric data agent MCP endpoints directly (server-side) via the Responses API, rather than calling them locally. User messages arrive through M365 channels via Azure Bot Service and the M365 Agents SDK.

## Prerequisites

- **Python 3.11+**
- **Azure CLI** — logged in to the correct tenant
- **Azure Bot registration** — with Client ID, Client Secret, and Tenant ID
- **Azure OpenAI** deployment with API key access (model supporting Responses API)
- **Microsoft Fabric** workspace with three data agents configured (Sales, Customer, Product)
- **Dev tunnel** (for local development) — [Get started with dev tunnels](https://learn.microsoft.com/azure/developer/dev-tunnels/get-started)

## Quick Start

### 1. Clone and create virtual environment

```bash
cd m365_agents_orchestrator
python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### 2. Configure environment variables

Copy `env.TEMPLATE` to `.env` and fill in your values:

```bash
cp env.TEMPLATE .env
```

```dotenv
# M365 Agents SDK — Azure Bot registration
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID=<your-bot-app-id>
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET=<your-bot-client-secret>
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID=<your-tenant-id>

# Azure OpenAI
AOAI_ENDPOINT=https://<your-aoai>.openai.azure.com/
AOAI_KEY=<your-api-key>
AZURE_OPENAI_DEPLOYMENT_NAME=<your-deployment>
# NOTE: Do NOT set AZURE_OPENAI_API_VERSION — the MAF SDK default ("preview") is correct.

# Fabric Data Agent MCP URLs
FABRIC_SALES_AGENT_MCP_URL=https://api.fabric.microsoft.com/v1/mcp/workspaces/<workspace-id>/dataagents/<sales-agent-id>/agent
FABRIC_CUSTOMER_AGENT_MCP_URL=https://api.fabric.microsoft.com/v1/mcp/workspaces/<workspace-id>/dataagents/<customer-agent-id>/agent
FABRIC_PRODUCT_AGENT_MCP_URL=https://api.fabric.microsoft.com/v1/mcp/workspaces/<workspace-id>/dataagents/<product-agent-id>/agent
```

### 3. Create an Azure Bot

1. [Create an Azure Bot](https://aka.ms/AgentsSDK-CreateBot)
2. Record the **Application ID**, **Tenant ID**, and **Client Secret**
3. Add the values to your `.env` file

### 4. Set up a dev tunnel (local development)

```bash
devtunnel host -p 8000 --allow-anonymous
```

Note the `Connect via browser:` URL and update the Azure Bot **Messaging endpoint** to `{tunnel-url}/api/messages`.

### 5. Configure Fabric SSO/OBO (per-user Fabric access)

To have each Teams user query Fabric with **their own identity**, configure an OAuth connection on your Azure Bot:

1. **Add API permission** to your Entra ID app registration:
   - Azure portal → App registrations → your app → **API permissions** → Add a permission
   - APIs my organization uses → search `Power BI Service` (or `https://analysis.windows.net`)
   - Delegated permissions → select `Workspace.Read.All` (or the Fabric scopes you need)
   - Grant admin consent

2. **Create an OAuth connection** on the Azure Bot resource:
   - Azure portal → Azure Bot → **Configuration** → **OAuth Connection Settings** → Add
   - **Name**: `FabricOAuth` (must match `FABRIC_ABS_OAUTH_CONNECTION_NAME` in `.env`)
   - **Service Provider**: `Azure Active Directory v2`
   - **Client ID**: your bot app registration client ID
   - **Client Secret**: your bot app registration secret
   - **Tenant ID**: your tenant (or `common` for multi-tenant)
   - **Scopes**: `https://api.fabric.microsoft.com/.default`

3. **Update `.env`**:
   ```dotenv
   USE_ANONYMOUS_MODE=False
   FABRIC_ABS_OAUTH_CONNECTION_NAME=FabricOAuth
   ```

When a Teams user sends a message, the SDK will silently attempt SSO. If successful, the user's token is exchanged via OBO for a Fabric-scoped token, which is passed to the MCP tool headers. The user never sees a sign-in prompt (unless SSO can't complete silently).

### 6. Log in to Azure (local dev only)

When running in **anonymous mode** (`USE_ANONYMOUS_MODE=True`), the agent uses your local Azure CLI session for Fabric access:

```bash
az login --tenant "<your-tenant>.onmicrosoft.com"
```

The agent uses `DefaultAzureCredential` → `AzureCliCredential` to obtain a Fabric bearer token for MCP authentication.

### 7. Start the agent

```bash
python -m src.main
```

You should see:

```
======== Running on http://0.0.0.0:8000 ========
```

### 8. Test in a channel

- **Web Chat**: Go to your Azure Bot Service resource → **Test in Web Chat**
- **Teams**: Add the bot to Teams via the Azure Bot **Channels** configuration
- **Copilot / Outlook**: Configure additional channels in the Azure portal

## Project Structure

```
m365_agents_orchestrator/
├── env.TEMPLATE               # Environment variable template
├── requirements.txt           # Python dependencies
├── prompts/
│   └── orchestrator_agent.md  # System prompt (tool routing, output formatting)
├── src/
│   ├── __init__.py
│   ├── main.py                # Entry point — logging + server start
│   ├── agent.py               # AgentApplication + Azure OpenAI + MCP tool wiring
│   └── start_server.py        # aiohttp server setup (/api/messages)
└── README.md                  # This file
```

## How It Works

1. **M365 Channel Ingress** — User messages arrive through Teams, Outlook, or other M365 channels via Azure Bot Service. The M365 Agents SDK (`AgentApplication`) receives them on the `/api/messages` endpoint.
2. **Authentication** — Two modes:
   - **Local (anonymous mode)**: `DefaultAzureCredential` obtains a Fabric token from your `az login` session.
   - **Teams (SSO/OBO)**: The M365 Agents SDK silently signs in the Teams user via SSO, then exchanges the token (On-Behalf-Of) for a Fabric-scoped token (`https://api.fabric.microsoft.com/.default`). Each user's own Fabric permissions are honoured.
   
   Bot-to-channel auth uses MSAL via the M365 Agents SDK.
3. **Azure OpenAI Responses API** — The user's message (plus conversation history and system prompt) is sent to Azure OpenAI's Responses API with three MCP tool definitions. Azure OpenAI decides which tool(s) to invoke and calls the Fabric MCP endpoints server-side.
4. **Response Delivery** — The combined response is sent back to the user through the same M365 channel.

## Streaming Behavior

The agent uses **streaming responses** when the M365 channel supports it. The MAF agent streams LLM tokens as fast as the model produces them, but the M365 Agents SDK throttles delivery to the client at a channel-appropriate rate:

| Channel | Streaming | Chunk Interval |
|---------|-----------|----------------|
| **Teams** (direct chat) | Yes | **1.0 s** |
| **DirectLine / Web Chat** | Yes | **0.5 s** |
| **Other** (`delivery_mode=stream`) | Yes | **0.1 s** |
| **Teams agentic** (Copilot-orchestrated) | **No** — not yet supported by the SDK | N/A |

Fast-arriving LLM deltas are coalesced: the SDK accumulates text in a buffer and sends the full accumulated message as one activity per interval. The user sees progressively growing text updating at a steady cadence — no tokens are lost, just batched into larger chunks.

For non-streaming channels (or agentic requests), the agent falls back to waiting for the complete response and sending it as a single message.

## Comparison with the DevUI Version

| | DevUI Version (`agents/`) | M365 Channels Version (`m365_agents_orchestrator/`) |
|---|---|---|
| **Channel** | Local DevUI browser (port 8080) | Teams, Outlook, Copilot, Web Chat, etc. |
| **Framework** | Microsoft Agent Framework (MAF) | M365 Agents SDK |
| **LLM Client** | `AzureOpenAIResponsesClient` (MAF) | `AsyncAzureOpenAI` (openai SDK) |
| **MCP Execution** | Hosted (server-side) | Hosted (server-side) |
| **Auth** | API key + Entra ID (Fabric) | MSAL (bot) + API key + Entra ID (Fabric) |
| **System Prompt** | Same | Same |

## Adapting for Your Own Agents

Follow the same customisation steps described in the [main README](../README.md#adapting-this-solution-for-your-own-fabric-data-agents):

1. Create your Fabric data agents
2. Construct MCP endpoint URLs
3. Update `.env` with your URLs
4. Add/remove MCP tool definitions in `src/agent.py` (inside the `_build_mcp_tools()` function)
5. Update the system prompt in `prompts/orchestrator_agent.md`
6. Configure authentication

The only additional step is creating an **Azure Bot registration** and configuring the M365 channels you want to use.
