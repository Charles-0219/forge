"""Local Forge client. It calls the HTTP API and does not keep a second transcript."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forge")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--api-key", default="")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve")
    serve.add_argument("--db", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--scope", required=True)

    clear = sub.add_parser("clear")
    clear.add_argument("--cwd", required=True)
    clear.add_argument("--model", default="deepseek-flash")
    clear.add_argument("--protocol", default="openai")

    resume = sub.add_parser("resume")
    resume.add_argument("conversation_id")

    send = sub.add_parser("send")
    send.add_argument("--conversation", required=True)
    send.add_argument("--body", required=True)
    send.add_argument("--request-id", required=True)

    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    if args.command == "resume":
        print(args.conversation_id)
        return 0
    if args.command == "clear":
        status, payload = _request(
            "POST",
            args.base_url + "/v1/conversations",
            args.api_key,
            {
                "model_ref": args.model,
                "protocol": args.protocol,
                "cwd": args.cwd,
                "mode": "default",
                "confirm_strategy": "confirm_risky",
            },
        )
    else:
        status, payload = _request(
            "POST",
            args.base_url + "/v1/conversations/" + args.conversation + "/messages",
            args.api_key,
            {"body": args.body, "client_request_id": args.request_id},
        )
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if status < 400 else 1


def _serve(args) -> int:
    if args.host != "127.0.0.1":
        print("Forge only listens on 127.0.0.1")
        return 2
    import uvicorn

    from forge.api import create_app
    from forge.coordinator import Coordinator
    from forge.kernel import Kernel
    from forge.skills import SkillService
    from forge.sqlite_store import SqliteEventStore

    store = SqliteEventStore(args.db)
    kernel = Kernel(store)
    app = create_app(
        kernel,
        Coordinator(kernel),
        SkillService(kernel, scope=args.scope),
        api_key=args.api_key,
        manage_store=True,
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _request(method: str, url: str, api_key: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"detail": raw}
        return exc.code, body


if __name__ == "__main__":
    raise SystemExit(main())
