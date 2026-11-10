"""Shared deterministic storage, locking, and safe command-line output."""

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile


class TaskError(Exception):
    """An operational error whose message is safe to report."""


def now():
    return datetime.now(timezone.utc).isoformat()


def redact(text):
    text = str(text)
    values = [os.environ.get(name, "") for name in
              ("JIRA_URL", "JIRA_USER", "JIRA_TOKEN")]
    user, token = values[1:]
    if user and token:
        values.append(base64.b64encode(f"{user}:{token}".encode()).decode())
    for value in sorted(filter(None, values), key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+\S+", r"\1 [REDACTED]", text)
    return text


def redact_data(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_data(item) for key, item in value.items()}
    return value


def emit(value):
    print(json.dumps(value, ensure_ascii=True))


def run(main):
    try:
        result = main()
        emit(result)
        return 1 if result.get("errors") else 0
    except TaskError as exc:
        emit({"status": "error", "errors": [redact(exc)]})
        return 1
    except Exception as exc:
        # Raw exceptions can contain authenticated URLs or HTTP headers.
        emit({"status": "error", "errors": [
            f"Unexpected {type(exc).__name__}; inspect local files and retry."
        ]})
        return 1


def parser(description, labels=False):
    result = argparse.ArgumentParser(description=description)
    result.add_argument(
        "--data-dir", type=Path, default=Path.cwd() / ".jira-agent",
        help="Runtime root (default: .jira-agent in the current directory).")
    if labels:
        result.add_argument("--no-jira-labels", action="store_true",
                            help="Offline testing only: do not update Jira labels.")
        result.add_argument("--api-version", choices=("2", "3"),
                            help="Override the API version recorded by fetch.")
        result.add_argument("--allow-http", action="store_true",
                            help="Allow HTTP only for a trusted local mock Jira.")
    return result


AI_LABELS = frozenset(("ai-processing", "ai-processed", "ai-failed"))
ALLOWED_STATUSES = ("NEW", "PAUSED", "TO DO", "REOPENED")


def has_allowed_status(issue):
    status = issue.get("status")
    name = status.get("name") if isinstance(status, dict) else None
    return isinstance(name, str) and name.strip().upper() in ALLOWED_STATUSES


def label_change(args, task, target):
    if args.no_jira_labels:
        return None
    return {"issue_key": task["key"], "target": target,
            "api_version": args.api_version or task.get("jira_api_version", "3"),
            "allow_http": args.allow_http}


def key_checked(key):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*-\d+", key):
        raise TaskError("Invalid Jira issue key.")
    return key


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=True).encode("utf-8")).hexdigest()


def read_json(path):
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        raise TaskError(f"Cannot read valid JSON from {path.name}.") from None


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Store:
    directories = ("inbox", "processing", "done", "failed", "excluded", "attachments", "results")

    def __init__(self, root):
        self.root = root.resolve()
        skills_root = Path(__file__).resolve().parents[2]
        if self.root.is_relative_to(skills_root):
            raise TaskError("Runtime data must be outside .agents/skills; choose a workspace data directory.")

    @contextmanager
    def locked(self, recover=True):
        self.root.mkdir(parents=True, exist_ok=True)
        for name in self.directories:
            (self.root / name).mkdir(exist_ok=True)
        lock = (self.root / ".queue.lock").open("a+b")
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0, os.SEEK_END)
                if lock.tell() == 0:
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise TaskError("Queue is busy; retry after the other script exits.") from None
            else:
                import fcntl
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise TaskError("Queue is busy; retry after the other script exits.") from None
            acquired = True
            self.recovered_transition = self.recover_transition() if recover else None
            if not (self.root / "state.json").exists():
                atomic_json(self.root / "state.json", {"schema_version": 1, "issues": {}})
            self.state = read_json(self.root / "state.json")
            if self.state.get("schema_version") != 1 or not isinstance(self.state.get("issues"), dict):
                raise TaskError("Unsupported or invalid state.json; restore a backup.")
            yield self
        finally:
            if acquired:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def save(self):
        atomic_json(self.root / "state.json", self.state)

    def relative_path(self, value):
        path = (self.root / value).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise TaskError("Runtime path escapes the data directory.")
        return path

    def recover_transition(self):
        journal = self.root / ".transition.json"
        if not journal.exists():
            return
        transition = read_json(journal)
        source = self.relative_path(transition["source"])
        destination = self.relative_path(transition["destination"])
        if source.exists() and destination.exists():
            raise TaskError("Conflicting transition files; restore from backup before retrying.")
        if not source.exists() and not destination.exists():
            raise TaskError("Interrupted transition lost both files; restore from backup.")
        if transition.get("label") and not transition["label"].get("synced"):
            from fetch_jira_tasks import sync_transition_label
            sync_transition_label(transition, journal)
        if source.exists():
            if "task" in transition:
                atomic_json(source, transition["task"])
            os.replace(source, destination)
        atomic_json(self.root / "state.json", transition["state"])
        journal.unlink()
        self.state = transition["state"]
        return transition

    def move(self, source, destination, task=None, label=None):
        if destination.exists():
            raise TaskError("Destination already exists; refusing to overwrite an issue version.")
        transition = {
            "source": str(source.relative_to(self.root)),
            "destination": str(destination.relative_to(self.root)),
            "state": self.state,
        }
        if task is not None:
            transition["task"] = task
        if label is not None:
            transition["label"] = label
        atomic_json(self.root / ".transition.json", transition)
        self.recover_transition()

    def replayed_task(self, folder, key, token):
        transition = self.recovered_transition
        if not transition:
            return None
        destination = self.relative_path(transition["destination"])
        if destination.parent != self.root / folder:
            return None
        task = read_json(destination)
        if task["key"] == key and token and secrets.compare_digest(
                task.get("claim", {}).get("token", ""), token):
            return destination, task
        return None

    def processing(self):
        paths = list((self.root / "processing").glob("*.json"))
        if len(paths) > 1:
            raise TaskError("Multiple processing issues detected; stop all agents and repair the queue.")
        return paths[0] if paths else None

    def authorized(self, key, token):
        key_checked(key)
        path = self.processing()
        if path is None:
            raise TaskError("No issue is being processed.")
        task = read_json(path)
        claim = task.get("claim", {})
        if task["key"] != key or not token or not secrets.compare_digest(
                claim.get("token", ""), token):
            raise TaskError("Issue key or claim token does not match the processing owner.")
        return path, task

    def latest_failures(self):
        latest = {}
        for path in (self.root / "failed").glob("*.json"):
            task = read_json(path)
            key = key_checked(task["key"])
            previous = latest.get(key)
            if previous is None or (task["failure"]["failed_at"], path.name) > (
                    previous[1]["failure"]["failed_at"], previous[0].name):
                latest[key] = (path, task)
        return latest

    def failure_superseded(self, task):
        completed = self.state["issues"].get(task["key"], {}).get("last_completed")
        return bool(completed and completed["completed_at"] >= task["failure"]["failed_at"])

    def is_excluded(self, key):
        return bool(self.state["issues"].get(key, {}).get("excluded"))

    def require_included(self, key):
        if self.is_excluded(key):
            raise TaskError("This issue is permanently excluded; restore it explicitly before selecting.")
        if self.state["issues"].get(key, {}).get("requeue_requested"):
            raise TaskError("Restored issue needs a fresh fetch before selection or retry.")
        if self.state["issues"].get(key, {}).get("in_scope") is False:
            raise TaskError("This issue is outside the latest Jira selection; fetch and list tasks again.")

    def refresh_scope(self, keys):
        for key, record in self.state["issues"].items():
            record["in_scope"] = key in keys
        self.save()

    def is_available(self, task):
        record = self.state["issues"].get(task["key"], {})
        return (has_allowed_status(task) and not record.get("excluded")
                and not record.get("requeue_requested") and record.get("in_scope") is not False)

    def available_tasks(self, include_failed=True):
        candidates = {}
        for path in (self.root / "inbox").glob("*.json"):
            task = read_json(path)
            key = key_checked(task["key"])
            if not self.is_available(task):
                continue
            if path.stem != key or key in candidates:
                raise TaskError("Invalid or duplicate inbox snapshot; repair the queue.")
            candidates[key] = (path, task, "pending")
        processing = self.processing()
        active_key = read_json(processing)["key"] if processing else None
        if include_failed:
            for key, (path, task) in self.latest_failures().items():
                if (key not in candidates and key != active_key and not self.failure_superseded(task)
                        and self.is_available(task)):
                    candidates[key] = (path, task, "failed")
        return sorted(candidates.values(), key=lambda item: (
            item[1]["queued_at"] if item[2] == "pending" else item[1]["failure"]["failed_at"],
            item[1]["key"]))

    def enqueue(self, issue):
        key = key_checked(issue["key"])
        current = self.state["issues"].setdefault(key, {})
        if self.is_excluded(key):
            return "excluded"
        version = {"updated": issue["updated"], "content_hash": issue["content_hash"]}
        seen = current.get("last_seen")
        if seen == version and not current.get("requeue_requested"):
            return "unchanged"
        path = self.root / "inbox" / f"{key}.json"
        # A crash after writing the snapshot but before state.json is harmless:
        # the same version is replaced, never appended as another queue entry.
        issue["queued_at"] = read_json(path)["queued_at"] if path.exists() else now()
        atomic_json(path, issue)
        current["last_seen"] = version
        current.pop("requeue_requested", None)
        self.save()
        return "updated" if seen else "new"


def archive_name(task):
    return f'{key_checked(task["key"])}-{task["version_id"]}-{secrets.token_hex(6)}.json'


def prepare_retry(task):
    task.pop("claim", None)
    task["retry_history"] = task.get("retry_history", []) + [task.pop("failure")]
    task["queued_at"] = now()


def is_stale(task, hours):
    if hours <= 0:
        raise TaskError("Stale threshold must be positive.")
    try:
        timestamp = datetime.fromisoformat(task["claim"]["claimed_at"])
        return (datetime.now(timezone.utc) - timestamp).total_seconds() >= hours * 3600
    except (KeyError, ValueError, TypeError):
        raise TaskError("Processing claim is invalid; restore or repair it with all agents stopped.") from None
