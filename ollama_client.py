import threading

import requests


class OllamaTimeoutError(Exception):
    """Raised when an Ollama request exceeds its deadline."""


class OllamaConnectionError(Exception):
    """Raised when Ollama server is unreachable after all retries."""


class OllamaInterruptedError(Exception):
    """Raised when the user interrupts an in-flight Ollama request."""


class OllamaClient:
    def __init__(self, base_url="http://localhost:11434", request_timeout=120,
                 fallback_model=None):
        self.base_url = base_url.rstrip("/")
        self.request_timeout = request_timeout
        self.fallback_model = fallback_model
        # Tracks which model was actually used in the last call
        self.last_model_used = None

    def _post(self, model, messages, tools, stream, options=None, request_timeout=None):
        url = f"{self.base_url}/api/chat"
        payload = {"model": model, "messages": messages, "stream": stream}
        if tools:
            payload["tools"] = tools
        if options:
            payload["options"] = options
        timeout = request_timeout if request_timeout is not None else self.request_timeout
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()

    def _post_with_fallback(self, model, messages, tools, stream, options=None,
                            request_timeout=None):
        """Try primary model, falling back on timeout/connection failure."""
        timeout = request_timeout if request_timeout is not None else self.request_timeout
        try:
            result = self._post(
                model, messages, tools, stream, options, request_timeout=timeout
            )
            return result, model
        except requests.Timeout as e:
            if not self.fallback_model or self.fallback_model == model:
                raise OllamaTimeoutError(
                    f"Ollama did not respond within {timeout}s"
                ) from e
            try:
                result = self._post(
                    self.fallback_model, messages, tools, stream, options,
                    request_timeout=timeout,
                )
                return result, self.fallback_model
            except requests.Timeout as fallback_error:
                raise OllamaTimeoutError(
                    f"Ollama did not respond within {timeout}s for either "
                    f"primary ({model}) or fallback ({self.fallback_model})."
                ) from fallback_error
            except (requests.ConnectionError, ConnectionRefusedError, OSError):
                raise OllamaConnectionError(
                    f"Cannot reach Ollama server at {self.base_url}. "
                    f"Primary ({model}) timed out and fallback ({self.fallback_model}) "
                    f"could not be reached."
                ) from e
        except (requests.ConnectionError, ConnectionRefusedError, OSError) as e:
            if not self.fallback_model or self.fallback_model == model:
                raise OllamaConnectionError(
                    f"Cannot reach Ollama server at {self.base_url}. "
                    f"Make sure Ollama is running."
                ) from e
            # Try fallback
            try:
                result = self._post(
                    self.fallback_model, messages, tools, stream, options,
                    request_timeout=timeout,
                )
                return result, self.fallback_model
            except requests.Timeout as fallback_error:
                raise OllamaTimeoutError(
                    f"Ollama did not respond within {timeout}s for fallback "
                    f"({self.fallback_model}) after primary ({model}) failed to connect."
                ) from fallback_error
            except (requests.ConnectionError, ConnectionRefusedError, OSError):
                raise OllamaConnectionError(
                    f"Cannot reach Ollama server at {self.base_url}. "
                    f"Both primary ({model}) and fallback ({self.fallback_model}) failed."
                ) from e

    def chat(self, model, messages, tools=None, stream=False, deadline=None, options=None,
             check_interrupt=None):
        """Send a chat request. If *deadline* (seconds) is set, raise
        OllamaTimeoutError when the call takes longer than that."""
        self.last_model_used = None
        timeout = self.request_timeout if deadline is None else max(1, float(deadline))
        if not check_interrupt:
            result, used_model = self._post_with_fallback(
                model, messages, tools, stream, options, request_timeout=timeout
            )
            self.last_model_used = used_model
            return result

        outcome = {}
        done = threading.Event()

        def _run_request():
            try:
                result, used_model = self._post_with_fallback(
                    model, messages, tools, stream, options,
                    request_timeout=timeout,
                )
                outcome["result"] = result
                outcome["used_model"] = used_model
            except Exception as exc:
                outcome["error"] = exc
            finally:
                done.set()

        thread = threading.Thread(target=_run_request, daemon=True)
        thread.start()

        while not done.wait(0.1):
            try:
                if check_interrupt():
                    raise OllamaInterruptedError("Interrupted by /stop.")
            except OllamaInterruptedError:
                raise
            except Exception:
                continue

        if "error" in outcome:
            raise outcome["error"]

        self.last_model_used = outcome.get("used_model")
        return outcome["result"]

    def tags(self, request_timeout=None):
        url = f"{self.base_url}/api/tags"
        timeout = request_timeout if request_timeout is not None else self.request_timeout
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json()
