# Adding Tool Support to an Ollama Model

Some local models chat fine but Ollama refuses to use them with tools. LumaKit
shows an error like this:

```
Ollama rejected the request for dolphin3 (HTTP 400): ... does not support tools
```

This guide explains why that happens and how to build a tool-capable copy of
the model yourself. The repo ships a working `Modelfile` for Dolphin 3 that you
can use as-is or as a starting point for other ChatML models.

## Why Ollama says "does not support tools"

Ollama does not inspect the model weights to decide what it can do. It reads
the model's **chat template**. If the template references `.Tools` and
`.ToolCalls`, Ollama marks the model as tool-capable. If it does not, every
request that includes tools is rejected with HTTP 400.

You can check any model with:

```
ollama show <model>
```

Look at the `Capabilities` section. A tool-capable model lists both
`completion` and `tools`. A chat-only model lists just `completion`.

So the fix is not a different download. It is a new template.

## Step 1: Confirm the base model is installed

```
ollama list
```

You should see the base model, for example `dolphin3:latest`. If not, pull it:

```
ollama pull dolphin3
```

## Step 2: Look at the base model's template

```
ollama show dolphin3 --template
```

This tells you which prompt format the model expects. Dolphin 3 uses ChatML:
each turn is wrapped in `<|im_start|>role` and `<|im_end|>`. Your new template
must keep that format, or the model will produce garbage.

## Step 3: Write a Modelfile

Create a file called `Modelfile` (no extension). The one in the repo root
looks like this:

```
FROM dolphin3:latest

TEMPLATE """{{- if or .System .Tools }}<|im_start|>system
{{ .System }}
{{- if .Tools }}

You have access to the following tools. When you need to call one, respond with a JSON object inside <tool_call></tool_call> tags, like:
<tool_call>
{"name": "<tool name>", "arguments": {<arguments>}}
</tool_call>

Available tools:
{{- range .Tools }}
{{ .Function }}
{{- end }}
{{- end }}<|im_end|>
{{ end }}
{{- range $i, $_ := .Messages }}
{{- $last := eq (len (slice $.Messages $i)) 1 -}}
{{- if eq .Role "user" }}<|im_start|>user
{{ .Content }}<|im_end|>
{{ else if eq .Role "assistant" }}<|im_start|>assistant
{{ if .Content }}{{ .Content }}
{{- else if .ToolCalls }}
{{- range .ToolCalls }}<tool_call>
{"name": "{{ .Function.Name }}", "arguments": {{ .Function.Arguments }}}
</tool_call>
{{- end }}
{{- end }}{{ if not $last }}<|im_end|>
{{ end }}
{{- else if eq .Role "tool" }}<|im_start|>tool
{{ .Content }}<|im_end|>
{{ end }}
{{- if and (ne .Role "assistant") $last }}<|im_start|>assistant
{{ end }}
{{- end }}"""

PARAMETER stop "<|im_start|>"
PARAMETER stop "<|im_end|>"
```

What each part does:

- **`FROM dolphin3:latest`** reuses the weights you already have. Nothing new
  is downloaded.
- **The system block** injects every tool's JSON schema into the system prompt
  and tells the model to answer with JSON inside `<tool_call>` tags. This is the
  Hermes-style format that Dolphin 3 and many other open models were trained on.
- **The `.Messages` loop** renders the conversation. User turns pass through.
  Assistant turns that contained tool calls are rendered back as `<tool_call>`
  blocks so the model sees its own history. Tool results arrive as a `tool`
  role turn.
- **The `.ToolCalls` reference** is what flips the `tools` capability on.
- **The stop tokens** keep the model from running past the end of its turn.
  Ollama does not always carry these over from the base model, so set them.

Ollama parses the `<tool_call>` JSON in the model's output and returns it as a
structured `tool_calls` field, which is what LumaKit reads.

## Step 4: Build the model

From the folder that contains the Modelfile:

```
ollama create dolphin3-tools -f Modelfile
```

You should see `using existing layer` lines followed by `success`. The build
takes a few seconds because it only writes a new manifest.

Watch the spelling of the name. Ollama will happily create `dophin3-tools` if
that is what you typed, and LumaKit will then get a 404 when it asks for
`dolphin3-tools`.

## Step 5: Verify

```
ollama show dolphin3-tools
```

`Capabilities` should now list `tools`. If it does not, the template is
missing a `.Tools` or `.ToolCalls` reference.

Then send a real tool request and check that the reply has a `tool_calls`
field instead of plain text:

```
curl http://localhost:11434/api/chat -d '{"model":"dolphin3-tools","stream":false,"messages":[{"role":"user","content":"What is the weather in Boston?"}],"tools":[{"type":"function","function":{"name":"get_weather","description":"Get weather for a city","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]}'
```

A good response looks like:

```json
"tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Boston"}}}]
```

## Step 6: Point LumaKit at it

Pick `dolphin3-tools` from the model dropdown in the web UI, or set it with
`/model dolphin3-tools` on the CLI or Telegram. No restart is needed. LumaKit
asks Ollama for the model on every request.

## Rebuilding or renaming

To start over:

```
ollama rm dolphin3-tools
ollama create dolphin3-tools -f Modelfile
```

To adapt this for another model, change the `FROM` line, then check that
model's template with `ollama show <model> --template` and match its turn
markers. A Llama 3 model uses `<|start_header_id|>` markers, not ChatML, so the
template body would need to change.

## Limits

Adding a template does not teach a model to call tools. It only tells Ollama
to let it try. An 8B model that was not trained heavily on function calling
will sometimes write the JSON as plain text, skip the tags, or invent a tool
name. If that happens in LumaKit, the model is the limit, not the Modelfile.
For the most reliable tool loops, use a model that already lists `tools` in
`ollama show` without any changes.
