"""AI analysis and user-facing prompt formatting."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from .settings import UserPreferences

if TYPE_CHECKING:
    from openai import AsyncOpenAI


@dataclass(frozen=True, slots=True)
class VideoAnalysis:
    """Model output separated into observations and qualified inferences."""

    observed: str
    inferred: str
    video_prompt: str


_SYSTEM_INSTRUCTIONS = """You analyze sampled frames from a user-provided video and write a useful video-generation prompt.
Be precise and conservative. Keep facts directly visible in OBSERVED. Put plausible interpretations in INFERRED and qualify them as uncertain. Do not invent identities, exact locations, dates, brands, readable text, camera bodies, lenses, focal lengths, or production details. Only describe text or logos if they are clearly legible. Never present an inference as an observation. If a detail is not supported, omit it. Use changes across the ordered frames to describe visible action and continuity. The VIDEO_PROMPT must stay faithful to the evidence and must not introduce unsupported objects, events, or scene details. Return only a JSON object with string keys observed, inferred, and video_prompt. All three values must use the requested output language."""


def _detail_instruction(detail: str) -> str:
    return {
        "Low": "Keep the analysis concise and the generation prompt to one or two sentences.",
        "Medium": "Give a balanced description with the main subject, action, setting, composition, lighting, and visible motion.",
        "High": "Provide a thorough but evidence-grounded description of subjects, action, setting, composition, color, lighting, and motion across time.",
    }[detail]


async def analyze_video_frames(
    client: AsyncOpenAI,
    frame_paths: Sequence[Path],
    timestamps: Sequence[float],
    preferences: UserPreferences,
    model: str,
) -> VideoAnalysis:
    """Send ordered sampled frames to OpenAI and validate the structured result."""
    if not frame_paths or len(frame_paths) != len(timestamps):
        raise ValueError("frame_paths and timestamps must contain the same non-zero number of items")

    detail = {"Low": "low", "Medium": "auto", "High": "high"}[preferences.analysis_detail]
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": (
            f"Prompt style: {preferences.prompt_style}.\n"
            f"Analysis detail: {preferences.analysis_detail}. {_detail_instruction(preferences.analysis_detail)}\n"
            f"Output language: {preferences.output_language}.\n"
            "The following images are evenly sampled frames in chronological order. Analyze only what they support."
        ),
    }]
    for index, (path, timestamp) in enumerate(zip(frame_paths, timestamps), 1):
        encoded = await asyncio.to_thread(lambda frame=path: base64.b64encode(frame.read_bytes()).decode("ascii"))
        content.append({"type": "text", "text": f"Frame {index}, approximately {timestamp:.2f} seconds."})
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": detail},
        })

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_INSTRUCTIONS},
            {"role": "user", "content": content},
        ],
        response_format={"type": "json_object"},
        max_completion_tokens=1800,
    )
    raw = response.choices[0].message.content if response.choices else None
    if not raw:
        raise ValueError("The AI returned an empty analysis.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("The AI returned malformed structured output.") from exc
    if not isinstance(payload, dict):
        raise ValueError("The AI response must be a JSON object.")
    fields = (payload.get("observed"), payload.get("inferred"), payload.get("video_prompt"))
    if not all(isinstance(field, str) and field.strip() for field in fields):
        raise ValueError("The AI response is missing a required analysis section.")
    return VideoAnalysis(*(field.strip() for field in fields))


def format_analysis_response(analysis: VideoAnalysis, preferences: UserPreferences) -> str:
    """Format a clear plain-text Telegram response with evidence labels."""
    return (
        f"🎬 Video prompt · {preferences.prompt_style} · {preferences.analysis_detail} detail · {preferences.output_language}\n\n"
        f"OBSERVED\n{analysis.observed.strip()}\n\n"
        f"INFERRED\n{analysis.inferred.strip()}\n\n"
        f"VIDEO GENERATION PROMPT\n{analysis.video_prompt.strip()}"
    )


def split_telegram_message(text: str, max_length: int = 3900) -> list[str]:
    """Split text by Telegram's UTF-16 message limit without losing characters."""
    if max_length <= 0:
        raise ValueError("max_length must be greater than zero")
    remaining = text
    if not remaining:
        return [""]
    parts: list[str] = []
    while remaining:
        units = 0
        max_index = 0
        for index, character in enumerate(remaining):
            width = 2 if ord(character) > 0xFFFF else 1
            if units + width > max_length:
                break
            units += width
            max_index = index + 1
        if max_index == len(remaining):
            parts.append(remaining)
            break
        if max_index == 0:
            raise ValueError("max_length is too small to contain the next Unicode character")

        boundary = max_index
        for separator in ("\n", " "):
            separator_index = remaining.rfind(separator, 0, max_index)
            if separator_index >= max_index // 2:
                boundary = separator_index + 1  # Keep the separator in the preceding chunk.
                break
        parts.append(remaining[:boundary])
        remaining = remaining[boundary:]
    return parts
