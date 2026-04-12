"""MCP server wrapping rust-analyzer's LSP protocol.

Spawns rust-analyzer as a subprocess, speaks LSP JSON-RPC over its
stdio, and exposes semantic queries (hover, definition, references,
implementations, symbol search) as MCP tools.

Configuration:
    WORKSPACE_ROOT  — path to the Rust project (default: cwd)
    RUST_ANALYZER_PATH — path to rust-analyzer binary (default: "rust-analyzer")

Automatically reads .vscode/settings.json for rust-analyzer config
(e.g. cargo.target, check.allTargets) so no_std and cross-compilation
projects work out of the box.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote, unquote

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

# ---------------------------------------------------------------------------
# LSP Symbol Kind enum
# ---------------------------------------------------------------------------
SYMBOL_KIND: dict[int, str] = {
    1: "File", 2: "Module", 3: "Namespace", 4: "Package",
    5: "Class", 6: "Method", 7: "Property", 8: "Field",
    9: "Constructor", 10: "Enum", 11: "Interface", 12: "Function",
    13: "Variable", 14: "Constant", 15: "String", 16: "Number",
    17: "Boolean", 18: "Array", 19: "Object", 20: "Key",
    21: "Null", 22: "EnumMember", 23: "Struct", 24: "Event",
    25: "Operator", 26: "TypeParameter",
}

# For symbol_references: user-friendly kind names → LSP kind labels
KIND_FILTER: dict[str, list[str]] = {
    "function": ["Function"],
    "method": ["Method"],
    "struct": ["Struct", "Class"],
    "enum": ["Enum"],
    "const": ["Constant"],
    "type": ["Struct", "Class", "Enum", "Interface", "TypeParameter"],
    "trait": ["Interface"],
    "module": ["Module"],
    "field": ["Field"],
    "any": [],
}


# ---------------------------------------------------------------------------
# LSP Client — async JSON-RPC over subprocess stdio
# ---------------------------------------------------------------------------
class LspClient:
    """Async LSP client that drives a rust-analyzer subprocess."""

    def __init__(self, workspace_root: str, ra_path: str = "rust-analyzer",
                 ra_settings: dict | None = None):
        self.workspace_root = Path(workspace_root).resolve()
        self.ra_path = ra_path
        self.ra_settings = ra_settings or {}
        self._proc: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._open_files: dict[str, tuple[int, float]] = {}  # uri → (version, mtime)
        self._initialized = asyncio.Event()
        self._workspace_loaded = asyncio.Event()
        self._active_progress: set[str] = set()
        self._read_buffer = b""

    # --- lifecycle ---

    async def start(self):
        """Spawn rust-analyzer and complete LSP initialize handshake."""
        self._proc = await asyncio.create_subprocess_exec(
            self.ra_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.workspace_root),
        )

        self._reader_task = asyncio.create_task(self._read_loop())

        root_uri = self._path_to_uri(str(self.workspace_root))
        await self._request("initialize", {
            "processId": os.getpid(),
            "capabilities": {
                "textDocument": {
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "definition": {},
                    "references": {},
                    "implementation": {},
                    "documentSymbol": {
                        "hierarchicalDocumentSymbolSupport": True,
                    },
                },
                "workspace": {
                    "symbol": {},
                },
                "window": {
                    "workDoneProgress": True,
                },
            },
            "rootUri": root_uri,
            "workspaceFolders": [{
                "uri": root_uri,
                "name": self.workspace_root.name,
            }],
        })

        await self._notify("initialized", {})
        self._initialized.set()

    async def wait_for_workspace(self, timeout: float = 120.0):
        """Poll until rust-analyzer has indexed the workspace.

        Progress notifications are unreliable for detecting full
        readiness (the first progress cycle completes before indexing
        starts). Instead, we poll workspace/symbol until we get a
        non-empty result.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        poll_interval = 2.0

        while asyncio.get_event_loop().time() < deadline:
            try:
                result = await asyncio.wait_for(
                    self._request("workspace/symbol", {"query": "main"}),
                    timeout=10.0,
                )
                if result and len(result) > 0:
                    self._workspace_loaded.set()
                    _log("workspace indexed — symbols available")
                    return
            except Exception:
                pass
            await asyncio.sleep(poll_interval)

        _log("workspace load timed out — proceeding with partial index")

    async def stop(self):
        """Gracefully shutdown rust-analyzer."""
        if self._proc and self._proc.returncode is None:
            try:
                await asyncio.wait_for(self._request("shutdown", None), timeout=5.0)
                await self._notify("exit", None)
            except Exception:
                pass
            try:
                self._proc.kill()
                await self._proc.wait()
            except Exception:
                pass
        if self._reader_task:
            self._reader_task.cancel()

    # --- URI helpers ---

    def _path_to_uri(self, filepath: str) -> str:
        p = Path(filepath).resolve()
        return f"file://{quote(str(p), safe='/:@')}"

    def _uri_to_path(self, uri: str) -> str:
        if uri.startswith("file://"):
            return unquote(uri[7:])
        return uri

    def _relative_path(self, uri: str) -> str:
        abspath = self._uri_to_path(uri)
        try:
            return str(Path(abspath).relative_to(self.workspace_root))
        except ValueError:
            return abspath

    # --- file tracking ---

    async def _ensure_open(self, filepath: str) -> str:
        """Open or refresh a file in rust-analyzer. Returns the file URI."""
        p = Path(filepath)
        if not p.is_absolute():
            p = self.workspace_root / p
        p = p.resolve()
        uri = self._path_to_uri(str(p))

        try:
            mtime = p.stat().st_mtime
        except FileNotFoundError:
            raise ValueError(f"File not found: {p}")

        existing = self._open_files.get(uri)

        if existing:
            old_version, old_mtime = existing
            if old_mtime == mtime:
                return uri
            content = p.read_text(encoding="utf-8")
            new_version = old_version + 1
            await self._notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": new_version},
                "contentChanges": [{"text": content}],
            })
            self._open_files[uri] = (new_version, mtime)
        else:
            content = p.read_text(encoding="utf-8")
            await self._notify("textDocument/didOpen", {
                "textDocument": {
                    "uri": uri,
                    "languageId": "rust",
                    "version": 1,
                    "text": content,
                },
            })
            self._open_files[uri] = (1, mtime)

        return uri

    # --- JSON-RPC transport ---

    async def _request(self, method: str, params) -> any:
        """Send request, return result (or raise on error)."""
        self._request_id += 1
        req_id = self._request_id

        msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params

        future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        await self._send(msg)
        return await future

    async def _notify(self, method: str, params):
        """Send notification (fire-and-forget)."""
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def _send(self, msg: dict):
        """Write a Content-Length framed JSON-RPC message."""
        assert self._proc and self._proc.stdin
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._proc.stdin.write(header + body)
        await self._proc.stdin.drain()

    async def _read_loop(self):
        """Read and dispatch all messages from rust-analyzer."""
        try:
            while True:
                msg = await self._read_message()
                if msg is None:
                    break
                await self._dispatch(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            _log(f"LSP read error: {e}")

    async def _read_message(self) -> dict | None:
        """Read one Content-Length framed JSON-RPC message."""
        assert self._proc and self._proc.stdout

        while True:
            sep = self._read_buffer.find(b"\r\n\r\n")
            if sep >= 0:
                header_bytes = self._read_buffer[:sep]
                self._read_buffer = self._read_buffer[sep + 4:]
                break
            chunk = await self._proc.stdout.read(8192)
            if not chunk:
                return None
            self._read_buffer += chunk

        content_length = None
        for line in header_bytes.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                content_length = int(line.split(b":")[1].strip())

        if content_length is None:
            _log("missing Content-Length header")
            return None

        while len(self._read_buffer) < content_length:
            chunk = await self._proc.stdout.read(8192)
            if not chunk:
                return None
            self._read_buffer += chunk

        body = self._read_buffer[:content_length]
        self._read_buffer = self._read_buffer[content_length:]
        return json.loads(body)

    async def _dispatch(self, msg: dict):
        """Route an incoming JSON-RPC message."""
        if "id" in msg and "method" not in msg:
            req_id = msg["id"]
            future = self._pending.pop(req_id, None)
            if future and not future.done():
                if "error" in msg:
                    future.set_exception(
                        RuntimeError(f"LSP error: {msg['error']}")
                    )
                else:
                    future.set_result(msg.get("result"))
        elif "id" in msg and "method" in msg:
            await self._handle_server_request(msg)
        else:
            self._handle_notification(msg)

    async def _handle_server_request(self, msg: dict):
        """Respond to requests from rust-analyzer."""
        method = msg["method"]
        req_id = msg["id"]

        if method == "window/workDoneProgress/create":
            token = str(msg.get("params", {}).get("token", ""))
            self._active_progress.add(token)

        elif method == "workspace/configuration":
            items = msg.get("params", {}).get("items", [])
            results = []
            for item in items:
                section = item.get("section", "")
                if section == "rust-analyzer":
                    results.append(self.ra_settings)
                else:
                    results.append({})
            await self._send({
                "jsonrpc": "2.0", "id": req_id,
                "result": results,
            })
            return

        # Default: acknowledge
        await self._send({"jsonrpc": "2.0", "id": req_id, "result": None})

    def _handle_notification(self, msg: dict):
        """Handle notifications from rust-analyzer."""
        method = msg.get("method", "")
        params = msg.get("params", {})

        if method == "$/progress":
            token = str(params.get("token", ""))
            value = params.get("value", {})
            kind = value.get("kind", "")

            if kind == "begin":
                self._active_progress.add(token)
            elif kind == "end":
                self._active_progress.discard(token)
                if not self._active_progress and not self._workspace_loaded.is_set():
                    _log("workspace fully loaded")
                    self._workspace_loaded.set()

    # --- Public LSP query methods ---
    # All positions are 0-based (LSP convention).
    # The MCP tool layer converts from 1-based user input.

    async def hover(self, filepath: str, line: int, col: int) -> str | None:
        """Get hover info (type + docs) at a position."""
        uri = await self._ensure_open(filepath)
        result = await self._request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
        })
        if not result:
            return None
        contents = result.get("contents", "")
        if isinstance(contents, dict):
            return contents.get("value", str(contents))
        if isinstance(contents, list):
            return "\n".join(
                c.get("value", str(c)) if isinstance(c, dict) else str(c)
                for c in contents
            )
        return str(contents)

    async def definition(self, filepath: str, line: int, col: int) -> list[dict]:
        """Get definition location(s) for symbol at position."""
        uri = await self._ensure_open(filepath)
        result = await self._request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
        })
        if not result:
            return []
        if isinstance(result, dict):
            result = [result]
        return self._parse_locations(result)

    async def references(self, filepath: str, line: int, col: int,
                         include_declaration: bool = True) -> list[dict]:
        """Find all references to symbol at position."""
        uri = await self._ensure_open(filepath)
        result = await self._request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
            "context": {"includeDeclaration": include_declaration},
        })
        return self._parse_locations(result or [])

    async def implementations(self, filepath: str, line: int, col: int) -> list[dict]:
        """Find all implementations of trait/method at position."""
        uri = await self._ensure_open(filepath)
        result = await self._request("textDocument/implementation", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
        })
        if not result:
            return []
        if isinstance(result, dict):
            result = [result]
        return self._parse_locations(result)

    async def workspace_symbols(self, query: str) -> list[dict]:
        """Search for symbols across the workspace by name."""
        result = await self._request("workspace/symbol", {"query": query})
        if not result:
            return []
        out = []
        for sym in result:
            loc = sym.get("location")
            if not loc:
                continue
            kind_num = sym.get("kind", 0)
            out.append({
                "name": sym["name"],
                "kind": SYMBOL_KIND.get(kind_num, f"Kind({kind_num})"),
                "file": self._relative_path(loc["uri"]),
                "line": loc["range"]["start"]["line"] + 1,
                "col": loc["range"]["start"]["character"] + 1,
                "container": sym.get("containerName", ""),
            })
        return out

    async def document_symbols(self, filepath: str) -> list[dict]:
        """Get all symbols defined in a file."""
        uri = await self._ensure_open(filepath)
        result = await self._request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        })
        if not result:
            return []
        symbols: list[dict] = []
        self._flatten_doc_symbols(result, symbols, "")
        return symbols

    # --- internal helpers ---

    def _parse_locations(self, items: list) -> list[dict]:
        out = []
        for loc in items:
            uri = loc.get("uri")
            rng = loc.get("range")
            if not uri or not rng:
                continue
            out.append({
                "file": self._relative_path(uri),
                "line": rng["start"]["line"] + 1,
                "col": rng["start"]["character"] + 1,
            })
        return out

    def _flatten_doc_symbols(self, items: list, out: list, prefix: str):
        for item in items:
            name = item.get("name", "?")
            full_name = f"{prefix}::{name}" if prefix else name
            kind_num = item.get("kind", 0)

            if "range" in item:
                out.append({
                    "name": full_name,
                    "kind": SYMBOL_KIND.get(kind_num, f"Kind({kind_num})"),
                    "line": item["range"]["start"]["line"] + 1,
                    "detail": item.get("detail", ""),
                })
                for child in item.get("children", []):
                    self._flatten_doc_symbols([child], out, full_name)
            elif "location" in item:
                out.append({
                    "name": name,
                    "kind": SYMBOL_KIND.get(kind_num, f"Kind({kind_num})"),
                    "file": self._relative_path(item["location"]["uri"]),
                    "line": item["location"]["range"]["start"]["line"] + 1,
                    "container": item.get("containerName", ""),
                })


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

WORKSPACE = os.environ.get("WORKSPACE_ROOT", os.getcwd())
RA_PATH = os.environ.get("RUST_ANALYZER_PATH", "rust-analyzer")

app = Server("mcp-rust-analyzer")
_client: LspClient | None = None
_client_lock = asyncio.Lock()


def _load_ra_settings(workspace: str) -> dict:
    """Load rust-analyzer settings from .vscode/settings.json.

    Extracts keys prefixed with "rust-analyzer." and restructures
    them into a nested dict that rust-analyzer expects via the
    workspace/configuration LSP request.

    Example: "rust-analyzer.cargo.target" → {"cargo": {"target": ...}}
    """
    vscode_settings = Path(workspace) / ".vscode" / "settings.json"
    if not vscode_settings.exists():
        return {}

    try:
        with open(vscode_settings) as f:
            all_settings = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        _log(f"warning: could not parse {vscode_settings}: {e}")
        return {}

    ra: dict = {}
    prefix = "rust-analyzer."
    for key, value in all_settings.items():
        if key.startswith(prefix):
            parts = key[len(prefix):].split(".")
            d = ra
            for part in parts[:-1]:
                d = d.setdefault(part, {})
            d[parts[-1]] = value

    return ra


async def get_client() -> LspClient:
    """Lazy-init the LSP client (singleton)."""
    global _client
    async with _client_lock:
        if _client is None:
            ra_settings = _load_ra_settings(WORKSPACE)
            _log(f"starting rust-analyzer for {WORKSPACE}")
            if ra_settings:
                _log(f"rust-analyzer settings: {json.dumps(ra_settings)}")
            _client = LspClient(WORKSPACE, RA_PATH, ra_settings=ra_settings)
            await _client.start()
            _log("waiting for workspace index...")
            await _client.wait_for_workspace(timeout=120.0)
            _log("ready")
        return _client


def _log(msg: str):
    """Log to stderr (stdout is the MCP transport)."""
    print(f"[mcp-rust-analyzer] {msg}", file=sys.stderr, flush=True)


def _format_locations(locs: list[dict]) -> str:
    return "\n".join(f"{l['file']}:{l['line']}:{l['col']}" for l in locs)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="symbols",
            description=(
                "Search for symbols (functions, types, constants, etc.) "
                "by name across the Rust workspace."
            ),
            inputSchema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Symbol name or partial name",
                    },
                },
            },
        ),
        types.Tool(
            name="hover",
            description=(
                "Get type information and documentation for a symbol "
                "at a specific file position."
            ),
            inputSchema={
                "type": "object",
                "required": ["file", "line", "column"],
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "File path (relative to workspace or absolute)",
                    },
                    "line": {"type": "integer", "description": "Line number (1-based)"},
                    "column": {"type": "integer", "description": "Column number (1-based)"},
                },
            },
        ),
        types.Tool(
            name="definition",
            description="Go to the definition of a symbol at a specific position.",
            inputSchema={
                "type": "object",
                "required": ["file", "line", "column"],
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "File path (relative to workspace or absolute)",
                    },
                    "line": {"type": "integer", "description": "Line number (1-based)"},
                    "column": {"type": "integer", "description": "Column number (1-based)"},
                },
            },
        ),
        types.Tool(
            name="references",
            description="Find all references to a symbol at a specific position.",
            inputSchema={
                "type": "object",
                "required": ["file", "line", "column"],
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "File path (relative to workspace or absolute)",
                    },
                    "line": {"type": "integer", "description": "Line number (1-based)"},
                    "column": {"type": "integer", "description": "Column number (1-based)"},
                    "include_declaration": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include the declaration itself",
                    },
                },
            },
        ),
        types.Tool(
            name="implementations",
            description=(
                "Find all implementations of a trait or method "
                "at a specific position."
            ),
            inputSchema={
                "type": "object",
                "required": ["file", "line", "column"],
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "File path (relative to workspace or absolute)",
                    },
                    "line": {"type": "integer", "description": "Line number (1-based)"},
                    "column": {"type": "integer", "description": "Column number (1-based)"},
                },
            },
        ),
        types.Tool(
            name="symbol_references",
            description=(
                "Find a symbol by name, then return all references to it. "
                "Combines workspace symbol search + reference lookup in one call."
            ),
            inputSchema={
                "type": "object",
                "required": ["symbol"],
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Exact symbol name to find",
                    },
                    "kind": {
                        "type": "string",
                        "enum": list(KIND_FILTER.keys()),
                        "default": "any",
                        "description": "Filter by symbol kind",
                    },
                },
            },
        ),
        types.Tool(
            name="document_symbols",
            description=(
                "List all symbols defined in a file — structural overview "
                "of a module's functions, types, constants, fields, etc."
            ),
            inputSchema={
                "type": "object",
                "required": ["file"],
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "File path (relative to workspace or absolute)",
                    },
                },
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        client = await get_client()

        if name == "symbols":
            return await _tool_symbols(client, arguments)
        elif name == "hover":
            return await _tool_hover(client, arguments)
        elif name == "definition":
            return await _tool_definition(client, arguments)
        elif name == "references":
            return await _tool_references(client, arguments)
        elif name == "implementations":
            return await _tool_implementations(client, arguments)
        elif name == "symbol_references":
            return await _tool_symbol_references(client, arguments)
        elif name == "document_symbols":
            return await _tool_document_symbols(client, arguments)
        else:
            return [_text(f"Unknown tool: {name}")]

    except Exception as e:
        return [_text(f"Error: {e}")]


def _text(s: str) -> types.TextContent:
    return types.TextContent(type="text", text=s)


async def _tool_symbols(client: LspClient, args: dict):
    query = args["query"]
    syms = await client.workspace_symbols(query)
    if not syms:
        return [_text(f'No symbols matching "{query}"')]
    lines = []
    for s in syms:
        container = f" (in {s['container']})" if s["container"] else ""
        lines.append(f"{s['kind']} {s['name']}{container} — {s['file']}:{s['line']}")
    return [_text("\n".join(lines))]


async def _tool_hover(client: LspClient, args: dict):
    result = await client.hover(
        args["file"], args["line"] - 1, args["column"] - 1,
    )
    return [_text(result if result else "No hover information at this position")]


async def _tool_definition(client: LspClient, args: dict):
    locs = await client.definition(
        args["file"], args["line"] - 1, args["column"] - 1,
    )
    if not locs:
        return [_text("No definition found")]
    return [_text(_format_locations(locs))]


async def _tool_references(client: LspClient, args: dict):
    include_decl = args.get("include_declaration", True)
    locs = await client.references(
        args["file"], args["line"] - 1, args["column"] - 1,
        include_declaration=include_decl,
    )
    if not locs:
        return [_text("No references found")]
    return [_text(f"{len(locs)} references:\n{_format_locations(locs)}")]


async def _tool_implementations(client: LspClient, args: dict):
    locs = await client.implementations(
        args["file"], args["line"] - 1, args["column"] - 1,
    )
    if not locs:
        return [_text("No implementations found")]
    return [_text(f"{len(locs)} implementations:\n{_format_locations(locs)}")]


async def _tool_symbol_references(client: LspClient, args: dict):
    symbol = args["symbol"]
    kind_filter = args.get("kind", "any")

    syms = await client.workspace_symbols(symbol)

    allowed = KIND_FILTER.get(kind_filter, [])
    if allowed:
        syms = [s for s in syms if s["kind"] in allowed]

    exact = [s for s in syms if s["name"] == symbol]
    target = exact[0] if exact else (syms[0] if syms else None)

    if not target:
        suffix = f" (kind: {kind_filter})" if kind_filter != "any" else ""
        return [_text(f'No symbol "{symbol}" found{suffix}')]

    extra = ""
    if len(exact) > 1:
        extra = "\n\nOther matches:\n" + "\n".join(
            f"  {s['kind']} {s['name']} (in {s['container']}) — {s['file']}:{s['line']}"
            for s in exact[1:]
        )

    refs = await client.references(
        target["file"], target["line"] - 1, target["col"] - 1,
        include_declaration=True,
    )

    header = f"{target['kind']} {target['name']}"
    if target["container"]:
        header += f" (in {target['container']})"
    header += f" — defined at {target['file']}:{target['line']}"

    text = f"{header}\n\n{len(refs)} references:\n{_format_locations(refs)}{extra}"
    return [_text(text)]


async def _tool_document_symbols(client: LspClient, args: dict):
    filepath = args["file"]
    syms = await client.document_symbols(filepath)
    if not syms:
        return [_text(f"No symbols in {filepath}")]
    lines = []
    for s in syms:
        detail = f" -> {s['detail']}" if s.get("detail") else ""
        lines.append(f"  {s['kind']:12} {s['name']}{detail}  (line {s['line']})")
    return [_text("\n".join(lines))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream, write_stream,
            app.create_initialization_options(),
        )
