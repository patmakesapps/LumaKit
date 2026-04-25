"""Autonomous task runner.

A background thread that wakes up every `interval` seconds, looks for tasks
whose next_run_at has passed, and advances them one step at a time.

Lifecycle of a task
-------------------
1. created  → status='planning', next_run_at=now
2. runner picks it up → generates a JSON plan via LLM → status='active'
3. runner picks up each step in order:
   - runs a dedicated LLM loop with tools for that step
   - evaluates the output (success / partial / blocked / done)
   - advances step, reschedules, or escalates
4. when all steps done (or evaluation says goal_met) → final report → status='done'
5. if due_at passes before completion → force final report → status='done'|'failed'
6. if evaluation says 'blocked' → notify owner, status='blocked', wait for reply
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from core import task_store
from ollama_client import OllamaClient


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_PLAN_PROMPT = """You are an autonomous planning agent. A user has given you a goal to accomplish.
Break the goal into concrete, ordered steps that another agent can execute one at a time.

Goal: {goal}
Constraints: {constraints}
Due date: {due_at}
Current date/time: {now}

Respond with ONLY a JSON array of step objects. Each step must have these keys:
- "description": what to do in this step (be specific and actionable)
- "success_criteria": how to know the step succeeded
- "check_in_minutes": how many minutes before the runner should check back after this step

Example format:
[
  {{
    "description": "Search for current arbitrage opportunities between X and Y",
    "success_criteria": "Have at least 3 options ranked by profit potential",
    "check_in_minutes": 30
  }}
]

Keep steps focused. Aim for 3-8 steps total. Output ONLY the JSON array, no other text."""


_STEP_PROMPT = """You are an autonomous agent executing one step of a larger goal.

Overall goal: {goal}
Constraints: {constraints}
Due date: {due_at}

Current step ({step_num} of {total_steps}):
{step_description}

Success criteria: {success_criteria}

History of completed steps so far:
{history_summary}

Execute this step now using your available tools. Be thorough but focused.
When done, summarize exactly what you found/did and whether the success criteria were met."""


_EVAL_PROMPT = """You are evaluating whether an agent step succeeded.

Step description: {step_description}
Success criteria: {success_criteria}
Agent output: {step_output}

Respond with ONLY a JSON object with these keys:
- "verdict": one of "success", "partial", "failed", "blocked", "goal_met"
  - "success": step criteria met, move on
  - "partial": some progress but not fully done, retry with different approach
  - "failed": step failed completely, move on anyway
  - "blocked": agent needs human input to continue (explain in reason)
  - "goal_met": the overall goal has been achieved, no more steps needed
- "reason": one sentence explaining the verdict
- "summary": 1-2 sentence summary of what was actually accomplished (for history)

Output ONLY the JSON object."""


_REPORT_PROMPT = """You are writing a final report for an autonomous task.

Goal: {goal}
Constraints: {constraints}
Status: {status}
Steps completed: {steps_done} of {total_steps}

Full history:
{history_full}

Write a concise report (3-8 sentences) covering:
1. What was accomplished
2. Key findings or results
3. What wasn't finished and why (if applicable)
4. Any recommended next steps

Write in plain, direct language. No bullet points, no headers."""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class TaskRunner:
    """Background thread that advances autonomous tasks."""

    # How many times to retry a failed step before moving on
    MAX_STEP_RETRIES = 2
    # How many times to retry a failed planning pass
    MAX_PLAN_RETRIES = 2
    # Hard cap on LLM rounds per step execution
    MAX_TOOL_ROUNDS = 8
    # Seconds per LLM call in a step
    STEP_LLM_DEADLINE = 180
    # Token budget for structured LLM calls (planning, evaluation, report)
    STRUCTURED_NUM_PREDICT = 4096

    def __init__(
        self,
        interval: int = 60,
        notify: Callable[[str, str | None], None] | None = None,
    ):
        """
        interval: poll interval in seconds
        notify: callable(message, chat_id) — sends a message to a Telegram user.
                chat_id=None means owner.
        """
        self._interval = interval
        self._notify = notify or (lambda msg, cid: print(f"[task] {msg}"))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()   # serializes LLM calls from this runner

        # Lazy — built on first use so we don't slow down startup
        self._ollama: OllamaClient | None = None
        self._registry = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="task-runner")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                print(f"[task-runner] tick error: {e}")
            self._stop.wait(self._interval)

    def _tick(self) -> None:
        # Check overdue tasks first (past due_at, not yet finalized)
        for task in task_store.get_overdue_tasks():
            with self._lock:
                self._finalize_overdue(task)

        # Then process tasks whose next_run_at has passed
        for task in task_store.get_due_tasks():
            with self._lock:
                if task["status"] == "planning":
                    self._run_planning(task)
                elif task["status"] == "active":
                    self._run_step(task)

    # ------------------------------------------------------------------
    # Planning pass
    # ------------------------------------------------------------------

    def _run_planning(self, task: dict) -> None:
        task_id = task["id"]
        print(f"[task-runner] planning task {task_id}: {task['title']}")

        try:
            constraints_str = json.dumps(task["constraints"]) if task["constraints"] else "none"
            prompt = _PLAN_PROMPT.format(
                goal=task["goal"],
                constraints=constraints_str,
                due_at=task.get("due_at") or "no hard deadline",
                now=datetime.now().strftime("%Y-%m-%d %H:%M"),
            )

            plan = None
            last_err = None
            for attempt in range(self.MAX_PLAN_RETRIES + 1):
                try:
                    raw = self._llm_call(prompt)
                    parsed = self._parse_json(raw)
                    if isinstance(parsed, list) and parsed:
                        plan = parsed
                        break
                    last_err = ValueError(f"LLM returned invalid plan: {raw[:200]}")
                except (ValueError, json.JSONDecodeError) as e:
                    last_err = e
                    if attempt < self.MAX_PLAN_RETRIES:
                        print(f"[task-runner] plan parse failed (attempt {attempt+1}), retrying: {e}")
                        continue

            if plan is None:
                raise last_err or ValueError("Planning failed after retries")

            # Ensure each step has required fields
            for i, step in enumerate(plan):
                step.setdefault("description", f"Step {i+1}")
                step.setdefault("success_criteria", "complete")
                step.setdefault("check_in_minutes", 60)
                step["index"] = i
                step["status"] = "pending"
                step["output"] = ""
                step["retries"] = 0

            next_run = datetime.now().isoformat()
            task_store.set_plan(task_id, plan, next_run_at=next_run)
            task_store.append_history(task_id, {
                "type": "plan_generated",
                "step_count": len(plan),
                "steps": [s["description"] for s in plan],
            })

            msg = (
                f"Task planned: {task['title']}\n\n"
                + "\n".join(f"{i+1}. {s['description']}" for i, s in enumerate(plan))
                + f"\n\nStarting now. I'll check in as I go."
            )
            self._notify(msg, task.get("owner_chat_id"))
            print(f"[task-runner] task {task_id} plan ready ({len(plan)} steps)")

        except Exception as e:
            print(f"[task-runner] planning failed for task {task_id}: {e}")
            task_store.fail_task(task_id, f"Planning failed: {e}")
            self._notify(
                f"Task failed during planning: {task['title']}\n\nError: {e}",
                task.get("owner_chat_id"),
            )

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------

    def _run_step(self, task: dict) -> None:
        task_id = task["id"]
        plan = task["plan"]
        step_idx = task["current_step"]

        # All steps done?
        if step_idx >= len(plan):
            self._generate_and_send_report(task, forced=False)
            return

        step = plan[step_idx]
        print(f"[task-runner] task {task_id} step {step_idx+1}/{len(plan)}: {step['description'][:60]}")

        history_summary = self._format_history_summary(task["history"])
        constraints_str = json.dumps(task["constraints"]) if task["constraints"] else "none"

        step_prompt = _STEP_PROMPT.format(
            goal=task["goal"],
            constraints=constraints_str,
            due_at=task.get("due_at") or "no hard deadline",
            step_num=step_idx + 1,
            total_steps=len(plan),
            step_description=step["description"],
            success_criteria=step["success_criteria"],
            history_summary=history_summary or "No prior steps.",
        )

        try:
            step_output = self._run_agentic_step(step_prompt)
        except Exception as e:
            step_output = f"Step execution error: {e}"
            print(f"[task-runner] step execution exception: {e}")

        # Evaluate the output
        verdict_data = self._evaluate_step(step, step_output)
        verdict = verdict_data.get("verdict", "failed")
        summary = verdict_data.get("summary", step_output[:200])
        reason = verdict_data.get("reason", "")

        print(f"[task-runner] task {task_id} step {step_idx+1} verdict: {verdict} — {reason}")

        # Record in history
        task_store.append_history(task_id, {
            "type": "step_result",
            "step_index": step_idx,
            "description": step["description"],
            "verdict": verdict,
            "summary": summary,
        })

        if verdict == "goal_met":
            self._generate_and_send_report(task, forced=False, extra_summary=summary)
            return

        if verdict == "blocked":
            task_store.update_task(task_id, status="blocked")
            self._notify(
                f"Task blocked: {task['title']}\n\n"
                f"Stuck on step {step_idx+1}: {step['description']}\n\n"
                f"Reason: {reason}\n\n"
                f"Reply with instructions and I'll resume.",
                task.get("owner_chat_id"),
            )
            return

        if verdict in ("success", "failed"):
            # Advance to next step
            retries = step.get("retries", 0)
            if verdict == "failed" and retries < self.MAX_STEP_RETRIES:
                # Retry the same step
                plan[step_idx]["retries"] = retries + 1
                task_store.update_task(task_id, plan=json.dumps(plan))
                mins = max(5, step.get("check_in_minutes", 30) // 2)
                next_run = (datetime.now() + timedelta(minutes=mins)).isoformat()
                task_store.update_task(task_id, next_run_at=next_run)
                self._notify(
                    f"Step {step_idx+1} needs a retry (attempt {retries+2}): {step['description'][:80]}",
                    task.get("owner_chat_id"),
                )
            else:
                # Move forward
                mins = step.get("check_in_minutes", 60)
                next_run = (datetime.now() + timedelta(minutes=mins)).isoformat()
                task_store.advance_step(task_id, next_run_at=next_run)

                remaining = len(plan) - (step_idx + 1)
                if remaining > 0:
                    self._notify(
                        f"Step {step_idx+1} done: {summary}\n\n"
                        f"Next: {plan[step_idx+1]['description'][:80]}\n"
                        f"({remaining} step{'s' if remaining != 1 else ''} remaining)",
                        task.get("owner_chat_id"),
                    )
                else:
                    # That was the last step — generate report on next tick
                    task_store.update_task(task_id, next_run_at=datetime.now().isoformat())

        elif verdict == "partial":
            # Keep same step, give it more time
            mins = step.get("check_in_minutes", 60)
            next_run = (datetime.now() + timedelta(minutes=mins)).isoformat()
            task_store.update_task(task_id, next_run_at=next_run)
            self._notify(
                f"Step {step_idx+1} partially done, continuing: {summary[:120]}",
                task.get("owner_chat_id"),
            )

    # ------------------------------------------------------------------
    # Agentic step execution (LLM + tools loop)
    # ------------------------------------------------------------------

    def _run_agentic_step(self, prompt: str) -> str:
        """Run a mini agent loop for one step. Returns the final text output."""
        registry = self._get_registry()
        ollama = self._get_ollama()

        tools_for_llm = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": registry.get(t["name"])["inputSchema"],
                },
            }
            for t in registry.list()
        ]

        lumi_email = os.getenv("LUMI_EMAIL_ADDRESS", "").strip()
        identity_parts = []
        if lumi_email:
            identity_parts.append(f"Your email address: {lumi_email}")
        from core.paths import get_data_dir
        identity_file = get_data_dir() / "identity" / "identity.txt"
        if identity_file.exists():
            identity_parts.append(
                f"Your identity file (existing accounts & credentials): {identity_file} — "
                "read it before signing up for anything new, and append new accounts after creating them."
            )
        identity_block = "\n".join(identity_parts) + "\n\n" if identity_parts else ""

        system = (
            "You are Lumi, an autonomous agent executing a specific task step. "
            "Use your tools to complete the step described. "
            "Be thorough — search the web, fetch pages, run code as needed.\n\n"
            f"{identity_block}"
            "When done, write a clear summary of what you found and did."
        )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

        model = os.getenv("OLLAMA_MODEL")

        for _ in range(self.MAX_TOOL_ROUNDS):
            try:
                response = ollama.chat(
                    model=model,
                    messages=messages,
                    tools=tools_for_llm,
                    stream=False,
                    deadline=self.STEP_LLM_DEADLINE,
                    options={"num_predict": self.STRUCTURED_NUM_PREDICT},
                    priority="background",
                )
            except Exception as e:
                return f"LLM error during step: {e}"

            message = response.get("message", {})
            messages.append(message)
            tool_calls = message.get("tool_calls", [])

            if not tool_calls:
                return (message.get("content") or "").strip() or "Step completed (no output)."

            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name")
                inputs = fn.get("arguments", {})
                result = registry.execute(name, inputs)
                messages.append({
                    "role": "tool",
                    "name": name,
                    "content": json.dumps(result),
                })

        # Hit round cap — ask for a summary
        messages.append({"role": "user", "content": "Summarize what you've found so far."})
        try:
            final = ollama.chat(model=model, messages=messages, stream=False,
                                deadline=self.STEP_LLM_DEADLINE,
                                priority="background")
            return (final.get("message", {}).get("content") or "").strip()
        except Exception:
            return "Step hit round limit."

    # ------------------------------------------------------------------
    # Evaluation pass
    # ------------------------------------------------------------------

    def _evaluate_step(self, step: dict, output: str) -> dict:
        prompt = _EVAL_PROMPT.format(
            step_description=step["description"],
            success_criteria=step["success_criteria"],
            step_output=output[:3000],
        )
        try:
            raw = self._llm_call(prompt)
            return self._parse_json(raw)
        except Exception as e:
            print(f"[task-runner] evaluation parse error: {e}")
            return {"verdict": "success", "reason": "eval failed, assuming success", "summary": output[:200]}

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _generate_and_send_report(
        self, task: dict, forced: bool = False, extra_summary: str = ""
    ) -> None:
        task_id = task["id"]
        plan = task["plan"]
        history = task["history"]
        steps_done = sum(1 for h in history if h.get("type") == "step_result")
        constraints_str = json.dumps(task["constraints"]) if task["constraints"] else "none"

        history_full = "\n".join(
            f"[{h.get('type')}] {h.get('summary') or h.get('steps') or ''}"
            for h in history
        )
        if extra_summary:
            history_full += f"\n[final] {extra_summary}"

        status = "forcibly completed (deadline passed)" if forced else "completed"

        prompt = _REPORT_PROMPT.format(
            goal=task["goal"],
            constraints=constraints_str,
            status=status,
            steps_done=steps_done,
            total_steps=len(plan),
            history_full=history_full or "No steps were recorded.",
        )

        try:
            report = self._llm_call(prompt)
        except Exception as e:
            report = f"Could not generate report: {e}\n\nSteps completed: {steps_done}/{len(plan)}"

        final_status = "done"
        task_store.complete_task(task_id, report)

        header = "Task complete" if not forced else "Task deadline reached"
        self._notify(
            f"{header}: {task['title']}\n\n{report}",
            task.get("owner_chat_id"),
        )
        print(f"[task-runner] task {task_id} {final_status}")

    def _finalize_overdue(self, task: dict) -> None:
        print(f"[task-runner] finalizing overdue task {task['id']}: {task['title']}")
        self._generate_and_send_report(task, forced=True)

    # ------------------------------------------------------------------
    # LLM helpers
    # ------------------------------------------------------------------

    def _llm_call(self, prompt: str) -> str:
        """Single-shot LLM call with no tools."""
        ollama = self._get_ollama()
        model = os.getenv("OLLAMA_MODEL")
        response = ollama.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
            deadline=90,
            options={"num_predict": self.STRUCTURED_NUM_PREDICT},
            priority="background",
        )
        return (response.get("message", {}).get("content") or "").strip()

    def _parse_json(self, text: str) -> dict | list:
        """Extract and parse the first JSON object or array from text."""
        text = text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(
                l for l in lines if not l.startswith("```")
            ).strip()
        # Use raw_decode to find and parse the first valid JSON structure —
        # avoids bracket-counting bugs with braces/brackets inside strings.
        # Try whichever of { or [ appears first in the text.
        decoder = json.JSONDecoder()
        candidates = [(text.find(c), c) for c in ("{", "[") if text.find(c) != -1]
        for idx, _ in sorted(candidates):
            try:
                obj, _ = decoder.raw_decode(text, idx)
                return obj
            except json.JSONDecodeError:
                continue
        raise ValueError(f"No JSON found in: {text[:200]}")

    # ------------------------------------------------------------------
    # Lazy init
    # ------------------------------------------------------------------

    def _get_ollama(self) -> OllamaClient:
        if self._ollama is None:
            self._ollama = OllamaClient(
                fallback_model=os.getenv("OLLAMA_FALLBACK_MODEL")
            )
        return self._ollama

    def _get_registry(self):
        if self._registry is None:
            from tool_registry import ToolRegistry
            self._registry = ToolRegistry()
            self._registry.load_tools_from_folder(skip_dirs={"code_intel"})
        return self._registry

    # ------------------------------------------------------------------
    # Format helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_history_summary(history: list) -> str:
        lines = []
        for h in history:
            if h.get("type") == "step_result":
                lines.append(
                    f"Step {h.get('step_index', '?')+1} [{h.get('verdict', '?')}]: "
                    f"{h.get('summary', '')}"
                )
        return "\n".join(lines)
