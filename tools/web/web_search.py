import json
import os
import re
import urllib.parse
import urllib.request


def get_web_search_tool():
    return {
        "name": "web_search",
        "description": "Searches the web and returns top results.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "num_results": {
                    "type": "number",
                    "description": "Number of results to return (default 5)",
                },
            },
            "required": ["query"],
        },
        "execute": _web_search,
    }


def _web_search(inputs):
    query = inputs["query"]
    num_results = int(inputs.get("num_results", 5))
    api_key = os.getenv("SERPAPI_KEY")

    if api_key:
        return _search_serpapi(query, num_results, api_key)
    return _search_duckduckgo(query, num_results)


def _search_serpapi(query, num_results, api_key):
    params = {"q": query, "api_key": api_key, "num": num_results}
    query_string = "&".join(
        f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items()
    )
    url = f"https://serpapi.com/search?{query_string}"

    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))

        results = []
        for result in data.get("organic_results", [])[:num_results]:
            results.append(
                {
                    "title": result.get("title", ""),
                    "link": result.get("link", ""),
                    "snippet": result.get("snippet", ""),
                }
            )
        return {"query": query, "results": results}
    except Exception as error:
        return {"error": str(error), "query": query}


def _search_duckduckgo(query, num_results):
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode("utf-8", errors="replace")

        results = []
        for match in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
            r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            html,
            re.DOTALL,
        ):
            if len(results) >= num_results:
                break
            link = match.group(1)
            # DDG wraps links in a redirect — extract the actual URL
            actual = re.search(r"uddg=([^&]+)", link)
            if actual:
                link = urllib.parse.unquote(actual.group(1))
            results.append(
                {
                    "title": re.sub(r"<[^>]+>", "", match.group(2)).strip(),
                    "link": link,
                    "snippet": re.sub(r"<[^>]+>", "", match.group(3)).strip(),
                }
            )

        return {"query": query, "results": results, "source": "duckduckgo"}
    except Exception as error:
        return {"error": str(error), "query": query}
