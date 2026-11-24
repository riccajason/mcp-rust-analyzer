# mcp-rust-analyzer

MCP server exposing rust-analyzer as tools for Claude Code — semantic code navigation without grepping. Saves time and tokens.

## Tools

| Tool | What it does |
|------|-------------|
| `symbols` | Search for symbols by name across the workspace |
| `hover` | Get type information and docs at a file position |
| `definition` | Jump to where a symbol is defined |
| `references` | Find all references to a symbol |
| `implementations` | Find all trait/method implementations |
| `symbol_references` | Find a symbol by name + get all references (one call) |
| `document_symbols` | List all symbols in a file (structural overview) |

## Prerequisites

- Python 3.11+
- [rust-analyzer](https://rust-analyzer.github.io/) in your PATH

Install rust-analyzer:

```bash
# Via rustup (recommended)
rustup component add rust-analyzer

# Via Homebrew (macOS)
brew install rust-analyzer
```

## Setup

### 1. Install the package

```bash
git clone https://github.com/riccajason/mcp-rust-analyzer.git
cd mcp-rust-analyzer

python3 -m venv .venv
.venv/bin/pip install -e .
```

### 2. Put the command on your PATH

From inside the clone:

```bash
mkdir -p ~/.local/bin
ln -s "$PWD/.venv/bin/mcp-rust-analyzer" ~/.local/bin/mcp-rust-analyzer
```

Any directory on your `PATH` works; `~/.local/bin` is a common one. `command -v mcp-rust-analyzer` should print the link.

### 3. Configure Claude Code

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "rust-analyzer": {
      "command": "mcp-rust-analyzer",
      "args": []
    }
  }
}
```

The command is found by name, so the file holds no machine-specific path and can be committed with the project. Claude Code starts the server from the project directory, which is the default `WORKSPACE_ROOT` (see [Configuration](#configuration)).

### 4. Restart Claude Code

The MCP server starts when Claude Code connects: restart Claude Code, or in a session that is already open run `/mcp` and reconnect `rust-analyzer`. First connection takes ~20 seconds while rust-analyzer indexes the workspace. Subsequent tool calls are instant.

## Configuration

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKSPACE_ROOT` | Current directory | Path to the Rust project root |
| `RUST_ANALYZER_PATH` | `rust-analyzer` | Path to the rust-analyzer binary |

### VS Code settings

The server automatically reads `.vscode/settings.json` in your workspace and forwards any `rust-analyzer.*` settings. Projects with custom targets (embedded, `no_std`, cross-compilation) work without extra configuration:

```json
{
  "rust-analyzer.cargo.target": "x86_64-unknown-none",
  "rust-analyzer.check.allTargets": false
}
```

## How it works

```
Claude Code  ──MCP/stdio──>  mcp-rust-analyzer  ──LSP/JSON-RPC──>  rust-analyzer
```

The server spawns rust-analyzer as a subprocess, speaks the Language Server Protocol over its stdio, and translates MCP tool calls into LSP requests. rust-analyzer stays warm between calls — no re-indexing per query.

File changes are tracked via mtime. When a file is modified between tool calls, the server pushes the update to rust-analyzer automatically.

## License

MIT
