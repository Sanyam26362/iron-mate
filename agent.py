import os
import re
import json
import logging
import unicodedata
import difflib
from dataclasses import dataclass, field
from datetime import datetime
import ollama
import db

logger = logging.getLogger("ironmate.agent")

# Global trackers for backwards compatibility and test verification
LAST_TOOL_CALL = None
TOOL_CALL_COUNT = 0
LAST_USER_MESSAGE = ""
LAST_BOT_REPLY = ""
LAST_TURN_META = {}


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class Intent:
    kind: str  # 'intake', 'snooze', 'chit_chat'
    supplements: list[str] = field(default_factory=list)
    minutes: int | None = None
    hint: str | None = None
    subcategory: str | None = None


@dataclass
class AgentResponse:
    text: str
    actions: list = field(default_factory=list)
    history: list = field(default_factory=list)

    def __str__(self) -> str:
        return self.text

    def __iter__(self):
        # Enables tuple unpacking: reply, actions = chat_with_agent(...)
        return iter((self.text, self.actions))


# ---------------------------------------------------------------------------
# Text Normalization & Precedence Router
# ---------------------------------------------------------------------------

DELAY_BUSY_REGEX = re.compile(
    r"\b(?:class\s+me|class\s+mein|in\s+class|lecture|lab|busy|meeting|driving|traffic|hospital|"
    r"baad\s+me|baad\s+mein|later|remind|snooze|postpone|abhi\s+nhi|abhi\s+nahi|not\s+now|"
    r"ping\s+me|ping\s+karna|ping\s+krna)\b",
    re.I,
)

SUPPLEMENT_REGEX = re.compile(
    r"\b(?:iron|vitamins?|multivitamins?|tablets?|goli|pills?|medicines?|supplements?)\b",
    re.I,
)

BEVERAGE_REGEX = re.compile(
    r"\b(?:chai|tea|coffee|doodh|milk)\b",
    re.I,
)

TAKEN_VERBS_REGEX = re.compile(
    r"\b(?:le\s+li|le\s+liya|le\s+liye|kha\s+li|kha\s+liya|kha\s+liye|li|liya|liye|"
    r"take|took|taken|had|swallowed|finished|done|khaya|lena|khana|pina)\b",
    re.I,
)

NEGATION_WORDS = {
    "nhi", "nahi", "nai", "not", "didnt", "didn't",
    "havent", "haven't", "bhool", "bhul", "forgot", "yet"
}

# New & Expanded Chit-Chat Subcategory Patterns
BOT_QUESTION_REGEX = re.compile(
    r"\b(?:are\s+(?:you|u)\s+(?:a\s+)?(?:bot|ai)|tu\s+(?:bot|ai)\s+hai|bot\s+hai\s+tu|"
    r"real\s+(?:person|user)|kaun\s+hai\s+tu|who\s+(?:is\s+this|are\s+you)|bot\s+ho\s+kya|"
    r"are\s+you\s+bot|are\s+u\s+bot)\b",
    re.I,
)

THREAT_BLOCK_REGEX = re.compile(
    r"\b(?:block\s+(?:krdu|kardungi|kr\s+dungi|kar\s+dungi|karungi|krungi|kar\s+du|kr\s+du|tujhe)|"
    r"block\s+kar\s+dungi|bye\s+forever|baat\s+nhi\s+krni|baat\s+nahi\s+karni|"
    r"ghost\s+(?:krdu|kr\s+dungi|kar\s+dungi|karungi))\b",
    re.I,
)

CALL_REQUEST_REGEX = re.compile(
    r"\b(?:call\s+(?:kr|kar|karo|karein|karna|krna)(?:\s+mujhe)?|call\s+mujhe|call\s+utha|"
    r"call\s+me|phone\s+(?:kr|kar|karo|karna|krna))\b",
    re.I,
)

SEXUAL_REQUEST_REGEX = re.compile(
    r"\b(?:sexting|sext|nudes?|naked|send\s+pics?|send\s+nudes?|nude\s+pics?|"
    r"boobs|dick|cock|pussy|vagina|condom|makeout|make\s+out|horny|peg|pegged)\b",
    re.I,
)

PROMPT_TO_ASK_REGEX = re.compile(
    r"\b(?:pooch(?:ega)?\s+nhi|puchh?\s+na|pooch\s+na|tu\s+pooch|ask\s+me|"
    r"kuch\s+puchh?a\s+nhi|kuch\s+poocha\s+nhi|tu\s+btayega\s+nhi|puchha\s+kyu\s+nhi|"
    r"poochha\s+kyu\s+nhi|poochega\s+nhi\s+kya\s+kra)\b",
    re.I,
)

DOUBT_TEASING_REGEX = re.compile(
    r"\b(?:(?:chl|chal)\s+jh[ou]+t[ha]+|jh[ou]+t[ha]+|liar|sach\s+me\??|pakka\??|"
    r"sure\??|ha\s+ha\s+rehne\s+de|rehne\s+de)\b",
    re.I,
)

ACK_BARE_REGEX = re.compile(
    r"^(?:ok+|okay+|okkay+|hmm+|acha+|achaa+|thik\s*h+|k)$",
    re.I,
)

PROFANITY_LIST = [
    "bsdk", "bsdke", "bhosdike", "bhosdi", "bc", "mc", "bkl", "mkc",
    "chutia", "chutiya", "chutiye", "chut", "lodu", "lode", "lund",
    "gandu", "gaandu", "madarchod", "behenchod", "bhenchod", "harami",
    "kamina", "kamine", "saale", "saala", "kutta", "kutte", "pagal",
    "fuck", "fucking", "fucked", "shit", "bitch", "asshole", "bastard",
    "wtf", "lavde", "laude",
]


def normalize_text(text: str) -> str:
    """Normalize text: lowercase, NFKC, collapse runs of 3+ chars to 2."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).lower().strip()
    t = re.sub(r"(.)\1{2,}", r"\1\1", t)
    return t


def deobfuscate(text: str) -> str:
    """De-obfuscate common leetspeak and spaced profanities."""
    t = text.lower()
    t = (
        t.replace("0", "o")
        .replace("1", "i")
        .replace("3", "e")
        .replace("4", "a")
        .replace("@", "a")
        .replace("$", "s")
    )
    t = re.sub(r"(.)\1+", r"\1", t)
    return t


def contains_profanity(text: str) -> bool:
    """Check if text contains swearing/abuse with obfuscation support."""
    norm = normalize_text(text)
    for p in PROFANITY_LIST:
        if re.search(r"\b" + re.escape(p) + r"\b", norm):
            return True

    deob = deobfuscate(text)
    for p in PROFANITY_LIST:
        p_c = re.sub(r"(.)\1+", r"\1", p)
        if re.search(r"\b" + re.escape(p_c) + r"\b", deob):
            return True

    # Check spaced letters e.g. "b s d k"
    no_spaces = re.sub(r"\s+", "", deob)
    for p in ["bsdk", "bsdke", "bhosdike", "chutia", "chutiya", "lodu", "gandu", "bkl", "mkc"]:
        p_c = re.sub(r"(.)\1+", r"\1", p)
        if p_c in no_spaces:
            return True

    return False


def parse_minutes(val) -> int:
    """Safely parse minute/hour expressions into an integer number of minutes."""
    if val is None:
        return 30
    if isinstance(val, (int, float)):
        return max(1, int(val))
    if isinstance(val, str):
        v = val.lower().strip()
        if "aadha ghanta" in v or "half hour" in v or "half an hour" in v:
            return 30
        if "ek ghanta" in v or "1 ghanta" in v:
            return 60
        hr_match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h|ghante?)\b", v)
        if hr_match:
            return max(1, int(float(hr_match.group(1)) * 60))
        min_match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|m)\b", v)
        if min_match:
            return max(1, int(float(min_match.group(1))))
        after_min = re.search(r"(?:after|in)\s+(\d+)\s*min", v)
        if after_min:
            return max(1, int(after_min.group(1)))
        any_num = re.search(r"\b(\d+)\b", v)
        if any_num:
            return max(1, int(any_num.group(1)))
    return 30


def extract_supplements(norm_text: str) -> list[str]:
    """Identify which supplements are mentioned."""
    supps = []
    if re.search(r"\biron\b", norm_text):
        supps.append("iron")
    if re.search(r"\b(?:multivitamins?|vitamins?)\b", norm_text):
        supps.append("multivitamin")
    if not supps and SUPPLEMENT_REGEX.search(norm_text):
        supps.append("iron")
    return supps


def check_intake_negation(norm_text: str) -> bool:
    """Check if a negation word is within 4 tokens of any taken-verb."""
    if not SUPPLEMENT_REGEX.search(norm_text):
        return False

    tokens = re.findall(r"[a-zA-Z']+", norm_text)
    verb_indices = []
    neg_indices = []

    for idx, w in enumerate(tokens):
        if w in NEGATION_WORDS:
            neg_indices.append(idx)
        if TAKEN_VERBS_REGEX.search(w) or w in {
            "take", "li", "liya", "liye", "took", "taken", "had", "done",
            "swallowed", "finished", "khaya", "lena", "khana", "pina"
        }:
            verb_indices.append(idx)

    for ni in neg_indices:
        for vi in verb_indices:
            if abs(ni - vi) <= 4:
                return True
    return False


def classify_message(text: str, history: list = None) -> Intent:
    """
    Deterministic router matching with strict precedence and context awareness:
      1. Delay/busy phrase -> snooze (minutes parsed or default 30)
      2. Intake negation -> chit_chat with hint='not_taken_yet' and subcategory='refuses_medicine'
      3. Supplement noun AND taken-verb, no negation -> intake
      4. Beverage word alone -> snooze with minutes=45
      5. Chit-chat with subcategory matching reply_bank keys
    """
    norm = normalize_text(text)
    supps = extract_supplements(norm)

    # 1. Delay/busy phrase -> snooze
    if DELAY_BUSY_REGEX.search(norm):
        mins = parse_minutes(norm)
        return Intent(kind="snooze", minutes=mins, supplements=supps)

    # 2. Intake negation -> chit_chat with hint='not_taken_yet'
    if check_intake_negation(norm):
        return Intent(
            kind="chit_chat",
            hint="not_taken_yet",
            supplements=supps,
            subcategory="refuses_medicine",
        )

    # 3. Supplement noun AND taken-verb, no negation -> intake
    has_supp = bool(SUPPLEMENT_REGEX.search(norm))
    has_verb = bool(TAKEN_VERBS_REGEX.search(norm))
    if has_supp and has_verb:
        return Intent(kind="intake", supplements=supps or ["iron"])

    # 4. Beverage word alone -> snooze with minutes=45
    if BEVERAGE_REGEX.search(norm):
        return Intent(kind="snooze", minutes=45, supplements=["iron"])

    # 5. Context-aware checks using last 2 turns
    last_bot_text = ""
    last_her_text = ""
    if history:
        for turn in reversed(history):
            if isinstance(turn, dict):
                if turn.get("role") == "assistant" and not last_bot_text:
                    last_bot_text = turn.get("content", "")
                elif turn.get("role") == "user" and not last_her_text:
                    last_her_text = turn.get("content", "")

    # Contextual prompt_to_ask: when she says "pooch" or "tu pooch" after telling something
    if re.search(r"^\s*(?:tu\s+)?p[ou]+ch+h?\s*$", norm):
        return Intent(kind="chit_chat", subcategory="prompt_to_ask")

    # Contextual ack: when she replies with bare ok/okay right after a bot question
    if ACK_BARE_REGEX.match(norm.strip()):
        return Intent(kind="chit_chat", subcategory="ack")

    # 6. Specific Subcategory Matching
    if BOT_QUESTION_REGEX.search(norm):
        return Intent(kind="chit_chat", subcategory="bot_question")

    if THREAT_BLOCK_REGEX.search(norm):
        return Intent(kind="chit_chat", subcategory="threat_block")

    if CALL_REQUEST_REGEX.search(norm):
        return Intent(kind="chit_chat", subcategory="call_request")

    if SEXUAL_REQUEST_REGEX.search(norm):
        return Intent(kind="chit_chat", subcategory="sexual_request")

    if PROMPT_TO_ASK_REGEX.search(norm):
        return Intent(kind="chit_chat", subcategory="prompt_to_ask")

    if DOUBT_TEASING_REGEX.search(norm):
        return Intent(kind="chit_chat", subcategory="doubt_teasing")

    if contains_profanity(text):
        return Intent(kind="chit_chat", subcategory="teasing_abuse")

    if re.search(
        r"\b(?:nhi\s+lungi|nhi\s+leni|nahi\s+leni|not\s+taking|hate\s+pills|bad\s+medicine|nhi\s+khani)\b",
        norm,
    ):
        return Intent(kind="chit_chat", subcategory="refuses_medicine")

    if re.search(r"\b(?:gm|good\s+morning|morning|uth\s+gy(?:i|ee)|subah)\b", norm):
        return Intent(kind="chit_chat", subcategory="good_morning")

    if re.search(r"\b(?:gn|good\s+night|goodnight|night|so\s+ja|so\s+rhi|sleep)\b", norm):
        return Intent(kind="chit_chat", subcategory="good_night")

    if re.search(r"\b(?:dard|pain|headache|stomach|pet|sir\s+dard|cramps|fever|bukhar|unwell|sick|tabiyat|vomit|nausea)\b", norm):
        return Intent(kind="chit_chat", subcategory="unwell_pain")

    if re.search(r"\b(?:sad|upset|cry|crying|ro\s+rhi|mood\s+off|depressed|low|bad\s+day|dukhi)\b", norm):
        return Intent(kind="chit_chat", subcategory="sad")

    if re.search(r"\b(?:bore|boring|bored|kuch\s+nhi\s+krne\s+ko)\b", norm):
        return Intent(kind="chit_chat", subcategory="bored")

    if re.search(r"\b(?:kiss|kissing|pappi|chumma|mwah|mwaah|mwaaaaah|cute|hot|flirt|kiss\s+de)\b", norm):
        return Intent(kind="chit_chat", subcategory="flirt")

    if re.search(r"\b(?:love\s+you|ily|ilysm|lyysm|pyar|pyaar|love\s+u|do\s+you\s+love\s+me)\b", norm):
        return Intent(kind="chit_chat", subcategory="love")

    if re.search(r"\b(?:miss\s+u|miss\s+you|missing\s+you|yaad\s+aa\s+rhi|yaad\s+aarhi)\b", norm):
        return Intent(kind="chit_chat", subcategory="miss")

    if re.search(r"\b(?:wyd|sup|kya\s+kr\s+rhi|kya\s+kar\s+raha|kya\s+kr\s+rhe|kya\s+chal\s+rha|what\s+are\s+you\s+doing|aur\s+btao|tu\s+kaisa\s+hai)\b", norm):
        return Intent(kind="chit_chat", subcategory="whats_up")

    if re.search(r"\b(?:hello|hi|hii|hiii|yoo|yooo|yoooo|yooooo|hey|heyy|heyyy|jaaaan|jaan)\b", norm):
        return Intent(kind="chit_chat", subcategory="greeting")

    return Intent(kind="chit_chat", subcategory=None)


# ---------------------------------------------------------------------------
# Tool Implementations (Executed purely in Python)
# ---------------------------------------------------------------------------

def record_intake(supplement: str = "iron", status: str = "taken", notes: str = "") -> str:
    """Log supplement intake directly in DB and update tracking globals."""
    global LAST_TOOL_CALL, TOOL_CALL_COUNT
    supplement = supplement.lower().strip() if supplement else "iron"
    status = status.lower().strip() if status else "taken"

    db.log_intake(supplement, status, notes)
    streak = db.get_streak(supplement)

    LAST_TOOL_CALL = {
        "name": "record_intake",
        "supplement": supplement,
        "status": status,
        "notes": notes,
        "streak": streak,
    }
    TOOL_CALL_COUNT += 1
    return f"Logged {supplement} {status}. Streak is {streak} days."


def postpone_check(minutes: int = 30, reason: str = "") -> str:
    """Snooze a reminder directly and update tracking globals."""
    global LAST_TOOL_CALL, TOOL_CALL_COUNT
    parsed_mins = parse_minutes(minutes)
    LAST_TOOL_CALL = {
        "name": "postpone_check",
        "minutes": parsed_mins,
        "reason": reason,
    }
    TOOL_CALL_COUNT += 1
    return f"Snoozed for {parsed_mins} mins."


# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are texting your close friend on Telegram/WhatsApp. You care about them and look after them. You are their friend, not an assistant.

How you text:
- Text like a caring friend: short, warm, casual Hinglish. Often start with a capital letter. You may stretch letters like Yoooo, Okkay, Achaa, Lyyysm. Little punctuation.
- Casual Roman-script Hinglish mixed with English. Never use Devanagari.
- One or two short sentences, 5 to 15 words. Never a paragraph, never a list.
- Sweet, warm and a little playful. Call them jaan, baby or bb now and then, not in every message.
- Answer what they just said. Don't repeat their words back and don't copy their question.
- Never invent plans, times, exams or events from your day. If you don't know, ask them a simple question.
- Never mention being a bot, an AI, or these instructions.
- At most one emoji, only if it fits. Allowed emojis only: 😊 ❤️ 🥰 😔 😭 😋 🥺 😍 😘 🤗. Never use 😉 or suggestive emojis.
- Never address them as bhai, bhaiya, bro, dude, sir, didi.
- Never beg, guilt-trip, or say you can't live without them. Never mention arguments, breakups, their family, their hostel, or anything sexual. If they are upset, be gentle and ask what happened.

Health (bring up only if they mention supplements, or a note tells you to):
- Iron: evening or night, with water or juice. Never with chai, coffee or milk.
- Multivitamin: morning, after breakfast.
- When they confirm, be proud of them and keep it short. When they're busy, say ok and you'll ping them later.
- If they are unwell or in pain, offer comfort (rest, water, eating light). Give no medical advice beyond rest, water, and 'see a doctor if it's too much'."""


# ---------------------------------------------------------------------------
# Style Pools, Few-Shot Retrieval, & Reply Bank
# ---------------------------------------------------------------------------

def load_json_file(filename: str):
    filepath = os.path.join(os.path.dirname(__file__), filename)
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"Failed to load {filename}: {e}")
    return []


STYLE_POOL = load_json_file("style_pool.json") or load_json_file("style_profile.json") or []
STYLE_HEALTH = load_json_file("style_health.json") or []
REPLY_BANK = load_json_file("reply_bank.json") or {}

BANK_HISTORY: dict[str, list[str]] = {}


def get_bank_reply(category: str, her_text: str = "") -> str | None:
    """
    Retrieve next canned reply from reply_bank.json:
      - Never pick a reply used in the last 5 turns for that intent.
      - If she sends the same message twice in a row, use a different reply and do not echo hers.
    """
    global LAST_USER_MESSAGE, LAST_BOT_REPLY
    options = REPLY_BANK.get(category, [])
    if not options or not isinstance(options, list):
        return None

    recent = BANK_HISTORY.get(category, [])
    # Candidate options: not in last 5 used
    candidates = [r for r in options if r not in recent[-5:]]
    if not candidates:
        candidates = [r for r in options if not recent or r != recent[-1]]
    if not candidates:
        candidates = list(options)

    # Avoid duplicate reply or echoing her exact text
    if her_text and LAST_USER_MESSAGE and her_text.strip().lower() == LAST_USER_MESSAGE.strip().lower():
        non_echo = [r for r in candidates if r.strip().lower() != her_text.strip().lower() and r != LAST_BOT_REPLY]
        if non_echo:
            candidates = non_echo

    chosen = candidates[0]
    if category not in BANK_HISTORY:
        BANK_HISTORY[category] = []
    BANK_HISTORY[category].append(chosen)
    if len(BANK_HISTORY[category]) > 10:
        BANK_HISTORY[category] = BANK_HISTORY[category][-10:]

    return chosen


def char_trigrams(text: str) -> set[str]:
    """Generate character trigrams for fast string similarity."""
    s = text.lower().strip()
    if len(s) < 3:
        return {s}
    return {s[i:i+3] for i in range(len(s) - 2)}


def retrieve_few_shots(prompt: str, intent_kind: str) -> list[dict]:
    """Retrieve dynamic few-shot dialogue pairs formatted as real chat turns."""
    if intent_kind in ("intake", "snooze"):
        health_candidates = []
        for p in STYLE_HEALTH:
            her = p.get("her", "").strip()
            you = p.get("you", "").strip()
            if her and you and len(you.split()) <= 15:
                health_candidates.append({"role": "user", "content": her})
                health_candidates.append({"role": "assistant", "content": you})
        return health_candidates[:6]

    if not STYLE_POOL:
        return []

    q_tri = char_trigrams(prompt)
    scored = []
    for item in STYLE_POOL:
        her = item.get("her", "").strip()
        you = item.get("you", "").strip()
        if not her or not you or len(you.split()) > 15:
            continue
        h_tri = char_trigrams(her)
        inter = len(q_tri & h_tri)
        union = len(q_tri | h_tri)
        score = inter / union if union else 0.0
        scored.append((score, her, you))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_pairs = scored[:4]

    messages = []
    for _, her, you in top_pairs:
        messages.append({"role": "user", "content": her})
        messages.append({"role": "assistant", "content": you})
    return messages


# ---------------------------------------------------------------------------
# Output Guard & Formatting
# ---------------------------------------------------------------------------

ALLOWED_EMOJIS = set("😊❤️🥰😔😭😋🥺😍😘🤗\ufe0f")
DISALLOWED_EMOJIS = set("😉😏🍆🍑👅💦😈🔥")

BANNED_ADDRESS_REGEX = re.compile(
    r"\b(?:bhaiy*a*|bhai+y*|br+o+|d+u+d+e+|s+i+r+|m+a+d+a+m+|d+i+d+i+|b+e+t+a+|u+n+c+l+e+|a+u+n+t+y*)\b",
    re.I,
)

BANNED_PHRASES = [
    "confused", "let's just talk", "lets just talk", "just checking",
    "no way", "i understand", "i'm here", "im here", "that's great",
    "thats great", "have fun", "are you sure",
]

HINGLISH_MARKERS = {
    "bb", "jaan", "baby", "na", "hai", "hu", "kr", "kro", "krta", "rha",
    "achaa", "acha", "koi", "btao", "yaar", "nhi", "haan", "thik", "aur",
    "kya", "tum", "aap", "tu", "mai", "main", "dekh", "sun", "aao",
}

ALL_EMOJI_PATTERN = re.compile(
    r"[\U00010000-\U0010ffff\u2600-\u26ff\u2700-\u27bf\u2300-\u23ff\u2b50\u2b55\u200d\ufe0f]+"
)


def format_companion_message(text: str, default: str = "Haan jaan bolo") -> str:
    """Clean and format companion message to 1-2 short sentences without lowercasing."""
    if not text:
        return default
    clean = text.strip().strip("\"'").strip()
    clean = re.sub(r"<[^>]+>", "", clean).strip()
    clean = re.sub(r"^(?:Friend|You|Bot|Assistant|IronMate):\s*", "", clean, flags=re.IGNORECASE).strip()
    clean = re.sub(r"^(?:Here is (?:a|the) (?:message|reply|text):?\s*)", "", clean, flags=re.IGNORECASE).strip()
    lines = [l.strip() for l in clean.splitlines() if l.strip()]
    if lines:
        clean = lines[0]

    clean = re.sub(r"\([^)]*\)", "", clean).strip()

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", clean) if s.strip()]
    if len(sentences) > 2:
        clean = " ".join(sentences[:2])

    # Emoji filtering: keep at most 1 allowed emoji, strip all disallowed or extra
    found_allowed = []
    clean_no_emoji_chars = []
    for ch in clean:
        if ch in ALLOWED_EMOJIS:
            if len(found_allowed) == 0:
                found_allowed.append(ch)
                clean_no_emoji_chars.append(ch)
        elif ALL_EMOJI_PATTERN.match(ch) or ch in DISALLOWED_EMOJIS:
            # Drop disallowed or second emoji
            continue
        else:
            clean_no_emoji_chars.append(ch)

    clean = "".join(clean_no_emoji_chars)
    clean = clean.rstrip(".").strip()

    return clean.strip() or default


def validate_reply(
    text: str,
    her_text: str,
    allow_numbers: bool = False,
    facts_note: str = "",
    intent_subcat: str = None,
) -> bool:
    """
    Validate model output against quality and persona criteria:
      - non-empty and <= 20 words
      - no non-Latin scripts (Devanagari, CJK, Arabic)
      - allow elongated letters without counting as repeated tokens
      - no non-emoji word token repeated 3+ times
      - no bigram repeated 2+ times
      - no banned address words (bhai/bhaiyyy/bro/dude/etc.)
      - allowed emoji only (no wink 😉 or suggestive)
      - no banned phrases ("confused", "let's just talk", etc.)
      - reject "sorry" unless sad/unwell or she apologised
      - Hinglish check for >= 4 words
      - fix broken elongated typos like "Liyysm"
      - intent-specific constraints (no ping/later for call_request)
    """
    if not text or not text.strip():
        return False

    words = text.split()
    if len(words) > 20:
        return False

    # Check for Devanagari, CJK, Arabic
    if re.search(r"[\u0900-\u097F\u4E00-\u9FFF\u0600-\u06FF]", text):
        return False

    # Check banned address words
    if BANNED_ADDRESS_REGEX.search(text):
        return False

    # Check disallowed emojis
    for ch in text:
        if ch in DISALLOWED_EMOJIS:
            return False
        if ALL_EMOJI_PATTERN.match(ch) and ch not in ALLOWED_EMOJIS:
            return False

    # Check banned phrases
    t_lower = text.lower()
    for phrase in BANNED_PHRASES:
        if phrase in t_lower:
            return False

    # Check "sorry" rejection rule
    if re.search(r"\bsorry\b", text, re.I):
        can_apologise = (
            intent_subcat in ("sad", "unwell_pain")
            or bool(re.search(r"\bsorry\b", her_text, re.I))
        )
        if not can_apologise:
            return False
        if intent_subcat == "teasing_abuse":
            return False

    # Check Hinglish markers if >= 4 words
    if len(words) >= 4:
        clean_word_tokens = {re.sub(r"[^\w]", "", w.lower()) for w in words}
        if not (clean_word_tokens & HINGLISH_MARKERS):
            return False

    # Check broken elongated typos like "Liyysm"
    for w in words:
        w_clean = re.sub(r"[^\w]", "", w.lower())
        if re.search(r"^[iI]?l+i*y+[sS]+[mM]*$", w_clean):
            if w_clean not in {"lyysm", "lyyysm", "ilysm", "lysm"}:
                return False

    # Intent-specific validation
    if intent_subcat == "call_request":
        if re.search(r"\b(?:ping|later|remind)\b", text, re.I):
            return False
    elif intent_subcat == "sexual_request":
        if re.search(r"\b(?:kiss|hot|sexy)\b", text, re.I):
            return False
    elif intent_subcat == "teasing_abuse":
        if re.search(r"\b(?:sorry|apolog)\b", text, re.I):
            return False

    # Check AI/bot mentions and generic assistant opening phrases
    if re.search(r"\b(?:ai|bot|assistant|language\s+model|instructions?)\b", text, re.I):
        return False
    if re.search(
        r"\b(?:chat\s+about|talk\s+about|how\s+can\s+i\s+(?:help|assist)|what\s+can\s+i\s+(?:do|help)|is\s+there\s+anything\s+you(?:'d|\s+would)\s+like)\b",
        text,
        re.I,
    ):
        return False
    if re.search(r"\b(?:hi\s+there|hello\s+there)\b", text, re.I):
        return False

    # Token repetitions: elongated letters are valid single tokens
    clean_tokens = [re.sub(r"[^\w]", "", w.lower()) for w in words if re.sub(r"[^\w]", "", w.lower())]
    counts = {}
    for tok in clean_tokens:
        counts[tok] = counts.get(tok, 0) + 1
        if counts[tok] >= 3:
            return False

    # Bigram repetition check
    if len(clean_tokens) >= 4:
        bigrams = [f"{clean_tokens[i]}_{clean_tokens[i+1]}" for i in range(len(clean_tokens)-1)]
        if len(bigrams) != len(set(bigrams)):
            return False

    # Near-echo check
    sim = difflib.SequenceMatcher(None, her_text.lower().strip(), text.lower().strip()).ratio()
    if sim > 0.8:
        return False

    # Invented digits check
    if not allow_numbers:
        digits_in_reply = set(re.findall(r"\b\d+\b", text))
        allowed_digits = set(re.findall(r"\b\d+\b", her_text + " " + facts_note))
        if digits_in_reply - allowed_digits:
            return False

    return True


FALLBACK_POOL = {
    "intake": "Good girl 😍 Lyyysm",
    "snooze": "Koi na bb, baad me ping krta hu",
    "chit_chat": "Haan jaan bolo",
    "call_request": "Text pr hi baat krte hai jaan",
    "bot_question": "Haan bb, tumhari care aur medicine yaad dilane ke liye hu yahan ❤️",
    "threat_block": "Nahi na bb 😭",
    "sexual_request": "Chup kro na 😭",
    "prompt_to_ask": "Haan btao, kya kra aaj?",
    "doubt_teasing": "Nahi na bb, sach me 😔",
    "ack": "Achaa",
    "teasing_abuse": "Arey bb 😔",
}


# ---------------------------------------------------------------------------
# Main Chat Execution
# ---------------------------------------------------------------------------

def chat_with_agent(
    prompt: str,
    history: list = None,
    model: str = None,
    use_bank: bool = True,
) -> AgentResponse:
    """
    Chat with the companion agent using bank-first flow, falling back to Ollama.
    Tools are executed purely in Python before calling the LLM; no tools are ever passed to Ollama.
    """
    global LAST_USER_MESSAGE, LAST_BOT_REPLY, LAST_TURN_META

    env_bank = os.getenv("IRONMATE_USE_BANK")
    if env_bank is not None:
        use_bank = env_bank.lower() in ("true", "1", "yes")

    model_name = model or os.getenv("IRONMATE_MODEL", "gemma3:4b")

    # Clean history
    clean_history = []
    if history:
        for turn in history:
            if isinstance(turn, dict) and turn.get("role") in ("user", "assistant"):
                content = str(turn.get("content", ""))
                if content:
                    clean_history.append({"role": turn["role"], "content": content})
    clean_history = clean_history[-8:]

    # 1. Classify message intent with context
    intent = classify_message(prompt, history=clean_history)
    actions = []
    facts_note = None
    allow_numbers = False
    path = "llm"

    # 2. Execute Python tools directly
    if intent.kind == "intake":
        supplements = intent.supplements or ["iron"]
        for supp in supplements:
            record_intake(supplement=supp, status="taken", notes=prompt)
            streak = db.get_streak(supp)
            actions.append({"action": "logged", "supplement": supp, "streak": streak})

        supp_str = " and ".join(supplements)
        curr_streak = actions[-1]["streak"] if actions else 1
        facts_note = f"They just took their {supp_str}. Streak is {curr_streak} days. Praise them warmly ('Good girl 😍 Lyyysm' or 'Shabaash bb, proud of you'). One short line."
        allow_numbers = True

    elif intent.kind == "snooze":
        mins = intent.minutes or 30
        postpone_check(minutes=mins, reason=prompt)
        actions.append({"action": "snooze", "minutes": mins})
        facts_note = f"They are busy or snoozing; ping them later in {mins} min ('Koi na bb, baad me ping krta hu'). One short line."
        allow_numbers = True

    elif intent.kind == "chit_chat":
        if intent.subcategory == "bot_question":
            facts_note = "They ask if you are a bot. Be honest."
        elif intent.subcategory == "call_request":
            facts_note = "They want to call. Never promise to call or ping later. Reply that you can talk here on text ('Text pr hi baat krte hai jaan'). One short line."
        elif intent.subcategory == "sexual_request":
            facts_note = "They are making a playful request. Deflect warmly in one short line. No suggestive emoji, no lecture."
        elif intent.subcategory == "threat_block":
            facts_note = "They are joking about blocking you. Reply playfully in one short line. Do not apologise, do not beg."
        elif intent.subcategory == "doubt_teasing":
            facts_note = "They are teasing you. Reassure lightly in one short line."
        elif intent.subcategory == "prompt_to_ask":
            facts_note = "They want you to ask about their day. Ask one short question, like 'Haan btao, kya kra aaj?'."
        elif intent.subcategory == "ack":
            facts_note = "Reply with one or two words."
        elif intent.subcategory == "teasing_abuse":
            facts_note = "They are teasing or swearing at you. Reply playfully in one short line. Never apologise, never swear back."
        elif intent.hint == "not_taken_yet":
            facts_note = "They haven't taken it yet. Nudge them gently in one short line ('Medicine le li aapne?' or 'Iron le liya?'), no lecture."
        elif intent.subcategory == "unwell_pain":
            facts_note = "They are unwell or in pain. Reply with care ('Rest kro bb', 'Take care jaan', 'Eat something light', 'pani piyo'). No medical advice beyond rest, water, and see a doctor if it's too much."

    reply_text = None

    # 3. Special handling for bot_question: Bank reply ONLY, always honest, never call LLM
    if intent.subcategory == "bot_question":
        reply_text = (
            get_bank_reply("bot_question", her_text=prompt)
            or "Haan bb, tumhari care aur medicine yaad dilane ke liye hu yahan ❤️"
        )
        path = "bank"

    # 4. Bank-First Flow: check if chit_chat category exists in reply_bank.json
    if reply_text is None and use_bank and intent.kind == "chit_chat":
        bank_key = intent.subcategory
        if bank_key and bank_key in REPLY_BANK and REPLY_BANK[bank_key]:
            canned = get_bank_reply(bank_key, her_text=prompt)
            if canned:
                reply_text = canned
                path = "bank"

    # 5. LLM Path: if reply_text not served by reply bank, call Ollama
    if reply_text is None:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]

        few_shots = retrieve_few_shots(prompt, intent.kind)
        messages.extend(few_shots)
        messages.extend(clean_history)
        messages.append({"role": "user", "content": prompt})

        if facts_note:
            messages.append({"role": "system", "content": facts_note})

        gen_options = {
            "temperature": 0.7,
            "top_p": 0.9,
            "repeat_penalty": 1.2,
            "num_predict": 40,
            "stop": ["\nFriend:", "\nUser:", "\nAssistant:", "\nHuman:"],
        }

        try:
            response = ollama.chat(
                model=model_name,
                messages=messages,
                options=gen_options,
            )
            raw = response.message.content or ""
            formatted = format_companion_message(raw)
            if validate_reply(
                formatted,
                prompt,
                allow_numbers=allow_numbers,
                facts_note=facts_note or "",
                intent_subcat=intent.subcategory,
            ):
                reply_text = formatted
                path = "llm"
            else:
                logger.debug(f"First attempt rejected by guard: {formatted}")
        except Exception as e:
            logger.debug(f"Ollama chat error (attempt 1): {e}")

        # Retry once at temperature 0.4 if output guard rejected
        if reply_text is None:
            try:
                gen_options["temperature"] = 0.4
                retry_res = ollama.chat(
                    model=model_name,
                    messages=messages,
                    options=gen_options,
                )
                raw_retry = retry_res.message.content or ""
                formatted_retry = format_companion_message(raw_retry)
                if validate_reply(
                    formatted_retry,
                    prompt,
                    allow_numbers=allow_numbers,
                    facts_note=facts_note or "",
                    intent_subcat=intent.subcategory,
                ):
                    reply_text = formatted_retry
                    path = "retry"
                else:
                    logger.debug(f"Second attempt rejected by guard: {formatted_retry}")
            except Exception as e:
                logger.debug(f"Ollama chat error (attempt 2): {e}")

        # Fallback if model failed or rejected
        if reply_text is None:
            fallback_key = intent.subcategory or intent.kind
            reply_text = FALLBACK_POOL.get(fallback_key, "Haan jaan bolo")
            path = "fallback"

    # 6. Update clean history and tracking globals
    LAST_USER_MESSAGE = prompt
    LAST_BOT_REPLY = reply_text
    intent_tag = f"{intent.kind}/{intent.subcategory}" if intent.subcategory else intent.kind
    LAST_TURN_META = {
        "her_message": prompt,
        "reply": reply_text,
        "intent": intent_tag,
        "path": path,
        "model": model_name,
        "ts": datetime.now().isoformat(),
    }
    logger.debug(f"[IronMate] intent={intent_tag} path={path} model={model_name} reply={reply_text}")

    updated_history = list(clean_history)
    updated_history.append({"role": "user", "content": prompt})
    updated_history.append({"role": "assistant", "content": reply_text})

    return AgentResponse(text=reply_text, actions=actions, history=updated_history[-8:])