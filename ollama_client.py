import requests


class OllamaClient:
    def __init__(self, base_url="http://localhost:11434"):
        self.base_url = base_url.rstrip("/")

    def chat(self, model, messages, tools=None, stream=False):
        url = f"{self.base_url}/api/chat"

        payload = {
            "model": model,
            "messages": messages,
            "stream": stream
        }

        if tools:
            payload["tools"] = tools

        response = requests.post(url, json=payload, timeout=120)
        response.raise_for_status()

        return response.json()