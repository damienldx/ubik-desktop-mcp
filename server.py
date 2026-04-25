#!/usr/bin/env python3
"""MCP server — UBIK-DESKTOP bridge (port 7891)"""

import json
import sys
import time
import urllib.request
import urllib.error
import yaml
import re
from pathlib import Path
from typing import Any

BASE = "http://127.0.0.1:7891"

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
            "Spawn un worker MCP dans UBIK-DESKTOP, paramétré avec model+skills, et le branche "
            "automatiquement à un thread Paperclip. Le worker reçoit sa tâche en prompt et reporte "
            "automatiquement (commentaire à chaque milestone, status=done à la fin, approval si bloqué). "
            "Si task et/ou model est fourni : crée un agent Paperclip dynamique + un thread, injecte le "
            "token Bearer + agentId + threadId dans l'env du PTY, et envoie la tâche au worker. "
            "Si seul tab_id est fourni : ouvre un PTY générique (mode legacy)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id":    {"type": "string", "description": "Identifiant unique du terminal (ex: 'mcp-0')"},
                "agent_id":  {"type": "string", "description": "ID d'un agent persona local (~/.ubik-desktop/agents/{id}.md). Optionnel."},
                "name":      {"type": "string", "description": "Nom du worker dans Paperclip (ex: 'auth-refactorer'). Si absent, dérivé de tab_id."},
                "model":     {"type": "string", "description": "Adapter Paperclip (ex: 'claude_local','codex_local','gemini_local','http'). Default 'claude_local'."},
                "skills":    {"type": "array", "items": {"type": "string"}, "description": "Skills attribués (doivent exister dans la company). Optionnel."},
                "task":      {"type": "string", "description": "La tâche à confier au worker — devient le prompt initial. Active le mode Paperclip auto."},
                "thread_id": {"type": "string", "description": "ID d'un thread Paperclip existant à attacher au worker. Si absent et task fourni, un thread est créé."},
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
        agents_dir = Path.home() / ".ubik-desktop" / "agents"
        agent_path = str(agents_dir / f"{agent_id}.md") if agent_id else None

        # ── Plain spawn (Paperclip wiring removed — isolation test) ─────────
        worker_name = args.get("name") or f"worker-{tab_id}"
        task = args.get("task")

        body = {
            "tab_id": tab_id,
            "rows": 40,
            "cols": 200,
            "agent": agent_path,
            "env": {},
        }
        result = http("POST", "/pty/create", body)
        if not result.get("ok"):
            return f"Erreur spawn: {result}"

        if task:
            time.sleep(0.5)
            http("POST", "/pty/write", {"tab_id": tab_id, "text": task + "\r"})

        summary = {
            "tab_id": tab_id,
            "worker_name": worker_name,
            "task_dispatched": bool(task),
        }
        return json.dumps(summary, ensure_ascii=False, indent=2)

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
        # Ctrl+C interrompt la commande en cours dans le PTY
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
