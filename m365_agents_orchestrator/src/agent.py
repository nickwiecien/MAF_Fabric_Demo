"""Fabric Data Agents Orchestrator — M365 Agents SDK version.

This module creates an M365-channel-compatible agent (Teams, Outlook, Copilot,
etc.) that orchestrates three Fabric data agent MCP tools (Sales, Customer,
Product) via the Azure OpenAI Responses API with hosted MCP execution.

Authentication strategy  (modelled after GEV SAA colleague implementation)
--------------------------------------------------------------------------
Fabric data agents require the **signed-in user's identity** (delegated access).
Managed-identity / app-only tokens are rejected by design.

*  **Teams / M365 channels** — we manually access ``UserTokenClient`` from the
   turn state to perform SSO token-exchange and OBO via the Bot Service OAuth
   connection.  Explicit ``invoke`` handlers cover ``signin/tokenExchange``,
   ``signin/verifystate``, and ``signin/failure``.
*  **Local dev / Emulator** — ``DefaultAzureCredential`` (``az login``) supplies
   the developer's own Fabric token.
"""

import base64
import json
import logging
import sys
import time
import traceback
from os import environ
from pathlib import Path
from typing import Any, Optional

from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from agent_framework.azure import AzureOpenAIResponsesClient

# ---------------------------------------------------------------------------
# Logging setup — force DEBUG level so all diagnostics reach App Service logs
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logging.getLogger("azure.identity").setLevel(logging.INFO)

from microsoft_agents.hosting.aiohttp import CloudAdapter
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.core import (
    AgentApplication,
    TurnState,
    TurnContext,
    MemoryStorage,
)
from microsoft_agents.activity import (
    Activity,
    ActivityTypes,
    CardAction,
    SigninCard,
    TokenExchangeState,
    load_configuration_from_env,
)

# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
load_dotenv()
agents_sdk_config = load_configuration_from_env(environ)

USE_ANONYMOUS_MODE = environ.get("USE_ANONYMOUS_MODE", "false").lower() == "true"

# OAuth connection name configured in Azure Bot Service → Settings → Configuration
OAUTH_CONNECTION_NAME = environ.get("FABRIC_ABS_OAUTH_CONNECTION_NAME", "FabricOAuth")

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
_prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "orchestrator_agent.md"
_instructions = _prompt_path.read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Fabric MCP auth — scope for OBO token exchange
# ---------------------------------------------------------------------------
FABRIC_DATA_AGENT_SCOPE = "https://api.fabric.microsoft.com/.default"

# ---------------------------------------------------------------------------
# M365 Agents SDK infrastructure  (NO Authorization/AuthHandler — manual flow)
# ---------------------------------------------------------------------------
STORAGE = MemoryStorage()

if USE_ANONYMOUS_MODE:
    logger.info("Anonymous mode — skipping MSAL auth (local dev only)")
    CONNECTION_MANAGER = None
    ADAPTER = CloudAdapter(connection_manager=None)
else:
    CONNECTION_MANAGER = MsalConnectionManager(**agents_sdk_config)
    ADAPTER = CloudAdapter(connection_manager=CONNECTION_MANAGER)

AGENT_APP = AgentApplication[TurnState](
    storage=STORAGE,
    adapter=ADAPTER,
    # NOTE: authorization intentionally omitted — we handle SSO/OBO manually
    **agents_sdk_config,
)

# ---------------------------------------------------------------------------
# Pending command store (in-memory, keyed by conversation ID).
# When a user sends a message that triggers sign-in, we store it here so we
# can replay the command after sign-in completes.
# ---------------------------------------------------------------------------
_pending_commands: dict[str, str] = {}

# ---------------------------------------------------------------------------
# DefaultAzureCredential — LOCAL-DEV FALLBACK ONLY (az login = your identity)
# ---------------------------------------------------------------------------
_credential: DefaultAzureCredential | None = None
_cached_token = None
_TOKEN_REFRESH_BUFFER_SECS = 300  # 5 minutes


def _ensure_credential() -> DefaultAzureCredential:
    global _credential
    if _credential is None:
        logger.info("Creating DefaultAzureCredential (local-dev fallback)")
        _credential = DefaultAzureCredential(
            logging_enable=True,
            exclude_managed_identity_credential=True,  # skip IMDS probe (30s timeout on local dev)
        )
    return _credential


def _decode_token_claims(token: str) -> dict:
    """Decode JWT payload (no verification) — for diagnostic logging only."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return {k: claims.get(k) for k in (
            "aud", "iss", "oid", "upn", "name", "appid",
            "app_displayname", "idtyp", "scp", "roles",
            "tid", "exp", "iat",
        )}
    except Exception as e:
        return {"decode_error": str(e)}


def _log_token_claims(token: str, hint: str = "token") -> None:
    """Log decoded JWT claims for diagnostics."""
    claims = _decode_token_claims(token)
    logger.info("%s claims: %s", hint, json.dumps(claims, indent=2))


# ═══════════════════════════════════════════════════════════════════════════
# Manual UserTokenClient helpers  (adapted from GEV-SAA pattern)
# ═══════════════════════════════════════════════════════════════════════════

def _get_user_token_client(context: TurnContext) -> Optional[Any]:
    """Extract UserTokenClient from turn context turn_state."""
    try:
        if hasattr(context, "turn_state"):
            client = context.turn_state.get("UserTokenClient")
            if client:
                return client
            # Some SDK versions use the class name as key
            for key, val in context.turn_state.items():
                if "UserTokenClient" in str(key):
                    return val
    except (AttributeError, KeyError, TypeError):
        pass
    return None


def _get_user_id(context: TurnContext) -> Optional[str]:
    """Extract user ID from turn context activity."""
    if hasattr(context.activity, "from_property"):
        return getattr(context.activity.from_property, "id", None)
    actor = getattr(context.activity, "from", None)
    return getattr(actor, "id", None)


def _get_channel_id(context: TurnContext) -> str:
    """Extract channel ID from turn context."""
    return getattr(context.activity, "channel_id", "msteams")


def _get_ms_app_id(context: TurnContext) -> Optional[str]:
    """Get the bot's Microsoft App ID from context or environment."""
    app_id = getattr(context, "app_id", None)
    if app_id:
        return app_id
    # Check multiple env var conventions
    for key in (
        "MICROSOFT_APP_ID",
        "MicrosoftAppId",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID",
    ):
        val = environ.get(key)
        if val:
            return val
    return None


def _extract_magic_code(activity) -> Optional[str]:
    """Extract OAuth magic code from activity text or value payload."""
    try:
        if hasattr(activity, "value") and isinstance(activity.value, dict):
            code = activity.value.get("code")
            if code:
                logger.debug("Magic code found in activity.value: %s", code)
                return str(code).strip()
            auth = activity.value.get("authentication", {})
            if isinstance(auth, dict) and auth.get("code"):
                logger.debug("Magic code found in activity.value.authentication")
                return str(auth["code"]).strip()
        text_val = (getattr(activity, "text", "") or "").strip()
        if text_val and text_val.isdigit() and 3 <= len(text_val) <= 12:
            logger.debug("Magic code found in activity.text: %s", text_val)
            return text_val
    except Exception:
        pass
    return None


async def _get_token_with_magic_code(
    user_token_client,
    user_id: str,
    channel_id: str,
    connection_name: str,
    code: Optional[str],
) -> Optional[str]:
    """Try to get a cached token (or redeem a magic code) from the Bot token service."""
    if not user_token_client or not user_id:
        return None
    try:
        logger.debug(
            "Calling user_token.get_token user_id=%s channel=%s connection=%s has_code=%s",
            user_id, channel_id, connection_name, bool(code),
        )
        token_response = await user_token_client.user_token.get_token(
            user_id=user_id,
            connection_name=connection_name,
            channel_id=channel_id,
            code=code,
        )
        token_value = getattr(token_response, "token", None)
        if token_value:
            _log_token_claims(token_value, "Fabric user-token")
            return token_value
    except Exception as exc:
        logger.warning("Token service get_token error: %s", exc)
    return None


async def _try_token_exchange_from_invoke(
    context: TurnContext,
    user_token_client,
    user_id: str,
    channel_id: str,
    connection_name: str,
) -> Optional[str]:
    """Attempt SSO token exchange from an invoke activity payload."""
    payload = getattr(context.activity, "value", None)
    if not isinstance(payload, dict) or not payload.get("token"):
        return None

    effective_conn = payload.get("connectionName") or connection_name
    try:
        exchange_response = await user_token_client.user_token.exchange_token(
            user_id=user_id,
            connection_name=effective_conn,
            channel_id=channel_id,
            body=payload,
        )
        exchange_token = getattr(exchange_response, "token", None)
        if exchange_token:
            logger.info("SSO token exchange succeeded connection=%s", effective_conn)
            _log_token_claims(exchange_token, "SSO-exchanged Fabric token")
            return exchange_token
    except Exception as exc:
        logger.warning("SSO token exchange failed connection=%s error=%s", effective_conn, exc)
    return None


async def _send_oauth_card(
    context: TurnContext,
    user_token_client,
    user_id: str,
    channel_id: str,
    connection_name: str,
    ms_app_id: Optional[str],
) -> None:
    """Send an OAuth sign-in card to the user."""
    try:
        conversation_ref = (
            context.activity.get_conversation_reference()
            if hasattr(context.activity, "get_conversation_reference")
            else None
        )
        if not conversation_ref or not ms_app_id:
            logger.warning(
                "Missing conversation_ref=%s or ms_app_id=%s — cannot send OAuth card",
                bool(conversation_ref), ms_app_id,
            )
            await context.send_activity(
                "⚠️ Sign-in is required but I couldn't build the sign-in card. "
                f"(conversation_ref={'OK' if conversation_ref else 'MISSING'}, "
                f"ms_app_id={'OK' if ms_app_id else 'MISSING'}). "
                "Please try again."
            )
            return

        token_state = TokenExchangeState(
            connection_name=connection_name,
            conversation=conversation_ref,
            relates_to=getattr(context.activity, "relates_to", None),
            agent_url=getattr(context.activity, "service_url", None),
            ms_app_id=ms_app_id,
        )
        encoded_state = token_state.get_encoded_state()
        logger.debug("TokenExchangeState encoded for connection=%s", connection_name)

        token_or_sign_in = (
            await user_token_client.user_token._get_token_or_sign_in_resource(
                user_id, connection_name, channel_id, encoded_state,
            )
        )

        # Check if we actually got a token (user was already signed in)
        token_resp = getattr(token_or_sign_in, "token_response", None)
        if token_resp and getattr(token_resp, "token", None):
            logger.info("Token obtained via _get_token_or_sign_in_resource (already signed in)")
            return

        # Extract sign-in resource and send SigninCard
        sign_in_resource = getattr(token_or_sign_in, "sign_in_resource", None)
        if sign_in_resource and getattr(sign_in_resource, "sign_in_link", None):
            logger.info("Sending SigninCard to user")
            signin_card = SigninCard(
                text=(
                    "**Sign in required**\n\n"
                    "To query Fabric data agents on your behalf, please sign in.\n\n"
                    "If you receive a verification code, paste it in this chat."
                ),
                buttons=[
                    CardAction(
                        type="signin",
                        title="Sign in to Fabric",
                        value=sign_in_resource.sign_in_link,
                    )
                ],
            )
            await context.send_activity(
                Activity(
                    type=ActivityTypes.message,
                    attachments=[
                        {
                            "contentType": "application/vnd.microsoft.card.signin",
                            "content": signin_card.model_dump(
                                by_alias=True, exclude_none=True,
                            ),
                        }
                    ],
                )
            )
            return

        logger.warning("Token service returned neither token nor sign-in resource")
        await context.send_activity(
            "⚠️ I couldn't initiate sign-in. Please try sending your question again."
        )

    except Exception as exc:
        logger.error("Failed to send OAuth card: %s", exc, exc_info=True)
        await context.send_activity(
            f"⚠️ Authentication error: `{exc}`\n\n"
            "Please try again. If you see a verification code, paste it here."
        )


async def fetch_token_or_prompt(
    context: TurnContext,
    connection_name: str = OAUTH_CONNECTION_NAME,
) -> Optional[str]:
    """Fetch a user token from the Bot token service, or prompt sign-in.

    Layered approach (matching the GEV-SAA pattern):
    1. Try ``get_token`` (returns cached token or redeems a magic code)
    2. Try ``exchange_token`` from invoke payload (SSO silent exchange)
    3. Send OAuth sign-in card and return ``None``
    """
    user_token_client = _get_user_token_client(context)
    user_id = _get_user_id(context)
    channel_id = _get_channel_id(context)
    magic_code = _extract_magic_code(context.activity)

    logger.info(
        "fetch_token_or_prompt: user_id=%s channel=%s connection=%s has_magic_code=%s "
        "has_token_client=%s",
        user_id, channel_id, connection_name, bool(magic_code),
        bool(user_token_client),
    )

    if not user_token_client or not user_id:
        logger.error("UserTokenClient or user_id missing from turn state — cannot acquire token")
        return None

    # Step 1: Try get_token (cached or magic-code redemption)
    token = await _get_token_with_magic_code(
        user_token_client, user_id, channel_id, connection_name, magic_code,
    )
    if token:
        return token

    # Step 2: Try SSO token exchange from invoke payload
    exchanged = await _try_token_exchange_from_invoke(
        context, user_token_client, user_id, channel_id, connection_name,
    )
    if exchanged:
        return exchanged

    # Step 3: Send OAuth card — user needs to sign in
    ms_app_id = _get_ms_app_id(context)
    await _send_oauth_card(
        context, user_token_client, user_id, channel_id, connection_name, ms_app_id,
    )
    return None


# ---------------------------------------------------------------------------
# Fabric header builders
# ---------------------------------------------------------------------------

def _get_fabric_headers_local() -> dict[str, str]:
    """Fabric MCP headers via DefaultAzureCredential (local dev / az login)."""
    global _cached_token
    cred = _ensure_credential()

    need_refresh = (
        _cached_token is None
        or time.time() >= _cached_token.expires_on - _TOKEN_REFRESH_BUFFER_SECS
    )
    if need_refresh:
        logger.info("Acquiring Fabric token via DefaultAzureCredential …")
        _cached_token = cred.get_token(FABRIC_DATA_AGENT_SCOPE)
        logger.info("Token acquired (expires in %.0f s)", _cached_token.expires_on - time.time())
        _log_token_claims(_cached_token.token, "DefaultAzureCredential Fabric token")

    return {
        "Authorization": f"Bearer {_cached_token.token}",
        "Content-Type": "application/json",
    }


def _fabric_headers_from_token(token: str) -> dict[str, str]:
    """Build Fabric MCP headers from a user token."""
    logger.info("Using SSO/OBO user token for Fabric MCP calls")
    _log_token_claims(token, "User Fabric token")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }





# ---------------------------------------------------------------------------
# Microsoft Agent Framework — client, agent, and MCP tools
# ---------------------------------------------------------------------------
_FABRIC_SALES_URL = environ["FABRIC_SALES_AGENT_MCP_URL"]
_FABRIC_CUSTOMER_URL = environ["FABRIC_CUSTOMER_AGENT_MCP_URL"]
_FABRIC_PRODUCT_URL = environ["FABRIC_PRODUCT_AGENT_MCP_URL"]

MAF_CLIENT = AzureOpenAIResponsesClient(
    endpoint=environ["AOAI_ENDPOINT"],
    api_key=environ["AOAI_KEY"],
    deployment_name=environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
)

ORCHESTRATOR_AGENT = MAF_CLIENT.as_agent(
    name="Fabric Data Agents Orchestrator",
    description=(
        "Orchestrates three Fabric data agent MCP tools (Sales, Customer, Product) "
        "to answer business questions across orders, customers, addresses, products, "
        "categories, and more."
    ),
    instructions=_instructions,
    # No tools here — they are supplied per-request with user-specific headers
)


def _build_mcp_tools(*, user_token: str | None = None):
    """Build MAF MCP tool objects with per-user Fabric auth headers."""
    logger.info(
        "=== Building MAF MCP tools (user_token=%s) ===",
        "present" if user_token else "None — using DefaultAzureCredential",
    )
    headers = (
        _fabric_headers_from_token(user_token)
        if user_token
        else _get_fabric_headers_local()
    )
    return [
        MAF_CLIENT.get_mcp_tool(
            name="Sales Agent",
            url=_FABRIC_SALES_URL,
            headers=headers,
            approval_mode="never_require",
        ),
        MAF_CLIENT.get_mcp_tool(
            name="Customer Agent",
            url=_FABRIC_CUSTOMER_URL,
            headers=headers,
            approval_mode="never_require",
        ),
        MAF_CLIENT.get_mcp_tool(
            name="Product Agent",
            url=_FABRIC_PRODUCT_URL,
            headers=headers,
            approval_mode="never_require",
        ),
    ]


# ---------------------------------------------------------------------------
# Conversation sessions — MAF handles history internally
# ---------------------------------------------------------------------------
_sessions: dict[str, Any] = {}


# ═══════════════════════════════════════════════════════════════════════════
# Core message handler — drives the LLM + MCP tools pipeline
# ═══════════════════════════════════════════════════════════════════════════

async def _run_agent_pipeline(
    context: TurnContext,
    user_text: str,
    conversation_id: str,
) -> None:
    """Acquire token → build MAF MCP tools → run agent → send response.

    Uses the M365 Agents SDK ``StreamingResponse`` to stream incremental
    chunks to the client when the channel supports it (Teams, DirectLine,
    etc.).  Falls back to a single ``send_activity`` for non-streaming
    channels or agentic requests that don't yet support streaming.
    """

    # ----- Acquire Fabric token -----
    user_token: str | None = None

    if USE_ANONYMOUS_MODE:
        logger.info("Anonymous mode — using DefaultAzureCredential for Fabric")
    else:
        logger.info("Requesting Fabric user-token via manual SSO/OBO flow …")
        user_token = await fetch_token_or_prompt(context, OAUTH_CONNECTION_NAME)
        if user_token is None:
            # Sign-in card was sent — store the user's message for replay
            logger.info(
                "No token yet (sign-in initiated). Storing pending command "
                "for conversation=%s",
                conversation_id,
            )
            _pending_commands[conversation_id] = user_text
            return

    # Build per-user MCP tools via the Agent Framework
    tools = _build_mcp_tools(user_token=user_token)

    # Get or create a MAF session for this conversation (handles history)
    session = _sessions.get(conversation_id)
    if not session:
        session = ORCHESTRATOR_AGENT.create_session()
        _sessions[conversation_id] = session

    logger.info(
        "Running MAF agent: conversation=%s, tools=%d",
        conversation_id, len(tools),
    )

    # ----- Determine if the channel supports streaming -----
    streamer = context.streaming_response  # lazy-created StreamingResponse
    use_streaming = streamer is not None and getattr(streamer, "_is_streaming_channel", False)
    logger.info("Streaming mode: %s", "enabled" if use_streaming else "disabled")

    if use_streaming:
        # ── Streaming path ────────────────────────────────────────────
        streamer.queue_informative_update("Querying Fabric data agents…")
        streamer.set_generated_by_ai_label(True)
        streamer.set_feedback_loop(True)

        response_stream = ORCHESTRATOR_AGENT.run(
            user_text,
            session=session,
            tools=tools,
            stream=True,
        )

        async for update in response_stream:
            chunk_text = update.text
            if chunk_text:
                streamer.queue_text_chunk(chunk_text)

        # Finalise the MAF stream (updates session history)
        await response_stream.get_final_response()

        # If nothing was streamed, send a fallback message
        if not streamer.get_message().strip():
            streamer.queue_text_chunk(
                "I wasn't able to generate a response. "
                "Please try rephrasing your question."
            )

        await streamer.end_stream()
        logger.info("Streamed response length: %d chars", len(streamer.get_message()))
    else:
        # ── Non-streaming fallback ────────────────────────────────────
        result = await ORCHESTRATOR_AGENT.run(
            user_text,
            session=session,
            tools=tools,
        )

        assistant_text = str(result).strip()
        if not assistant_text:
            assistant_text = (
                "I wasn't able to generate a response. "
                "Please try rephrasing your question."
            )

        logger.info("Agent response length: %d chars", len(assistant_text))
        await context.send_activity(assistant_text)


# ═══════════════════════════════════════════════════════════════════════════
# Activity handlers
# ═══════════════════════════════════════════════════════════════════════════

# NOTE: No welcome/intro message — the bot stays silent until the user asks.


# ---------------------------------------------------------------------------
# Invoke handler — explicit SSO/token-exchange support
# ---------------------------------------------------------------------------

@AGENT_APP.activity("invoke")
async def on_invoke(context: TurnContext, _state: TurnState) -> None:
    """Handle Bot Framework invoke activities for SSO and token exchange."""
    invoke_name = getattr(context.activity, "name", None)
    payload = getattr(context.activity, "value", None) or {}

    logger.info(
        "INVOKE received: name=%s type=%s payload_keys=%s",
        invoke_name,
        getattr(context.activity, "type", None),
        list(payload.keys()) if isinstance(payload, dict) else [],
    )

    conversation_id = (
        getattr(getattr(context.activity, "conversation", None), "id", None)
        or "default"
    )

    # ── signin/tokenExchange ──────────────────────────────────────────────
    if invoke_name == "signin/tokenExchange":
        logger.info("Processing signin/tokenExchange invoke")
        user_token_client = _get_user_token_client(context)
        user_id = _get_user_id(context)
        channel_id = _get_channel_id(context)

        token = None
        if user_token_client and user_id:
            token = await _try_token_exchange_from_invoke(
                context, user_token_client, user_id, channel_id, OAUTH_CONNECTION_NAME,
            )

        if token:
            logger.info("tokenExchange succeeded — replaying pending command if any")
            await context.send_activity(
                Activity(
                    type=ActivityTypes.invoke_response,
                    value={"status": 200, "body": {}},
                )
            )
            # Replay pending command
            pending = _pending_commands.pop(conversation_id, None)
            if pending:
                logger.info("Replaying pending command: %r", pending)
                await context.send_activity("✅ Sign-in complete! Running your request…")
                try:
                    await _run_agent_pipeline(context, pending, conversation_id)
                except Exception as exc:
                    logger.error("Error replaying pending command: %s", exc, exc_info=True)
                    await context.send_activity(
                        f"⚠️ Error processing your request: {str(exc)[:300]}"
                    )
        else:
            logger.warning("tokenExchange failed — sending 409 so Teams retries")
            await context.send_activity(
                Activity(
                    type=ActivityTypes.invoke_response,
                    value={"status": 409, "body": {"failureDetail": "Token exchange failed"}},
                )
            )
        return

    # ── signin/verifyState (magic code) ───────────────────────────────────
    if invoke_name in ("signin/verifystate", "signin/verifyState"):
        logger.info("Processing signin/verifyState invoke")
        magic_code = None
        if isinstance(payload, dict):
            magic_code = payload.get("state")

        token = None
        if magic_code:
            user_token_client = _get_user_token_client(context)
            user_id = _get_user_id(context)
            channel_id = _get_channel_id(context)
            if user_token_client and user_id:
                token = await _get_token_with_magic_code(
                    user_token_client, user_id, channel_id,
                    OAUTH_CONNECTION_NAME, str(magic_code),
                )

        await context.send_activity(
            Activity(
                type=ActivityTypes.invoke_response,
                value={"status": 200, "body": {}},
            )
        )

        if token:
            logger.info("Magic code redeemed successfully — replaying pending command")
            pending = _pending_commands.pop(conversation_id, None)
            if pending:
                await context.send_activity("✅ Sign-in complete! Running your request…")
                try:
                    await _run_agent_pipeline(context, pending, conversation_id)
                except Exception as exc:
                    logger.error("Error replaying pending command: %s", exc, exc_info=True)
                    await context.send_activity(
                        f"⚠️ Error processing your request: {str(exc)[:300]}"
                    )
            else:
                await context.send_activity(
                    "✅ Sign-in complete! Please re-send your question."
                )
        else:
            logger.warning("Magic code redemption returned no token")
        return

    # ── signin/failure ────────────────────────────────────────────────────
    if invoke_name == "signin/failure":
        logger.warning("Teams reported sign-in failure payload=%s", payload)
        await context.send_activity(
            Activity(
                type=ActivityTypes.invoke_response,
                value={"status": 200},
            )
        )
        await context.send_activity(
            "⚠️ Sign-in failed. Please try sending your question again "
            "to get a new sign-in prompt.\n\n"
            "_If you keep seeing this, your organization may need an admin "
            "to grant consent for the Fabric API permissions._"
        )
        return

    # ── Unrecognised invoke — default 200 ─────────────────────────────────
    logger.debug("Unrecognised invoke name=%s — returning 200", invoke_name)
    await context.send_activity(
        Activity(
            type=ActivityTypes.invoke_response,
            value={"status": 200},
        )
    )


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

@AGENT_APP.activity("message")
async def on_message(context: TurnContext, _state: TurnState):
    """Handle an incoming user message via the Microsoft Agent Framework."""
    user_text = (context.activity.text or "").strip()
    conversation_id = (
        getattr(getattr(context.activity, "conversation", None), "id", None)
        or "default"
    )

    # ── Check if this message is a magic code (numeric-only) ──────────────
    magic_code = _extract_magic_code(context.activity)
    if magic_code and not user_text.replace(magic_code, "").strip():
        # The entire message is a magic code — redeem it
        logger.info("Message is a magic code (%s) — redeeming", magic_code)
        user_token_client = _get_user_token_client(context)
        user_id = _get_user_id(context)
        channel_id = _get_channel_id(context)

        token = None
        if user_token_client and user_id:
            token = await _get_token_with_magic_code(
                user_token_client, user_id, channel_id,
                OAUTH_CONNECTION_NAME, magic_code,
            )

        if token:
            pending = _pending_commands.pop(conversation_id, None)
            if pending:
                await context.send_activity("✅ Sign-in complete! Running your request…")
                try:
                    await _run_agent_pipeline(context, pending, conversation_id)
                except Exception as exc:
                    logger.error("Error replaying pending command: %s", exc, exc_info=True)
                    await context.send_activity(
                        f"⚠️ Error processing your request: {str(exc)[:300]}"
                    )
            else:
                await context.send_activity(
                    "✅ Sign-in complete! Please send your question."
                )
        else:
            await context.send_activity(
                "⚠️ Could not redeem that code. Please click the sign-in "
                "card again."
            )
        return

    if not user_text:
        return

    # ── Check for a pending command to restore after sign-in ──────────────
    pending = _pending_commands.pop(conversation_id, None)
    if pending and user_text:
        # User typed something new — use the new message
        logger.info(
            "Had pending command (%r) but user sent new message (%r) — using new",
            pending, user_text,
        )

    try:
        logger.info(">>> on_message: user_text=%r  conversation=%s", user_text, conversation_id)
        await _run_agent_pipeline(context, user_text, conversation_id)

    except Exception as exc:
        logger.error("!!! Error in agent pipeline: %s", exc, exc_info=True)
        error_detail = str(exc)[:500]
        await context.send_activity(
            f"⚠️ Error: {error_detail}\n\n"
            "_Check the App Service logs for full token/credential diagnostics._"
        )


# ---------------------------------------------------------------------------
# Global error handler
# ---------------------------------------------------------------------------

@AGENT_APP.error
async def on_error(context: TurnContext, error: Exception):
    """Global error handler."""
    print(f"\n[on_turn_error] unhandled error: {error}", file=sys.stderr)
    traceback.print_exc()
    await context.send_activity(
        "⚠️ The agent encountered an unexpected error. Please try again."
    )
