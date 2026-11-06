"""Permanently exclude a Jira key from local analysis, or explicitly restore it."""

import sys

from _common import Store, TaskError, archive_name, key_checked, now, parser, read_json, redact, run


def main():
    args_parser = parser(__doc__)
    args_parser.add_argument("--issue-key", required=True)
    args_parser.add_argument("--reason", help="Nonsecret reason (default: Won't do).")
    args_parser.add_argument("--restore", action="store_true",
                             help="Remove exclusion; next fetch queues the current Jira version.")
    args = args_parser.parse_args()
    key = key_checked(args.issue_key)
    if args.restore and args.reason is not None:
        raise TaskError("--restore cannot be combined with --reason.")
    reason = redact(args.reason if args.reason is not None else "Won't do").strip()
    if not reason:
        raise TaskError("Exclusion reason must not be empty.")
    # Exclusion is local only, including when a live Jira write is pending.
    with Store(args.data_dir).locked(recover=False) as store:
        journal = store.root / ".transition.json"
        if journal.exists():
            transition = read_json(journal)
            source = store.relative_path(transition["source"])
            destination = store.relative_path(transition["destination"])
            if (not args.restore and not transition.get("label")
                    and source == store.root / "inbox" / f"{key}.json"
                    and destination.parent == store.root / "excluded"
                    and transition["state"]["issues"].get(key, {}).get("excluded")):
                store.recover_transition()
            else:
                raise TaskError("A transition is pending; finish its command before excluding or restoring.")
        processing = store.processing()
        if processing and read_json(processing)["key"] == key:
            raise TaskError("Cannot exclude or restore an active issue; finish/fail it or recover its stopped owner.")
        record = store.state["issues"].setdefault(key, {})
        if args.restore:
            exclusion = record.get("excluded")
            if not exclusion:
                raise TaskError("This issue is not excluded.")
            record["last_exclusion"] = {**record.pop("excluded"), "restored_at": now()}
            record["requeue_requested"] = True
            store.save()
            return {"status": "restored", "issue_key": key, "needs_fetch": True, "errors": []}
        if record.get("excluded"):
            return {"status": "already_excluded", "issue_key": key,
                    "exclusion": record["excluded"], "errors": []}
        pending = store.root / "inbox" / f"{key}.json"
        snapshot = (pending, read_json(pending)) if pending.exists() else store.latest_failures().get(key)
        if snapshot and snapshot[1]["key"] != key:
            raise TaskError("Snapshot issue key does not match the requested exclusion.")
        record["excluded"] = {
            "excluded_at": now(), "reason": reason,
            "summary": snapshot[1]["summary"] if snapshot else None,
            "updated": snapshot[1]["updated"] if snapshot else None,
            "snapshot_path": None,
        }
        if pending.exists():
            destination = store.root / "excluded" / archive_name(snapshot[1])
            record["excluded"]["snapshot_path"] = str(destination.relative_to(store.root))
            store.move(pending, destination)
        else:
            if snapshot:
                record["excluded"]["snapshot_path"] = str(snapshot[0].relative_to(store.root))
            store.save()
        return {"status": "excluded", "issue_key": key,
                "exclusion": store.state["issues"][key]["excluded"], "errors": []}


if __name__ == "__main__":
    sys.exit(run(main))
