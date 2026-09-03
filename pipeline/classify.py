"""The precision layer: decide which candidates are real offers, and label them.

Three parts, cheapest first:

  looks_like_offer  regex prefilter. Cuts obvious non-offers before anything costs money.
  Labels            data/labels.json, keyed by candidate id. Every reviewed decision lives
                    here, positive or negative, whether a person or a model made it. The
                    pipeline never re-labels an id that has an entry, so runs are cheap
                    and a hand correction sticks.
  llm_label         asks Claude for a label when an id has none. Opt in with --llm.
"""
import html
import json
import re
from pathlib import Path

KINDS = [
    {"id": "help", "label": "Help & skills"},
    {"id": "feedback", "label": "Feedback & review"},
    {"id": "intro", "label": "Intros & leads"},
    {"id": "free", "label": "Free sessions"},
    {"id": "invite", "label": "Invites & spaces"},
    {"id": "opportunity", "label": "Opportunities"},
]
KIND_IDS = [k["id"] for k in KINDS]
RESPOND = ["dm", "reply", "link", "like"]

OFFER = re.compile(
    r"standing offer"
    r"|(happy|glad|down|available|keen|willing|more than happy) to (help|assist|chat|pair|review|advise|consult|jump|hop|talk)"
    r"|(dm|message|ping|reach out to|hmu|hit me up) me\b"
    r"|reach out (to me|if you)"
    r"|i('|’)?m offering|i offer\b|i can offer|offering (free|to|my|a|up|help)"
    r"|office hours"
    r"|for free|no charge|pro bono|free of charge"
    r"|if (you|anyone|any of you|y'?all|you all|folks|someone)[^.]{0,40}(need|want|are looking|would like|could use)"
    r"|(want|happy|glad) to help"
    r"|let me know if (you|anyone)"
    r"|hmu if|holler if",
    re.I,
)
# The author is asking, not offering. Dropped unless a strong offer phrase is also present.
REQUEST = re.compile(
    r"^\s*(i need|i really need|need help|can (someone|anyone) help me|help me\b|looking for someone"
    r"|does anyone (know|have)|any recs|anyone know how|how do i|pls help|please help)",
    re.I,
)
STRONG = re.compile(r"standing offer|i('|’)?m offering|office hours|dm me|reach out|happy to help", re.I)
# The author is asking the group for something.
ASK = re.compile(
    r"^(does|do) anyone|anyone (know|have|recommend|got|using|tried)"
    r"|looking for (a|an|some|someone|anyone|recs|recommendations|people|folks)"
    r"|any (recs|recommendations|tips|leads|suggestions)"
    r"|who should i (talk|ask|follow|read)|seeking (a|an|help|someone|collaborators?)"
    r"|^wanted:|^ask:|can (someone|anyone) (help|point|recommend|explain)"
    r"|need (a|an|some|help|advice|an intro|recommendations)",
    re.I,
)
# Posting convention, published on the board. Either word opens a notice.
CONVENTION = re.compile(r"^(standing offer|offer|ask|wanted)\s*:", re.I)


def side_of(text):
    """'offer', 'ask', or None. Cheap prefilter; the labeler has the final say."""
    if len(text) < 25:
        return None
    m = CONVENTION.match(text)
    if m:
        return "ask" if m.group(1).lower() in ("ask", "wanted") else "offer"
    if OFFER.search(text) and not (REQUEST.search(text) and not STRONG.search(text)):
        return "offer"
    if ASK.search(text) or REQUEST.search(text):
        return "ask"
    return None


def looks_like_offer(text):
    return side_of(text) == "offer"


def clean_text(text):
    """Verbatim tweet, minus t.co stubs and the leading @mentions of a reply."""
    text = html.unescape(text)
    text = re.sub(r"https?://t\.co/\w+", "", text)
    text = re.sub(r"^(\s*@\w+)+\s+", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class Labels:
    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, cid):
        return self.data.get(cid)

    def put(self, cid, label):
        self.data[cid] = label
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")


LABEL_SCHEMA = {
    "type": "object",
    "properties": {
        "is_offer": {"type": "boolean", "description": "True if this is a notice for the board: the author offers something to others, or asks the group for something."},
        "side": {"type": "string", "enum": ["offer", "ask"], "description": "offer = author gives; ask = author wants."},
        "kind": {"type": "string", "enum": KIND_IDS},
        "summary": {"type": "string", "description": "One sentence, third person, what is on offer. No hype."},
        "topics": {"type": "array", "items": {"type": "string"}, "description": "2 to 4 lowercase subject tags."},
        "respond": {"type": "string", "enum": RESPOND, "description": "How the author asked people to respond."},
        "standing": {"type": "boolean", "description": "True if the offer is open indefinitely."},
        "expires_at": {"type": "string", "description": "YYYY-MM-DD if the offer has an end date, else empty."},
        "place": {"type": "string", "description": "City or region if the offer is tied to one, else empty."},
        "confidence": {"type": "number"},
    },
    "required": ["is_offer", "side", "kind", "summary", "topics", "respond", "standing", "expires_at", "place", "confidence"],
    "additionalProperties": False,
}

SYSTEM = (
    "You label tweets for a community notice board. The board lists offers people make to "
    "the group and asks they make of it: help with a skill, feedback on work, introductions, "
    "free sessions, invitations to a space or event, and opportunities like roles or funding. "
    "Decide whether the tweet is a genuine offer to other people or a genuine ask of them. "
    "Commentary, jokes, rhetorical questions, and product promotion are neither. Fill the "
    "label only when it is one."
)


def llm_label(cand, client=None):
    """Label one candidate with Claude. Returns the label dict, or None on refusal."""
    import anthropic  # imported here so the pipeline runs without the SDK unless --llm

    client = client or anthropic.Anthropic()
    payload = {"author": f"@{cand['author']['handle']}", "posted_at": cand["posted_at"], "text": cand["text"]}
    resp = client.beta.messages.create(
        model="claude-opus-5",
        max_tokens=1024,
        betas=["server-side-fallback-2026-06-01"],
        fallbacks=[{"model": "claude-opus-4-8"}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": LABEL_SCHEMA}},
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    if resp.stop_reason == "refusal":
        return None
    text = next(b.text for b in resp.content if b.type == "text")
    label = json.loads(text)
    label["expires_at"] = label["expires_at"] or None
    label["place"] = label["place"] or None
    label["by"] = resp.model
    return label
