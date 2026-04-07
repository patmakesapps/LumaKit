# Title

Add `http_request` web tool

# Suggested labels

`tooling`, `web-tool`

# Body

## Problem

The current web tools cover search and basic URL fetches, but there is no generic API-capable HTTP tool. That makes it hard for the agent to interact with JSON APIs, authenticated endpoints, or non-HTML resources.

## Proposed tool

- Tool name: `http_request`
- Namespace: `web`
- File: `tools/web/http_request.py`

## Required structure

The module should export `get_http_request_tool()` and return the standard tool dict with `name`, `description`, `inputSchema`, and `execute`. Follow [`CONTRIBUTING.md`](../../CONTRIBUTING.md) for the current loader contract.

## Suggested schema

- `url` (string, required)
- `method` (string, optional, default `GET`)
- `headers` (object, optional)
- `query` (object, optional)
- `body` (string, optional)
- `body_json` (object, optional)
- `timeout` (number, optional, default `10`)

## Implementation notes

- Keep the implementation consistent with the current `urllib` approach used in `tools/web/`.
- Validate that the URL starts with `http://` or `https://`.
- Normalize the method to uppercase.
- If `body_json` is provided, serialize it to JSON and set `Content-Type: application/json` unless the caller already supplied a content type.
- Return a structured payload such as:
  - `url`
  - `method`
  - `status_code`
  - `headers`
  - `content_type`
  - `text`
  - `json`
- Parse JSON responses when possible, but still include the raw text body when useful.
- Return clear error payloads for timeouts, URL errors, and non-2xx responses.

## Acceptance criteria

- The new file lives at `tools/web/http_request.py`
- `python main.py` prints `http_request` in the available tool list
- GET requests work against public JSON endpoints
- POST requests work when a JSON body is provided
- The return shape is predictable enough for LLM consumption

## Out of scope

- Streaming responses
- Multipart file uploads
- Cookie/session persistence across calls
