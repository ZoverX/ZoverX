from __future__ import annotations
"""
Educational version of the League of Legends stats SVG generator.

What this script does:
1. Load Riot API settings from environment variables or local .env files.
2. Call Riot's APIs to find the player's account and latest match.
3. Call Data Dragon to fetch champion artwork, lore, and abilities.
4. Convert that data into a single SVG card.
5. Always write a valid SVG, even when something goes wrong.

Notes for learning:
- This file is intentionally organized into small functions. Each function does one job.
- Most functions either:
  - fetch data,
  - normalize data into a simpler shape, or
  - render part of the SVG.
- The goal is to keep "API logic" separate from "display logic".

About ``from __future__ import annotations``:
- This lets us use modern type hints more easily.
- For example, we can write ``dict[str, Any]`` and ``int | None`` naturally.
- It does not change the main program behavior; it mainly improves typing ergonomics.
"""

import base64
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from textwrap import shorten
from typing import Any
from urllib.parse import quote

import requests


OUTPUT_PATH = Path("assets/lol-stats.svg")
REQUEST_TIMEOUT = 10
CARD_WIDTH = 900
CARD_HEIGHT = 1080
DATA_DRAGON_LANG = "en_US"

QUEUE_NAME_MAP = {
    400: "Normal Draft",
    420: "Ranked Solo/Duo",
    430: "Normal Blind",
    440: "Ranked Flex",
    450: "ARAM",
    700: "Clash",
    1700: "Arena",
    1710: "Arena",
}


class RiotApiError(Exception):
    """Raised when Riot API data cannot be fetched cleanly."""

    def __init__(self, message: str, *, retryable: bool = False, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


# ``@dataclass`` is a decorator.
# In general, a decorator is something that modifies or extends a function/class.
# You can think of it as "wrapping extra behavior around a definition".
#
# Here, ``@dataclass`` tells Python:
# "Please generate useful boilerplate for this class automatically."
#
# Without ``@dataclass``, we would usually write:
# - an ``__init__`` method,
# - a readable ``__repr__`` for debugging,
# - and sometimes equality helpers.
#
# In this specific case, ``Config`` is just a container for settings.
# ``@dataclass`` is perfect for that because the class mainly stores values.
@dataclass
class Config:
    """Typed container for all required runtime configuration."""

    api_key: str
    region: str
    platform: str
    game_name: str
    tag_line: str


def load_dotenv_file(dotenv_path: Path) -> None:
    """
    Load simple KEY=VALUE pairs from a local file into ``os.environ``.

    Why do this:
    - It lets us keep secrets in a local file instead of hardcoding them.
    - We avoid needing an extra package like ``python-dotenv``.

    Important behavior:
    - Blank lines and comments are ignored.
    - Existing environment variables win over the file.
      That means GitHub Actions secrets or shell-set values take priority.
    """
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_dotenv() -> None:
    """
    Load local environment files in a small, predictable order.

    Why two files:
    - ``.env`` is a common standard name.
    - ``.env.riot`` is a project-specific alternative if you want to separate secrets.
    """
    load_dotenv_file(Path(".env"))
    load_dotenv_file(Path(".env.riot"))


def load_config() -> Config:
    """
    Read the required settings from environment variables.

    Why return a ``Config`` object instead of a raw dict:
    - attribute access is clearer: ``config.region`` vs ``values["RIOT_REGION"]``
    - the rest of the code gets a predictable structure
    - type hints become easier to understand
    """
    values = {
        "RIOT_API_KEY": os.getenv("RIOT_API_KEY", "").strip(),
        "RIOT_REGION": os.getenv("RIOT_REGION", "").strip(),
        "RIOT_PLATFORM": os.getenv("RIOT_PLATFORM", "").strip(),
        "RIOT_GAME_NAME": os.getenv("RIOT_GAME_NAME", "").strip(),
        "RIOT_TAG_LINE": os.getenv("RIOT_TAG_LINE", "").strip(),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError("Missing required environment variables: " + ", ".join(missing))
    return Config(
        api_key=values["RIOT_API_KEY"],
        region=values["RIOT_REGION"],
        platform=values["RIOT_PLATFORM"],
        game_name=values["RIOT_GAME_NAME"],
        tag_line=values["RIOT_TAG_LINE"],
    )


def describe_payload(payload: Any) -> str:
    """Return a short human-readable summary of an API payload for error messages."""
    if isinstance(payload, dict):
        keys = ", ".join(sorted(payload.keys())[:8]) or "no keys"
        return f"dict with keys: {keys}"
    if isinstance(payload, list):
        return f"list with {len(payload)} item(s)"
    return type(payload).__name__


def require_value(payload: dict[str, Any] | None, field_names: tuple[str, ...], label: str) -> Any:
    """
    Pull a required field out of a JSON object.

    Why this helper exists:
    - APIs sometimes change shape or omit fields.
    - If we fail, we want a useful error message, not a vague ``KeyError``.
    """
    if not isinstance(payload, dict):
        raise RiotApiError(f"Unexpected Riot API response for {label}: {describe_payload(payload)}.")

    for field_name in field_names:
        value = payload.get(field_name)
        if value not in (None, ""):
            return value

    fields = ", ".join(field_names)
    raise RiotApiError(
        f"Riot API response for {label} did not include {fields}. Received {describe_payload(payload)}."
    )


def riot_get(url: str, api_key: str, params: dict[str, Any] | None = None) -> Any:
    """
    Perform a GET request to Riot's API and convert common failures into ``RiotApiError``.

    General idea:
    - ``requests.get(...)`` sends the HTTP request.
    - ``headers`` includes the Riot API key.
    - ``params`` is used for query-string values like ``count=5``.

    Why wrap this in one function:
    - We avoid repeating the same request/error logic everywhere.
    - The rest of the program can focus on data flow rather than HTTP details.
    """
    headers = {"X-Riot-Token": api_key}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise RiotApiError("Network error while contacting Riot API.", retryable=True) from exc

    if response.status_code == 404:
        return None
    if response.status_code == 429:
        raise RiotApiError("Riot API rate limit reached.", retryable=True, status_code=429)
    if response.status_code in {401, 403}:
        raise RiotApiError("Riot API key rejected.", status_code=response.status_code)
    if 500 <= response.status_code <= 599:
        raise RiotApiError("Riot API temporary server error.", retryable=True, status_code=response.status_code)
    if not response.ok:
        raise RiotApiError(
            f"Riot API request failed with status {response.status_code}.",
            status_code=response.status_code,
        )

    try:
        return response.json()
    except ValueError as exc:
        raise RiotApiError("Riot API returned invalid JSON.", retryable=True) from exc


def fetch_account(config: Config) -> dict[str, Any]:
    """Look up a Riot account from Riot ID (game name + tag line)."""
    game_name = quote(config.game_name, safe="")
    tag_line = quote(config.tag_line, safe="")
    url = f"https://{config.region}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
    account = riot_get(url, config.api_key)
    if not account:
        raise RiotApiError("Riot account not found.")
    return account


def fetch_summoner(config: Config, puuid: str) -> dict[str, Any]:
    """Fetch League-specific summoner data using the account's PUUID."""
    encoded_puuid = quote(puuid, safe="")
    url = f"https://{config.platform}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{encoded_puuid}"
    summoner = riot_get(url, config.api_key)
    if not summoner:
        raise RiotApiError("Summoner profile not found for the resolved account.")
    return summoner


def extract_puuid(account: dict[str, Any]) -> str:
    """Read the ``puuid`` field from the Riot account response."""
    return str(require_value(account, ("puuid",), "account lookup"))


def extract_summoner_level(summoner: dict[str, Any]) -> int | None:
    """
    Convert ``summonerLevel`` into an integer if possible.

    We return ``None`` instead of crashing if the field is missing or malformed.
    """
    value = summoner.get("summonerLevel")
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_data_dragon_version() -> str:
    """Fetch the latest available Data Dragon version string."""
    versions = fetch_json("https://ddragon.leagueoflegends.com/api/versions.json")
    if not isinstance(versions, list) or not versions:
        raise RiotApiError("Data Dragon version list was unavailable.", retryable=True)
    return str(versions[0])


def fetch_json(url: str) -> Any:
    """Download and decode JSON from non-Riot endpoints like Data Dragon."""
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise RiotApiError("Network error while contacting Data Dragon.", retryable=True) from exc

    if not response.ok:
        raise RiotApiError(f"Data Dragon request failed with status {response.status_code}.", retryable=True)

    try:
        return response.json()
    except ValueError as exc:
        raise RiotApiError("Data Dragon returned invalid JSON.", retryable=True) from exc


def fetch_binary(url: str) -> bytes:
    """Download raw bytes, used here for champion icon images."""
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise RiotApiError("Network error while downloading champion artwork.", retryable=True) from exc

    if not response.ok:
        raise RiotApiError(f"Champion artwork request failed with status {response.status_code}.", retryable=True)
    return response.content


def strip_html(text: str | None) -> str:
    """Remove simple HTML tags from Data Dragon descriptions so they render as plain text in SVG."""
    if not text:
        return ""
    clean = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", "", clean)
    return re.sub(r"\s+", " ", clean).strip()


def fetch_champion_profile(champion_name: str) -> dict[str, Any]:
    """
    Build a small champion profile from Data Dragon.

    Data Dragon is Riot's static content service.
    We use it for:
    - champion icon,
    - lore,
    - passive,
    - Q/W/E/R descriptions.

    The returned dict is intentionally smaller than the raw API response.
    This is a common pattern called "normalization":
    we keep only the fields the UI actually needs.
    """
    version = fetch_data_dragon_version()
    champion_catalog_url = (
        f"https://ddragon.leagueoflegends.com/cdn/{version}/data/{DATA_DRAGON_LANG}/champion.json"
    )
    catalog = fetch_json(champion_catalog_url)
    data = catalog.get("data") if isinstance(catalog, dict) else None
    if not isinstance(data, dict):
        raise RiotApiError("Data Dragon champion catalog was incomplete.", retryable=True)

    champion_key = None
    normalized_name = champion_name.lower().replace(" ", "").replace("'", "")
    for key, champion in data.items():
        if not isinstance(champion, dict):
            continue
        names_to_match = {
            str(key).lower().replace(" ", "").replace("'", ""),
            str(champion.get("id", "")).lower().replace(" ", "").replace("'", ""),
            str(champion.get("name", "")).lower().replace(" ", "").replace("'", ""),
        }
        if normalized_name in names_to_match:
            champion_key = str(champion.get("id") or key)
            break

    if not champion_key:
        raise RiotApiError(f"Could not find Data Dragon profile for champion {champion_name}.")

    detail_url = (
        f"https://ddragon.leagueoflegends.com/cdn/{version}/data/{DATA_DRAGON_LANG}/champion/{champion_key}.json"
    )
    detail_payload = fetch_json(detail_url)
    detail_root = detail_payload.get("data", {}) if isinstance(detail_payload, dict) else {}
    champion = detail_root.get(champion_key)
    if not isinstance(champion, dict):
        raise RiotApiError(f"Data Dragon champion detail was incomplete for {champion_key}.", retryable=True)

    image_url = f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{champion_key}.png"
    image_data = base64.b64encode(fetch_binary(image_url)).decode("ascii")
    passive = champion.get("passive") or {}
    spells = champion.get("spells") or []

    ability_rows = []
    passive_name = strip_html(passive.get("name")) or "Passive"
    passive_description = strip_html(passive.get("description")) or "No passive description available."
    ability_rows.append({"slot": "P", "name": passive_name, "description": passive_description})

    for slot, spell in zip(("Q", "W", "E", "R"), spells):
        if not isinstance(spell, dict):
            continue
        ability_rows.append(
            {
                "slot": slot,
                "name": strip_html(spell.get("name")) or f"{slot} Ability",
                "description": strip_html(spell.get("description")) or "No ability description available.",
            }
        )

    return {
        "version": version,
        "id": champion_key,
        "name": champion.get("name") or champion_key,
        "title": champion.get("title") or "",
        "lore": strip_html(champion.get("lore")) or "Lore unavailable.",
        "abilities": ability_rows,
        "icon_data_uri": f"data:image/png;base64,{image_data}",
    }


def fetch_latest_match(config: Config, puuid: str) -> dict[str, Any] | None:
    """
    Fetch recent match ids, then fetch match details.

    Why request 5 ids instead of 1:
    - sometimes the newest match can fail or have incomplete data
    - trying a few makes the script more robust
    """
    encoded_puuid = quote(puuid, safe="")
    list_url = f"https://{config.region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{encoded_puuid}/ids"
    match_ids = riot_get(list_url, config.api_key, params={"start": 0, "count": 5}) or []
    if not match_ids:
        return None

    last_error: RiotApiError | None = None
    for raw_match_id in match_ids:
        match_id = quote(str(raw_match_id), safe="")
        detail_url = f"https://{config.region}.api.riotgames.com/lol/match/v5/matches/{match_id}"
        try:
            match_data = riot_get(detail_url, config.api_key)
        except RiotApiError as exc:
            last_error = exc
            continue
        if isinstance(match_data, dict):
            return match_data

    if last_error:
        raise last_error
    return None


def format_timestamp(timestamp_ms: int | None) -> str | None:
    """Convert Riot millisecond timestamps into a readable UTC string."""
    if not timestamp_ms:
        return None
    moment = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def format_duration(duration_seconds: int | None) -> str | None:
    """Convert a match length in seconds into ``Xm YYs`` or ``Xh Ym`` form."""
    if not duration_seconds:
        return None
    minutes, seconds = divmod(int(duration_seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds:02d}s"


def queue_label_from_match(info: dict[str, Any]) -> str:
    """
    Turn Riot's queue/mode fields into a friendlier label.

    Why this logic is a bit defensive:
    - Riot can expose queue information in several places
    - some rotating modes are not always labeled consistently
    """
    queue_id = info.get("queueId")
    if queue_id in QUEUE_NAME_MAP:
        return QUEUE_NAME_MAP[queue_id]
    game_mode = str(info.get("gameMode") or "").upper()
    game_type = str(info.get("gameType") or "").upper()
    queue_name = str(info.get("queueName") or "").upper()
    map_id = info.get("mapId")

    aram_markers = ("ARAM", "HA", "HOWLING", "BRIDGE", "KOESHIN", "MAYHEM")
    if (
        game_mode == "ARAM"
        or game_type == "ARAM"
        or map_id in {12, 14}
        or any(marker in game_mode for marker in aram_markers)
        or any(marker in game_type for marker in aram_markers)
        or any(marker in queue_name for marker in aram_markers)
    ):
        if "MAYHEM" in game_mode or "MAYHEM" in game_type or "MAYHEM" in queue_name:
            return "ARAM: Mayhem"
        return "ARAM"
    if "ARENA" in game_mode or "ARENA" in game_type:
        return "Arena"

    return info.get("gameMode") or info.get("gameType") or "Unknown Queue"


def normalize_latest_match(match_data: dict[str, Any] | None, puuid: str) -> dict[str, Any]:
    """
    Convert the raw match payload into a much smaller display-friendly shape.

    "Normalize" means:
    - choose the fields we care about
    - rename them into simpler keys
    - convert numbers/timestamps into a format the renderer can use directly
    """
    if not match_data:
        return {
            "queue_or_mode": "No recent match",
            "champion": "-",
            "kills": 0,
            "deaths": 0,
            "assists": 0,
            "result": "Unavailable",
            "timestamp": None,
            "duration": None,
        }

    info = match_data.get("info") or {}
    participants = info.get("participants") or []
    participant = next((item for item in participants if item.get("puuid") == puuid), None)
    if not participant:
        return {
            "queue_or_mode": queue_label_from_match(info),
            "champion": "-",
            "kills": 0,
            "deaths": 0,
            "assists": 0,
            "result": "Unavailable",
            "timestamp": format_timestamp(info.get("gameEndTimestamp") or info.get("gameCreation")),
            "duration": format_duration(info.get("gameDuration")),
        }

    result = "Win" if participant.get("win") else "Loss"
    return {
        "queue_or_mode": queue_label_from_match(info),
        "champion": participant.get("championName") or "-",
        "kills": int(participant.get("kills", 0)),
        "deaths": int(participant.get("deaths", 0)),
        "assists": int(participant.get("assists", 0)),
        "result": result,
        "timestamp": format_timestamp(info.get("gameEndTimestamp") or info.get("gameCreation")),
        "duration": format_duration(info.get("gameDuration")),
    }


def safe_text(value: Any, *, width: int = 24) -> str:
    """
    Prepare user/API text for SVG output.

    Two jobs happen here:
    1. ``shorten(...)`` trims text so long strings do not break the layout.
    2. ``escape(...)`` prevents characters like ``<`` and ``&`` from breaking SVG/XML.
    """
    return escape(shorten(str(value), width=width, placeholder="..."))


def wrap_text(text: str, line_length: int) -> list[str]:
    """Wrap plain text into multiple lines based on an approximate character limit."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= line_length:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def render_multiline_text(
    x: int,
    y: int,
    lines: list[str],
    class_name: str,
    line_height: int,
    width: int,
) -> str:
    """
    Render multiple SVG ``<text>`` lines.

    ``line_height`` is the vertical gap between each line.
    This is similar to line spacing in normal text layout.
    """
    elements = []
    for index, line in enumerate(lines):
        escaped = safe_text(line, width=width)
        elements.append(f'<text x="{x}" y="{y + (index * line_height)}" class="{class_name}">{escaped}</text>')
    return "\n".join(elements)


def render_last_game_card(x: int, y: int, width: int, height: int, match: dict[str, Any]) -> str:
    """Render the top card that shows the latest match summary."""
    result_class = "success" if match["result"] == "Win" else "danger"
    if match["result"] not in {"Win", "Loss"}:
        result_class = "muted"
    meta_parts = [part for part in [match.get("duration"), match.get("timestamp")] if part]
    meta_line = "  •  ".join(meta_parts) if meta_parts else "Recent match details unavailable"
    kda_line = f'{match["kills"]} / {match["deaths"]} / {match["assists"]}'

    return f"""
    <g transform="translate({x},{y})">
      <rect width="{width}" height="{height}" rx="30" fill="url(#panelGradient)" stroke="#ffffff" stroke-opacity="0.10" />
      <text x="32" y="40" class="label">Last Game</text>
      <text x="32" y="86" class="title">{safe_text(match["queue_or_mode"], width=34)}</text>
      <text x="32" y="120" class="muted">{safe_text(meta_line, width=60)}</text>
      <text x="32" y="150" class="label">Champion Played</text>
      <text x="32" y="194" class="champion">{safe_text(match["champion"], width=20)}</text>
      <text x="330" y="150" class="label">K / D / A</text>
      <text x="330" y="194" class="kda">{safe_text(kda_line, width=18)}</text>
      <text x="676" y="150" class="label">Result</text>
      <text x="676" y="194" class="{result_class}">{safe_text('WIN' if match['result'] == 'Win' else 'LOSS' if match['result'] == 'Loss' else match['result'], width=12)}</text>
    </g>
    """


def render_svg(
    summoner_name: str,
    account_level: int | None,
    latest_match: dict[str, Any],
    champion_profile: dict[str, Any] | None,
    status_message: str | None = None,
) -> str:
    """
    Build the full SVG document as one string.

    Why string-building is okay here:
    - SVG is text-based XML
    - the layout is fixed enough that a template-style approach is simple
    - keeping rendering in one place makes design tweaks easier
    """
    status_line = status_message or "Live data from Riot API"
    level_text = f"Level {account_level}" if account_level is not None else "Level unavailable"
    lore_lines = wrap_text(
        champion_profile["lore"] if champion_profile else "Champion lore is unavailable right now.",
        100,
    )[:5]
    ability_rows = (champion_profile or {}).get("abilities") or [
        {"slot": "P", "name": "Abilities unavailable", "description": "Data Dragon could not be reached."}
    ]
    ability_svg_parts = []
    card_width = 148
    gap = 8
    row_y = 0
    col_x = 0
    for ability in ability_rows[:5]:
        description = wrap_text(ability["description"], 15)[:5]
        ability_svg_parts.append(
            f"""
    <g transform="translate({col_x},{row_y})">
      <rect width="{card_width}" height="172" rx="18" fill="#132136" stroke="#ffffff" stroke-opacity="0.06" />
      <circle cx="24" cy="28" r="14" fill="#214066" />
      <text x="24" y="33" text-anchor="middle" class="slot">{safe_text(ability["slot"], width=4)}</text>
      <text x="48" y="33" class="abilityName">{safe_text(ability["name"], width=14)}</text>
      {render_multiline_text(16, 64, description, "abilityDesc", 18, 16)}
    </g>
            """
        )
        col_x += card_width + gap
    ability_svg = "".join(ability_svg_parts)

    champion_name = champion_profile["name"] if champion_profile else latest_match["champion"]
    champion_title = champion_profile["title"] if champion_profile else ""
    champion_subtitle = f"{champion_name}, {champion_title}" if champion_title else champion_name
    icon_svg = (
        f'<image x="64" y="510" width="164" height="164" href="{champion_profile["icon_data_uri"]}" preserveAspectRatio="xMidYMid slice" clip-path="url(#iconClip)" />'
        if champion_profile and champion_profile.get("icon_data_uri")
        else '<rect x="64" y="510" width="164" height="164" rx="28" fill="#1b2c46" />'
    )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{CARD_WIDTH}" height="{CARD_HEIGHT}" viewBox="0 0 {CARD_WIDTH} {CARD_HEIGHT}" role="img" aria-labelledby="title desc">
  <title id="title">League of Legends stats for {safe_text(summoner_name, width=40)}</title>
  <desc id="desc">League profile card with account level, latest match, and champion details generated from Riot API and Data Dragon.</desc>
  <defs>
    <linearGradient id="bgGradient" x1="0%" x2="100%" y1="0%" y2="100%">
      <stop offset="0%" stop-color="#09131f" />
      <stop offset="55%" stop-color="#111f33" />
      <stop offset="100%" stop-color="#060d16" />
    </linearGradient>
    <linearGradient id="panelGradient" x1="0%" x2="100%" y1="0%" y2="100%">
      <stop offset="0%" stop-color="#16263d" />
      <stop offset="100%" stop-color="#0d1728" />
    </linearGradient>
    <clipPath id="iconClip">
      <rect x="64" y="510" width="164" height="164" rx="28" />
    </clipPath>
    <style>
      text {{
        font-family: Georgia, 'Trebuchet MS', 'Segoe UI', sans-serif;
        fill: #f4f7fb;
      }}
      .eyebrow {{ font-size: 14px; letter-spacing: 0.26em; text-transform: uppercase; fill: #87a0c8; }}
      .heading {{ font-size: 36px; font-weight: 700; }}
      .subtle {{ font-size: 16px; fill: #b8c5dd; }}
      .label {{ font-size: 14px; letter-spacing: 0.14em; text-transform: uppercase; fill: #8fa7cf; }}
      .title {{ font-size: 32px; font-weight: 700; }}
      .champion {{ font-size: 40px; font-weight: 700; }}
      .body {{ font-size: 20px; fill: #d7e0ef; }}
      .kda {{ font-size: 36px; font-weight: 700; }}
      .muted {{ font-size: 16px; fill: #9eb0ce; }}
      .success {{ font-size: 28px; font-weight: 700; fill: #79e5a4; }}
      .danger {{ font-size: 28px; font-weight: 700; fill: #ff8f8f; }}
      .section {{ font-size: 24px; font-weight: 700; }}
      .lore {{ font-size: 17px; fill: #d3dded; }}
      .abilityName {{ font-size: 18px; font-weight: 700; }}
      .abilityDesc {{ font-size: 14px; fill: #b8c5dd; }}
      .slot {{ font-size: 15px; font-weight: 700; }}
    </style>
  </defs>

  <rect width="{CARD_WIDTH}" height="{CARD_HEIGHT}" rx="32" fill="url(#bgGradient)" />
  <circle cx="760" cy="94" r="138" fill="#1f3553" opacity="0.26" />
  <circle cx="120" cy="160" r="94" fill="#17304f" opacity="0.24" />
  <circle cx="830" cy="980" r="180" fill="#10223c" opacity="0.24" />

  <text x="40" y="54" class="eyebrow">League of Legends</text>
  <text x="40" y="98" class="heading">{safe_text(summoner_name, width=42)}</text>
  <text x="40" y="128" class="subtle">{safe_text(level_text, width=30)}</text>
  <text x="40" y="154" class="subtle">{safe_text(status_line, width=80)}</text>

  {render_last_game_card(40, 196, 820, 240, latest_match)}

  <g transform="translate(0,0)">
    <rect x="40" y="470" width="820" height="570" rx="30" fill="url(#panelGradient)" stroke="#ffffff" stroke-opacity="0.10" />
    {icon_svg}
    <text x="258" y="548" class="section">{safe_text(champion_subtitle, width=44)}</text>
    <text x="258" y="580" class="label">Lore</text>
    {render_multiline_text(258, 610, lore_lines, "lore", 24, 72)}
    <text x="64" y="720" class="label">Abilities</text>
    <g transform="translate(64,744)">
      {ability_svg}
    </g>
  </g>
</svg>
"""


def build_fallback_payload(title: str, reason: str) -> tuple[int | None, dict[str, Any], dict[str, Any] | None, str]:
    """
    Create safe placeholder data for failure cases.

    This is important because the README should still show a valid image even when:
    - env vars are missing
    - Riot is rate-limited
    - Riot/Data Dragon is temporarily unavailable
    """
    latest_match = {
        "queue_or_mode": "Last Game",
        "champion": "Data unavailable",
        "kills": 0,
        "deaths": 0,
        "assists": 0,
        "result": "Unavailable",
        "timestamp": None,
        "duration": None,
    }
    champion_profile = {
        "name": "Champion details unavailable",
        "title": "",
        "lore": "Data Dragon or Riot API data could not be loaded for this profile card.",
        "abilities": [
            {
                "slot": "P",
                "name": "No ability data",
                "description": "Try again later or verify the Riot and Data Dragon requests are available.",
            }
        ],
        "icon_data_uri": None,
    }
    return None, latest_match, champion_profile, f"{title} • {reason}"


def write_svg(svg: str) -> None:
    """Write the final SVG text to disk, creating the output folder if needed."""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(svg, encoding="utf-8")


def generate_svg() -> str:
    """
    Main workflow for the script.

    High-level flow:
    1. load local env files
    2. read config
    3. fetch Riot account + summoner + latest match
    4. fetch champion details from Data Dragon
    5. render SVG
    6. write SVG to ``assets/lol-stats.svg``
    """
    load_dotenv()

    try:
        config = load_config()
    except ValueError as exc:
        account_level, latest_match, champion_profile, status = build_fallback_payload(
            "League Stats",
            str(exc),
        )
        svg = render_svg("Riot config required", account_level, latest_match, champion_profile, status)
        write_svg(svg)
        return svg

    summoner_name = f"{config.game_name}#{config.tag_line}"

    try:
        account = fetch_account(config)
        puuid = extract_puuid(account)
        summoner = fetch_summoner(config, puuid)
        account_level = extract_summoner_level(summoner)
        latest_match = normalize_latest_match(fetch_latest_match(config, puuid), puuid)
        champion_profile = (
            fetch_champion_profile(latest_match["champion"])
            if latest_match["champion"] not in {"-", "Data unavailable"}
            else None
        )
        status = "Live data from Riot API"
    except RiotApiError as exc:
        account_level, latest_match, champion_profile, status = build_fallback_payload(
            summoner_name,
            str(exc),
        )

    svg = render_svg(summoner_name, account_level, latest_match, champion_profile, status)
    write_svg(svg)
    return svg


if __name__ == "__main__":
    # This is the standard Python entrypoint check.
    #
    # It means:
    # - run ``generate_svg()`` when this file is executed directly
    # - do not auto-run it if this file is imported from another Python file
    generate_svg()
