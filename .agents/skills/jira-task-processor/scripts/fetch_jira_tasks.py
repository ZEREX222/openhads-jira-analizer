"""Download assigned Jira issue snapshots and enqueue new or changed versions."""

import base64
import hashlib
from http.client import HTTPException
import json
import os
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from _common import AI_LABELS, ALLOWED_STATUSES, Store, TaskError, atomic_json, digest, has_allowed_status, key_checked, now, parser, redact, redact_data, run


DEFAULT_JQL = ('assignee = currentUser() AND status IN ('
               + ", ".join(f'"{status}"' for status in ALLOWED_STATUSES)
               + ') ORDER BY updated ASC')
FIELDS = ("summary,description,status,priority,issuetype,assignee,reporter,"
          "labels,components,created,updated,attachment")


def origin(url):
    parsed = urlsplit(url)
    return parsed.scheme.lower(), parsed.hostname, parsed.port


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if request.get_method() != "GET":
            raise TaskError("Refusing to redirect a Jira write request.")
        if urlsplit(newurl).scheme != "https" and origin(newurl) != origin(request.full_url):
            raise TaskError("Refusing an insecure cross-origin Jira redirect.")
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected is not None and origin(newurl) != origin(request.full_url):
            redirected.remove_header("Authorization")
            redirected.remove_header("Cookie")
        return redirected


class ApiError(TaskError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Jira HTTP {status}; check access, API version, and configuration.")


class Jira:
    def __init__(self, allow_http=False):
        values = [os.environ.get(name) for name in ("JIRA_URL", "JIRA_USER", "JIRA_TOKEN")]
        if not all(values):
            raise TaskError("Set JIRA_URL, JIRA_USER, and JIRA_TOKEN before fetching.")
        self.base = values[0].rstrip("/") + "/"
        parsed = urlsplit(self.base)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.scheme not in ("https", "http")):
            raise TaskError("JIRA_URL must be a base HTTP(S) URL without credentials, query, or fragment.")
        if parsed.scheme != "https" and not allow_http:
            raise TaskError("HTTPS is required; use --allow-http only for a trusted local test server.")
        self.auth = "Basic " + base64.b64encode(f"{values[1]}:{values[2]}".encode()).decode()
        self.opener = build_opener(SafeRedirect())

    def open(self, url, accept="application/json", method="GET", data=None):
        parsed = urlsplit(url)
        if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
            raise TaskError("Refusing an invalid attachment or API URL.")
        if origin(url) != origin(self.base):
            raise TaskError("Attachment metadata must point to the configured Jira origin.")
        headers = {"Accept": accept, "Authorization": self.auth}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, headers=headers, method=method, data=data)
        try:
            return self.opener.open(request, timeout=60)
        except HTTPError as exc:
            raise ApiError(exc.code) from None
        except (URLError, OSError, ValueError, HTTPException):
            raise TaskError("Jira network request failed; check connectivity and TLS configuration.") from None

    def get(self, path, parameters=None):
        url = urljoin(self.base, path)
        if parameters:
            url += "?" + urlencode(parameters)
        with self.open(url) as response:
            try:
                return json.load(response)
            except (ValueError, UnicodeError):
                raise TaskError("Jira returned invalid JSON.") from None
            except (OSError, HTTPException):
                raise TaskError("Jira response download was interrupted; retry the fetch.") from None

    def issues(self, jql, version):
        start, token = 0, None
        seen_tokens = set()
        while True:
            parameters = {"jql": jql, "fields": FIELDS, "maxResults": 100}
            if version == "3":
                if token:
                    parameters["nextPageToken"] = token
                data = self.get("rest/api/3/search/jql", parameters)
            else:
                parameters["startAt"] = start
                data = self.get("rest/api/2/search", parameters)
            batch = data.get("issues")
            if not isinstance(batch, list):
                raise TaskError("Jira search response is missing its issues array.")
            yield from batch
            if version == "3":
                next_token = data.get("nextPageToken")
                if data.get("isLast") is True:
                    break
                if not next_token:
                    if data.get("isLast") is False:
                        raise TaskError("Jira search response is missing its next page token.")
                    break
                if next_token in seen_tokens or not batch:
                    raise TaskError("Jira search pagination did not advance.")
                seen_tokens.add(next_token)
                token = next_token
            else:
                start += len(batch)
                total = data.get("total")
                if not isinstance(total, int):
                    raise TaskError("Jira search response is missing its total count.")
                if start >= total:
                    break
                if not batch:
                    raise TaskError("Jira search pagination did not advance.")

    def comments(self, key, version):
        start, comments = 0, []
        while True:
            data = self.get(f"rest/api/{version}/issue/{key}/comment",
                            {"startAt": start, "maxResults": 100})
            batch = data.get("comments")
            total = data.get("total")
            if not isinstance(batch, list) or not isinstance(total, int):
                raise TaskError("Jira comment response is missing comments or total.")
            comments.extend(batch)
            start += len(batch)
            if start >= total:
                return sorted(comments, key=lambda item: (item.get("created", ""), str(item.get("id", ""))))
            if not batch:
                raise TaskError("Jira comment pagination did not advance.")

    def download(self, url, destination, maximum):
        fd, temporary = tempfile.mkstemp(prefix=".download-", dir=destination.parent)
        try:
            checksum = hashlib.sha256()
            size = 0
            with os.fdopen(fd, "wb") as stream, self.open(url, "*/*") as response:
                while chunk := response.read(65536):
                    size += len(chunk)
                    if size > maximum:
                        raise TaskError("Attachment exceeds --max-attachment-bytes; issue was not queued.")
                    checksum.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            return size, checksum.hexdigest()
        except (OSError, HTTPException):
            raise TaskError("Attachment download or local write failed; issue was not queued.") from None
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def normalize(jira, raw, version, store, maximum):
    key = key_checked(raw.get("key"))
    fields = raw.get("fields", {})
    if not fields.get("updated") or not fields.get("summary"):
        raise TaskError("Issue response lacks summary or updated timestamp.")
    comments = [{
        "id": comment.get("id"), "body": comment.get("body"),
        "created": comment.get("created"), "updated": comment.get("updated"),
        "author": person(comment.get("author")),
        "update_author": person(comment.get("updateAuthor")),
        "visibility": comment.get("visibility"),
    } for comment in jira.comments(key, version)]
    metadata = fields.get("attachment") or []
    issue = {"key": key, "summary": fields["summary"], "description": fields.get("description"),
             "status": entity(fields.get("status")), "priority": entity(fields.get("priority")),
             "issue_type": entity(fields.get("issuetype")), "assignee": person(fields.get("assignee")),
             "reporter": person(fields.get("reporter")), "labels": sorted(fields.get("labels") or []),
             "components": sorted([entity(item) for item in fields.get("components") or []],
                                  key=lambda item: str(item.get("id", ""))),
             "created": fields.get("created"), "updated": fields["updated"], "comments": comments}
    issue = {"key": key, **redact_data({name: value for name, value in issue.items() if name != "key"})}
    metadata = sorted(metadata, key=lambda item: str(item.get("id", "")))
    # Content-addressed filenames keep active snapshots immutable across fetches.
    folder = store.root / "attachments" / key
    folder.mkdir(parents=True, exist_ok=True)
    attachments = []
    ids = set()
    for attachment in metadata:
        attachment_id = str(attachment.get("id", ""))
        if not re.fullmatch(r"\d+", attachment_id) or attachment_id in ids:
            raise TaskError("Invalid or duplicate Jira attachment ID.")
        ids.add(attachment_id)
        original_filename = redact(str(attachment.get("filename") or "attachment"))
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", original_filename)[:80]
        local = folder / f"{attachment_id}-{filename}"
        url = attachment.get("content")
        if not isinstance(url, str):
            raise TaskError("Attachment metadata is missing its download URL.")
        size, checksum = jira.download(url, local, maximum)
        declared_size = attachment.get("size")
        if declared_size is not None and (
                not isinstance(declared_size, int) or declared_size < 0 or size != declared_size):
            local.unlink()
            raise TaskError("Downloaded attachment size differs from Jira metadata; fetch again.")
        immutable = folder / f"{attachment_id}-{checksum}-{filename}"
        os.replace(local, immutable)
        # Exclude URLs (which may contain signed credentials) from stored metadata.
        attachment_data = redact_data({
            "id": attachment_id, "filename": original_filename,
            "mime_type": attachment.get("mimeType"), "created": attachment.get("created"),
            "author": person(attachment.get("author")), "declared_size": attachment.get("size"),
            "downloaded_size": size, "sha256": checksum,
        })
        attachment_data["local_path"] = str(immutable.relative_to(store.root))
        attachments.append(attachment_data)
    issue["attachments"] = attachments
    latest = jira.get(f"rest/api/{version}/issue/{key}", {"fields": "updated"})
    if latest.get("fields", {}).get("updated") != issue["updated"]:
        raise TaskError("Issue changed during download; fetch again for a consistent snapshot.")
    # Local paths are transport details, not Jira content.
    content = {**issue, "attachments": [
        {key: value for key, value in item.items() if key != "local_path"} for item in attachments]}
    content.pop("updated")
    content["labels"] = [label for label in content["labels"] if label not in AI_LABELS]
    issue["content_hash"] = digest(content)
    issue["version_id"] = digest({"updated": issue["updated"], "content_hash": issue["content_hash"]})
    issue["jira_api_version"] = version
    return issue


def sync_transition_label(transition, journal):
    """Idempotently apply a journaled label change before committing a local move."""
    change = transition["label"]
    key = key_checked(change["issue_key"])
    target = change["target"]
    if target is not None and target not in AI_LABELS:
        raise TaskError("Invalid AI lifecycle label in the pending transition.")
    jira = Jira(change["allow_http"])
    path = f'rest/api/{change["api_version"]}/issue/{key}'
    current = jira.get(path, {"fields": "labels,updated"}).get("fields", {})
    labels = current.get("labels")
    if not isinstance(labels, list) or not current.get("updated"):
        raise TaskError("Jira response lacks labels or updated; label transition remains pending.")
    operations = [{"remove": label} for label in sorted(AI_LABELS)
                  if label in labels and label != target]
    if target is not None and target not in labels:
        operations.append({"add": target})
    if operations:
        # Persist the pre-write timestamp so a lost PUT response can be replayed
        # without interpreting our own Jira update as another analysis task.
        change.setdefault("started_updated", current["updated"])
        atomic_json(journal, transition)
        payload = json.dumps({"update": {"labels": operations}}).encode("utf-8")
        try:
            with jira.open(urljoin(jira.base, path), method="PUT", data=payload) as response:
                response.read()
        except (OSError, HTTPException):
            raise TaskError("Jira label write was interrupted; retry the pending transition.") from None
        current = jira.get(path, {"fields": "labels,updated"}).get("fields", {})
    actual = current.get("labels")
    expected = {target} if target is not None else set()
    if not isinstance(actual, list) or set(actual) & AI_LABELS != expected or not current.get("updated"):
        raise TaskError("Jira did not confirm the requested label; transition remains pending.")
    record = transition["state"]["issues"].setdefault(key, {})
    seen = record.get("last_seen")
    if seen and seen["updated"] == change.get("started_updated"):
        # Other field/comment/attachment edits still change content_hash and
        # will be detected even if they arrive concurrently with this write.
        seen["updated"] = current["updated"]
    record["last_label_update"] = {
        "label": target, "updated": current["updated"], "synced_at": now(),
    }
    change["synced"] = True
    atomic_json(journal, transition)


def person(value):
    if value is None:
        return None
    return {key: value.get(key) for key in ("accountId", "name", "displayName", "active")}


def entity(value):
    if value is None:
        return None
    result = {key: value[key] for key in ("id", "name", "description") if key in value}
    category = value.get("statusCategory")
    if isinstance(category, dict):
        result["status_category"] = {key: category.get(key) for key in ("id", "key", "name")}
    return result


def main():
    args_parser = parser(__doc__)
    args_parser.add_argument("--api-version", choices=("auto", "2", "3"), default="auto")
    args_parser.add_argument("--allow-http", action="store_true")
    args_parser.add_argument("--check-connection", action="store_true",
                             help="GET myself only; do not create runtime files or queue issues.")
    args_parser.add_argument("--max-attachment-bytes", type=int, default=100 * 1024 * 1024)
    args = args_parser.parse_args()
    summary = {"new_issue_keys": [], "updated_issue_keys": [], "unchanged_issue_count": 0,
               "excluded_issue_keys": [], "skipped_status_issue_keys": [], "errors": []}
    try:
        if args.max_attachment_bytes <= 0:
            raise TaskError("Attachment size limit must be positive.")
        jira = Jira(args.allow_http)
        version = "3" if args.api_version == "auto" else args.api_version
        try:
            jira.get(f"rest/api/{version}/myself")
        except ApiError as exc:
            if args.api_version != "auto" or exc.status not in (404, 410):
                raise
            version = "2"
            jira.get("rest/api/2/myself")
        if args.check_connection:
            return {"status": "connected", "api_version": version, **summary}
        jql = os.environ.get("JIRA_JQL", DEFAULT_JQL)
        if not jql.strip():
            raise TaskError("JIRA_JQL must not be empty.")
        with Store(args.data_dir).locked() as store:
            seen = set()
            eligible = set()
            for raw in jira.issues(jql, version):
                key = raw.get("key")
                try:
                    key_checked(key)
                    if key in seen:
                        raise TaskError("Search returned the same issue twice; fetch again.")
                    seen.add(key)
                    if store.is_excluded(key):
                        summary["excluded_issue_keys"].append(key)
                        continue
                    if not has_allowed_status(raw.get("fields", {})):
                        summary["skipped_status_issue_keys"].append(key)
                        continue
                    eligible.add(key)
                    issue = normalize(jira, raw, version, store, args.max_attachment_bytes)
                    change = store.enqueue(issue)
                    if change == "unchanged":
                        summary["unchanged_issue_count"] += 1
                    else:
                        summary[f"{change}_issue_keys"].append(key)
                except TaskError as exc:
                    summary["errors"].append({"issue_key": key if isinstance(key, str) else None,
                                              "error": redact(exc)})
            # Only a fully enumerated search can retire missing cached tasks.
            store.refresh_scope(eligible)
    except TaskError as exc:
        summary["errors"].append({"error": redact(exc)})
    return summary


if __name__ == "__main__":
    sys.exit(run(main))
