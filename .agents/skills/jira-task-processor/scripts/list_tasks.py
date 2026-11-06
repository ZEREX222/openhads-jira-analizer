"""List pending and retryable failed issues without claiming or contacting Jira."""

import sys

from _common import Store, is_stale, parser, read_json, redact_data, run


def main():
    args_parser = parser(__doc__)
    args_parser.add_argument("--pending-only", action="store_true",
                             help="Exclude failed issues; default includes retryable failures.")
    args_parser.add_argument("--include-excluded", action="store_true",
                             help="Also display permanently excluded keys as nonselectable.")
    args = args_parser.parse_args()
    # Listing must not replay a pending remote write or claim an issue while
    # the operator is deciding which task to process.
    with Store(args.data_dir).locked(recover=False) as store:
        errors = []
        transition_pending = (store.root / ".transition.json").exists()
        if transition_pending:
            errors.append("An interrupted transition is pending; retry its command before selecting.")
        processing = store.processing()
        active = None
        if processing:
            task = read_json(processing)
            stale = is_stale(task, 24)
            active = {"issue_key": task["key"], "summary": task["summary"],
                      "path": str(processing), "claimed_at": task["claim"]["claimed_at"],
                      "stale": stale}
            if stale:
                errors.append("Stale processing claim; stop its owner and use explicit recovery.")
        can_claim = active is None and not transition_pending
        tasks = []
        counts = {"pending": 0, "failed": 0}
        for path, task, status in store.available_tasks(not args.pending_only):
            counts[status] += 1
            tasks.append({
                "issue_key": task["key"], "summary": task["summary"], "queue_status": status,
                "jira_status": (task.get("status") or {}).get("name"),
                "priority": (task.get("priority") or {}).get("name"),
                "updated": task["updated"], "path": str(path), "selectable": can_claim,
                "failure": task.get("failure") if status == "failed" else None,
                "exclusion_reason": None,
            })
        exclusions = {key: record["excluded"] for key, record in store.state["issues"].items()
                      if record.get("excluded")}
        if args.include_excluded:
            counts["excluded"] = len(exclusions)
            for key, exclusion in sorted(exclusions.items()):
                path = exclusion.get("snapshot_path")
                tasks.append({
                    "issue_key": key, "summary": exclusion["summary"], "queue_status": "excluded",
                    "jira_status": None, "priority": None, "updated": exclusion["updated"],
                    "path": str(store.relative_path(path)) if path else None,
                    "selectable": False, "failure": None, "exclusion_reason": exclusion["reason"],
                })
        return redact_data({
            "status": "blocked" if errors else "listed" if tasks or active else "empty",
            "tasks": tasks, "counts": counts, "processing": active,
            "can_claim": can_claim, "transition_pending": transition_pending, "errors": errors,
            "excluded_issue_count": len(exclusions),
        })


if __name__ == "__main__":
    sys.exit(run(main))
