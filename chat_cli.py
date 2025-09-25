from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import warnings
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Set

import requests
from dotenv import load_dotenv

# Vertex AI SDK (Generative)
import vertexai
from vertexai.generative_models import (
    GenerativeModel,
    Tool,
    FunctionDeclaration,
    Part,
)

# Reuse our MCP client & models
from mcp_client import MultiMCPClient, load_servers_from_yaml, ToolRecord  # noqa: E402

# ---- OAuth2 M2M: obtain access token from your IdP and wrap as Google Credentials
from google.oauth2.credentials import Credentials  # noqa: E402
from google.oauth2 import service_account


# ------------------------- ENV & AUTH -------------------------



def init_vertex(system_text) -> Tuple[GenerativeModel, str, str]:
    """
    Initializes Vertex AI Client
    """

    credentials_path = os.getenv("vertex_credentials_path")
    if not credentials_path:
        raise RuntimeError("Environment variable 'vertex_credentials_path' must point to a service account JSON file.")

    credentials = service_account.Credentials.from_service_account_file(
        credentials_path
    )

    project = os.getenv("vertex_project")
    if not project:
        raise RuntimeError("Environment variable 'vertex_project' is required.")

    location = os.getenv("vertex_location", "us-central1")

    vertexai.init(
        project=project,
        location=location,
        credentials=credentials,
    )

    model_name = os.getenv("model")
    if not model_name:
        raise RuntimeError("Environment variable 'model' (model name) is required.")

    model = GenerativeModel(
        model_name=model_name,
        system_instruction=system_text,
    )
    return model, project, location


# ------------------------- MCP ➜ Vertex mapping -------------------------

def mcp_tools_to_vertex_functions(tools: List[ToolRecord]) -> List[FunctionDeclaration]:
    """
    Convert MCP ToolRecord list into Vertex FunctionDeclarations.
    We use the fully qualified name (FQN) '<server>.<tool>' as the function name.
    MCP tools already expose JSON Schema; Vertex accepts OpenAPI/JSON-Schema-like dicts.
    """
    decls: List[FunctionDeclaration] = []
    for t in tools:
        params = t.input_schema if isinstance(t.input_schema, dict) else {"type": "object"}
        desc = t.description or f"Tool from {t.server}: {t.name}"
        decls.append(
            FunctionDeclaration(
                name=t.fqn,
                description=desc,
                parameters=params,
            )
        )
    return decls


@dataclass
class ProposedCall:
    name: str
    args: Dict[str, Any]


def extract_function_calls(model_response) -> List[ProposedCall]:
    """
    Robustly extract function calls from a Vertex response across SDK variants.
    Looks at:
      - candidate.function_calls (if present), else
      - candidate.content.parts[*].function_call
    Returns zero, one, or many calls (parallel).
    """
    calls: List[ProposedCall] = []
    if not getattr(model_response, "candidates", None):
        return calls

    for cand in model_response.candidates:
        # Newer fields
        if hasattr(cand, "function_calls") and cand.function_calls:
            for fc in cand.function_calls:
                # fc.args could be a Mapping or Struct-like; coerce to dict
                args = dict(getattr(fc, "args", {}) or {})
                calls.append(ProposedCall(name=fc.name, args=args))

        # Fallback: scan parts
        parts = getattr(cand, "content", None)
        if parts and getattr(parts, "parts", None):
            for p in parts.parts:
                fc = getattr(p, "function_call", None)
                if fc and getattr(fc, "name", None):
                    args = dict(getattr(fc, "args", {}) or {})
                    calls.append(ProposedCall(name=fc.name, args=args))

    return calls


def build_function_response_part(
    tool_name: str, tool_result: Any
) -> Part:
    """
    Build a Content message carrying a function response for the model.
    We wrap the actual tool result under "result" to keep things consistent.
    """
    return Part.from_function_response(
        name=tool_name,
        response={"result": tool_result},
    )


# ------------------------- Tool call handling -------------------------
async def handle_function_calls(
    chat,
    multi: MultiMCPClient,
    initial_response,
    tools: List[Tool],
    *,
    timeout: float,
) -> Any:
    """
    Execute all model-proposed function calls until none remain, printing each
    call and its result, then return the final model response.
    """

    response = initial_response
    printed_calls: Set[Tuple[str, str]] = set()

    while True:
        proposed = extract_function_calls(response)
        if not proposed:
            return response

        tasks = [
            multi.call_tool(call.name, call.args, timeout=timeout, raise_on_error=False)
            for call in proposed
        ]
        results = await asyncio.gather(*tasks)

        response_parts: List[Part] = []

        for call, result in zip(proposed, results):
            call_key = (call.name, json.dumps(call.args, sort_keys=True))
            if call_key not in printed_calls:
                print(f"[Tool Call] {call.name}({json.dumps(call.args)})")
                if result.get("is_error"):
                    print(f"[Error] {result.get('content_text') or 'Tool error'}")
                else:
                    print(f"[Result] {json.dumps(result['data'], indent=2)}")
                printed_calls.add(call_key)

            payload = result.get("data")
            if result.get("is_error") or payload is None:
                payload = {
                    "error": True,
                    "message": result.get("content_text") or "Tool error",
                    "structured_content": result.get("structured_content"),
                }

            response_parts.append(build_function_response_part(call.name, payload))

        response = chat.send_message(response_parts, tools=tools)


# ------------------------- Chat loop -------------------------

async def chat_loop(
    system_md_path: str,
    servers_yaml: str,
    model: GenerativeModel,
    *,
    timeout: float = 45.0,
):
    # Load system prompt and create chat session with instructions
    with open(system_md_path, "r", encoding="utf-8") as f:
        system_text = f.read()

    # Create a chat session. System instruction can be set here or on the model.
    chat = model.start_chat()
    # (Multi-turn context is preserved by this chat session.)  # docs confirm multi-turn chat support

    # Connect to MCP servers and discover tools
    servers = load_servers_from_yaml(servers_yaml)
    async with MultiMCPClient(servers, timeout=timeout) as multi:
        # Sanity ping
        ping = await multi.ping_all()
        not_ok = [k for k, v in ping.items() if not v]
        if not_ok:
            print(f"Warning: some servers failed ping: {not_ok}")

        # Build Vertex tool declarations
        mcp_catalog = await multi.list_tools()
        fn_decls = mcp_tools_to_vertex_functions(mcp_catalog)
        tools = [Tool(function_declarations=fn_decls)]

        # AUTO mode: model decides if/what to call (sequential or parallel)
        print("\nReady. Type your prompt (or /exit):\n")
        while True:
            try:
                user = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if user.lower() in ("/exit", "exit", "quit", "/quit"):
                break
            if not user:
                continue

            resp = chat.send_message(user, tools=tools)

            final = await handle_function_calls(
                chat,
                multi,
                resp,
                tools,
                timeout=timeout,
            )

            if hasattr(final, "text") and final.text:
                print(final.text)
            else:
                print("[No text in final response]")


def setup_warning_filters(verbose: bool = False):
    """
    Configure warning filters to suppress noisy warnings that don't affect functionality.
    This ensures a clean CLI experience while maintaining reliability.

    Args:
        verbose: If True, show all warnings for debugging. If False, suppress noisy warnings.

    These warnings are suppressed because:
    - absl logging warnings: Expected when initializing Google's internal logging system
    - ALTS credentials warnings: Normal when running locally (not on GCP infrastructure)
    - Protobuf deprecation warnings: From google-cloud-aiplatform library, don't affect functionality
    - Google Cloud deprecation warnings: Common in SDK updates, don't break functionality
    """
    if not verbose:
        # Additional suppression for any warnings that slipped through early setup
        warnings.filterwarnings("ignore", message=".*including_default_value_fields.*", module="proto")
        warnings.filterwarnings("ignore", message=".*always_print_fields_with_no_presence.*", module="proto")
        warnings.filterwarnings("ignore", message=".*DeprecationWarning.*", module="google.cloud")
        warnings.filterwarnings("ignore", message=".*UserWarning.*", module="vertexai")

        # Set logging level to WARNING to suppress INFO and DEBUG messages from Google libraries
        logging.getLogger("google").setLevel(logging.WARNING)
        logging.getLogger("google.cloud").setLevel(logging.WARNING)
        logging.getLogger("google.auth").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("absl").setLevel(logging.WARNING)

        # Also suppress specific noisy loggers that might still appear
        logging.getLogger("google.api_core").setLevel(logging.WARNING)
        logging.getLogger("google.auth.transport").setLevel(logging.WARNING)
    else:
        # In verbose mode, reset logging to show more information
        logging.getLogger().setLevel(logging.INFO)
        logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(name)s: %(message)s')


def main():
    # Parse arguments first to get verbose-warnings setting
    parser = argparse.ArgumentParser(description="Vertex AI Chat CLI (MCP tools)")
    parser.add_argument(
        "--servers", required=True, help="Path to YAML with MCP HTTP endpoints"
    )
    parser.add_argument(
        "--system", required=True, help="Path to system prompt .md file"
    )
    parser.add_argument(
        "--timeout", type=float, default=45.0, help="Per-request timeout seconds"
    )
    parser.add_argument(
        "--verbose-warnings", action="store_true",
        help="Show all warnings (useful for debugging, default: suppressed)"
    )
    args = parser.parse_args()

    # Set up clean warning handling before loading environment
    setup_warning_filters(args.verbose_warnings)

    load_dotenv()  # load .env values

    with open(args.system, "r", encoding="utf-8") as f:
        system_text = f.read()

    # Init Vertex AI with M2M credentials and custom endpoint
    model, project, location = init_vertex(system_text)
    print(f"Vertex initialized: project={project}, location={location}, model={os.getenv('model')}")

    # Run async chat loop
    try:
        asyncio.run(
            chat_loop(
                system_md_path=args.system,
                servers_yaml=args.servers,
                model=model,
                timeout=args.timeout,
            )
        )
    except KeyboardInterrupt:
        print("\nBye!")


if __name__ == "__main__":
    main()
