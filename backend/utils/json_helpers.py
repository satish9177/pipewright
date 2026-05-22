"""
json_helpers.py
Shared utilities for cleaning and parsing AI model responses.
Every pipeline module that calls Claude imports from here.
Never copy paste these helpers into individual modules.
"""

import json


def clean_json_response(text: str) -> str:
    """
    Clean raw AI response text into parseable JSON string.

    Claude occasionally wraps JSON in markdown code blocks
    even when explicitly told not to. This helper strips:
      - leading and trailing whitespace
      - ```json opening fence
      - ``` closing fence
      - any text before the first { character
      - any text after the last } character

    Returns a clean JSON string ready for json.loads()
    Raises ValueError if no JSON object found in text.
    """
    if not text:
        raise ValueError("json_helpers.py: empty response from model")

    text = text.strip()

    # Remove markdown fences if present
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]

    if text.endswith("```"):
        text = text[:-3]

    text = text.strip()

    # Extract only the JSON object portion
    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1:
        raise ValueError(
            f"json_helpers.py: no JSON object found in response. "
            f"Raw text preview: {text[:200]}"
        )

    return text[start:end + 1]


def safe_parse_json(text: str) -> dict:
    """
    Clean and parse AI response into a Python dict.
    Combines clean_json_response and json.loads.
    Raises ValueError with context if parsing fails.
    """
    try:
        cleaned = clean_json_response(text)
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"json_helpers.py: JSON parsing failed after cleaning. "
            f"Error: {e}"
        )
