"""Complete the owned issue only after its analysis report exists."""

import sys

from _common import Store, TaskError, archive_name, label_change, now, parser, run


def main():
    args_parser = parser(__doc__, labels=True)
    args_parser.add_argument("--issue-key", required=True)
    args_parser.add_argument("--claim-token", required=True)
    args = args_parser.parse_args()
    with Store(args.data_dir).locked() as store:
        replayed = store.replayed_task("done", args.issue_key, args.claim_token)
        if replayed:
            completed = store.state["issues"][args.issue_key]["last_completed"]
            return {"status": "completed", **completed, "errors": []}
        source, task = store.authorized(args.issue_key, args.claim_token)
        result = store.relative_path(str(
            store.root / "results" / task["key"] / "analysis.md"))
        if not result.is_file() or not result.read_text(encoding="utf-8").strip():
            raise TaskError("A nonempty analysis.md must exist before completion.")
        # An older version's report must not accidentally complete a newer claim.
        if result.stat().st_mtime < source.stat().st_mtime:
            raise TaskError("analysis.md predates this claim; write the selected version's analysis first.")
        completed = {
            "issue_key": task["key"], "updated": task["updated"],
            "content_hash": task["content_hash"], "version_id": task["version_id"],
            "completed_at": now(), "result_path": str(result),
        }
        store.state["issues"].setdefault(task["key"], {})["last_completed"] = completed
        store.move(source, store.root / "done" / archive_name(task),
                   label=label_change(args, task, "ai-processed"))
        return {"status": "completed", **completed, "errors": []}


if __name__ == "__main__":
    sys.exit(run(main))
