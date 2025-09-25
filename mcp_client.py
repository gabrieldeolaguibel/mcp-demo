

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from contextlib import AsyncExitStack

import yaml  # PyYAML

from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.client.transports import StreamableHttpTransport


# ---------- Data models ----------

@dataclass
class ServerConfig:
    name: str
    url: str
    headers: Optional[Dict[str, str]] = None


@dataclass
class ToolRecord:
    server: str
    name: str                 # original tool name from server (e.g., "math.add")
    fqn: str                  # "<server>.<name>" (e.g., "math_server.math.add")
    description: Optional[str]
    input_schema: Optional[Dict[str, Any]]
    meta: Optional[Dict[str, Any]]


# ---------- Loader ----------

def load_servers_from_yaml(path: str) -> List[ServerConfig]:
    """
    Accepts a simple YAML like:
      servers:
        - name: math_server
          url: "http://127.0.0.1:8000/mcp"
          headers:
            X-Demo: "1"

    (Bonus) Also tolerates Claude-style config:
      mcpServers:
        math_server:
          transport: "http"
          url: "http://127.0.0.1:8000/mcp"
          headers: { ... }
    """
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    servers: List[ServerConfig] = []

    if "servers" in data and isinstance(data["servers"], list):
        for item in data["servers"]:
            servers.append(ServerConfig(
                name=item["name"],
                url=item["url"],
                headers=item.get("headers") or None,
            ))
        return servers

    if "mcpServers" in data and isinstance(data["mcpServers"], dict):
        for name, conf in data["mcpServers"].items():
            if not isinstance(conf, dict):
                continue
            # Only HTTP transport is in scope for this project.
            url = conf.get("url")
            if url:
                servers.append(ServerConfig(
                    name=name,
                    url=url,
                    headers=conf.get("headers") or None,
                ))
        return servers

    raise ValueError(
        f"Could not find 'servers' or 'mcpServers' in {path}. "
        "See the sample YAML below."
    )


# ---------- Multi-server client ----------

class MultiMCPClient:
    """
    Manages connections to multiple MCP servers (Streamable HTTP) at once.
    Exposes:
      - list_tools() -> List[ToolRecord]
      - call_tool("server.tool", args)
    """

    def __init__(self, servers: List[ServerConfig], timeout: Optional[float] = 30.0):
        self._servers = servers
        self._timeout = timeout
        self._clients: Dict[str, Client] = {}
        self._stack: Optional[AsyncExitStack] = None

    async def __aenter__(self) -> "MultiMCPClient":
        self._stack = AsyncExitStack()
        # Create and enter each Client context.
        for s in self._servers:
            transport = StreamableHttpTransport(url=s.url, headers=s.headers or {})
            client = Client(transport, timeout=self._timeout)
            await self._stack.enter_async_context(client)
            self._clients[s.name] = client
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._stack:
            await self._stack.aclose()
        self._clients.clear()
        self._stack = None

    async def ping_all(self) -> Dict[str, bool]:
        results: Dict[str, bool] = {}
        for name, client in self._clients.items():
            try:
                await client.ping()
                results[name] = True
            except Exception:
                results[name] = False
        return results

    async def list_tools(self) -> List[ToolRecord]:
        """
        Returns a merged catalog of tools with namespaced FQNs: "<server>.<tool>"
        """
        catalog: List[ToolRecord] = []

        async def fetch_one(server_name: str, client: Client) -> List[ToolRecord]:
            out: List[ToolRecord] = []
            tools = await client.list_tools()  # list[mcp.types.Tool] in FastMCP
            for t in tools:
                # inputSchema may be a plain dict or a pydantic object; normalize.
                input_schema = None
                if hasattr(t, "inputSchema") and t.inputSchema is not None:
                    input_schema = (
                        t.inputSchema.model_dump()  # type: ignore[attr-defined]
                        if hasattr(t.inputSchema, "model_dump")
                        else t.inputSchema
                    )
                meta = getattr(t, "meta", None)
                out.append(
                    ToolRecord(
                        server=server_name,
                        name=t.name,
                        fqn=f"{server_name}.{t.name}",
                        description=getattr(t, "description", None),
                        input_schema=input_schema,
                        meta=meta if isinstance(meta, dict) else None,
                    )
                )
            return out

        # Run tool discovery concurrently across servers
        tasks = [fetch_one(name, client) for name, client in self._clients.items()]
        for group in await asyncio.gather(*tasks):
            catalog.extend(group)
        return catalog

    async def call_tool(
        self,
        fqn: str,
        arguments: Dict[str, Any] | None = None,
        *,
        timeout: Optional[float] = None,
        raise_on_error: bool = True,
    ) -> Dict[str, Any]:
        """
        Call a tool by fully-qualified name "<server>.<tool>".

        Returns a dict with:
          - "data": hydrated Python value if available (FastMCP convenience)
          - "structured_content": raw structured JSON (if any)
          - "is_error": bool
          - "content_text": first text block (if present)
        """
        server_name, tool_name = self._split_fqn(fqn)
        client = self._clients.get(server_name)
        if client is None:
            raise ValueError(f"Unknown server '{server_name}' in '{fqn}'")

        try:
            result = await client.call_tool(
                tool_name,
                arguments or {},
                timeout=timeout,
                raise_on_error=raise_on_error,
            )
        except ToolError as e:
            # Normalize ToolError into a structured result for the caller.
            return {"data": None, "structured_content": None, "is_error": True, "content_text": str(e)}

        # Extract convenience fields
        content_text = None
        for c in getattr(result, "content", []) or []:
            if hasattr(c, "text") and c.text:
                content_text = c.text
                break

        return {
            "data": getattr(result, "data", None),
            "structured_content": getattr(result, "structured_content", None),
            "is_error": getattr(result, "is_error", False),
            "content_text": content_text,
        }

    @staticmethod
    def _split_fqn(fqn: str) -> Tuple[str, str]:
        """
        Split "<server>.<tool>" into (server, tool). The tool name itself
        can contain dots; we only split on the FIRST dot.
        """
        if "." not in fqn:
            raise ValueError("Tool name must be fully-qualified as '<server>.<tool>'")
        server, rest = fqn.split(".", 1)
        if not server or not rest:
            raise ValueError("Invalid fully-qualified tool name")
        return server, rest


# ---------- Simple CLI ----------

def _pretty_print_tools(tools: List[ToolRecord]) -> None:
    if not tools:
        print("No tools discovered.")
        return
    # width calc
    w_server = max(len(t.server) for t in tools)
    w_name = max(len(t.name) for t in tools)
    print(f"{'SERVER'.ljust(w_server)}  {'TOOL'.ljust(w_name)}  FQN")
    print("-" * (w_server + w_name + 7 + 10))
    for t in sorted(tools, key=lambda x: (x.server, x.name)):
        print(f"{t.server.ljust(w_server)}  {t.name.ljust(w_name)}  {t.fqn}")


async def _run_cli(args: argparse.Namespace) -> None:
    servers = load_servers_from_yaml(args.servers)
    async with MultiMCPClient(servers, timeout=args.timeout) as multi:
        if args.ping:
            statuses = await multi.ping_all()
            for name, ok in statuses.items():
                print(f"{name}: {'OK' if ok else 'FAILED'}")

        if args.list_tools:
            tools = await multi.list_tools()
            _pretty_print_tools(tools)

        if args.call:
            payload = json.loads(args.args) if args.args else {}
            result = await multi.call_tool(args.call, payload, timeout=args.timeout)
            print(json.dumps(result, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(description="Multi-server MCP client (Streamable HTTP)")
    parser.add_argument("--servers", required=True, help="Path to YAML with server endpoints")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout seconds")

    parser.add_argument("--ping", action="store_true", help="Ping all servers")
    parser.add_argument("--list-tools", action="store_true", help="List all tools across servers")
    parser.add_argument("--call", help="Call a tool by FQN '<server>.<tool>'")
    parser.add_argument("--args", help="JSON dict of arguments for --call", default=None)

    args = parser.parse_args()
    asyncio.run(_run_cli(args))


if __name__ == "__main__":
    main()
