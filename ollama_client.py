import itertools
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests


# Local Ollama is much more reliable when this process sends one generation
# request at a time. Foreground chat, background tasks, heartbeat, and memory
# tooling all share the same daemon, so uncoordinated concurrent calls can
# fail or thrash model loads. Serialize chat requests process-wide and let
# callers wait their turn.
_OLLAMA_CHAT_SLOT = threading.Lock()

_PRIORITY_VALUES = {
    "foreground": 0,
    "high": 1,
    "normal": 2,
    "medium": 3,
    "background": 4,
    "low": 5,
}


class _OllamaGenerationScheduler:
    """Priority gate in front of the hard Ollama serialization lock."""

    def __init__(self):
        self._condition = threading.Condition()
        self._sequence = itertools.count()
        self._pending = []
        self._busy = False

    def _priority_value(self, priority):
        if isinstance(priority, int):
            return priority
        return _PRIORITY_VALUES.get(str(priority or "normal"), _PRIORITY_VALUES["normal"])

    def acquire(self, priority="normal", check_interrupt=None):
        request = [self._priority_value(priority), next(self._sequence)]
        with self._condition:
            self._pending.append(request)
            try:
                while True:
                    best = min(self._pending)
                    if not self._busy and request == best:
                        self._busy = True
                        self._pending.remove(request)
                        break
                    if check_interrupt:
                        try:
                            if check_interrupt():
                                raise OllamaInterruptedError("Interrupted by /stop.")
                        except OllamaInterruptedError:
                            raise
                        except Exception:
                            pass
                    self._condition.wait(timeout=0.1)
            except Exception:
                if request in self._pending:
                    self._pending.remove(request)
                    self._condition.notify_all()
                raise

        acquired_hard_slot = False
        try:
            while not acquired_hard_slot:
                acquired_hard_slot = _OLLAMA_CHAT_SLOT.acquire(timeout=0.1)
                if acquired_hard_slot:
                    return
                if check_interrupt:
                    try:
                        if check_interrupt():
                            raise OllamaInterruptedError("Interrupted by /stop.")
                    except OllamaInterruptedError:
                        raise
                    except Exception:
                        pass
        except Exception:
            with self._condition:
                self._busy = False
                self._condition.notify_all()
            raise

    def release(self):
        _OLLAMA_CHAT_SLOT.release()
        with self._condition:
            self._busy = False
            self._condition.notify_all()


_OLLAMA_GENERATION_SCHEDULER = _OllamaGenerationScheduler()


class OllamaTimeoutError(Exception):
    """Raised when an Ollama request exceeds its deadline."""


class OllamaConnectionError(Exception):
    """Raised when Ollama server is unreachable after all retries."""


class OllamaInterruptedError(Exception):
    """Raised when the user interrupts an in-flight Ollama request."""


class OllamaClient:
    CLOUD_MODEL_MIN_TIMEOUT = 240

    def __init__(self, base_url="http://localhost:11434", request_timeout=120,
                 fallback_model=None):
        self.base_url = base_url.rstrip("/")
        self.request_timeout = request_timeout
        self.fallback_model = fallback_model
        # Tracks which model was actually used in the last call
        self.last_model_used = None

    def _resolve_timeout(self, model, request_timeout=None):
        timeout = request_timeout if request_timeout is not None else self.request_timeout
        model_name = str(model or "").strip().lower()
        if model_name.endswith(":cloud"):
            return max(timeout, self.CLOUD_MODEL_MIN_TIMEOUT)
        return timeout

    # Keep non-cloud models resident in Ollama for this long between calls so
    # the next turn doesn't pay the cold-load cost. Cloud-hosted models ignore
    # this field. No tokens generated while idle — pure memory retention.
    DEFAULT_KEEP_ALIVE = "30m"

    def _acquire_chat_slot(self, check_interrupt=None, priority="normal"):
        _OLLAMA_GENERATION_SCHEDULER.acquire(
            priority=priority,
            check_interrupt=check_interrupt,
        )

    def _merge_stream_chunk(self, aggregate, payload, on_chunk=None):
        message = payload.get("message") or {}
        if message:
            target = aggregate.setdefault("message", {})
            role = message.get("role")
            if role and not target.get("role"):
                target["role"] = role
            content = message.get("content")
            if content:
                target["content"] = target.get("content", "") + content
                if on_chunk:
                    on_chunk(content)
            tool_calls = message.get("tool_calls")
            if tool_calls:
                target.setdefault("tool_calls", []).extend(tool_calls)
            for key, value in message.items():
                if key not in {"role", "content", "tool_calls"}:
                    target[key] = value
        for key, value in payload.items():
            if key != "message":
                aggregate[key] = value

    def _post(self, model, messages, tools, stream, options=None, request_timeout=None,
              on_chunk=None):
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "keep_alive": self.DEFAULT_KEEP_ALIVE,
        }
        if tools:
            payload["tools"] = tools
        if options:
            payload["options"] = options
        self._dump_payload_if_enabled(url, payload)
        timeout = self._resolve_timeout(model, request_timeout)
        response = requests.post(url, json=payload, timeout=timeout, stream=bool(stream))
        response.raise_for_status()
        if stream:
            aggregate = {}
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                self._merge_stream_chunk(aggregate, json.loads(line), on_chunk=on_chunk)
            aggregate.setdefault("message", {}).setdefault("content", "")
            return aggregate
        return response.json()

    def _dump_payload_if_enabled(self, url, payload):
        enabled = str(os.getenv("LUMAKIT_DUMP_LLM_PAYLOADS", "")).strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            return

        dump_dir = os.getenv("LUMAKIT_LLM_PAYLOAD_DIR", "").strip()
        if dump_dir:
            target_dir = Path(dump_dir).expanduser()
        else:
            from core.paths import get_data_dir

            target_dir = get_data_dir() / "llm_payloads"

        target_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        path = target_dir / f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}.json"
        body = {
            "created_at": now.isoformat(),
            "url": url,
            "payload": payload,
        }
        text = json.dumps(body, indent=2, ensure_ascii=False, default=str)
        path.write_text(text, encoding="utf-8")
        (target_dir / "latest.json").write_text(text, encoding="utf-8")

    def _post_with_fallback(self, model, messages, tools, stream, options=None,
                            request_timeout=None, on_chunk=None):
        """Try primary model, falling back on timeout/connection failure."""
        timeout = self._resolve_timeout(model, request_timeout)
        emitted_chunks = False

        def _recording_on_chunk(chunk):
            nonlocal emitted_chunks
            emitted_chunks = True
            if on_chunk:
                on_chunk(chunk)

        chunk_callback = _recording_on_chunk if on_chunk else None
        try:
            result = self._post(
                model, messages, tools, stream, options, request_timeout=timeout,
                on_chunk=chunk_callback,
            )
            return result, model
        except requests.Timeout as e:
            if emitted_chunks:
                raise OllamaTimeoutError(
                    f"Ollama stream stopped responding within {timeout}s"
                ) from e
            if not self.fallback_model or self.fallback_model == model:
                raise OllamaTimeoutError(
                    f"Ollama did not respond within {timeout}s"
                ) from e
            fallback_timeout = self._resolve_timeout(self.fallback_model, request_timeout)
            try:
                result = self._post(
                    self.fallback_model, messages, tools, stream, options,
                    request_timeout=request_timeout,
                    on_chunk=on_chunk,
                )
                return result, self.fallback_model
            except requests.Timeout as fallback_error:
                raise OllamaTimeoutError(
                    f"Ollama did not respond within {timeout}s for primary ({model}) "
                    f"or within {fallback_timeout}s for fallback ({self.fallback_model})."
                ) from fallback_error
            except (requests.ConnectionError, ConnectionRefusedError, OSError):
                raise OllamaConnectionError(
                    f"Cannot reach Ollama server at {self.base_url}. "
                    f"Primary ({model}) timed out and fallback ({self.fallback_model}) "
                    f"could not be reached."
                ) from e
        except (requests.ConnectionError, ConnectionRefusedError, OSError) as e:
            if emitted_chunks:
                raise OllamaConnectionError(
                    f"Ollama stream from primary ({model}) failed after partial output."
                ) from e
            if not self.fallback_model or self.fallback_model == model:
                raise OllamaConnectionError(
                    f"Cannot reach Ollama server at {self.base_url}. "
                    f"Make sure Ollama is running."
                ) from e
            # Try fallback
            fallback_timeout = self._resolve_timeout(self.fallback_model, request_timeout)
            try:
                result = self._post(
                    self.fallback_model, messages, tools, stream, options,
                    request_timeout=request_timeout,
                    on_chunk=on_chunk,
                )
                return result, self.fallback_model
            except requests.Timeout as fallback_error:
                raise OllamaTimeoutError(
                    f"Ollama did not respond within {fallback_timeout}s for fallback "
                    f"({self.fallback_model}) after primary ({model}) failed to connect."
                ) from fallback_error
            except (requests.ConnectionError, ConnectionRefusedError, OSError):
                raise OllamaConnectionError(
                    f"Cannot reach Ollama server at {self.base_url}. "
                    f"Both primary ({model}) and fallback ({self.fallback_model}) failed."
                ) from e

    def chat(self, model, messages, tools=None, stream=False, deadline=None, options=None,
             check_interrupt=None, priority="normal", on_chunk=None):
        """Send a chat request. If *deadline* (seconds) is set, raise
        OllamaTimeoutError when the call takes longer than that."""
        self.last_model_used = None
        timeout = self.request_timeout if deadline is None else max(1, float(deadline))
        if not check_interrupt:
            self._acquire_chat_slot(priority=priority)
            try:
                result, used_model = self._post_with_fallback(
                    model, messages, tools, stream, options, request_timeout=timeout,
                    on_chunk=on_chunk,
                )
                self.last_model_used = used_model
                return result
            finally:
                _OLLAMA_GENERATION_SCHEDULER.release()

        outcome = {}
        done = threading.Event()

        def _run_request():
            acquired = False
            try:
                self._acquire_chat_slot(
                    check_interrupt=check_interrupt,
                    priority=priority,
                )
                acquired = True
                try:
                    result, used_model = self._post_with_fallback(
                        model, messages, tools, stream, options,
                        request_timeout=timeout,
                        on_chunk=on_chunk,
                    )
                finally:
                    if acquired:
                        _OLLAMA_GENERATION_SCHEDULER.release()
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
