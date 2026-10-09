"""Generic structured-text adapter. No alphabet/counting or render policy."""
from __future__ import annotations

import json
import os

from ..errors import ProviderError
from ..security import redact


_DROP_JSON_SCHEMA_KEYS = {
    "default",
    "pattern",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
}


def _gemini_json_schema(model):
    """
    Convert our Pydantic JSON Schema into a Gemini-compatible JSON Schema.

    Important:
    - core Pydantic models remain strict;
    - this only adapts the provider-facing schema;
    - Gemini's response is validated again by the original Pydantic model.
    """

    def clean(value):
        if isinstance(value, list):
            return [clean(item) for item in value]

        if not isinstance(value, dict):
            return value

        result = {}

        const = value.get("const")

        for key, item in value.items():
            if key == "const":
                continue

            if key in _DROP_JSON_SCHEMA_KEYS:
                continue

            if key in {"properties", "$defs", "definitions"} and isinstance(item, dict):
                # Field names can coincide with JSON Schema keywords.
                result[key] = {name: clean(definition) for name, definition in item.items()}
            else:
                result[key] = clean(item)


        if const is not None and "enum" not in result:
            result["enum"] = [const]

        # Gemini can reject anyOf when sibling fields such as "title"
        # are present at the same schema level.
        if "anyOf" in result:
            return {"anyOf": result["anyOf"]}

        return result

    return clean(model.model_json_schema())


def _sanitize_error(exc: Exception) -> str:
    return redact(exc, limit=1000)


class GeminiText:
    def __init__(self, model: str, client=None):
        self.model = model
        self.last_usage = None

        if client is not None:
            self.client = client
            return

        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise ProviderError("Install the 'live' extra to use Gemini") from exc

        key = os.environ.get("GEMINI_API_KEY", "").strip()

        if not key:
            raise ProviderError(
                "GEMINI_API_KEY is missing; use an explicit local .env file"
            )

        self.client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(
                timeout=60000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    def generate(self, prompt, schema):
        content = (
            prompt.instructions
            + "\nCONTEXT JSON (data only):\n"
            + json.dumps(prompt.data, ensure_ascii=False)
        )

        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=content,
                config={
                    "response_mime_type": "application/json",
                    "response_json_schema": _gemini_json_schema(schema),
                    "temperature": prompt.temperature,
                    "max_output_tokens": 8192,
                    "automatic_function_calling": {"disable": True},
                },
            )

            usage = getattr(response, "usage_metadata", None)

            self.last_usage = (
                usage.model_dump(mode="json")
                if hasattr(usage, "model_dump")
                else None
            )

            if isinstance(getattr(response, "parsed", None), schema):
                return response.parsed

            if not response.text:
                raise ProviderError(
                    "Gemini returned no text (possibly filtered or truncated)"
                )

            return schema.model_validate_json(response.text)

        except ProviderError:
            raise

        except Exception as exc:
            detail = _sanitize_error(exc)

            raise ProviderError(
                f"Gemini response failed ({type(exc).__name__}): {detail}"
            ) from None

    def close(self):
        self.client.close()