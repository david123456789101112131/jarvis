"""A stand-in for llama.cpp's `llama-server`, for the chat model's tests: the same command line, /health and an
OpenAI-compatible /v1/chat/completions that checks the API key. It answers from what it is asked:

  a request with tools        -> one tool call to the first tool, with {"city": "ירושלים"}
  a json_schema reply format  -> {"steps": ["a", "b"]} (a chat turn's schema: a two-sentence reply), or a 400 about
                                 the grammar when FAKE_LLAMA_NO_SCHEMA is set
  anything else               -> "שלום! <the last user message>"

With "stream": true the same answer comes as server-sent events, four characters at a time.

`--help` prints the options listed in FAKE_LLAMA_FLAGS (space separated). `--list-devices` lists a graphics card
when GGML_BACKEND_PATH names a backend (as Ollama's copy needs) or FAKE_LLAMA_GPU is set, else none. Each start
appends its command line (without the key) to FAKE_LLAMA_LOG, one JSON list per line, then the backend it was given
as {"backend": ...}.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main() -> int:
    args = sys.argv[1:]
    if "--help" in args:
        print("usage: llama-server [options]\n" + "\n".join(f"{f}  something" for f in
                                                           os.environ.get("FAKE_LLAMA_FLAGS", "").split()))
        return 0
    backend = os.environ.get("GGML_BACKEND_PATH", "")
    if "--list-devices" in args:
        gpu = backend or os.environ.get("FAKE_LLAMA_GPU")
        print("Available devices:\n" + ("  CUDA0: Fake GPU (16000 MiB, 15000 MiB free)" if gpu else ""))
        return 0
    port = int(args[args.index("--port") + 1])
    key = args[args.index("--api-key") + 1]
    if os.environ.get("FAKE_LLAMA_LOG"):
        shown = [("<key>" if a == key else a) for a in args]
        with open(os.environ["FAKE_LLAMA_LOG"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(shown) + "\n")
            if backend:
                fh.write(json.dumps({"backend": backend}) + "\n")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            if self.headers.get("Authorization") != f"Bearer {key}":
                self._send(401, {"error": {"message": "Invalid API Key", "type": "authentication_error"}})
                return False
            return True

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send(200, {"status": "ok"})
            else:
                self._send(404, {"error": {"message": "not found"}})

        def do_POST(self) -> None:
            if not self._authorized():
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            message: dict = {"role": "assistant", "content": None}
            finish = "stop"
            fmt = body.get("response_format") or {}
            if body.get("tools"):
                name = body["tools"][0]["function"]["name"]
                message["tool_calls"] = [{"id": "call_1", "type": "function",
                                          "function": {"name": name, "arguments": '{"city": "ירושלים"}'}}]
                finish = "tool_calls"
            elif fmt.get("type") == "json_schema":
                if os.environ.get("FAKE_LLAMA_NO_SCHEMA"):
                    self._send(400, {"error": {"message": "failed to parse grammar from the JSON schema",
                                               "type": "invalid_request_error"}})
                    return
                props = ((fmt.get("json_schema") or {}).get("schema") or {}).get("properties") or {}
                if "reply" in props:                       # JARVIS's chat turn: answer it in two sentences
                    last = [m for m in body["messages"] if m["role"] == "user"][-1]["content"]
                    message["content"] = json.dumps({"run_task": False, "reply": f"שלום! {last}. זה המשפט השני.",
                                                     "task": "", "agent": ""}, ensure_ascii=False)
                else:
                    message["content"] = json.dumps({"steps": ["a", "b"]})
            else:
                last = [m for m in body["messages"] if m["role"] == "user"][-1]["content"]
                message["content"] = "שלום! " + str(last)
            usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
            if body.get("stream") and not message.get("tool_calls"):
                self._stream(body, str(message["content"] or ""), finish, usage)
                return
            self._send(200, {"id": "x", "object": "chat.completion", "created": 0, "model": body.get("model", ""),
                             "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                             "usage": usage, "seen_format": fmt.get("type")})

        def _stream(self, body: dict, content: str, finish: str, usage: dict) -> None:
            """Server-sent events, a few characters at a time, as llama-server streams."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            base = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": body.get("model", "")}
            pieces = [content[i:i + 4] for i in range(0, len(content), 4)]
            events = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                                            "finish_reason": None}]}]
            events += [{**base, "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                       for piece in pieces]
            events.append({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
            if (body.get("stream_options") or {}).get("include_usage"):
                events.append({**base, "choices": [], "usage": usage})
            for item in events:
                self.wfile.write(b"data: " + json.dumps(item, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
