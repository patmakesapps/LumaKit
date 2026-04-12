import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import requests


class OllamaTimeoutError(Exception):
    """Raised when an Ollama request exceeds its deadline."""


class OllamaConnectionError(Exception):
    """Raised when Ollama server is unreachable after all retries."""


class OllamaClient:
    def __init__(self, base_url="http://localhost:11434", request_timeout=120,
                 fallback_model=None):
        self.base_url = base_url.rstrip("/")
        self.request_timeout = request_timeout
        self.fallback_model = fallback_model
        # Tracks which model was actually used in the last call
        self.last_model_used = None

    def _post(self, model, messages, tools, stream, options=None):
        url = f"{self.base_url}/api/chat"
        payload = {"model": model, "messages": messages, "stream": stream}
        if tools:
            payload["tools"] = tools
        if options:
            payload["options"] = options
        response = requests.post(url, json=payload, timeout=self.request_timeout)
        response.raise_for_status()
        return response.json()

    def _post_with_fallback(self, model, messages, tools, stream, options=None):
        """Try primary model, fall back on connection failure."""
        try:
            result = self._post(model, messages, tools, stream, options)
            self.last_model_used = model
            return result
        except (requests.ConnectionError, requests.Timeout,
                ConnectionRefusedError, OSError) as e:
            if not self.fallback_model or self.fallback_model == model:
                raise OllamaConnectionError(
                    f"Cannot reach Ollama server at {self.base_url}. "
                    f"Make sure Ollama is running."
                ) from e
            # Try fallback
            try:
                result = self._post(self.fallback_model, messages, tools, stream, options)
                self.last_model_used = self.fallback_model
                return result
            except (requests.ConnectionError, requests.Timeout,
                    ConnectionRefusedError, OSError):
                raise OllamaConnectionError(
                    f"Cannot reach Ollama server at {self.base_url}. "
                    f"Both primary ({model}) and fallback ({self.fallback_model}) failed."
                ) from e

    def chat(self, model, messages, tools=None, stream=False, deadline=None, options=None):
        """Send a chat request. If *deadline* (seconds) is set, raise
        OllamaTimeoutError when the call takes longer than that."""
        if deadline is None:
            return self._post_with_fallback(model, messages, tools, stream, options)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                self._post_with_fallback, model, messages, tools, stream, options
            )
            try:
                return future.result(timeout=deadline)
            except TimeoutError:
                future.cancel()
                raise OllamaTimeoutError(
                    f"Ollama did not respond within {deadline}s"
                )