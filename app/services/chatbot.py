"""app/services/chatbot.py — the public landing-page enquiry bot. Answers
prospect questions about BrandsLens using a system prompt grounded in the
product's actual facts (pricing, sources, how it works) rather than letting
the model improvise details it might get wrong.

This endpoint is deliberately public and unauthenticated (a visitor hasn't
signed up yet), which means it's also the one part of the system with no
natural per-account usage limit — so real rate limiting isn't optional here
the way it is for authenticated routes. Kept simple and in-memory: genuinely
enough at MVP scale, and honest about not surviving a server restart or
scaling past one process (see the note on RATE_LIMITS below).
"""
from __future__ import annotations
import time
import logging
from collections import defaultdict, deque

from anthropic import Anthropic
from ..config import ANTHROPIC_API_KEY, CLASSIFIER_MODEL
from ..branding import BRAND

log = logging.getLogger("chatbot")

SYSTEM_PROMPT = f"""You are the enquiry assistant on the BrandsLens marketing website. \
BrandsLens is a media monitoring and brand reputation SaaS platform.

FACTS YOU MUST GET RIGHT — never invent numbers, features, or sources beyond these:

Tagline: "{BRAND['tagline']}"
Brand attributes: {', '.join(BRAND['attributes'])}

Pricing (annual; monthly costs 15% more; daily costs 20% more than the annual daily-equivalent rate):
- Standard: $1,500/yr — 1 workspace, 25 tracked keywords
- Growth: $2,500/yr — 5 workspaces, unlimited keywords, look-alike domain watch, full analytics/reporting
- Professional: $3,500/yr — 10 workspaces, unlimited keywords, priority support, dedicated onboarding
- Enterprise: custom quote, priced to the scope of the work — direct people to the "Get a quote" \
form on the pricing section for this tier; never invent a number for it.

There is NO free trial. Payment is required to activate an account.

Sources currently monitored, with zero setup required: global news (via GDELT) and any RSS feed \
the customer adds, Nairaland (a major Nigerian web forum), Hacker News, and look-alike/typosquat \
domain monitoring for phishing protection. Reddit and YouTube can be added once configured. \
Do not claim Facebook, Instagram, TikTok, or X/Twitter are currently monitored — they are not yet \
available (pending platform approval or cost).

Core capabilities: AI-based severity classification (HIGH/MEDIUM/WATCH) and sentiment analysis of \
mentions, a "Media Room" escalation workflow with an SLA clock and a hash-chained audit trail for \
serious incidents, branded PDF and Excel reporting with charts, team accounts with role-based \
access (Owner/Team Lead/Team Member), and profile management including a photo, phone, and address.

TONE: concise, confident, helpful — a few sentences per answer, not an essay. If asked something \
you genuinely don't know or that isn't in these facts, say so plainly and suggest they use the \
Enterprise contact form or reach out directly, rather than guessing or inventing an answer. \
Never discuss internal implementation details, security architecture, or anything not meant for \
a public prospect audience. Never generate code, unrelated content, or anything outside answering \
questions about BrandsLens as a product."""

# --- Simple in-memory rate limiter ---
# Keyed by client IP; a sliding window, not a token bucket — simpler, and
# genuinely sufficient for MVP traffic. Resets on server restart, and only
# applies per-process — both acceptable trade-offs at this scale, not
# oversights; revisit if this ever needs to survive across multiple workers
# or a restart-heavy deploy cadence.
RATE_LIMIT_MAX = 15
RATE_LIMIT_WINDOW_SECONDS = 60 * 10  # 10 minutes
_hits: dict[str, deque] = defaultdict(deque)


def check_rate_limit(client_ip: str) -> bool:
    """Returns True if this request is allowed, False if the caller should
    be rejected. Deliberately fails OPEN (allows the request) if something
    about the client_ip is malformed, since a broken rate limiter should
    never be the reason a real prospect can't get an answer."""
    if not client_ip:
        return True
    now = time.time()
    window = _hits[client_ip]
    while window and now - window[0] > RATE_LIMIT_WINDOW_SECONDS:
        window.popleft()
    if len(window) >= RATE_LIMIT_MAX:
        return False
    window.append(now)
    return True


def answer_app_chat(message: str, history: list[dict], workspace_name: str, mode: str, stats: dict) -> str:
    """The in-app 'Ask BrandsLens' copilot — a completely different system
    from the public marketing bot above. This one is authenticated, scoped
    to one real workspace, and grounded in that workspace's actual current
    numbers (passed in as `stats`), not general product facts. `mode`
    genuinely changes the instruction, not just cosmetic framing: 'basic'
    (Growth) answers straightforwardly from the numbers given; 'advanced'
    (Professional/Enterprise) is explicitly asked to reason about patterns
    across the data and give real, specific recommendations."""
    if not ANTHROPIC_API_KEY:
        return "I'm not fully connected yet — please contact support and a real person will help in the meantime."
    if not message or not message.strip():
        return "What would you like to know about this workspace's monitoring?"
    if len(message) > 2000:
        return "That's a lot to take in at once — could you ask in a shorter message?"

    depth_instruction = ("Answer straightforwardly and concisely from the numbers given below." if mode == "basic" else
                         "This is the advanced tier — don't just recite the numbers back. Look for patterns "
                         "across severity, sentiment, platform, and tags, and give specific, actionable "
                         "recommendations grounded in what's actually in the data, not generic advice.")
    system = f"""You are the in-app monitoring copilot for the BrandsLens workspace "{workspace_name}". \
You have access to this workspace's real current data below — answer using ONLY this data, never invent \
numbers or incidents that aren't shown here. If something isn't in the data provided, say so plainly \
rather than guessing.

{depth_instruction}

Current workspace data:
{stats}

Keep answers to a few sentences unless the question genuinely calls for more detail. Never discuss other \
customers' data, internal system architecture, or anything outside this workspace's own monitoring."""

    messages = []
    for turn in (history or [])[-6:]:
        role = turn.get("role")
        content = turn.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:2000]})
    messages.append({"role": "user", "content": message.strip()})

    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(model=CLASSIFIER_MODEL, max_tokens=500, system=system, messages=messages)
        return "".join(b.text for b in resp.content if b.type == "text").strip() or \
            "I couldn't quite form an answer to that — could you rephrase?"
    except Exception:  # noqa: BLE001
        log.exception("In-app chat call failed")
        return "Something went wrong on my end — please try again in a moment."


def answer(message: str, history: list[dict] | None = None) -> str:
    if not ANTHROPIC_API_KEY:
        return ("I'm not fully connected yet — please use the contact form for Enterprise "
                "enquiries, or reach out directly, and a real person will help.")
    if not message or not message.strip():
        return "What would you like to know about BrandsLens?"
    if len(message) > 2000:
        return "That's a lot to take in at once — could you ask in a shorter message?"

    messages = []
    for turn in (history or [])[-6:]:  # cap history so a long chat can't balloon token usage
        role = turn.get("role")
        content = turn.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:2000]})
    messages.append({"role": "user", "content": message.strip()})

    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=CLASSIFIER_MODEL, max_tokens=400, system=SYSTEM_PROMPT, messages=messages,
        )
        return "".join(block.text for block in resp.content if hasattr(block, "text")).strip() or \
            "I couldn't quite form an answer to that — could you rephrase?"
    except Exception:  # noqa: BLE001
        log.exception("Chatbot call failed")
        return "Something went wrong on my end — please try again in a moment."
