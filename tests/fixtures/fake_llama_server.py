"""A stand-in for llama.cpp's `llama-server`, for the chat model's tests: the same command line, /health and an
OpenAI-compatible /v1/chat/completions that checks the API key. It answers from what it is asked:

  a request with tools        -> one tool call to the first tool, with {"city": "ירושלים"}
  a json_schema reply format  -> {"steps": ["a", "b"]}, or a 400 about the grammar when FAKE_LLAMA_NO_SCHEMA is set
  anything else               -> "שלום! <the last user message>"

`--help` prints the options listed in FAKE_LLAMA_FLAGS (space separated). Each start appends its command line
(without the key) to FAKE_LLAMA_LOG, one JSON list per line.
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
    port = int(args[args.index("--port") + 1])
    key = args[args.index("--api-key") + 1]
    if os.environ.get("FAKE_LLAMA_LOG"):
        shown = [("<key>" if a == key else a) for a in args]
        with open(os.environ["FAKE_LLAMA_LOG"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(shown) + "\n")

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
                message["content"] = json.dumps({"steps": ["a", "b"]})
            else:
                last = [m for m in body["messages"] if m["role"] == "user"][-1]["content"]
                message["content"] = "שלום! " + str(last)
            self._send(200, {"id": "x", "object": "chat.completion", "created": 0, "model": body.get("model", ""),
                             "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                             "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                             "seen_format": fmt.get("type")})

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
