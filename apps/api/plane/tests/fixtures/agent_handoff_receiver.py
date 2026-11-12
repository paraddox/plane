"""Local protocol demo, not an AI runtime. Never deploy this receiver publicly.

The main receiver claims a new test task and hands it off once. The independent
worker receiver reads feedback, acknowledges explicit answers and posts receipts.
Prompts/model execution are deliberately outside Plane and outside this fixture.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from queue import Queue
import sqlite3
from threading import Lock, Thread
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


class Demo:
    def __init__(self, config_path, state_dir):
        self.config = json.loads(Path(config_path).read_text())
        self.directory = Path(state_dir)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock, self.queue = Lock(), Queue()
        self.opener = build_opener(NoRedirect)
        self.database = self.directory / "receiver.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS inbox (id TEXT PRIMARY KEY, route TEXT NOT NULL, "
                "event TEXT NOT NULL, processed INTEGER NOT NULL DEFAULT 0, error TEXT)"
            )
        os.chmod(self.database, 0o600)

    def connect(self):
        return sqlite3.connect(self.database)

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.connect() as db:
            db.execute(
                "INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def api(self, role, token, path, method="GET", data=None, idempotency=None):
        counts = self.get("api_calls", {"main": 0, "worker": 0})
        counts[role] += 1
        self.put("api_calls", counts)
        headers = {"X-Api-Key": token, "Content-Type": "application/json"}
        if idempotency:
            headers["Idempotency-Key"] = idempotency
        body = json.dumps(data).encode() if data is not None else None
        request = Request(self.config["base_url"] + path, data=body, headers=headers, method=method)
        with self.opener.open(request, timeout=self.config["http_timeout"]) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    def start(self):
        if self.get("main_subscription") is None:
            subscription = self.api(
                "main",
                self.config["dispatcher_token"],
                self.config["prefix"] + "subscriptions/",
                "POST",
                {
                    "url": self.config["callback_url"] + "/main",
                    "event_types": ["task.ready"],
                },
            )
            self.put("main_subscription", subscription)
        Thread(target=self.consume, daemon=True).start()
        self.queue.put(True)

    def receive(self, route, headers, body):
        if route == "/main":
            subscription = self.get("main_subscription")
        elif route.startswith("/worker/"):
            assignment = self.get("run:" + route.split("/")[-1])
            subscription = assignment.get("subscription") if assignment else None
        else:
            return 404
        if not subscription:
            return 503
        timestamp, identifier = headers.get("webhook-timestamp", ""), headers.get("webhook-id", "")
        try:
            if abs(time.time() - int(timestamp)) > self.config["signature_tolerance"]:
                return 401
            secret = base64.b64decode(subscription["signing_secret"].removeprefix("whsec_"))
            signed = f"{identifier}.{timestamp}.".encode() + body
            expected = "v1," + base64.b64encode(hmac.new(secret, signed, hashlib.sha256).digest()).decode()
            if not identifier or not hmac.compare_digest(headers.get("webhook-signature", ""), expected):
                return 401
            event = json.loads(body)
            if event["specversion"] != "1.0" or event["data"]["project_id"] != self.config["project_id"]:
                return 400
        except (ValueError, KeyError):
            return 400
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO inbox (id,route,event) VALUES (?,?,?)", (identifier, route, json.dumps(event))
            )
        self.queue.put(True)
        return 202

    def consume(self):
        while True:
            self.queue.get()
            with self.lock:
                with self.connect() as db:
                    rows = db.execute("SELECT id,route,event FROM inbox WHERE processed=0 ORDER BY rowid").fetchall()
                for identifier, route, body in rows:
                    try:
                        event = json.loads(body)
                        if route == "/main":
                            self.main(event)
                        else:
                            self.worker(route.split("/")[-1], event)
                    except Exception as error:
                        with self.connect() as db:
                            db.execute("UPDATE inbox SET error=? WHERE id=?", (type(error).__name__, identifier))
                        print(
                            json.dumps(
                                {"delivery": identifier, "status": "needs_retry", "error": type(error).__name__}
                            ),
                            flush=True,
                        )
                        continue
                    with self.connect() as db:
                        db.execute("UPDATE inbox SET processed=1,error=NULL WHERE id=?", (identifier,))

    def main(self, event):
        issue_id = event["data"]["issue_id"]
        if self.get("issue:" + issue_id):
            return
        prefix, token = self.config["prefix"], self.config["dispatcher_token"]
        run = self.get("claim:" + issue_id)
        if run is None:
            run = self.api(
                "main",
                token,
                prefix + f"work-items/{issue_id}/claim/",
                "POST",
                {
                    "agent": self.config["worker_id"],
                    "worker_id": "local-handoff-demo:" + issue_id,
                    "repository": self.config["repository"],
                },
                idempotency="handoff-demo:" + event["id"],
            )
            self.put("claim:" + issue_id, run)
        grant = self.api("main", token, prefix + f"runs/{run['id']}/handoff/", "POST", {})
        assignment = {
            "token": grant["token"],
            "run_id": run["id"],
            "issue_id": issue_id,
            "cursor": grant["event_cursor"],
            "main_calls_at_handoff": self.get("api_calls")["main"],
        }
        self.put("issue:" + issue_id, run["id"])
        self.put("run:" + run["id"], assignment)
        self.bootstrap_worker(assignment)

    def bootstrap_worker(self, assignment):
        prefix, run_id, token = self.config["prefix"], assignment["run_id"], assignment["token"]
        subscription = self.api(
            "worker",
            token,
            prefix + "subscriptions/",
            "POST",
            {
                "url": self.config["callback_url"] + "/worker/" + run_id,
                "after": assignment["cursor"],
            },
        )
        assignment["subscription"] = subscription
        self.put("run:" + run_id, assignment)
        execution, overview = self.execution(assignment)
        question = self.api(
            "worker",
            execution,
            prefix + f"work-items/{assignment['issue_id']}/questions/",
            "POST",
            {
                "text": "Local handoff test: what should the implementation emphasize? "
                "Add an answer, comment or attachment to test direct worker notifications.",
                "blocking": True,
            },
        )
        assignment["question_id"] = question["id"]
        self.put("run:" + run_id, assignment)
        print(json.dumps({"status": "handed_off", "run": run_id, "issue": assignment["issue_id"]}), flush=True)

    def execution(self, assignment):
        prefix, token, run_id = self.config["prefix"], assignment["token"], assignment["run_id"]
        overview = self.api("worker", token, prefix + f"work-items/{assignment['issue_id']}/overview/")
        grant = self.api(
            "worker",
            token,
            prefix + f"runs/{run_id}/refresh/",
            "POST",
            {
                "expected_revision": overview["contract"]["spec_revision"],
            },
        )
        return grant["token"], overview

    def worker(self, run_id, event):
        if event["type"] in ("run.cancelled", "task.merged"):
            receipts = self.get("receipts:" + run_id, [])
            receipts.append({"event_id": event["id"], "type": event["type"], "closed": True})
            self.put("receipts:" + run_id, receipts)
            return
        if event["data"]["actor_id"] == self.config["worker_id"]:
            return
        assignment = self.get("run:" + run_id)
        if assignment is None:
            raise ValueError("Unknown assignment")
        execution, overview = self.execution(assignment)
        prefix, issue_id = self.config["prefix"], assignment["issue_id"]
        if event["type"] == "question.answered":
            for question in overview["questions"]:
                if question["answer"] and not question["resolved_at"]:
                    self.api(
                        "worker",
                        execution,
                        prefix + f"work-items/{issue_id}/questions/{question['id']}/resolve/",
                        "POST",
                        {
                            "answer_comment": question["answer_comment"],
                        },
                    )
        receipt = {
            "event_id": event["id"],
            "type": event["type"],
            "spec_revision": overview["contract"]["spec_revision"],
        }
        if event["type"] == "attachment.uploaded":
            asset = self.api(
                "worker", assignment["token"], prefix + f"runs/{run_id}/assets/{event['data']['asset_id']}/"
            )
            with self.opener.open(asset["download_url"], timeout=self.config["http_timeout"]) as response:
                receipt["asset_sha256"] = hashlib.sha256(response.read()).hexdigest()
        if event["type"] not in ("run.cancelled", "task.merged"):
            work_items = prefix.removesuffix("coordination/") + f"work-items/{issue_id}/comments/"
            self.api(
                "worker",
                execution,
                work_items,
                "POST",
                {
                    "comment_html": "<p>Direct worker notification received: <code>"
                    + event["type"]
                    + "</code>. Refreshed task access without calling the main agent.</p>",
                },
                idempotency="worker-receipt:" + event["id"],
            )
        receipts = self.get("receipts:" + run_id, [])
        receipts.append(receipt)
        self.put("receipts:" + run_id, receipts)

    def status(self):
        with self.connect() as db:
            assignments = [json.loads(row[0]) for row in db.execute("SELECT value FROM state WHERE key LIKE 'run:%'")]
            errors = [
                {"delivery": row[0], "error": row[1]}
                for row in db.execute("SELECT id,error FROM inbox WHERE error IS NOT NULL")
            ]
            received = db.execute("SELECT count(*) FROM inbox").fetchone()[0]
        return {
            "mode": "deterministic-protocol-demo",
            "api_calls": self.get("api_calls", {}),
            "received": received,
            "errors": errors,
            "assignments": [
                {
                    "run_id": item["run_id"],
                    "issue_id": item["issue_id"],
                    "question_id": item.get("question_id"),
                    "main_calls_at_handoff": item["main_calls_at_handoff"],
                    "receipts": self.get("receipts:" + item["run_id"], []),
                }
                for item in assignments
            ],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    demo = Demo(args.config, args.state_dir)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/status":
                self.send_error(404)
                return
            body = json.dumps(demo.status()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(demo.receive(self.path, self.headers, body))
            self.end_headers()

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer((args.host, args.port), Handler) as server:
        demo.start()
        server.serve_forever()


if __name__ == "__main__":
    main()
