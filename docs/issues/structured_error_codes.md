# Title

Add structured error codes to tool failure responses

# Suggested labels

`enhancement`, `tooling`, `good first issue`

# Body

## Problem

When a tool fails, the error message is sometimes just a raw `{"error": "..."}` string. This makes it harder for the model to recover gracefully or explain the issue to the user. Richer, structured error responses would improve reliability.

## Proposed solution

- Define a standard error response format with error codes, categories, and human-readable messages.
- Apply this format consistently across all tools.

## Implementation notes

- Create an `ErrorCode` enum or constants module in `core/errors.py`.
- Define categories like `FILE_NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_INPUT`, `TIMEOUT`, `EXTERNAL_SERVICE_ERROR`.
- Update the tool execution wrapper to catch exceptions and return structured error dicts:
  ```json
  {
    "error": true,
    "code": "FILE_NOT_FOUND",
    "message": "The file 'foo.txt' does not exist.",
    "recoverable": true,
    "suggestion": "Check the file path or use list_files to see available files."
  }
  ```
- Add a `recoverable` flag so the model knows whether to retry or give up.
- Add an optional `suggestion` field to guide the model toward a fix.

## Acceptance criteria

- All tool errors return a structured error dict with at least `code` and `message`.
- Error codes are documented.
- The model can distinguish between recoverable and non-recoverable errors.
- Existing tool behavior is not broken.

## Out of scope

- Automatic retry logic based on error codes
- Error telemetry or logging to external services
