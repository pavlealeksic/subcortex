"""MockLLM that reports a configurable prompt-token usage (to make a TUI auto-compact)."""

from typing import Any, Dict, List

from mock_llm import MockLLM


class UsageMockLLM(MockLLM):
    prompt_tokens = 10  # reported on every Chat Completions / Messages response

    def _usage_openai(self) -> Dict[str, int]:
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": 5,
                "total_tokens": self.prompt_tokens + 5}

    def _handler(self):
        base = super()._handler()
        mock = self

        class Handler(base):
            def _json(self, code: int, body: Any) -> None:
                if isinstance(body, dict) and isinstance(body.get("usage"), dict):
                    if "prompt_tokens" in body["usage"]:
                        body = dict(body, usage=mock._usage_openai())
                    elif "input_tokens" in body["usage"]:
                        body = dict(body, usage=dict(body["usage"], input_tokens=mock.prompt_tokens))
                super()._json(code, body)

            def _sse(self, events: List[Any], anthropic: bool) -> None:
                patched = []
                for e in events:
                    if isinstance(e, dict) and not anthropic and "usage" in e:
                        e = dict(e, usage=mock._usage_openai())
                    elif isinstance(e, dict) and anthropic and e.get("type") == "message_start":
                        msg = dict(e["message"], usage=dict(e["message"]["usage"], input_tokens=mock.prompt_tokens))
                        e = dict(e, message=msg)
                    patched.append(e)
                super()._sse(patched, anthropic)

        return Handler
