# LumaKit

LumaKit is a small local CLI agent that talks to an Ollama model and gives that model access to repo, runtime, and web tools.

## What It Does

- Loads tools automatically from the `tools/` directory
- Sends chat requests to a local Ollama server
- Lets the model call tools in multiple rounds before returning a final answer
- Keeps a short conversation history so the CLI stays lightweight

## Model Note

Tool calling quality depends heavily on the model you run through Ollama. Smaller models may answer basic prompts fine, but they can be less reliable when choosing certain tools, formatting tool arguments, or handling multi-step tool-call loops. If a tool seems inconsistent, test with a stronger model before assuming the tool implementation is broken.

## Requirements

- Python 3.10+
- Ollama running locally at `http://localhost:11434`
- An Ollama model pulled locally

Install dependencies:

```bash
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and set the values you want to use.

Current environment variables:

- `OLLAMA_MODEL`: model used for chat requests.
- `SERPAPI_KEY`: optional, used by premium web search tooling.

## Run It

Start the CLI:

```bash
python main.py
```

Verbose mode:

```bash
python main.py --verbose
```

Type `exit` or `quit` to leave the session.

## Tools

Tools are auto-registered from `tools/**/*.py`. The current layout is:

- `tools/repo/` for file and repository operations
- `tools/runtime/` for shell, Python, and system helpers
- `tools/web/` for HTTP and search features

To add a new tool, follow the guidance in [CONTRIBUTING.md](CONTRIBUTING.md).


