import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import requests


class OllamaTimeoutError(Exception):
    """Raised when an Ollama request exceeds its deadline."""


class OllamaClient:
    def __init__(self, base_url="http://localhost:11434", request_timeout=120):
        self.base_url = base_url.rstrip("/")
        self.request_timeout = request_timeout

    def _post(self, model, messages, tools, stream):
        url = f"{self.base_url}/api/chat"
        payload = {"model": model, "messages": messages, "stream": stream}
        if tools:
            payload["tools"] = tools
        response = requests.post(url, json=payload, timeout=self.request_timeout)
        response.raise_for_status()
        return response.json()

    def chat(self, model, messages, tools=None, stream=False, deadline=None):
        """Send a chat request. If *deadline* (seconds) is set, raise
        OllamaTimeoutError when the call takes longer than that."""
        if deadline is None:
            return self._post(model, messages, tools, stream)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._post, model, messages, tools, stream)
            try:
                return future.result(timeout=deadline)
            except TimeoutError:
                future.cancel()
                raise OllamaTimeoutError(
                    f"Ollama did not respond within {deadline}s"
                )