"""Read a Claude Code transcript (JSONL) and pull out the current turn."""
import json


def read(path):
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except (OSError, TypeError):
        pass
    return out


def text_of(content):
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def is_real_user(entry):
    """A message the person typed: not a tool result, a meta line or a harness wrapper."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    t = text_of(content).strip()
    return bool(t) and not t.startswith("<")


def current_turn(entries):
    """Entries after the last message the person typed."""
    last = max((i for i, e in enumerate(entries) if is_real_user(e)), default=-1)
    return entries[last + 1:]


def events(entries):
    """Yield ('use', tool_name, tool_input) / ('result', '', text) / ('text', '', assistant_text) in order."""
    for e in entries:
        content = (e.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                yield "use", b.get("name", ""), b.get("input") or {}
            elif b.get("type") == "tool_result":
                r = b.get("content")
                yield "result", "", (r if isinstance(r, str) else text_of(r))
            elif b.get("type") == "text" and e.get("type") == "assistant":
                yield "text", "", b.get("text", "")
