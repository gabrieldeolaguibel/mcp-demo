
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

mcp = FastMCP(name="SimpleMathMCP")

# ---- Tools ----

@mcp.tool(
    name="math.add",
    description="Add two numbers.",
    tags={"math"},
    annotations={"title": "Add", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
def add(a: float, b: float) -> float:
    """Returns a + b (pure, deterministic)."""
    return a + b


@mcp.tool(
    name="math.subtract",
    description="Subtract two numbers (a - b).",
    tags={"math"},
    annotations={"title": "Subtract", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
def subtract(a: float, b: float) -> float:
    """Returns a - b (pure, deterministic)."""
    return a - b


@mcp.tool(
    name="math.multiply",
    description="Multiply two numbers.",
    tags={"math"},
    annotations={"title": "Multiply", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
def multiply(a: float, b: float) -> float:
    """Returns a * b (pure, deterministic)."""
    return a * b


@mcp.tool(
    name="math.divide",
    description="Divide two numbers (a / b). Returns an error if b = 0.",
    tags={"math"},
    annotations={"title": "Divide", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
def divide(a: float, b: float) -> float:
    """Returns a / b. Raises a clear ToolError on division by zero."""
    if b == 0:
        # Send a friendly error to clients instead of crashing.
        raise ToolError("Division by zero is not allowed.")
    return a / b


# ---- Entrypoint ----

if __name__ == "__main__":
    host = "127.0.0.1"
    port = 8000
    print(f"SimpleMathMCP running at http://{host}:{port}/mcp/  (Streamable HTTP)")
    # Exposes the MCP endpoint at /mcp/ using Streamable HTTP transport.
    mcp.run(transport="http", host=host, port=port)
