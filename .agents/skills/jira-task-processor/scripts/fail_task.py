"""Fail an owned issue, or explicitly retry the most recent failed version."""

import sys

from _common import Store, TaskError, archive_name, key_checked, label_change, now, parser, prepare_retry, read_json, redact, run


def main():
    args_parser = parser(__doc__, labels=True)
    args_parser.add_argument("--issue-key")
    args_parser.add_argument("--claim-token")
    args_parser.add_argument("--error", help="Nonsecret failure description; otherwise read stdin.")
    args_parser.add_argument("--retry", metavar="ISSUE_KEY")
    args = args_parser.parse_args()
    if args.retry and (args.issue_key or args.claim_token or args.error):
        raise TaskError("--retry cannot be combined with failure arguments.")
    if not args.retry and (not args.issue_key or not args.claim_token):
        raise TaskError("Failure requires --issue-key and --claim-token.")
    error = None
    if not args.retry:
        error = redact(args.error if args.error is not None else sys.stdin.read()).strip()
        if not error:
            raise TaskError("Provide a nonempty, nonsecret failure description.")
    with Store(args.data_dir).locked() as store:
        replayed = store.replayed_task("failed", args.issue_key, args.claim_token)
        if replayed and not args.retry:
            path, task = replayed
            return {"status": "failed", "issue_key": task["key"], "path": str(path), "errors": []}
        if args.retry:
            key = key_checked(args.retry)
            store.require_included(key)
            processing = store.processing()
            if processing and read_json(processing)["key"] == key:
                raise TaskError("This issue is already processing; do not retry it concurrently.")
            pending = store.root / "inbox" / f"{key}.json"
            if pending.exists():
                return {"status": "already_queued", "issue_key": key,
                        "path": str(pending), "errors": []}
            failure = store.latest_failures().get(key)
            if failure is None:
                raise TaskError("No failed version exists for this issue.")
            source, task = failure
            if store.failure_superseded(task):
                raise TaskError("A successful analysis supersedes this failure; fetch changes instead.")
            prepare_retry(task)
            store.move(source, pending, task, label_change(args, task, None))
            return {"status": "retried", "issue_key": key, "path": str(pending), "errors": []}
        source, task = store.authorized(args.issue_key, args.claim_token)
        failure = {"issue_key": task["key"], "updated": task["updated"],
                   "content_hash": task["content_hash"], "version_id": task["version_id"],
                   "failed_at": now(), "error": error}
        task["failure"] = failure
        store.state["issues"].setdefault(task["key"], {})["last_failed"] = failure
        destination = store.root / "failed" / archive_name(task)
        store.move(source, destination, task, label_change(args, task, "ai-failed"))
        return {"status": "failed", "issue_key": task["key"], "path": str(destination), "errors": []}


if __name__ == "__main__":
    sys.exit(run(main))
