# Operational workflow

## Environment and activation

Use Python 3.10 or later. These scripts use only the Python standard library.
No third-party dependencies or background poller are installed. Run a fetch
explicitly for each analysis cycle; schedule the Python fetch externally if
polling is desired. The LLM never schedules, polls, or manages the queue.

In Windows PowerShell at the workspace root:

```powershell
$env:JIRA_URL = 'https://your-company.atlassian.net'
$env:JIRA_USER = Read-Host 'Jira account email or username'
$secureToken = Read-Host 'Jira API token' -AsSecureString
$env:JIRA_TOKEN = [System.Net.NetworkCredential]::new('', $secureToken).Password
Remove-Variable secureToken
$scripts = Join-Path (Get-Location) '.agents\skills\jira-task-processor\scripts'
```

Do not print these variables, save them in a file, put the token on a command
line, or commit them. Environment variables are inherited only by processes
launched from this shell. Configure them separately in the OpenHands runtime
if it runs in a container or remote workspace; prefer its secret configuration.
Basic authentication uses an account email/API token on Jira Cloud and a
username/token accepted by your Jira Server/Data Center deployment. Bearer-only
deployments need an authentication adaptation; they are not silently supported.

The default JQL is:

```text
assignee = currentUser()
AND status IN ("NEW", "PAUSED", "TO DO", "REOPENED")
ORDER BY updated ASC
```

For a safe one-issue trial, narrow the query while retaining the assignment filter:

```powershell
$env:JIRA_JQL = 'assignee = currentUser() AND status IN ("NEW", "PAUSED", "TO DO", "REOPENED") AND key = DEMO-123 ORDER BY updated ASC'
```

Only `NEW`, `PAUSED`, `TO DO`, and `REOPENED` issues are eligible, including
updated issues and failed retries. Status names are compared case-insensitively
with surrounding whitespace ignored. A custom `JIRA_JQL` cannot bypass this
local status filter; fetch reports rejected keys in `skipped_status_issue_keys`
without downloading their comments or attachments.
After a fully enumerated search, cached issues absent from the eligible search
results are hidden from listing, claiming, and retrying. This also applies when
assignment or a custom JQL condition changes. Snapshots and failure history are
preserved, and eligible issues can return after a later fetch. Failed or
interrupted search pagination does not retire missing cached issues.
Listing is offline: fetch first to observe Jira status changes. Existing active
claims remain owned by their original conversation and can finish normally.

Start a **new OpenHands conversation** after installing/updating the skill.
The repository containing `.agents\skills\jira-task-processor\SKILL.md` must be
in its mounted workspace with project skills discovery enabled by your
OpenHands version. Ask: "Use jira-task-processor to analyze one assigned Jira
issue. Do not change code or Jira fields other than the AI lifecycle labels."
Existing conversations may not reload
skills. If it is not discovered, check your OpenHands version's project-skill
support and workspace mounting rather than moving runtime files into the skill.

## Connectivity and every script

Normal lifecycle commands require Jira **Edit Issues** permission for labels.
They manage mutually exclusive labels, preserving every unrelated label:

| Local transition | Jira label |
| --- | --- |
| Claim an issue | `ai-processing` |
| Complete analysis | `ai-processed` |
| Fail analysis | `ai-failed` |
| Retry or recover to inbox | Clear the three AI labels |

`ai-processed` means analysis was written, not that a source fix was implemented.
Fetching by itself does not mark issues processed or change labels. A changed
issue receives `ai-processing` when it is claimed again, replacing its previous
AI label. Jira workflow status, comments, and other fields are never changed.

```powershell
python "$scripts\fetch_jira_tasks.py" --check-connection
python "$scripts\fetch_jira_tasks.py"
$list = python "$scripts\list_tasks.py" | ConvertFrom-Json
$list.tasks | Format-Table issue_key, summary, queue_status, priority, selectable
$selected = Read-Host 'Choose a Jira issue key'
$claim = python "$scripts\claim_next_task.py" --issue-key $selected | ConvertFrom-Json
$claim
```

`list_tasks.py` lists local **new/updated pending issues and retryable failed
issues**. Run fetch first for current Jira data. Listing never contacts Jira,
changes labels, claims an issue, or replays a pending transition. It uses the
queue mutex for a consistent read; a busy queue is reported explicitly.
Its JSON contains `tasks`, `counts`, `processing`, `can_claim`,
`transition_pending`, and `errors`. Each task includes key, summary, queue
status, Jira status, priority, updated timestamp, local snapshot path, whether
selection is permitted, and failure details when applicable. Owner tokens
are never included. `--pending-only` excludes failed issues.

Each key is listed once: a newer inbox snapshot takes precedence over failed
versions; otherwise the latest failed snapshot is listed unless superseded by
successful analysis. Active issues appear separately in `processing`, not
as retryable failures. A newer pending version of an active key can still be
listed, but **all selection is disabled** until the active issue is finished.
Stale claims and interrupted transactions are returned as blocked errors;
listing does not recover either of them.

`claim_next_task.py --issue-key DEMO-123` claims only that key. For failed
issues it explicitly retries the listed snapshot, preserving `retry_history`,
and sets `ai-processing` without an intermediate manual queue move. Invalid,
completed, unavailable, or superseded keys cause an error: there is **no
fallback to another issue**. If the list changed, refresh it and choose again.
Without `--issue-key`, the previous oldest-inbox-first behavior is unchanged;
failed issues are never automatically retried.

In OpenHands ask "Show unprocessed Jira issues so I can choose." The skill
fetches, shows the list, and waits without claiming. Then reply with a key,
for example "Analyze DEMO-123." To bypass the prompt deliberately, ask for
"the oldest pending issue." Keep the same runtime root across these turns.

Connectivity checks use `GET myself`, return only a safe status and API
version, and create no runtime files. Auto detection tries API 3, falling back
to API 2 only when `myself` returns 404/410. Cloud uses the enhanced
`rest/api/3/search/jql` endpoint with page tokens; API 2 uses `search` with
offsets. Comments are independently paginated; do not assume search embeds all
comments. If necessary use `--api-version 2` or `--api-version 3` explicitly.
401/403, invalid JQL, network/TLS failures, and incomplete downloads are errors,
not empty queues. HTTPS is required; `--allow-http` is only for a trusted local
mock server because Basic authentication is otherwise visible on the network.

Only `status = claimed` permits starting analysis. Keep its `claim_token`
until completion/failure. `already_processing` supplies the existing key/path
but **not** the owner token; a second conversation must stop. If `empty`, stop.
After writing a real analysis report for the selected version:

```powershell
python "$scripts\complete_task.py" --issue-key $claim.issue_key --claim-token $claim.claim_token
```

If analysis fails instead:

```powershell
python "$scripts\fail_task.py" --issue-key $claim.issue_key --claim-token $claim.claim_token --error 'Local project is unavailable; no source diagnosis was possible.'
```

For multiline errors, omit `--error` and pipe a **nonsecret** explanation to
stdin. Do not supply raw credential-bearing exceptions. Known credential
values and Basic/Bearer header patterns are redacted from failure records.
Any unexpected exception is reported by type, not its possibly secret text.
Commands output one JSON object on normal/operational-error paths; exit code
1 means errors. Argparse usage errors use normal argparse stderr/exit code 2.

All commands accept `--data-dir <absolute path>`. Use the identical directory
across all invocations. The default is `.jira-agent` in the current directory.
Lifecycle commands use the API version recorded by fetch; `--api-version 2`
or `--api-version 3` can override it. They also accept `--allow-http` for mock
servers and `--no-jira-labels` for explicitly offline queue tests only. Normal
OpenHands execution must not use `--no-jira-labels`.
The 100 MiB per-attachment limit can be adjusted using
`fetch_jira_tasks.py --max-attachment-bytes <bytes>`. An oversized/unreadable
attachment causes that issue's fetch to fail without queuing incomplete data.
Downloaded byte counts are checked against Jira metadata, and the issue update
timestamp is checked again after downloading to reject snapshots that changed
mid-fetch. Retry such fetches rather than analyzing inconsistent evidence.
Signed attachment URLs are used only in memory; URL metadata is not saved.
Configured credential values are also redacted from normalized Jira text and
user metadata; authentication headers and account self/avatar URLs are not stored.
Cross-origin metadata URLs are refused. HTTPS redirects from the configured
origin may download CDN bytes but do not receive Jira authentication.

## Runtime layout and semantics

```text
.jira-agent/
  inbox/             <KEY>.json (latest pending version per issue)
  processing/        zero or one <KEY>.json, with owner token and claim time
  done/              uniquely named completed issue snapshots
  failed/            uniquely named failed snapshots and diagnostic metadata
  excluded/          archived inbox snapshots for permanently excluded keys
  attachments/       <KEY>/<id>-<sha256>-<safe-filename>
  results/           <KEY>/analysis.md
  state.json         schema_version and issue last_seen/last_completed/last_failed
  .queue.lock        OS-backed short-lived script mutex
  .transition.json   temporary crash-recovery journal, only during a move
```

The scripts create this layout on first queue operation. Runtime data is
gitignored and never written inside `.agents\skills`. Jira text can contain
confidential project data: keep this root private and on a **local filesystem**.
Do not share it via a network filesystem or independent container volumes.
The OS lock serializes fetching, claims, completion, failure, and recovery
across processes (Windows `msvcrt`, POSIX `flock`). Busy scripts fail explicitly;
retry after the other script exits. Fetch holds this mutex while downloading,
so do not run a fetch concurrently with completion.

The durable processing lease has no automatically renewed heartbeat. A claim
older than 24 hours is reported as stale, not stolen. Age is only a warning:
it does not prove the agent is dead. The claim token fences completion and
failure; only the conversation receiving a new token may perform analysis.
Physical locks are released by the OS on exit; `.queue.lock` remaining on
disk is normal and does not itself mean an agent is active.

SHA-256 covers normalized Jira content, all paginated comments, attachment
metadata, and downloaded bytes, excluding transport/local paths. Both the
Jira update timestamp **and** content hash are compared to `last_seen`.
The content hash excludes `updated` and the three AI labels. Label writes
record their resulting Jira timestamp in the journal/state, so our own label
changes do not enqueue the issue again. Other labels and all actual issue
content remain part of the hash; concurrent source-related Jira edits are
still detected. Unrelated timestamp-only updates still enqueue a new version.
Existing runtime snapshots use the previous hash format and may be queued
once when first fetched after this upgrade.
Identical fetches do not enqueue duplicates. Failed unchanged issues stay
failed until explicit retry. Completed issues return when either comparison
changes. Fetch coalesces multiple pending updates for one key to its latest
snapshot while preserving its initial FIFO queue time. An active snapshot is
never replaced: changes arriving during analysis wait in inbox for the next
conversation. Attachments are content-addressed so active paths remain stable.
No automatic attachment retention/deletion policy is applied.
Permanent exclusions are recorded per key in `state.json`; they take
precedence over all timestamp/hash comparisons, retries, and selection.
Fetch reports matching excluded keys in `excluded_issue_keys` without fetching
their comments or downloading attachments. Restore requests force a new fetch
of the current issue version, even if it is unchanged.

Moves use atomic replacement and a write-ahead journal. If a script exits
between a move and its state update, the next locked command finishes that
recorded transition. Jira label updates are journaled before the API write,
confirmed by reading labels back, and applied before the local move. Label
updates use atomic add/remove operations, not replacement of the full labels
array. A failed or uncertain API write reports an error and leaves the local
transition pending; it never silently completes a task or releases its lock.
Restore permissions/connectivity and retry the same lifecycle command.
The next queue command can also replay this existing journal, including its
authorized Jira write; it does not create a new operation. Lost PUT responses
are reconciled without duplicating labels. An undelivered recovered claim
returns its token exactly once; it does not steal a previously delivered claim.
Never delete the journal or use offline flags to bypass a pending live update.
JSON writes use temporary files, flush/fsync, and atomic
replacement. This recovers process interruption; backups are still needed
for disk failures/power loss. Unexpected corruption or multiple processing
files is an explicit error requiring operator repair, not silently ignored.
The fixed `analysis.md` path represents the latest analysis; archive the
report externally if immutable report history is required.

## Recovering a stale processing lease

1. Stop the owning OpenHands conversation/agent and all commands using this
   runtime root. Make sure it cannot resume. Back up the runtime root.
2. Inspect the existing processing issue through `claim_next_task.py`; it
   reports `stale_lock` after 24 hours. Do not delete lock or state files.
3. Explicitly requeue its snapshot:

```powershell
python "$scripts\claim_next_task.py" --recover-stale DEMO-123 --confirm-owner-stopped
python "$scripts\claim_next_task.py"
```

`--stale-after-hours <positive hours>` changes the age threshold for inspection
and recovery; lower it only after independently confirming the old owner is
stopped. Recovery never claims automatically. It removes the abandoned token
and the next claim generates a fresh token. If a newer version is already
queued, recovery preserves it and archives the old snapshot as failed.
Ordinary commands never automatically recover a stale lease.

## Retrying a failed issue

```powershell
python "$scripts\fail_task.py" --retry DEMO-123
python "$scripts\claim_next_task.py"
```

Retry selects the most recent failed snapshot, preserves its error in
`retry_history`, clears the old claim, and queues it with a new queue time.
Retry and stale recovery clear the AI labels because the issue is pending,
not actively analyzed. They refuse/stop on Jira label errors.
It refuses a retry of the same active issue, preserves an existing newer
pending version, and refuses failures superseded by successful analysis.
Fetch first if you want the latest Jira version rather than the failed one.

## Permanently excluding tasks ("Won't do")

To exclude an issue you will not analyze:

```powershell
python "$scripts\exclude_task.py" --issue-key DEMO-123
# Optional nonsecret explanation:
python "$scripts\exclude_task.py" --issue-key DEMO-456 --reason "Out of scope"
```

This is a **local permanent exclusion**, not a Jira status transition. It
never contacts Jira or changes Jira labels, fields, assignee, or status.
The issue disappears from the normal task list and both automatic and manual
claims refuse it. Failed retries are also refused. Future Jira changes cannot
put it back in inbox. You can exclude a key before its first fetch.
Inbox snapshots are archived under `.jira-agent\excluded`; failed snapshots,
attachments, previous reports, and completed history are retained for diagnosis.
Repeated exclusion is idempotent and preserves the existing reason/timestamp.
An active issue cannot be excluded; finish/fail it, or explicitly recover its
stopped owner, before excluding it. A pending live label transition must finish
first; exclusion never replays that Jira write. An interrupted local exclusion
can be safely retried with the same exclusion command.

To inspect the excluded keys:

```powershell
$list = python "$scripts\list_tasks.py" --include-excluded | ConvertFrom-Json
$list.tasks | Format-Table issue_key, summary, queue_status, exclusion_reason, selectable
```

`--include-excluded` is a management view only. Excluded entries have
`queue_status: excluded` and `selectable: false`; the response also reports
`excluded_issue_count`. A key excluded before fetching may have no summary or
snapshot path, since the script never invents Jira data.

Only if you change your mind, explicitly restore the key:

```powershell
python "$scripts\exclude_task.py" --issue-key DEMO-123 --restore
python "$scripts\fetch_jira_tasks.py"
```

Restore preserves the last exclusion for audit and requires a fresh fetch
before selection/retry; it never queues an old archived snapshot directly.
The next successful fetch queues the current matching Jira version even if
its timestamp and content are unchanged. Nothing restores exclusions
automatically. Use the same `--data-dir` for exclusion, listing, and all other
operations.

In OpenHands say "Exclude DEMO-123 from analysis, won't do." The skill
performs the local exclusion and stops without analyzing it or touching Jira.

## Offline queue tests (no Jira or real issue processing)

From the repository root:

```powershell
python -m unittest discover -s tests -p 'test_jira_task_processor.py' -v
```

Tests use synthetic issue keys, fake credentials, a loopback mock Jira server,
and temporary runtime roots. They never use your Jira configuration or modify
the real `.jira-agent`. They cover fetching, pagination, attachment bytes,
version comparisons, queue ordering, concurrent claims, completion guards,
failure/retry, stale recovery, credential redaction, and interrupted moves.
They also cover listing pending/failed issues, explicit selection, deduplication,
superseded failures, blocked selection, and read-only listing during a pending
label transition.
They also verify permanent exclusions, skipped downloads after Jira updates,
blocked claims/retries, explicit restoration, and preservation of snapshots.
Pure queue tests explicitly use `--no-jira-labels`; lifecycle integration
tests write labels only to the loopback mock Jira, never to a real instance.
Do not fabricate a successful report in the real queue just to test locking.

For a live safe trial, run the connectivity check first, restrict `JIRA_JQL`
to exactly one assigned non-Done issue, and use an isolated root. This trial
will update that real issue's AI labels when claimed/failed; add
`--no-jira-labels` to **every lifecycle command** if you only want to test the
local queue without writing to Jira:

```powershell
$trial = Join-Path (Get-Location) '.jira-agent\trial'
python "$scripts\fetch_jira_tasks.py" --data-dir $trial
python "$scripts\fetch_jira_tasks.py" --data-dir $trial
$claim = python "$scripts\claim_next_task.py" --data-dir $trial | ConvertFrom-Json
python "$scripts\claim_next_task.py" --data-dir $trial
```

Expect one new key, then one unchanged issue, then `claimed`, then
`already_processing` for the same key. Do **not** start analysis in another
conversation. Continue with the owning conversation, specifying this same
trial root and selected claim, or fail this trial explicitly with its token
if stopping. Restore the default query with `Remove-Item Env:JIRA_JQL` when
finished. Fetch/connectivity checks are read-only unless replaying a pending
label transaction; claim, complete, fail, retry, and stale recovery normally
write only the AI lifecycle labels.
