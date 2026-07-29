from typing import Any

import httpx


class HermesClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

    def create_response(
        self,
        input_text: str,
        conversation: str,
    ) -> str:
        try:
            response = self._client.post(
                "/v1/responses",
                json={
                    "model": "hermes-agent",
                    "input": input_text,
                    "conversation": conversation,
                    "store": True,
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise RuntimeError(
                "Hermes API returned "
                f"HTTP {error.response.status_code}"
            ) from error
        except httpx.RequestError as error:
            raise RuntimeError(
                "Failed to connect to Hermes API"
            ) from error

        return _extract_output_text(response.json())


def _extract_output_text(payload: dict[str, Any]) -> str:
    direct_output = payload.get("output_text")

    if isinstance(direct_output, str) and direct_output.strip():
        return direct_output.strip()

    text_parts: list[str] = []

    for output_item in payload.get("output", []):
        if not isinstance(output_item, dict):
            continue

        if output_item.get("type") != "message":
            continue

        for content_item in output_item.get("content", []):
            if not isinstance(content_item, dict):
                continue

            if content_item.get("type") != "output_text":
                continue

            text = content_item.get("text")

            if isinstance(text, str) and text.strip():
                text_parts.append(text.strip())

    result = "\n".join(text_parts).strip()

    if not result:
        raise RuntimeError(
            "Hermes API response did not contain output text"
        )

    return result
