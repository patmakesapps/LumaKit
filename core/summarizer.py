"""Conversation summarizer for context window management.

Keeps the last RECENT_TURNS turns verbatim. Everything older gets compressed
into a running summary that sits right after the system prompt.
"""

RECENT_TURNS = 10  # keep this many recent turns verbatim

SUMMARIZE_PROMPT = (
    "Summarize the conversation so far in 2-3 concise sentences. "
    "Focus on: what the user asked for, what was done, key decisions made, "
    "and the current state of the work. Be specific about file names and actions."
)


def needs_summarization(messages: list[dict]) -> bool:
    """Check if the conversation has grown past the recent window.

    Count user messages as a proxy for turns — each user message corresponds
    to one turn. Counting assistant messages would inflate the count since
    tool calls produce extra assistant messages within a single turn.
    """
    turns = sum(1 for m in messages if m.get("role") == "user")
    return turns > RECENT_TURNS + 2


def build_summary_request(messages: list[dict]) -> list[dict]:
    """Build a minimal message list to ask the LLM for a summary.

    Takes the messages that are about to be trimmed and asks for a summary.
    """
    # Collect the older messages that will be compressed
    older = _get_older_messages(messages)
    if not older:
        return []

    # Include any existing summary
    existing_summary = _get_existing_summary(messages)
    context = ""
    if existing_summary:
        context = f"Previous context: {existing_summary}\n\n"

    # Build a readable version of the older turns
    turn_text = []
    for msg in older:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if role == "tool":
            name = msg.get("name", "tool")
            # Truncate tool results to keep summary request small
            if len(content) > 200:
                content = content[:200] + "..."
            turn_text.append(f"[Tool: {name}] {content}")
        elif content:
            turn_text.append(f"{role.title()}: {content}")

    conversation_block = "\n".join(turn_text)

    return [
        {
            "role": "user",
            "content": (
                f"{context}"
                f"Here is the conversation to summarize:\n\n{conversation_block}\n\n"
                f"{SUMMARIZE_PROMPT}"
            ),
        }
    ]


def apply_summary(messages: list[dict], summary_text: str) -> list[dict]:
    """Replace older messages with a summary, keeping recent turns verbatim.

    Returns a new messages list: [system_prompt, summary_msg, ...recent turns...]
    """
    system = messages[0] if messages and messages[0].get("role") == "system" else None
    recent = _get_recent_messages(messages)

    result = []
    if system:
        result.append(system)

    result.append({
        "role": "system",
        "content": f"[Context from earlier in this conversation]\n{summary_text}",
    })

    result.extend(recent)
    return result


def _get_existing_summary(messages: list[dict]) -> str | None:
    """Find an existing summary message if one exists."""
    for msg in messages:
        if (msg.get("role") == "system" and
                msg.get("content", "").startswith("[Context from earlier")):
            # Extract just the summary text
            content = msg["content"]
            prefix = "[Context from earlier in this conversation]\n"
            if content.startswith(prefix):
                return content[len(prefix):]
            return content
    return None


def _split_point(messages: list[dict]) -> int:
    """Find the index where recent messages start.

    We want to keep the last RECENT_TURNS user/assistant exchanges,
    plus any tool messages attached to them.
    """
    # Walk backward, counting user+assistant turns
    turn_count = 0
    split = len(messages)

    for i in range(len(messages) - 1, 0, -1):
        role = messages[i].get("role")
        if role in ("user", "assistant"):
            turn_count += 1
            if turn_count >= RECENT_TURNS * 2:  # user + assistant = 2 messages per turn
                split = i
                break

    return split


def _get_older_messages(messages: list[dict]) -> list[dict]:
    """Get messages that will be summarized (everything before the recent window)."""
    split = _split_point(messages)
    # Skip system messages at the start
    start = 0
    for i, msg in enumerate(messages):
        if msg.get("role") != "system":
            start = i
            break
    return messages[start:split]


def _get_recent_messages(messages: list[dict]) -> list[dict]:
    """Get the recent messages to keep verbatim."""
    split = _split_point(messages)
    return messages[split:]
