"""Claim one issue, or explicitly recover a stale processing lease."""

import secrets
import sys

from _common import Store, TaskError, archive_name, atomic_json, is_stale, key_checked, label_change, now, parser, prepare_retry, read_json, run


def deliver_claim(path, task):
    task["claim"]["delivered"] = True
    atomic_json(path, task)
    return {"status": "claimed", "issue_key": task["key"], "path": str(path),
            "claimed": True, "claim_token": task["claim"]["token"], "errors": []}


def main():
    args_parser = parser(__doc__, labels=True)
    args_parser.add_argument("--stale-after-hours", type=float, default=24)
    args_parser.add_argument("--recover-stale", metavar="ISSUE_KEY")
    args_parser.add_argument("--confirm-owner-stopped", action="store_true")
    args_parser.add_argument("--issue-key", help="Select this pending or retryable failed issue.")
    args = args_parser.parse_args()
    if args.stale_after_hours <= 0:
        raise TaskError("Stale threshold must be positive.")
    if args.confirm_owner_stopped and not args.recover_stale:
        raise TaskError("--confirm-owner-stopped requires --recover-stale.")
    if args.issue_key:
        key_checked(args.issue_key)
        if args.recover_stale:
            raise TaskError("--issue-key cannot be combined with --recover-stale.")
    with Store(args.data_dir).locked() as store:
        existing = store.processing()
        if args.recover_stale:
            key_checked(args.recover_stale)
            if not args.confirm_owner_stopped:
                raise TaskError("Recovery requires --confirm-owner-stopped after stopping the owning agent.")
            if existing is None:
                raise TaskError("There is no processing issue to recover.")
            task = read_json(existing)
            if task["key"] != args.recover_stale or not is_stale(task, args.stale_after_hours):
                raise TaskError("The specified issue is not a stale processing claim.")
            # Preserve a newer inbox version; never replace it with stale content.
            pending = store.root / "inbox" / f'{task["key"]}.json'
            if pending.exists():
                task["failure"] = {"error": "Stale claim recovered; newer inbox version preserved.",
                                   "failed_at": now()}
                destination = store.root / "failed" / archive_name(task)
            else:
                task.pop("claim", None)
                destination = pending
            record = store.state["issues"].setdefault(task["key"], {})
            record["last_recovery"] = {"recovered_at": now(), "version_id": task["version_id"]}
            store.move(existing, destination, task, label_change(args, task, None))
            return {"status": "recovered", "issue_key": task["key"],
                    "path": str(pending), "errors": []}
        if existing:
            task = read_json(existing)
            if args.issue_key and args.issue_key != task["key"]:
                raise TaskError("Another issue is processing; finish or explicitly recover it before selecting.")
            stale = is_stale(task, args.stale_after_hours)
            if not stale and task["claim"].get("delivered") is False:
                return deliver_claim(existing, task)
            return {"status": "stale_lock" if stale else "already_processing",
                    "issue_key": task["key"], "path": str(existing), "claimed": False,
                    "claimed_at": task["claim"]["claimed_at"],
                    "errors": ["Stale processing claim; stop its owner and use explicit recovery."] if stale else []}
        candidates = store.available_tasks(include_failed=bool(args.issue_key))
        if args.issue_key:
            candidates = [item for item in candidates if item[1]["key"] == args.issue_key]
            if not candidates:
                raise TaskError("Selected issue is not pending or retryable; refresh and list tasks again.")
        if not candidates:
            return {"status": "empty", "issue_key": None, "path": None, "claimed": False, "errors": []}
        pending, task, status = candidates[0]
        key_checked(task["key"])
        if status == "failed":
            prepare_retry(task)
        token = secrets.token_urlsafe(32)
        task["claim"] = {"token": token, "claimed_at": now(), "delivered": False}
        destination = store.root / "processing" / f'{task["key"]}.json'
        store.move(pending, destination, task, label_change(args, task, "ai-processing"))
        return deliver_claim(destination, task)


if __name__ == "__main__":
    sys.exit(run(main))
