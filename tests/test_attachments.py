from __future__ import annotations

import base64
import io
import re
from pathlib import Path

import pytest
from PIL import Image

from engine.attachments import build_multimodal_content, prepare_messages_for_llm, save_attachment
from providers.anthropic import AnthropicProvider
from providers.gemini import GeminiAIStudioProvider
from providers.openai import OpenAIProvider
from providers.openai_responses import OpenAIResponsesProvider


def test_runtime_requirements_include_pillow() -> None:
    requirements = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"(?im)^pillow(?:[<>=!~;\s\[]|$)", requirements), "Native image preprocessing requires Pillow"


@pytest.fixture(params=["PNG", "JPEG"])
def prepared_image_message(request: pytest.FixtureRequest, tmp_path: Path) -> dict:
    image_bytes = io.BytesIO()
    with Image.new("RGB", (24, 12), "red") as image:
        image.save(image_bytes, format=request.param)
    suffix = request.param.lower()
    attachment = save_attachment(
        tmp_path,
        "native-vision",
        image_bytes.getvalue(),
        filename=f"sample.{suffix}",
        mime_type=f"image/{suffix}",
    )
    content = build_multimodal_content("Describe this image", [attachment], workspace_root=tmp_path)
    messages = prepare_messages_for_llm([{"role": "user", "content": content}], tmp_path)
    return messages[0]


@pytest.mark.parametrize("provider_name", ["openai", "openai-responses", "anthropic", "gemini-aistudio"])
def test_uploaded_image_reaches_native_provider_payload(prepared_image_message: dict, provider_name: str) -> None:
    messages = [prepared_image_message]
    options = {"http": None, "api_key": "test", "base_url": None, "model": "test"}
    if provider_name == "openai":
        payload = OpenAIProvider(**options)._build_payload(messages, None)
        image_part = next(part for part in payload["messages"][0]["content"] if part["type"] == "image_url")
        data_url = image_part["image_url"]["url"]
        header, encoded = data_url.split(",", 1)
        assert header == "data:image/jpeg;base64"
    elif provider_name == "openai-responses":
        provider = OpenAIResponsesProvider(**options)
        instructions, input_items = provider._convert_messages(messages)
        payload = provider._build_payload(input_items=input_items, instructions=instructions, tools=None, stream=False)
        image_part = next(part for part in payload["input"][0]["content"] if part["type"] == "input_image")
        header, encoded = image_part["image_url"].split(",", 1)
        assert header == "data:image/jpeg;base64"
    elif provider_name == "anthropic":
        payload = AnthropicProvider(**options)._build_payload(messages)
        image_part = next(part for part in payload["messages"][0]["content"] if part["type"] == "image")
        assert image_part["source"]["type"] == "base64"
        assert image_part["source"]["media_type"] == "image/jpeg"
        encoded = image_part["source"]["data"]
    else:
        payload = GeminiAIStudioProvider(**options)._build_body(messages, None)
        image_part = next(part for part in payload["contents"][0]["parts"] if "inlineData" in part)
        assert image_part["inlineData"]["mimeType"] == "image/jpeg"
        encoded = image_part["inlineData"]["data"]
    with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as image:
        assert image.format == "JPEG"
        assert image.size == (24, 12)
