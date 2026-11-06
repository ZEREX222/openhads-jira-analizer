# openhads-jira-analizer

The [jira-task-processor skill](.agents/skills/jira-task-processor/SKILL.md)
downloads assigned Jira issues with deterministic Python scripts, then directs
OpenHands to analyze exactly one issue against its local source project without
changing code, Jira status, or comments. The scripts mark Jira issues with
`ai-processing`, `ai-processed`, or `ai-failed`, preserving unrelated labels.
Use `list_tasks.py` to list pending and failed issues, then
`claim_next_task.py --issue-key <KEY>` to select one. OpenHands shows the list
and waits for your selection unless automatic oldest-first processing is
explicitly requested.

See the [operational workflow](.agents/skills/jira-task-processor/references/workflow.md)
for Windows environment configuration, manual commands, offline tests, safe
one-issue trials, stale-claim recovery, and failed-issue retries.

Requires Python 3.10+ and no external Python dependencies. Runtime data belongs
in the gitignored `.jira-agent` directory, outside the installed skill.