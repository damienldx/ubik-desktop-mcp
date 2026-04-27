#!/usr/bin/env python3
"""MCP server — UBIK-DESKTOP bridge (port 7891)"""

import http.client as _http_client
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import yaml
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE = "http://127.0.0.1:7891"
PAPERCLIP_API = "http://127.0.0.1:3100/api"
SYSTEM_REGISTRY = Path.home() / ".ubik-desktop" / "system-agents.json"
SOCKETS_DIR = Path.home() / ".ubik-desktop" / "sockets"
# PTY-based agents (Claude CLI) use wakeup/ so write_to_smart doesn't intercept
WAKEUP_DIR  = Path.home() / ".ubik-desktop" / "wakeup"

# Stopwords pour éviter la pollution du matching (FR + EN)
STOPWORDS = {
    # Français
    "le", "la", "les", "un", "une", "des", "du", "de", "ce", "cet", "cette", "ces",
    "mon", "ton", "son", "ma", "ta", "sa", "mes", "tes", "ses", "notre", "votre", "leur",
    "nos", "vos", "leurs", "je", "tu", "il", "elle", "on", "nous", "vous", "ils", "elles",
    "me", "te", "se", "lui", "leur", "y", "en", "qui", "que", "quoi", "dont", "où",
    "et", "ou", "mais", "donc", "car", "ni", "or", "si", "pour", "par", "dans", "sur",
    "avec", "sans", "sous", "vers", "chez", "est", "sont", "être", "avoir", "fait", "faire",
    # Anglais
    "the", "a", "an", "and", "or", "but", "if", "then", "else", "when", "at", "from",
    "by", "for", "with", "about", "against", "between", "into", "through", "during",
    "before", "after", "above", "below", "to", "up", "down", "in", "out", "on", "off",
    "over", "under", "again", "further", "once", "here", "there", "all", "any", "both",
    "each", "few", "more", "most", "other", "some", "such", "no", "nor", "not", "only",
    "own", "same", "so", "than", "too", "very", "can", "will", "just", "should", "now"
}

def _nvm_path_env() -> dict:
    """Inject nvm node bin into PATH so gemini/codex/npx are findable in spawned PTYs."""
    import glob as _glob
    nvm_bins = sorted(_glob.glob(str(Path.home() / ".config/nvm/versions/node/*/bin")), reverse=True)
    if not nvm_bins:
        nvm_bins = sorted(_glob.glob(str(Path.home() / ".nvm/versions/node/*/bin")), reverse=True)
    if not nvm_bins:
        return {}
    current_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    return {"PATH": f"{nvm_bins[0]}:{current_path}"}

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


def pc_call(method: str, path: str, body: dict | None = None) -> Any:
    """Call Paperclip API (local tunnel, no bearer needed for board ops in dev)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{PAPERCLIP_API}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
        if not raw:
            return {}
        return json.loads(raw)


def _registry_load() -> dict:
    if not SYSTEM_REGISTRY.exists():
        return {}
    try:
        return json.loads(SYSTEM_REGISTRY.read_text())
    except Exception:
        return {}


def _registry_save_all(data: dict) -> None:
    SYSTEM_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    SYSTEM_REGISTRY.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _registry_set(agent_id: str, info: dict) -> None:
    reg = _registry_load()
    reg[agent_id] = info
    _registry_save_all(reg)


def _registry_remove(agent_id: str) -> dict | None:
    reg = _registry_load()
    info = reg.pop(agent_id, None)
    _registry_save_all(reg)
    return info


def _registry_for_thread(thread_id: str) -> dict[str, dict]:
    reg = _registry_load()
    return {aid: info for aid, info in reg.items() if info.get("threadId") == thread_id}


def _wake_socket(tab_id: str, payload: str) -> bool:
    """Atomic write to ubik-cli socket — interrupts prompt_toolkit via _MCPWake."""
    sock_path = WAKEUP_DIR / f"{tab_id}.sock"
    if not sock_path.exists():
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(sock_path))
        s.sendall(payload.encode("utf-8"))
        s.close()
        return True
    except Exception:
        return False


def _pre_trust_workspace(workspace: str) -> None:
    """Write hasTrustDialogAccepted=True for workspace into ~/.claude.json so
    Claude CLI skips the interactive trust dialog at startup."""
    claude_json = Path.home() / ".claude.json"
    try:
        with open(claude_json) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    projects = data.setdefault("projects", {})
    projects.setdefault(workspace, {})["hasTrustDialogAccepted"] = True
    with open(claude_json, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _start_socket_listener(tab_id: str) -> None:
    """Start a daemon thread that listens on ~/.ubik-desktop/wakeup/{tab_id}.sock.
    Uses wakeup/ (not sockets/) so write_to_smart in Rust does NOT intercept
    regular PTY writes, preventing the infinite-loop regression.
    wake_thread_agents in paperclip.rs checks wakeup/ first.
    """
    WAKEUP_DIR.mkdir(parents=True, exist_ok=True)
    sock_path = str(WAKEUP_DIR / f"{tab_id}.sock")

    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass

    def _listen() -> None:
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(sock_path)
            srv.listen(5)
            srv.settimeout(1.0)
        except Exception:
            return
        try:
            while True:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except Exception:
                    break
                try:
                    chunks: list[bytes] = []
                    while True:
                        data = conn.recv(4096)
                        if not data:
                            break
                        chunks.append(data)
                    msg = b"".join(chunks).decode("utf-8", errors="replace").strip()
                    if msg:
                        http("POST", "/pty/write", {"tab_id": tab_id, "text": msg + "\r"})
                except Exception:
                    pass
                finally:
                    conn.close()
        finally:
            srv.close()
            try:
                os.unlink(sock_path)
            except Exception:
                pass

    threading.Thread(target=_listen, daemon=True, name=f"sock-{tab_id}").start()


def _start_sse_listener(tab_id: str, thread_id: str, own_agent_id: str | None = None) -> None:
    """Subscribe to SSE stream for a thread and forward new messages to the agent's PTY.

    Filters out the agent's own comments (via authorAgentId) to avoid self-loops.
    Reconnects automatically on connection failure (60s backoff cap).
    """
    def _listen() -> None:
        backoff = 2.0
        path = f"/api/events/stream?threadId={urllib.parse.quote(thread_id)}"
        host = "127.0.0.1"
        port = 3100
        while True:
            conn: "_http_client.HTTPConnection | None" = None
            try:
                conn = _http_client.HTTPConnection(host, port, timeout=60)
                conn.request("GET", path, headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"})
                resp = conn.getresponse()
                if resp.status != 200:
                    raise OSError(f"SSE status {resp.status}")
                backoff = 2.0
                buf = b""
                while True:
                    chunk = resp.read(1)
                    if not chunk:
                        break
                    buf += chunk
                    if buf.endswith(b"\n\n"):
                        for raw_line in buf.split(b"\n"):
                            line = raw_line.decode("utf-8", errors="replace").strip()
                            if not line.startswith("data:"):
                                continue
                            payload_str = line[5:].strip()
                            if not payload_str or payload_str == '{"type":"connected"}':
                                continue
                            try:
                                evt = json.loads(payload_str)
                            except json.JSONDecodeError:
                                continue
                            if evt.get("type") != "comment_added":
                                continue
                            comment = evt.get("comment", {})
                            if own_agent_id and comment.get("authorAgentId") == own_agent_id:
                                continue
                            # Format: compact JSON message injected into PTY
                            msg = json.dumps({
                                "type": "new_message",
                                "threadId": evt.get("threadId", thread_id),
                                "commentId": comment.get("id", ""),
                                "senderName": comment.get("authorName") or "inconnu",
                                "senderId": comment.get("authorAgentId") or "",
                                "body": comment.get("body", ""),
                            }, ensure_ascii=False)
                            http("POST", "/pty/write", {"tab_id": tab_id, "text": msg + "\r"})
                        buf = b""
            except Exception:
                pass
            finally:
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
            time.sleep(min(backoff, 60.0))
            backoff = min(backoff * 2, 60.0)

    threading.Thread(target=_listen, daemon=True, name=f"sse-{tab_id}").start()


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
            "Spawn un agent UBIK (Genie/ubik-cli) dans un terminal visible UBIK-DESKTOP. "
            "Si 'name' est fourni : crée un agent Paperclip, un thread, injecte les env vars, "
            "enregistre dans le registre system-agents — l'agent peut recevoir des messages via "
            "system_send_to_thread. Sans 'name' : PTY générique visible. "
            "Provider : UBIK / Genie. Pour Claude, utiliser claude_spawn_terminal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id":           {"type": "string", "description": "Identifiant unique du terminal (ex: 'mcp-0')."},
                "agent_id":         {"type": "string", "description": "ID d'un agent persona local (~/.ubik-desktop/agents/{id}.md). Optionnel."},
                "name":             {"type": "string", "description": "Nom de l'agent Paperclip (ex: 'ubik-refactor-auth'). Si fourni, active le wiring Paperclip."},
                "model":            {"type": "string", "description": "Adapter Paperclip (ex: 'claude_local','gemini_local'). Défaut: 'claude_local'."},
                "role":             {"type": "string", "description": "Rôle Paperclip (défaut: engineer)."},
                "skills":           {"type": "array", "items": {"type": "string"}, "description": "Skills Paperclip attribués."},
                "threadId":         {"type": "string", "description": "Thread Paperclip existant à rejoindre. Si absent et name fourni, crée un nouveau thread."},
                "title":            {"type": "string", "description": "Titre du thread si on en crée un nouveau."},
                "initialDirective": {"type": "string", "description": "Premier message envoyé à l'agent au démarrage."},
                "workspace":        {"type": "string", "description": "Répertoire de travail de l'agent."},
                "companyId":        {"type": "string", "description": "Company UUID. Sinon prend la première company."},
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
    {
        "name": "ubik_interrupt",
        "description": (
            "Interrompt la commande en cours dans un terminal UBIK-DESKTOP (Ctrl+C PTY), "
            "puis injecte un message de redirection. "
            "À utiliser quand un agent dévie de sa mission : build intempestif, boucle infinie, mauvais chemin. "
            "Force l'agent à lire les nouvelles instructions immédiatement."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id":  {"type": "string", "description": "ID du terminal à interrompre"},
                "message": {"type": "string", "description": "Instructions de redirection envoyées après l'interruption"},
            },
            "required": ["tab_id", "message"],
        },
    },
    {
        "name": "ubik_route_agent",
        "description": "Trouve l'agent le plus pertinent pour un prompt donné en analysant les manifests locaux.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Le besoin de l'utilisateur à router"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "system_send_to_thread",
        "description": (
            "Poste un commentaire dans un thread ET wake tous les agents SYSTEM attachés à ce thread "
            "(sauf l'auteur). Primitive unifiée que humain/CLI/agent doivent utiliser pour communiquer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "threadId":       {"type": "string"},
                "body":           {"type": "string"},
                "authorAgentId":  {"type": "string", "description": "ID Paperclip de l'auteur. Exclu du wake. Si absent, traité comme humain (board)."},
            },
            "required": ["threadId", "body"],
        },
    },
    {
        "name": "system_interrupt_agent",
        "description": (
            "Envoie SIGINT (Ctrl+C) à un agent SYSTEM headless. "
            "Interrompt la commande/round LLM en cours sans tuer le process. "
            "À utiliser quand un agent boucle, est bloqué, ou doit recevoir une nouvelle directive immédiatement. "
            "Le SIGINT vise le foreground process group du PTY — pas seulement le shell leader."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agentId": {"type": "string", "description": "ID Paperclip de l'agent à interrompre."},
            },
            "required": ["agentId"],
        },
    },
    {
        "name": "system_stop_agent",
        "description": "Stoppe proprement un agent SYSTEM : kill le PTY background, retire du registre.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agentId": {"type": "string", "description": "ID Paperclip de l'agent à stopper."},
            },
            "required": ["agentId"],
        },
    },
    {
        "name": "system_list_agents",
        "description": "Liste les agents SYSTEM actifs dans cette session DESKTOP. Filtrable par thread.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "threadId": {"type": "string", "description": "Si fourni, ne retourne que les agents attachés à ce thread."},
            },
        },
    },
    {
        "name": "system_react_to_comment",
        "description": (
            "Réagit à un commentaire avec un emoji (👍, 🚀, 🛑, ❤, ✅, ❌…). "
            "Encode la réaction comme un commentaire spécial '`:reaction:<emoji>:<targetCommentId>`' "
            "que l'UI groupe sous le commentaire cible. Pas de wake (signal léger)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "threadId":         {"type": "string"},
                "targetCommentId":  {"type": "string", "description": "ID du commentaire à réagir."},
                "emoji":            {"type": "string", "description": "L'emoji de la réaction."},
            },
            "required": ["threadId", "targetCommentId", "emoji"],
        },
    },
    {
        "name": "system_set_topic",
        "description": (
            "Définit ou met à jour le topic épinglé d'un thread (= description Paperclip). "
            "Le topic est ce que tout nouvel agent rejoignant le thread voit en premier comme contexte directeur."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "threadId": {"type": "string"},
                "topic":    {"type": "string", "description": "Le nouveau topic en markdown."},
            },
            "required": ["threadId", "topic"],
        },
    },
    {
        "name": "system_create_subthread",
        "description": (
            "Ouvre un sub-thread (issue enfant) attaché à un thread parent. Permet à un agent ou orchestrateur "
            "d'ouvrir une discussion technique parallèle sans polluer le canal principal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "parentThreadId": {"type": "string", "description": "Thread parent."},
                "title":          {"type": "string"},
                "description":    {"type": "string", "description": "Topic initial du sub-thread (devient le pinned)."},
                "assigneeAgentId":{"type": "string"},
                "labels":         {"type": "array", "items": {"type": "string"}},
                "companyId":      {"type": "string"},
            },
            "required": ["parentThreadId", "title"],
        },
    },
    # ── Claude CLI terminals ──────────────────────────────────────────────────
    {
        "name": "claude_spawn_terminal",
        "description": (
            "Ouvre un terminal Claude CLI interactif dans UBIK-DESKTOP. "
            "Le terminal apparaît dans une fenêtre MCP dédiée avec XTerm. "
            "Claude tourne avec --dangerously-skip-permissions (auto-approve). "
            "Attend que Claude soit prêt (prompt ❯ visible) avant de retourner. "
            "Si 'name' est fourni : crée un agent Paperclip, un thread, injecte les env vars, "
            "enregistre dans le registre system-agents — l'agent peut recevoir des messages via "
            "system_send_to_thread. "
            "Provider : Claude CLI. Pour UBIK/Genie, utiliser ubik_create_session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id":           {"type": "string", "description": "Identifiant unique du terminal (ex: 'claude-0'). Préfixe 'claude-' recommandé."},
                "cwd":              {"type": "string", "description": "Répertoire de travail initial. Optionnel."},
                "rows":             {"type": "integer", "description": "Hauteur du terminal (défaut: 40)"},
                "cols":             {"type": "integer", "description": "Largeur du terminal (défaut: 220)"},
                "initial_prompt":   {"type": "string", "description": "Prompt à envoyer dès que Claude est prêt. Optionnel (sans wiring Paperclip)."},
                "name":             {"type": "string", "description": "Nom de l'agent Paperclip (ex: 'claude-architecte'). Si fourni, active le wiring Paperclip."},
                "model":            {"type": "string", "description": "Adapter Paperclip (défaut: 'claude_local')."},
                "role":             {"type": "string", "description": "Rôle Paperclip (défaut: engineer)."},
                "skills":           {"type": "array", "items": {"type": "string"}, "description": "Skills Paperclip attribués."},
                "threadId":         {"type": "string", "description": "Thread Paperclip existant à rejoindre. Si absent et name fourni, crée un nouveau thread."},
                "title":            {"type": "string", "description": "Titre du thread si on en crée un nouveau."},
                "initialDirective": {"type": "string", "description": "Premier message envoyé à Claude au démarrage (wiring Paperclip). Prioritaire sur initial_prompt."},
                "workspace":        {"type": "string", "description": "Répertoire de travail (wiring Paperclip, prioritaire sur cwd)."},
                "companyId":        {"type": "string", "description": "Company UUID. Sinon prend la première company."},
            },
            "required": ["tab_id"],
        },
    },
    {
        "name": "claude_run_task",
        "description": (
            "Lance Claude CLI en mode headless (--print) pour exécuter une tâche en une passe. "
            "Le terminal est headless (pas de fenêtre MCP). "
            "Utilise claude_read pour récupérer la réponse."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id":  {"type": "string", "description": "Identifiant unique du terminal."},
                "prompt":  {"type": "string", "description": "La tâche à confier à Claude."},
                "cwd":     {"type": "string", "description": "Répertoire de travail (optionnel)."},
            },
            "required": ["tab_id", "prompt"],
        },
    },
    {
        "name": "claude_list_terminals",
        "description": "Liste les sessions Claude CLI actives dans UBIK-DESKTOP (tab_id préfixés par 'claude-').",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "claude_write",
        "description": (
            "Envoie du texte à un terminal Claude CLI (claude_spawn_terminal). "
            "Ajoute \\r automatiquement. Utilise claude_read après pour récupérer la réponse."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "string"},
                "text":   {"type": "string", "description": "Texte ou prompt à envoyer"},
            },
            "required": ["tab_id", "text"],
        },
    },
    {
        "name": "claude_read",
        "description": (
            "Lit et vide le buffer de sortie d'un terminal Claude CLI. "
            "Attends 3-5s après claude_write pour laisser Claude répondre. "
            "Strip les codes ANSI automatiquement."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "string"},
            },
            "required": ["tab_id"],
        },
    },
    {
        "name": "claude_interrupt",
        "description": (
            "Interrompt la commande en cours dans un terminal Claude CLI (Ctrl+C PTY), "
            "puis injecte un message de redirection. "
            "À utiliser quand Claude dévie, boucle, ou doit recevoir une nouvelle directive immédiatement."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id":  {"type": "string"},
                "message": {"type": "string", "description": "Instructions de redirection envoyées après l'interruption"},
            },
            "required": ["tab_id", "message"],
        },
    },
    {
        "name": "claude_kill",
        "description": "Ferme et supprime une session terminal Claude CLI.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "string"},
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
        return _ubik_create_session(args)

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
        clean = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', output)
        clean = re.sub(r'\x1b\][^\x07]*\x07', '', clean)
        clean = clean.replace('\r\n', '\n').replace('\r', '')
        return clean.strip()

    elif name == "ubik_interrupt":
        tab_id  = args["tab_id"]
        message = args["message"]
        # Real SIGINT to the foreground pgrp (kernel-level) — the raw \x03 byte
        # alone is ignored when the CLI's stdin is wedged. We still send \x03 as
        # a safety net for CLIs that handle Ctrl+C themselves in raw mode.
        http("POST", f"/pty/interrupt/{tab_id}")
        http("POST", "/pty/write", {"tab_id": tab_id, "text": "\x03"})
        time.sleep(0.5)
        if not message.endswith("\r"):
            message += "\r"
        result = http("POST", "/pty/write", {"tab_id": tab_id, "text": message})
        return "Interrompu et redirigé." if result.get("ok") else f"Erreur: {result}"

    elif name == "ubik_kill_session":
        tab_id = args["tab_id"]
        result = http("DELETE", f"/pty/{tab_id}")
        return f"Session '{tab_id}' fermée." if result.get("ok") else f"Erreur: {result}"

    elif name == "ubik_route_agent":
        prompt = args.get("prompt", "").lower()
        agents_dir = Path.home() / ".ubik-desktop" / "agents"
        if not agents_dir.exists():
            return json.dumps({"agent_id": None, "confidence": 0.0, "reasoning": "Répertoire agents introuvable.", "skills_bias": []})

        best_agent = None
        max_score = 0
        reasoning = "Aucun agent ne correspond de manière significative."
        
        # Nettoyage du prompt (ponctuation + stopwords)
        clean_prompt = re.sub(r'[^\w\s]', ' ', prompt)
        prompt_words = {w for w in clean_prompt.split() if w not in STOPWORDS and len(w) > 1}
        
        if not prompt_words:
            # Si le prompt ne contient que des stopwords, on tente un match exact sur l'ID quand même
            prompt_words = set(prompt.split())

        for agent_file in agents_dir.glob("*.md"):
            try:
                content = agent_file.read_text()
                if not content.startswith("---"):
                    continue
                
                parts = content.split("---", 2)
                if len(parts) < 3:
                    continue
                
                frontmatter = yaml.safe_load(parts[1])
                if not frontmatter:
                    continue
                
                agent_id = frontmatter.get("id", agent_file.stem)
                description = frontmatter.get("description", "").lower()
                tags = frontmatter.get("metadata", {}).get("tags", [])
                skills = frontmatter.get("context", {}).get("skills_bias", [])
                
                score = 0
                matches = []
                
                # Matching ID (poids fort)
                if agent_id.lower() in prompt:
                    score += 8
                    matches.append(f"ID match ({agent_id})")
                
                # Matching Description (filtré par stopwords)
                clean_desc = re.sub(r'[^\w\s]', ' ', description)
                desc_words = {w for w in clean_desc.split() if w not in STOPWORDS and len(w) > 1}
                common_desc = prompt_words.intersection(desc_words)
                if common_desc:
                    score += len(common_desc) * 3
                    matches.append(f"Description matches: {list(common_desc)}")
                
                # Matching Tags
                for tag in tags:
                    if tag.lower() in prompt:
                        score += 4
                        matches.append(f"Tag match: {tag}")
                
                # Matching Skills
                for skill in skills:
                    if skill.lower() in prompt:
                        score += 2
                        matches.append(f"Skill match: {skill}")
                
                if score > max_score:
                    max_score = score
                    # Confidence normalisée (diviseur augmenté à 20 pour être plus conservateur)
                    confidence = min(1.0, score / 20.0)
                    best_agent = {
                        "agent_id": agent_id,
                        "confidence": round(confidence, 2),
                        "reasoning": f"Matches trouvés: {', '.join(matches)}",
                        "skills_bias": skills
                    }
            except Exception:
                continue

        if best_agent and best_agent["confidence"] > 0.15:
            return json.dumps(best_agent)
        else:
            return json.dumps({"agent_id": None, "confidence": 0.0, "reasoning": reasoning, "skills_bias": []})

    elif name == "system_send_to_thread":
        return _system_send_to_thread(args)

    elif name == "system_interrupt_agent":
        return _system_interrupt_agent(args)

    elif name == "system_stop_agent":
        return _system_stop_agent(args)

    elif name == "system_list_agents":
        return _system_list_agents(args)

    elif name == "system_react_to_comment":
        return _system_react_to_comment(args)

    elif name == "system_set_topic":
        return _system_set_topic(args)

    elif name == "system_create_subthread":
        return _system_create_subthread(args)

    elif name == "claude_spawn_terminal":
        tab_id = args["tab_id"]
        env = {}
        env.update(_nvm_path_env())
        cwd = args.get("workspace") or args.get("cwd")
        if cwd:
            env["PWD"] = cwd

        pc_agent_id = None
        pc_thread_id = None

        if args.get("name"):
            try:
                pc_agent_id, pc_thread_id, pc_env = _paperclip_wire(args, tab_id)
                env.update(pc_env)
            except (ValueError, urllib.error.HTTPError) as e:
                return f"[error: Paperclip wiring: {e}]"

        # Pre-trust the workspace so Claude CLI skips the interactive trust dialog
        _pre_trust_workspace(cwd or str(Path.home()))

        body = {
            "tab_id": tab_id,
            "rows": args.get("rows", 40),
            "cols": args.get("cols", 220),
            "cli_mode": "claude",
            "env": env,
            "headless": False,
        }
        result = http("POST", "/pty/create", body)
        if not result.get("ok"):
            if pc_agent_id:
                try:
                    pc_call("DELETE", f"/agents/{pc_agent_id}")
                except Exception:
                    pass
            return f"[error: spawn Claude: {result}]"

        _start_socket_listener(tab_id)
        if pc_thread_id:
            _start_sse_listener(tab_id, pc_thread_id, pc_agent_id)

        if cwd:
            time.sleep(0.3)
            http("POST", "/pty/write", {"tab_id": tab_id, "text": f"cd {cwd}\r"})

        import re as _re
        ansi_escape = _re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        deadline = time.time() + 20
        ready = False
        while time.time() < deadline:
            time.sleep(0.5)
            buf = ansi_escape.sub('', http("GET", f"/pty/read/{tab_id}").get("output", ""))
            # Auto-confirm the workspace trust dialog (PTY may strip spaces)
            if "trust" in buf.lower() and ("Yes, I trust" in buf or "Yes,Itrust" in buf or "Yes,\xa0I" in buf):
                http("POST", "/pty/write", {"tab_id": tab_id, "text": "\r"})
                continue
            if "❯" in buf or "> " in buf:
                ready = True
                break

        if not ready:
            summary = {"tab_id": tab_id, "pid": result.get("pid"), "status": "terminal ouvert (timeout init)"}
            if pc_agent_id:
                summary["agentId"] = pc_agent_id
                summary["threadId"] = pc_thread_id
            return json.dumps(summary)

        first_message = args.get("initialDirective") or args.get("initial_prompt")
        if first_message:
            text = first_message if first_message.endswith("\r") else first_message + "\r"
            http("POST", "/pty/write", {"tab_id": tab_id, "text": text})

        summary = {"tab_id": tab_id, "pid": result.get("pid"), "status": "terminal prêt"}
        if pc_agent_id:
            summary["agentId"] = pc_agent_id
            summary["threadId"] = pc_thread_id
            summary["name"] = args.get("name")
        return json.dumps(summary, ensure_ascii=False)

    elif name == "claude_run_task":
        tab_id  = args["tab_id"]
        prompt  = args["prompt"]
        env = {}
        if cwd := args.get("cwd"):
            env["PWD"] = cwd
        body = {
            "tab_id": tab_id,
            "rows": 40,
            "cols": 220,
            "cli_mode": "claude",
            "env": env,
            "headless": True,
        }
        result = http("POST", "/pty/create", body)
        if not result.get("ok"):
            return f"Erreur spawn Claude: {result}"
        # cd first if cwd provided, then send the prompt in print mode
        if cwd := args.get("cwd"):
            time.sleep(0.2)
            http("POST", "/pty/write", {"tab_id": tab_id, "text": f"cd {cwd}\r"})
            time.sleep(0.2)
        # Pass the prompt as a -p flag via shell
        escaped = prompt.replace("'", "'\\''")
        http("POST", "/pty/write", {"tab_id": tab_id, "text": f"claude -p '{escaped}'\r"})
        return json.dumps({"tab_id": tab_id, "pid": result.get("pid"), "status": "tâche lancée — utilise claude_read pour la réponse"})

    elif name == "claude_list_terminals":
        sessions = http("GET", "/pty/sessions")
        if isinstance(sessions, list):
            claude_sessions = [s for s in sessions if s.startswith("claude")]
            return "\n".join(claude_sessions) if claude_sessions else "Aucun terminal Claude actif."
        return str(sessions)

    elif name == "claude_write":
        tab_id = args["tab_id"]
        text   = args["text"]
        # Strip trailing \r — we'll send it as a separate write after a delay
        # so Claude CLI doesn't absorb it into the paste buffer
        text = text.rstrip("\r")
        http("POST", "/pty/write", {"tab_id": tab_id, "text": text})
        time.sleep(0.15)
        result = http("POST", "/pty/write", {"tab_id": tab_id, "text": "\r"})
        return "Envoyé." if result.get("ok") else f"Erreur: {result}"

    elif name == "claude_read":
        tab_id = args["tab_id"]
        result = http("GET", f"/pty/read/{tab_id}")
        output = result.get("output", "")
        if not output:
            return "(buffer vide — Claude n'a peut-être pas encore répondu)"
        clean = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', output)
        clean = re.sub(r'\x1b\][^\x07]*\x07', '', clean)
        clean = clean.replace('\r\n', '\n').replace('\r', '')
        return clean.strip()

    elif name == "claude_interrupt":
        tab_id  = args["tab_id"]
        message = args["message"]
        http("POST", f"/pty/interrupt/{tab_id}")
        http("POST", "/pty/write", {"tab_id": tab_id, "text": "\x03"})
        time.sleep(0.5)
        if not message.endswith("\r"):
            message += "\r"
        result = http("POST", "/pty/write", {"tab_id": tab_id, "text": message})
        return "Interrompu et redirigé." if result.get("ok") else f"Erreur: {result}"

    elif name == "claude_kill":
        tab_id = args["tab_id"]
        result = http("DELETE", f"/pty/{tab_id}")
        return f"Terminal Claude '{tab_id}' fermé." if result.get("ok") else f"Erreur: {result}"

    return f"Outil inconnu: {name}"


# ── SYSTEM tool implementations ──────────────────────────────────────────────

def _ubik_list_sessions(args: dict) -> str:
    sessions = http("GET", "/pty/sessions")
    if isinstance(sessions, list):
        return "\n".join(sessions) if sessions else "Aucune session active."
    return str(sessions)

def _ubik_read(args: dict) -> str:
    tab_id = args["tab_id"]
    result = http("GET", f"/pty/read/{tab_id}")
    output = result.get("output", "")
    if not output:
        return "(buffer vide)"
    clean = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', output)
    clean = re.sub(r'\x1b\][^\x07]*\x07', '', clean)
    clean = clean.replace('\r\n', '\n').replace('\r', '')
    return clean.strip()

def _ubik_write(args: dict) -> str:
    tab_id = args["tab_id"]
    text = args["text"]
    if not text.endswith("\r"):
        text += "\r"
    result = http("POST", "/pty/write", {"tab_id": tab_id, "text": text})
    return "Envoyé." if result.get("ok") else f"Erreur: {result}"


def _ubik_create_session(args: dict) -> str:
    tab_id = args["tab_id"]
    agent_manifest = args.get("agent_id")
    agents_dir = Path.home() / ".ubik-desktop" / "agents"
    agent_path = str(agents_dir / f"{agent_manifest}.md") if agent_manifest else None

    env = {}
    pc_agent_id = None
    pc_thread_id = None

    if args.get("name"):
        try:
            pc_agent_id, pc_thread_id, env = _paperclip_wire(args, tab_id)
        except (ValueError, urllib.error.HTTPError) as e:
            return f"[error: Paperclip wiring: {e}]"

    env.update(_nvm_path_env())

    body = {
        "tab_id": tab_id,
        "rows": 40,
        "cols": 200,
        "agent": agent_path,
        "headless": False,
        "env": env,
    }
    result = http("POST", "/pty/create", body)
    if not result.get("ok"):
        if pc_agent_id:
            try:
                pc_call("DELETE", f"/agents/{pc_agent_id}")
            except Exception:
                pass
        return f"[error: PTY spawn failed: {result}]"

    _start_socket_listener(tab_id)
    if pc_thread_id:
        _start_sse_listener(tab_id, pc_thread_id, pc_agent_id)

    initial_directive = args.get("initialDirective")
    if initial_directive:
        # Wait for ubik-cli to be ready before sending the directive
        _ansi = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        deadline = time.time() + 20
        ready = False
        while time.time() < deadline:
            time.sleep(0.4)
            buf = _ansi.sub('', http("GET", f"/pty/read/{tab_id}").get("output", ""))
            if "Ready." in buf:
                ready = True
                break
        if ready:
            http("POST", "/pty/write", {"tab_id": tab_id, "text": initial_directive + "\r"})

    summary: dict = {"tab_id": tab_id, "status": "terminal ouvert"}
    if pc_agent_id:
        summary["agentId"] = pc_agent_id
        summary["threadId"] = pc_thread_id
        summary["name"] = args.get("name")
    return json.dumps(summary, ensure_ascii=False, indent=2)


def _paperclip_wire(args: dict, tab_id: str) -> tuple[str | None, str | None, dict]:
    """Register agent + thread in ubik-threads local backend.

    Returns (agent_id, thread_id, env_vars). Returns (None, None, {}) if name absent.
    Raises ValueError on errors.
    """
    name = args.get("name")
    if not name:
        return None, None, {}

    company_id = _resolve_company_id(args)
    if not company_id:
        raise ValueError("no company found — is ubik-threads running on :3100?")

    role = args.get("role") or "engineer"
    model = args.get("model") or "claude_local"
    skills = args.get("skills") or []
    workspace = args.get("workspace")
    initial_directive = args.get("initialDirective")
    thread_id = args.get("threadId")

    agent_body = {"name": name, "role": role, "adapterType": model, "adapterConfig": {}}
    if skills:
        agent_body["desiredSkills"] = skills
    pc_agent = pc_call("POST", f"/companies/{company_id}/agents", agent_body)
    agent_id = pc_agent["id"]

    thread_topic = ""
    if not thread_id:
        title = (args.get("title") or initial_directive or name)[:80]
        issue = pc_call("POST", f"/companies/{company_id}/issues", {
            "title": title,
            "description": initial_directive or "",
            "assigneeAgentId": agent_id,
        })
        thread_id = issue["id"]
        thread_topic = issue.get("description") or ""
    else:
        try:
            existing = pc_call("GET", f"/issues/{thread_id}")
            thread_topic = existing.get("description") or ""
        except Exception:
            thread_topic = ""

    env = {
        "PAPERCLIP_API_URL": PAPERCLIP_API,
        "PAPERCLIP_AGENT_ID": agent_id,
        "PAPERCLIP_COMPANY_ID": company_id,
        "PAPERCLIP_THREAD_ID": thread_id,
    }
    if workspace:
        env["WORKSPACE_PATH"] = workspace
    if thread_topic:
        env["PAPERCLIP_THREAD_TOPIC"] = thread_topic

    _registry_set(agent_id, {
        "tabId": tab_id,
        "threadId": thread_id,
        "name": name,
        "workspace": workspace,
        "spawnedAt": datetime.now(timezone.utc).isoformat(),
    })

    return agent_id, thread_id, env


def _resolve_company_id(args: dict) -> str | None:
    cid = args.get("companyId")
    if cid:
        return cid
    try:
        companies = pc_call("GET", "/companies")
        if isinstance(companies, list) and companies:
            return companies[0]["id"]
    except Exception:
        pass
    return None


_MENTION_RE = re.compile(r'@([A-Za-z0-9_\-]+)')


def _system_send_to_thread(args: dict) -> str:
    thread_id = args.get("threadId")
    body = args.get("body")
    if not thread_id or not body:
        return "[error: threadId and body required]"
    author_agent_id = args.get("authorAgentId")

    comment_payload: dict = {"body": body}
    if author_agent_id:
        comment_payload["authorAgentId"] = author_agent_id
    try:
        pc_call("POST", f"/issues/{thread_id}/comments", comment_payload)
    except urllib.error.HTTPError as e:
        return f"[error: Paperclip add_comment {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}]"
    except Exception as e:
        return f"[error: Paperclip add_comment: {e}]"

    # Resolve author display name for the wake payload.
    author_name = args.get("authorName")
    if not author_name and author_agent_id:
        reg = _registry_load()
        author_name = reg.get(author_agent_id, {}).get("name") or author_agent_id[:8]
    if not author_name:
        author_name = "Human"
    wake_payload = json.dumps({
        "type": "new_message",
        "threadId": thread_id,
        "senderName": author_name,
        "senderId": author_agent_id or "",
        "body": body,
        "ts": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False)

    attached = _registry_for_thread(thread_id)
    mentions = set(_MENTION_RE.findall(body))
    if mentions:
        targets = {aid: info for aid, info in attached.items() if info.get("name") in mentions}
        mode = "mentions"
    else:
        targets = attached
        mode = "broadcast"

    woken = []
    skipped = []
    for aid, info in targets.items():
        if author_agent_id and aid == author_agent_id:
            skipped.append(aid)
            continue
        ok = _wake_socket(info["tabId"], wake_payload)
        (woken if ok else skipped).append(aid)

    return json.dumps({
        "posted": True,
        "mode": mode,
        "mentions": sorted(mentions),
        "woken": woken,
        "skipped": skipped,
    }, ensure_ascii=False, indent=2)


def _system_interrupt_agent(args: dict) -> str:
    agent_id = args.get("agentId")
    if not agent_id:
        return "[error: agentId required]"
    info = _registry_load().get(agent_id)
    if not info:
        return "[error: agent not in SYSTEM registry]"
    tab_id = info["tabId"]
    result = http("POST", f"/pty/interrupt/{tab_id}")
    if not result.get("ok"):
        return f"[error: interrupt failed: {result}]"
    return json.dumps({
        "interrupted": agent_id,
        "tabId": tab_id,
        "pgid": result.get("pgid"),
        "fallback": result.get("fallback"),
    }, ensure_ascii=False)


def _system_stop_agent(args: dict) -> str:
    agent_id = args.get("agentId")
    if not agent_id:
        return "[error: agentId required]"
    info = _registry_load().get(agent_id)
    if not info:
        return "[error: agent not in SYSTEM registry]"
    tab_id = info["tabId"]
    http("DELETE", f"/pty/{tab_id}")
    _registry_remove(agent_id)
    return json.dumps({"stopped": agent_id, "tabId": tab_id}, ensure_ascii=False)


def _system_list_agents(args: dict) -> str:
    thread_id = args.get("threadId")
    reg = _registry_load()
    if thread_id:
        reg = {aid: info for aid, info in reg.items() if info.get("threadId") == thread_id}
    return json.dumps(reg, ensure_ascii=False, indent=2)


def _system_react_to_comment(args: dict) -> str:
    thread_id = args.get("threadId")
    target_id = args.get("targetCommentId")
    emoji = args.get("emoji")
    if not (thread_id and target_id and emoji):
        return "[error: threadId, targetCommentId, emoji required]"
    body = f":reaction:{emoji}:{target_id}"
    try:
        comment = pc_call("POST", f"/issues/{thread_id}/comments", {"body": body})
    except urllib.error.HTTPError as e:
        return f"[error: react failed {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}]"
    except Exception as e:
        return f"[error: react failed: {e}]"
    return json.dumps({"reacted": True, "emoji": emoji, "target": target_id, "commentId": comment.get("id")}, ensure_ascii=False)


def _system_set_topic(args: dict) -> str:
    thread_id = args.get("threadId")
    topic = args.get("topic")
    if not (thread_id and topic is not None):
        return "[error: threadId and topic required]"
    try:
        issue = pc_call("PATCH", f"/issues/{thread_id}", {"description": topic})
    except urllib.error.HTTPError as e:
        return f"[error: set_topic failed {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}]"
    except Exception as e:
        return f"[error: set_topic failed: {e}]"

    # Wake all agents attached to this thread so they pick up the new topic
    attached = _registry_for_thread(thread_id)
    payload = f":topic-updated: {topic}"
    woken = []
    for aid, info in attached.items():
        if _wake_socket(info["tabId"], payload):
            woken.append(aid)

    return json.dumps({"topicUpdated": True, "issueId": issue.get("id"), "wokenAgents": woken}, ensure_ascii=False)


def _system_create_subthread(args: dict) -> str:
    parent_id = args.get("parentThreadId")
    title = args.get("title")
    if not (parent_id and title):
        return "[error: parentThreadId and title required]"
    company_id = _resolve_company_id(args)
    if not company_id:
        return "[error: no Paperclip company found]"

    body = {"title": title, "parentIssueId": parent_id}
    for k in ("description", "assigneeAgentId", "assigneeUserId", "labels"):
        v = args.get(k)
        if v is not None and v != "":
            body[k] = v
    try:
        issue = pc_call("POST", f"/companies/{company_id}/issues", body)
    except urllib.error.HTTPError as e:
        return f"[error: create_subthread failed {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}]"
    except Exception as e:
        return f"[error: create_subthread failed: {e}]"
    return json.dumps({
        "subthreadId": issue.get("id"),
        "identifier": issue.get("identifier"),
        "parentThreadId": parent_id,
        "title": title,
    }, ensure_ascii=False, indent=2)

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
                "serverInfo": {"name": "ubik-desktop-mcp", "version": "2.0.0"},
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
