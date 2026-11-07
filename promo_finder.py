"""Find, validate, and rank short-form promo candidates in screenplay packs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"), override=False)


TIMESTAMP_RE = re.compile(
    r"(?<!\d)(\d{2}:\d{2}(?::\d{2})?(?:\.\d{1,3})?)(?!\d)"
)
EPISODE_RE = re.compile(r"(?im)^\s*EPISODE NUMBER:\s*(.+?)\s*$")
SCORE_KEYS = ("hook", "cliffhanger", "standalone", "timing")
MAX_ATTEMPTS = 3

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "segments": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "start_timestamp": {"type": "STRING"},
                    "end_timestamp": {"type": "STRING"},
                },
                "required": ["start_timestamp", "end_timestamp"],
            },
        },
        "hook": {"type": "STRING"},
        "cliffhanger": {"type": "STRING"},
        "reasoning": {"type": "STRING"},
        "quality_scores": {
            "type": "OBJECT",
            "properties": {
                "hook": {"type": "INTEGER"},
                "cliffhanger": {"type": "INTEGER"},
                "standalone": {"type": "INTEGER"},
                "timing": {"type": "INTEGER"},
            },
            "required": ["hook", "cliffhanger", "standalone", "timing"],
        },
    },
    "required": [
        "segments",
        "hook",
        "cliffhanger",
        "reasoning",
        "quality_scores",
    ],
}

CROSS_EPISODE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "segments": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "episode_number": {"type": "STRING"},
                    "start_timestamp": {"type": "STRING"},
                    "end_timestamp": {"type": "STRING"},
                },
                "required": [
                    "episode_number",
                    "start_timestamp",
                    "end_timestamp",
                ],
            },
        },
        "hook": {"type": "STRING"},
        "cliffhanger": {"type": "STRING"},
        "reasoning": {"type": "STRING"},
        "quality_scores": RESPONSE_SCHEMA["properties"]["quality_scores"],
    },
    "required": [
        "segments",
        "hook",
        "cliffhanger",
        "reasoning",
        "quality_scores",
    ],
}

ARC_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "episode_numbers": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
        },
        "narrative_thread": {"type": "STRING"},
        "selection_reason": {"type": "STRING"},
    },
    "required": ["episode_numbers", "narrative_thread", "selection_reason"],
}


class ModelProviderError(RuntimeError):
    """Normalized model-provider failure used by the retry workflow."""

    def __init__(self, message: str, status_code: int | None, retryable: bool):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class JSONModelProvider(Protocol):
    """Provider contract required by promo extraction."""

    provider_name: str

    def generate_json(
        self, model: str, contents: str, response_schema: dict[str, Any]
    ) -> str: ...


class GeminiProvider:
    """Gemini implementation of the common JSON-generation interface."""

    provider_name = "gemini"

    def __init__(self, api_key: str, client: Any | None = None):
        if client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise RuntimeError(
                    "Gemini SDK is missing. Install dependencies with: "
                    "pip install -r requirements.txt"
                ) from exc
            client = genai.Client(api_key=api_key)
        self.client = client
        self.api_key = api_key

    def generate_json(
        self, model: str, contents: str, response_schema: dict[str, Any]
    ) -> str:
        from google.genai.errors import APIError

        try:
            response = self.client.models.generate_content(
                model=model,
                contents=contents,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": response_schema,
                },
            )
        except APIError as exc:
            message = str(exc).replace(self.api_key, "[REDACTED]")
            raise ModelProviderError(
                f"Gemini request failed: {message}",
                status_code=exc.code,
                retryable=exc.code in (429, 500, 502, 503, 504),
            ) from exc
        return _response_text(response)


class OpenAICompatibleProvider:
    """Adapter for APIs implementing the OpenAI Chat Completions JSON mode."""

    provider_name = "openai-compatible"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        client: Any | None = None,
    ):
        self.api_key = api_key
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "OpenAI SDK is missing. Install dependencies with: "
                    "pip install -r requirements.txt"
                ) from exc
            client = OpenAI(api_key=api_key, base_url=base_url or None)
        self.client = client

    def generate_json(
        self, model: str, contents: str, response_schema: dict[str, Any]
    ) -> str:
        from openai import APIConnectionError, APIStatusError, APITimeoutError

        try:
            response = self.client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"{contents}\n\nReturn JSON matching this schema:\n"
                            f"{json.dumps(response_schema, ensure_ascii=False)}"
                        ),
                    }
                ],
                response_format={"type": "json_object"},
            )
        except (APIStatusError, APIConnectionError, APITimeoutError) as exc:
            status_code = getattr(exc, "status_code", None)
            message = str(exc).replace(self.api_key, "[REDACTED]")
            raise ModelProviderError(
                f"OpenAI-compatible request failed: {message}",
                status_code=status_code,
                retryable=status_code in (408, 409, 429, 500, 502, 503, 504)
                or isinstance(exc, (APIConnectionError, APITimeoutError)),
            ) from exc
        try:
            text = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise ValueError("OpenAI-compatible API returned no message content") from exc
        if not isinstance(text, str) or not text.strip():
            raise ValueError("OpenAI-compatible API returned empty content")
        return text


def create_provider(
    provider_name: str,
    api_key: str,
    base_url: str | None = None,
) -> GeminiProvider | OpenAICompatibleProvider:
    normalized_name = provider_name.strip().lower().replace("_", "-")
    if normalized_name == "gemini":
        return GeminiProvider(api_key)
    if normalized_name in ("openai-compatible", "openai"):
        return OpenAICompatibleProvider(api_key, base_url)
    raise ValueError(
        "Unsupported provider. Choose 'gemini' or 'openai-compatible'. "
        "Other API protocols require an adapter implementing JSONModelProvider."
    )


def _redact_api_keys(message: str) -> str:
    for key_name in ("MODEL_API_KEY", "GEMINI_API_KEY"):
        api_key = os.getenv(key_name)
        if api_key:
            message = message.replace(api_key, "[REDACTED]")
    return message


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Model returned an empty response")
    return text


def parse_timestamp(value: str) -> float:
    """Convert MM:SS.xx or HH:MM:SS.xx to seconds."""
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"Unsupported timestamp: {value!r}")
    try:
        seconds = float(parts[-1])
        minutes = int(parts[-2])
        hours = int(parts[-3]) if len(parts) == 3 else 0
    except ValueError as exc:
        raise ValueError(f"Unsupported timestamp: {value!r}") from exc
    if seconds >= 60 or minutes >= 60 or min(hours, minutes, seconds) < 0:
        raise ValueError(f"Invalid timestamp: {value!r}")
    return hours * 3600 + minutes * 60 + seconds


def _read_episode(path: str, content: str) -> dict[str, Any]:
    match = EPISODE_RE.search(content)
    if not match:
        raise ValueError(f"Missing EPISODE NUMBER header in {path}")
    return {
        "episode_number": match.group(1).strip(),
        "source_file": path,
        "content": content,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "logline": _header_value(content, "EPISODE LOGLINE"),
        "continuity_notes": _header_value(content, "CONTINUITY NOTES"),
    }


def _header_value(content: str, header: str) -> str:
    match = re.search(rf"(?im)^\s*{re.escape(header)}:\s*(.+?)\s*$", content)
    return match.group(1).strip() if match else ""


def load_episodes(input_path: str | Path) -> list[dict[str, Any]]:
    """Read screenplay text files from a directory or a ZIP archive."""
    source = Path(input_path)
    if source.is_dir():
        files: Iterable[tuple[str, bytes]] = (
            (str(path.relative_to(source)), path.read_bytes())
            for path in sorted(source.rglob("*.txt"))
        )
    elif source.is_file() and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            files = [
                (name, archive.read(name))
                for name in sorted(archive.namelist())
                if name.lower().endswith(".txt") and not name.endswith("/")
            ]
    else:
        raise ValueError(f"Input must be a directory or ZIP file: {source}")

    episodes = []
    for name, raw_content in files:
        content = raw_content.decode("utf-8-sig", errors="replace")
        episodes.append(_read_episode(name, content))
    if not episodes:
        raise ValueError(f"No .txt screenplay files found in {source}")
    return episodes


def load_uploaded_episodes(
    uploaded_files: Iterable[tuple[str, bytes]],
) -> list[dict[str, Any]]:
    """Load Streamlit-uploaded ZIP packs and/or individual screenplay text files."""
    episodes: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="promo-input-") as temp_dir:
        temp_path = Path(temp_dir)
        text_dir = temp_path / "screenplays"
        text_dir.mkdir()
        zip_paths = []
        text_count = 0
        for index, (filename, contents) in enumerate(uploaded_files):
            name = Path(filename).name
            suffix = Path(name).suffix.lower()
            if suffix == ".zip":
                zip_path = temp_path / f"upload_{index}.zip"
                zip_path.write_bytes(contents)
                zip_paths.append(zip_path)
            elif suffix == ".txt":
                text_path = text_dir / f"{index}_{name}"
                text_path.write_bytes(contents)
                text_count += 1
            else:
                raise ValueError(
                    f"Unsupported upload {name!r}; upload ZIP or TXT files."
                )
        if not zip_paths and not text_count:
            raise ValueError("Upload a screenplay ZIP or one or more .txt files.")
        for zip_path in zip_paths:
            episodes.extend(load_episodes(zip_path))
        if text_count:
            episodes.extend(load_episodes(text_dir))
    episode_numbers = [episode["episode_number"] for episode in episodes]
    if len(set(episode_numbers)) != len(episode_numbers):
        raise ValueError("Uploaded files contain duplicate episode numbers.")
    return episodes


def _score_value(scores: dict[str, Any], key: str) -> int:
    value = scores.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise ValueError(f"quality_scores.{key} must be an integer from 1 to 5")
    return value


def _validate_payload(
    payload: Any,
    source_episodes: dict[str, dict[str, Any]],
    allow_cross_episode: bool,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Model response must be a JSON object")
    for field in ("segments", "hook", "cliffhanger", "reasoning", "quality_scores"):
        if field not in payload:
            raise ValueError(f"Missing required field: {field}")
    if not all(
        isinstance(payload[field], str) and payload[field].strip()
        for field in ("hook", "cliffhanger", "reasoning")
    ):
        raise ValueError("hook, cliffhanger, and reasoning must be non-empty strings")
    segments = payload["segments"]
    if not isinstance(segments, list) or not 2 <= len(segments) <= 4:
        raise ValueError("segments must contain between 2 and 4 moments")
    normalized_segments = []
    seen_episodes: set[str] = set()
    previous_end_by_episode: dict[str, float] = {}
    total_duration = 0.0
    for index, segment in enumerate(segments, start=1):
        if not isinstance(segment, dict):
            raise ValueError(f"Segment {index} must be an object")
        episode_number = segment.get("episode_number")
        if not allow_cross_episode:
            episode_number = next(iter(source_episodes))
        if episode_number not in source_episodes:
            raise ValueError(
                f"Segment {index} references an unknown episode: {episode_number!r}"
            )
        if not allow_cross_episode and "episode_number" in segment:
            if segment["episode_number"] != episode_number:
                raise ValueError(f"Segment {index} must use its candidate episode")
        start_text = segment.get("start_timestamp")
        end_text = segment.get("end_timestamp")
        if not isinstance(start_text, str) or not isinstance(end_text, str):
            raise ValueError(f"Segment {index} timestamps must be strings")
        start = parse_timestamp(start_text)
        end = parse_timestamp(end_text)
        source_times = {
            round(parse_timestamp(value), 3)
            for value in TIMESTAMP_RE.findall(
                source_episodes[episode_number]["content"]
            )
        }
        if round(start, 3) not in source_times or round(end, 3) not in source_times:
            raise ValueError(
                f"Segment {index} uses a timestamp not present in {episode_number}"
            )
        previous_end = previous_end_by_episode.get(episode_number, -1.0)
        if not allow_cross_episode and start < previous_end:
            raise ValueError("Segments in an episode must be chronological and non-overlapping")
        if allow_cross_episode and start < previous_end:
            raise ValueError(f"Segments in {episode_number} must not overlap")
        if end <= start:
            raise ValueError(f"Segment {index} must end after it starts")
        total_duration += end - start
        previous_end_by_episode[episode_number] = end
        seen_episodes.add(episode_number)
        normalized = {
            "episode_number": episode_number,
            "start_timestamp": start_text,
            "end_timestamp": end_text,
        }
        normalized_segments.append(normalized)

    if not 20 <= total_duration <= 60:
        raise ValueError(
            f"Combined segment duration is {total_duration:.2f}s; expected 20–60s. "
            f"Add at least {max(0.0, 20.0 - total_duration):.2f}s of valid source footage."
        )
    if allow_cross_episode and len(seen_episodes) < 2:
        raise ValueError("A cross-episode promo must use at least 2 different episodes")
    scores = payload["quality_scores"]
    if not isinstance(scores, dict):
        raise ValueError("quality_scores must be an object")
    checked_scores = {key: _score_value(scores, key) for key in SCORE_KEYS}
    # Hook, cliffhanger, and standalone value most; timing is a smaller tie-breaker.
    rank_score = round(
        20
        * (
            checked_scores["hook"] * 0.30
            + checked_scores["cliffhanger"] * 0.30
            + checked_scores["standalone"] * 0.30
            + checked_scores["timing"] * 0.10
        ),
        1,
    )
    return {
        "segments": normalized_segments,
        "hook": payload["hook"].strip(),
        "cliffhanger": payload["cliffhanger"].strip(),
        "reasoning": payload["reasoning"].strip(),
        "quality_scores": checked_scores,
        "rank_score": rank_score,
        "duration_seconds": round(total_duration, 2),
    }


def validate_candidate(
    payload: Any, episode: dict[str, Any]
) -> dict[str, Any]:
    """Validate a multi-moment promo against one screenplay and its source timecodes."""
    candidate = _validate_payload(
        payload, {episode["episode_number"]: episode}, allow_cross_episode=False
    )
    candidate["episode_number"] = episode["episode_number"]
    return candidate


def validate_cross_episode_candidate(
    payload: Any, episodes: list[dict[str, Any]]
) -> dict[str, Any]:
    """Validate a montage whose moments come from multiple episodes."""
    candidate = _validate_payload(
        payload,
        {episode["episode_number"]: episode for episode in episodes},
        allow_cross_episode=True,
    )
    candidate["episode_number"] = "Cross-episode"
    candidate["episode_numbers"] = list(
        dict.fromkeys(segment["episode_number"] for segment in candidate["segments"])
    )
    return candidate


def build_prompt(episode: dict[str, Any], correction: str = "") -> str:
    return f"""You are selecting one strong short-form promotional clip from a microdrama screenplay.

Return exactly one JSON candidate for {episode['episode_number']}. Use 2–4 distinct, non-overlapping moments from this episode; connect them into a
miniature story, not one continuous scene. Target a total playable duration of
35–45 seconds (the valid range is 20–60 seconds). Give each moment enough footage
to play clearly, usually 6–18 seconds, rather than choosing only a single short line.
Put the strongest hook in the first moment (visible/actionable within 3 seconds),
then build context and end on an unresolved cliffhanger. Make it clear to a
first-time viewer. Before returning JSON, add up all segment durations and ensure
the sum is at least 20 seconds.
Use only timecodes that appear exactly in the screenplay; do not invent, round, or
interpolate timestamps. Start and end at relevant action/dialogue/camera boundaries.
Score each dimension from 1 (weak) to 5 (excellent); be conservative.

JSON structure:
{{
  "segments": [{{"start_timestamp": "00:00.00", "end_timestamp": "00:00.00"}}],
  "hook": "What happens in the opening three seconds",
  "cliffhanger": "What unresolved question remains at the cut",
  "reasoning": "One sentence on why this works for a new viewer",
  "quality_scores": {{
    "hook": 1,
    "cliffhanger": 1,
    "standalone": 1,
    "timing": 1
  }}
}}

Quality score meanings:
- hook: stopping power within the first 3 seconds
- cliffhanger: strength of the unresolved ending
- standalone: clarity without prior episode context
- timing: pacing and fit within 20–60 seconds

{correction}
SCREENPLAY:
{episode['content']}"""


def _decode_response(response: Any) -> tuple[str, Any]:
    text = response if isinstance(response, str) else _response_text(response)
    try:
        return text, json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model returned invalid JSON: {exc}") from exc


def request_candidate(
    client: JSONModelProvider,
    model: str,
    episode: dict[str, Any],
    delay_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], str]:
    """Request and validate one candidate, retrying with actionable validation feedback."""
    correction = ""
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response_text = client.generate_json(
                model=model,
                contents=build_prompt(episode, correction),
                response_schema=RESPONSE_SCHEMA,
            )
            raw_text, payload = _decode_response(response_text)
            return validate_candidate(payload, episode), raw_text
        except ModelProviderError as exc:
            if not exc.retryable or attempt == MAX_ATTEMPTS:
                raise
            last_error = exc
            sleep(min(60.0, max(delay_seconds, 30.0) * attempt))
        except (ValueError, TypeError, KeyError) as exc:
            last_error = exc
            correction = (
                f"Your previous response failed validation: {exc}. "
                "Correct it and return a complete JSON object. For a duration failure, "
                "recalculate every segment and add a third valid moment if needed; "
                "the combined duration must be at least 20 seconds."
            )
            if attempt < MAX_ATTEMPTS and delay_seconds > 0:
                sleep(delay_seconds)
    raise ValueError(
        f"Could not produce a valid candidate after {MAX_ATTEMPTS} attempts: {last_error}"
    )


def request_cross_episode_candidate(
    client: JSONModelProvider,
    model: str,
    episodes: list[dict[str, Any]],
    delay_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], str, str]:
    """Choose a connected story arc, then generate a timecode-validated montage."""
    summaries = [
        {
            "episode_number": episode["episode_number"],
            "logline": episode["logline"],
            "continuity_notes": episode["continuity_notes"],
        }
        for episode in episodes
    ]
    selection_prompt = (
        "Choose 2–4 episodes from this one show's ordered episode summaries that form "
        "the clearest connected dramatic arc for a short promotional montage. Prefer "
        "adjacent or explicitly connected episodes, but continuity matters more than "
        "strict adjacency. Return episode numbers in story order, one concise narrative "
        "thread, and why the moments belong together. Choose only exact episode labels "
        "from the input. Return one JSON object with keys episode_numbers (array of "
        "strings), narrative_thread (string), and selection_reason (string).\n\n"
        + json.dumps(summaries, ensure_ascii=False)
    )
    selection: dict[str, Any] | None = None
    selection_raw = ""
    selection_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response_text = client.generate_json(
                model=model,
                contents=selection_prompt
                + (
                    f"\nCorrect your last response: {selection_error}"
                    if selection_error
                    else ""
                ),
                response_schema=ARC_SCHEMA,
            )
            selection_raw, decoded = _decode_response(response_text)
            numbers = decoded.get("episode_numbers") if isinstance(decoded, dict) else None
            known_numbers = {episode["episode_number"] for episode in episodes}
            if (
                not isinstance(numbers, list)
                or not 2 <= len(numbers) <= 4
                or any(number not in known_numbers for number in numbers)
                or len(set(numbers)) != len(numbers)
            ):
                raise ValueError("Select 2–4 unique episode numbers from the input")
            if not isinstance(decoded.get("narrative_thread"), str) or not decoded[
                "narrative_thread"
            ].strip():
                raise ValueError("narrative_thread must be a non-empty string")
            selection = decoded
            break
        except ModelProviderError as exc:
            if not exc.retryable or attempt == MAX_ATTEMPTS:
                raise
            sleep(min(60.0, max(delay_seconds, 30.0) * attempt))
        except (ValueError, TypeError, KeyError) as exc:
            selection_error = str(exc)
            if attempt < MAX_ATTEMPTS and delay_seconds > 0:
                sleep(delay_seconds)
    if selection is None:
        raise ValueError(f"Could not select a related episode arc: {selection_error}")

    by_number = {episode["episode_number"]: episode for episode in episodes}
    selected_episodes = [by_number[number] for number in selection["episode_numbers"]]
    script_bundle = "\n\n".join(
        f"===== {episode['episode_number']} =====\n{episode['content']}"
        for episode in selected_episodes
    )
    candidate_prompt = f"""Create one promotional montage using this selected, connected story arc:
{selection['narrative_thread']}

Use 2–4 short, distinct moments across at least 2 of the selected episodes, in the
story order. The first segment must hook within 3 seconds. Build enough context for a
new viewer, then cut on an unresolved cliffhanger. Total playable duration across all
segments must be 20–60 seconds. Each segment must include its exact episode_number,
start_timestamp, and end_timestamp copied from that episode's screenplay. Do not invent
or interpolate timestamps. Keep segments non-overlapping within each episode.

Return JSON with this shape:
{{
  "segments": [
    {{"episode_number": "Episode X", "start_timestamp": "00:00.00", "end_timestamp": "00:00.00"}}
  ],
  "hook": "What happens in the opening three seconds",
  "cliffhanger": "What unresolved question remains at the cut",
  "reasoning": "Why this edit connects the moments for a new viewer",
  "quality_scores": {{"hook": 1, "cliffhanger": 1, "standalone": 1, "timing": 1}}
}}

Rate hook, cliffhanger, standalone clarity, and timing from 1 (weak) to 5 (excellent).
{script_bundle}"""

    correction = ""
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response_text = client.generate_json(
                model=model,
                contents=candidate_prompt + correction,
                response_schema=CROSS_EPISODE_SCHEMA,
            )
            raw_text, payload = _decode_response(response_text)
            return (
                validate_cross_episode_candidate(payload, selected_episodes),
                raw_text,
                selection_raw,
            )
        except ModelProviderError as exc:
            if not exc.retryable or attempt == MAX_ATTEMPTS:
                raise
            last_error = exc
            sleep(min(60.0, max(delay_seconds, 30.0) * attempt))
        except (ValueError, TypeError, KeyError) as exc:
            last_error = exc
            correction = (
                f"\nYour previous montage failed validation: {exc}. "
                "Return a corrected complete JSON object."
            )
            if attempt < MAX_ATTEMPTS and delay_seconds > 0:
                sleep(delay_seconds)
    raise ValueError(
        f"Could not produce a valid cross-episode montage after {MAX_ATTEMPTS} "
        f"attempts: {last_error}"
    )


def rank_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(
        candidates,
        key=lambda item: (
            -item["rank_score"],
            int(re.search(r"\d+", item["episode_number"]).group())
            if re.search(r"\d+", item["episode_number"])
            else sys.maxsize,
        ),
    )
    return [{**candidate, "rank": rank} for rank, candidate in enumerate(ranked, 1)]


def run_extraction(
    episodes: list[dict[str, Any]],
    client: JSONModelProvider,
    model: str,
    output_dir: str | Path,
    delay_seconds: float = 12.0,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[int, int, str], None] | None = None,
    resume: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Analyze the input pack and persist raw model responses and ranked candidates."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    candidates = []
    failures = []
    episodes_to_process = []
    for index, episode in enumerate(episodes):
        episode_digits = re.search(r"\d+", episode["episode_number"])
        suffix = f"{int(episode_digits.group()):03d}" if episode_digits else f"{index + 1:03d}"
        candidate_path = output / f"episode_{suffix}_promo.json"
        if resume and candidate_path.is_file():
            try:
                previous = json.loads(candidate_path.read_text(encoding="utf-8"))
                candidates.append(validate_candidate(previous, episode))
                continue
            except (OSError, ValueError, TypeError):
                pass
        episodes_to_process.append((episode, suffix))

    for index, (episode, suffix) in enumerate(episodes_to_process):
        try:
            candidate, raw_text = request_candidate(
                client, model, episode, delay_seconds=delay_seconds, sleep=sleep
            )
            (output / f"episode_{suffix}_model.json").write_text(
                raw_text, encoding="utf-8"
            )
            (output / f"episode_{suffix}_promo.json").write_text(
                json.dumps(candidate, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            candidates.append(candidate)
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            error_text = _redact_api_keys(error_text)
            failure = {
                "episode_number": episode["episode_number"],
                "source_file": episode["source_file"],
                "error": error_text,
            }
            failures.append(failure)
        if progress:
            progress(index + 1, len(episodes_to_process), episode["episode_number"])
        if index + 1 < len(episodes_to_process) and delay_seconds > 0:
            sleep(delay_seconds)

    cross_episode_promos = []
    cross_promo_path = output / "cross_episode_promo.json"
    if len(episodes) >= 2 and resume and cross_promo_path.is_file():
        try:
            previous = json.loads(cross_promo_path.read_text(encoding="utf-8"))
            cross_episode_promos.append(
                validate_cross_episode_candidate(previous, episodes)
            )
        except (OSError, ValueError, TypeError):
            pass

    cross_failures = []
    if len(episodes) >= 2 and not cross_episode_promos:
        try:
            cross_candidate, cross_raw, arc_raw = request_cross_episode_candidate(
                client, model, episodes, delay_seconds=delay_seconds, sleep=sleep
            )
            (output / "cross_episode_model.json").write_text(
                cross_raw, encoding="utf-8"
            )
            (output / "cross_episode_arc_model.json").write_text(
                arc_raw, encoding="utf-8"
            )
            cross_promo_path.write_text(
                json.dumps(cross_candidate, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            cross_episode_promos.append(cross_candidate)
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            error_text = _redact_api_keys(error_text)
            cross_failures.append(
                {
                    "scope": "cross_episode",
                    "episode_number": "Cross-episode",
                    "source_file": "episode pack",
                    "error": error_text,
                }
            )

    ranked = rank_candidates(candidates)
    ranked_cross_episode = [
        {**candidate, "rank": rank}
        for rank, candidate in enumerate(cross_episode_promos, start=1)
    ]
    all_failures = failures + cross_failures
    provider_name = getattr(client, "provider_name", None)
    if not isinstance(provider_name, str):
        provider_name = type(client).__name__
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider_name,
        "model": model,
        "episodes_in_pack": len(episodes),
        "candidates_generated": len(candidates),
        "cross_episode_candidates_generated": len(ranked_cross_episode),
        "failures": all_failures,
        "ranking_method": (
            "Weighted model rubric: hook 30%, cliffhanger 30%, standalone 30%, "
            "timing 10%; scores are 1–5 and mapped to 20–100."
        ),
        "ranked_promos": ranked,
        "cross_episode_promos": ranked_cross_episode,
    }
    (output / "ranked_promos.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    failure_path = output / "failures.json"
    if all_failures:
        failure_path.write_text(
            json.dumps(all_failures, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        failure_path.unlink(missing_ok=True)
    return ranked, all_failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract and rank promo candidates from screenplay .txt files."
    )
    parser.add_argument("--input", required=True, help="Screenplay directory or ZIP pack")
    parser.add_argument("--output", default="raw_outputs", help="Output folder")
    parser.add_argument(
        "--provider",
        default=os.getenv("MODEL_PROVIDER", "gemini"),
        choices=("gemini", "openai-compatible"),
        help="Model API provider",
    )
    parser.add_argument(
        "--model",
        default=os.getenv(
            "MODEL_NAME",
            os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        ),
        help="Provider model ID",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("MODEL_BASE_URL"),
        help="Base URL for OpenAI-compatible endpoints",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=12.0,
        help="Seconds between requests (default: 12)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Reuse valid saved candidates and retry only missing or invalid "
            "episode and cross-episode promos"
        ),
    )
    args = parser.parse_args()
    api_key = os.getenv("MODEL_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        parser.error(
            "Set MODEL_API_KEY (or GEMINI_API_KEY for Gemini) in your environment."
        )
    if args.delay < 0:
        parser.error("--delay must not be negative")

    try:
        episodes = load_episodes(args.input)
        print(f"Loaded {len(episodes)} screenplay(s).")
        ranked, failures = run_extraction(
            episodes,
            create_provider(args.provider, api_key, args.base_url),
            args.model,
            args.output,
            args.delay,
            resume=args.resume,
            progress=lambda done, total, episode: print(
                f"[{done}/{total}] Finished {episode}"
            ),
        )
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"Extraction failed: {exc}", file=sys.stderr)
        return 1
    manifest = json.loads(
        (Path(args.output) / "ranked_promos.json").read_text(encoding="utf-8")
    )
    print(
        f"Saved {len(ranked)} per-episode promo(s) and "
        f"{len(manifest['cross_episode_promos'])} cross-episode promo(s) "
        f"to {args.output}."
    )
    if failures:
        print(
            f"{len(failures)} episode(s) failed; see {Path(args.output) / 'failures.json'}.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
