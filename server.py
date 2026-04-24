#!/usr/bin/env python3
"""MCP server — UBIK-DESKTOP bridge (port 7891)"""

import json
import sys
import urllib.request
import urllib.error
from typing import Any

BASE = "http://127.0.0.1:7891"

def http(method: str, path: str, body: dict | None = None) -> Any:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        return {"error": str(e), "ok": False}

TOOLS = [
    {
        "name": "ubik_list_agents",
        "description": "Liste tous les agents disponibles dans UBIK-DESKTOP (~/.ubik-desktop/agents/).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "ubik_list_sessions",
        "description": "Liste les sessions PTY actives dans UBIK-DESKTOP (tabIds en cours).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "ubik_create_session",
        "description": (
            "Ouvre une nouvelle session PTY dans UBIK-DESKTOP. "
            "agent_id est l'id d'un agent (ex: 'foundry-smith'). "
            "tab_id identifie le terminal (ex: 'mcp-0'). "
            "Si agent_id est omis, ouvre un terminal ubik-genie générique."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id":   {"type": "string", "description": "Identifiant unique du terminal"},
                "agent_id": {"type": "string", "description": "ID de l'agent à charger (optionnel)"},
            },
            "required": ["tab_id"],
        },
    },
    {
        "name": "ubik_write",
        "description": (
            "Envoie du texte à un terminal UBIK-DESKTOP. "
            "Ajoute \\r automatiquement pour valider (comme Entrée). "
            "Utilise ubik_read après pour lire la réponse."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "string", "description": "ID du terminal cible"},
                "text":   {"type": "string", "description": "Texte à envoyer"},
            },
            "required": ["tab_id", "text"],
        },
    },
    {
        "name": "ubik_read",
        "description": (
            "Lit et vide le buffer de sortie d'un terminal UBIK-DESKTOP depuis le dernier appel. "
            "Attends 2-3s après ubik_write avant de lire pour laisser le temps à ubik-genie de répondre."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "string", "description": "ID du terminal à lire"},
            },
            "required": ["tab_id"],
        },
    },
    {
        "name": "ubik_kill_session",
        "description": "Ferme et supprime une session PTY UBIK-DESKTOP.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "string", "description": "ID du terminal à fermer"},
            },
            "required": ["tab_id"],
        },
    },
]

def handle_tool(name: str, args: dict) -> str:
    if name == "ubik_list_agents":
        agents = http("GET", "/agents")
        if isinstance(agents, list):
            lines = [f"• {a['id']} — {a.get('description','')}" for a in agents]
            return "\n".join(lines) if lines else "Aucun agent trouvé."
        return str(agents)

    elif name == "ubik_list_sessions":
        sessions = http("GET", "/pty/sessions")
        if isinstance(sessions, list):
            return "\n".join(sessions) if sessions else "Aucune session active."
        return str(sessions)

    elif name == "ubik_create_session":
        tab_id   = args["tab_id"]
        agent_id = args.get("agent_id")
        agents_dir = f"{__import__('pathlib').Path.home()}/.ubik-desktop/agents"
        agent_path = f"{agents_dir}/{agent_id}.md" if agent_id else None
        body = {
            "tab_id": tab_id,
            "rows": 40,
            "cols": 200,
            "agent": agent_path,
        }
        result = http("POST", "/pty/create", body)
        return f"Session '{tab_id}' créée." if result.get("ok") else f"Erreur: {result}"

    elif name == "ubik_write":
        tab_id = args["tab_id"]
        text   = args["text"]
        if not text.endswith("\r"):
            text += "\r"
        result = http("POST", "/pty/write", {"tab_id": tab_id, "text": text})
        return "Envoyé." if result.get("ok") else f"Erreur: {result}"

    elif name == "ubik_read":
        tab_id = args["tab_id"]
        result = http("GET", f"/pty/read/{tab_id}")
        output = result.get("output", "")
        if not output:
            return "(buffer vide — ubik-genie n'a peut-être pas encore répondu)"
        # Strip ANSI for readability
        import re
        clean = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', output)
        clean = re.sub(r'\x1b\][^\x07]*\x07', '', clean)
        clean = clean.replace('\r\n', '\n').replace('\r', '')
        return clean.strip()

    elif name == "ubik_kill_session":
        tab_id = args["tab_id"]
        result = http("DELETE", f"/pty/{tab_id}")
        return f"Session '{tab_id}' fermée." if result.get("ok") else f"Erreur: {result}"

    return f"Outil inconnu: {name}"

# ── MCP stdio protocol ────────────────────────────────────────────────────────

def send(msg: dict):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = req.get("method", "")
        req_id = req.get("id")

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ubik-desktop-mcp", "version": "1.0.0"},
            }})

        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})

        elif method == "tools/call":
            tool_name = req.get("params", {}).get("name", "")
            tool_args = req.get("params", {}).get("arguments", {})
            try:
                result = handle_tool(tool_name, tool_args)
            except Exception as e:
                result = f"Erreur interne: {e}"
            send({"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": result}],
                "isError": False,
            }})

        elif method == "notifications/initialized":
            pass  # no response needed

        elif req_id is not None:
            send({"jsonrpc": "2.0", "id": req_id, "error": {
                "code": -32601, "message": f"Method not found: {method}"
            }})

if __name__ == "__main__":
    main()
