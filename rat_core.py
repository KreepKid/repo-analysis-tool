from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
REPOS_DIR = DATA_DIR / "repos"
ANALYSES_DIR = DATA_DIR / "analyses"
UPLOADS_DIR = DATA_DIR / "uploads"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MAX_UPLOAD_BYTES = 1_000_000_000


@dataclass
class RepoRecord:
    repo_id: str
    name: str
    source_type: str
    source: str
    path: str


def ensure_data_dirs() -> None:
    for directory in (DATA_DIR, REPOS_DIR, ANALYSES_DIR, UPLOADS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def _run_git(repo_path: Path, args: list[str], timeout: int = 120) -> str:
    process = subprocess.run(
        ["git", *args], cwd=repo_path, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=timeout, check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip() or "Git command failed")
    return process.stdout


def _safe_repo_name(value: str) -> str:
    base = value.rstrip("/").split("/")[-1] or "repository"
    if base.endswith(".git"):
        base = base[:-4]
    clean = "".join(char if char.isalnum() or char in "._-" else "-" for char in base).strip(".-_")
    return clean or "repository"


def is_valid_remote_url(value: str) -> bool:
    value = value.strip()
    if value.startswith("git@") and ":" in value:
        return True
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https", "ssh"} and bool(parsed.netloc or parsed.path)


def _repo_db_path() -> Path:
    return DATA_DIR / "repos.json"


def load_repos() -> list[RepoRecord]:
    ensure_data_dirs()
    if not _repo_db_path().exists():
        return []
    return [RepoRecord(**record) for record in json.loads(_repo_db_path().read_text())]


def save_repos(records: list[RepoRecord]) -> None:
    ensure_data_dirs()
    _repo_db_path().write_text(json.dumps([asdict(record) for record in records], indent=2))


def get_repo(repo_id: str) -> RepoRecord | None:
    return next((repo for repo in load_repos() if repo.repo_id == repo_id), None)


def clone_repository(url: str) -> RepoRecord:
    if not is_valid_remote_url(url):
        raise ValueError("Enter a valid HTTP(S) or SSH Git repository URL.")
    ensure_data_dirs()
    repo_id = uuid.uuid4().hex[:10]
    name = _safe_repo_name(url)
    target = REPOS_DIR / f"{repo_id}-{name}"
    process = subprocess.run(
        ["git", "clone", "--no-single-branch", url, str(target)], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900, check=False,
    )
    if process.returncode != 0:
        if target.exists():
            shutil.rmtree(target)
        raise RuntimeError(process.stderr.strip() or "Repository clone failed")
    record = RepoRecord(repo_id, name, "url", url, str(target))
    repos = load_repos()
    repos.append(record)
    save_repos(repos)
    return record


def import_zip(zip_bytes: bytes, filename: str) -> RepoRecord:
    if not filename.lower().endswith(".zip") or not zipfile.is_zipfile(io.BytesIO(zip_bytes)):
        raise ValueError("Upload a valid .zip file containing a Git repository.")
    if len(zip_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError("The ZIP file exceeds the 1 GB upload limit.")
    ensure_data_dirs()
    repo_id = uuid.uuid4().hex[:10]
    name = _safe_repo_name(filename.rsplit(".", 1)[0])
    upload_path = UPLOADS_DIR / f"{repo_id}-{name}.zip"
    extract_root = REPOS_DIR / f"{repo_id}-{name}"
    upload_path.write_bytes(zip_bytes)
    extract_root.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(upload_path) as archive:
            _safe_extract(archive, extract_root)
        repo_path = _find_git_repo(extract_root)
        if repo_path is None:
            raise ValueError("The ZIP does not contain a Git repository with a .git directory or file.")
    except Exception:
        shutil.rmtree(extract_root, ignore_errors=True)
        raise
    record = RepoRecord(repo_id, name, "zip", filename, str(repo_path))
    repos = load_repos()
    repos.append(record)
    save_repos(repos)
    return record


def _safe_extract(archive: zipfile.ZipFile, target: Path) -> None:
    target_root = target.resolve()
    for member in archive.infolist():
        destination = (target / member.filename).resolve()
        if destination != target_root and target_root not in destination.parents:
            raise ValueError("ZIP contains an unsafe file path.")
    archive.extractall(target)


def _find_git_repo(root: Path) -> Path | None:
    if (root / ".git").exists():
        return root
    for current, directories, files in os.walk(root):
        if ".git" in directories or ".git" in files:
            return Path(current)
    return None


def list_branches_and_refs(repo_path: Path) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    try:
        output = _run_git(repo_path, ["for-each-ref", "--format=%(refname:short)|%(objectname)", "refs/heads", "refs/remotes"], 60)
        refs = [{"name": name, "sha": sha} for line in output.splitlines() if "|" in line for name, sha in [line.split("|", 1)]]
        head = _run_git(repo_path, ["rev-parse", "HEAD"], 30).strip()
        refs = [{"name": "HEAD", "sha": head}, *[ref for ref in refs if ref["name"] != "HEAD"]]
    except RuntimeError:
        pass
    return refs


def _analysis_key(ref: str, start_ts: int | None, end_ts: int | None, selected: set[str] | None) -> str:
    raw = json.dumps({"ref": ref, "start": start_ts, "end": end_ts, "selected": sorted(selected or [])}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def analyze_repository(repo: RepoRecord, ref: str = "HEAD", start_ts: int | None = None, end_ts: int | None = None, selected_commits: set[str] | None = None) -> dict[str, Any]:
    if start_ts is not None and end_ts is not None and start_ts >= end_ts:
        raise ValueError("The start date must be before the end date.")
    repo_path = Path(repo.path)
    resolved_ref = _run_git(repo_path, ["rev-parse", ref], 30).strip()
    cache_key = _analysis_key(resolved_ref, start_ts, end_ts, selected_commits)
    cached = load_analysis(repo.repo_id, cache_key)
    if cached:
        return cached

    file_totals: dict[str, dict[str, Any]] = {}
    dir_totals: dict[str, dict[str, Any]] = {"/": _blank_total("directory", "/")}
    author_totals: dict[tuple[str, str], dict[str, Any]] = {}
    authors: dict[str, dict[str, Any]] = {}
    commits: list[dict[str, Any]] = []

    for commit, diffs in _history_with_numstat(repo_path, resolved_ref, start_ts, end_ts, selected_commits):
        commits.append(commit)
        changed_objects: set[str] = set()
        for path, added, removed in diffs:
            growth, churn = added - removed, added + removed
            file_total = file_totals.setdefault(path, _blank_total("file", path))
            _add_change(file_total, added, removed, growth, churn)
            objects = [path, *_directories_for(path)]
            changed_objects.update(objects)
            for directory in objects[1:]:
                _add_change(dir_totals.setdefault(directory, _blank_total("directory", directory)), added, removed, growth, churn)
            for object_path in objects:
                author_total = author_totals.setdefault((object_path, commit["author"]), _blank_author_total(object_path, commit["author"]))
                _add_author_change(author_total, added, removed, growth, churn)
        for object_path in changed_objects:
            target = file_totals.get(object_path) or dir_totals.get(object_path)
            if target:
                target["modifications"] += 1
            author_total = author_totals.get((object_path, commit["author"]))
            if author_total:
                author_total["modifications"] += 1
        authors.setdefault(commit["author"], {"author": commit["author"], "commits": 0})["commits"] += 1

    all_objects = [*dir_totals.values(), *file_totals.values()]
    for total in all_objects:
        _finalize_total(total, len(commits))
    for metric in author_totals.values():
        total_churn = next((obj["churn"] for obj in all_objects if obj["path"] == metric["path"]), 0)
        metric["ownership"] = metric["churn"] / total_churn if total_churn else 0
    repository = {**dir_totals["/"], "object_type": "repository"}
    result = {
        "repo": asdict(repo), "ref": ref, "ref_sha": resolved_ref, "commit_count": len(commits),
        "commits": commits, "repository": repository,
        "objects": sorted((obj for obj in all_objects if obj["path"] != "/"), key=lambda obj: (obj["object_type"], obj["path"])),
        "authors": sorted(authors.values(), key=lambda author: author["commits"], reverse=True),
        "author_metrics": sorted(author_totals.values(), key=lambda metric: metric["churn"], reverse=True),
    }
    _write_analysis(repo.repo_id, cache_key, result)
    return result


def _history_with_numstat(repo_path: Path, ref: str, start_ts: int | None, end_ts: int | None, selected: set[str] | None):
    fmt = "%x1e%H%x1f%P%x1f%aN <%aE>%x1f%ct"
    args = ["log", "--use-mailmap", "--no-merges", "--reverse", "--numstat", "-M50%", f"--format={fmt}"]
    if not selected:
        if start_ts is not None:
            args.append(f"--since=@{start_ts}")
        if end_ts is not None:
            args.append(f"--before=@{end_ts}")
    args.append(ref)
    output = _run_git(repo_path, args, 900)
    wanted = {sha.strip() for sha in selected or set()}
    for chunk in output.split("\x1e"):
        if not chunk.strip():
            continue
        header, *stat_lines = chunk.lstrip("\n").splitlines()
        parts = header.split("\x1f")
        if len(parts) != 4:
            continue
        sha, parents, author, timestamp = parts
        if wanted and sha not in wanted:
            continue
        commit = {"sha": sha, "short_sha": sha[:8], "parent": parents.split()[0] if parents else "", "author": author, "date": int(timestamp)}
        diffs: list[tuple[str, int, int]] = []
        for line in stat_lines:
            columns = line.split("\t")
            if len(columns) < 3 or columns[0] == "-" or columns[1] == "-":
                continue
            diffs.append((_clean_diff_path("\t".join(columns[2:])), int(columns[0]), int(columns[1])))
        yield commit, diffs


def _clean_diff_path(path: str) -> str:
    if " => " not in path:
        return path.strip()
    if path.startswith("{") and "}" in path:
        prefix, rest = path[1:].split("}", 1)
        return (prefix.split(" => ", 1)[1] + rest).strip().strip("/")
    return path.split(" => ", 1)[1].strip()


def _directories_for(path: str) -> list[str]:
    directories, current = ["/"], ""
    for piece in Path(path).parts[:-1]:
        current = f"{current}/{piece}" if current else piece
        directories.append(current + "/")
    return directories


def _blank_total(object_type: str, path: str) -> dict[str, Any]:
    return {"object_type": object_type, "path": path, "added": 0, "removed": 0, "growth": 0, "churn": 0, "modifications": 0, "modification_frequency": 0, "churn_rate": 0}


def _blank_author_total(path: str, author: str) -> dict[str, Any]:
    return {"path": path, "author": author, "added": 0, "removed": 0, "growth": 0, "churn": 0, "modifications": 0, "ownership": 0}


def _add_change(total: dict[str, Any], added: int, removed: int, growth: int, churn: int) -> None:
    for key, value in (("added", added), ("removed", removed), ("growth", growth), ("churn", churn)):
        total[key] += value


def _add_author_change(total: dict[str, Any], added: int, removed: int, growth: int, churn: int) -> None:
    _add_change(total, added, removed, growth, churn)


def _finalize_total(total: dict[str, Any], commit_count: int) -> None:
    if commit_count:
        total["modification_frequency"] = total["modifications"] / commit_count
        total["churn_rate"] = total["churn"] / commit_count


def _write_analysis(repo_id: str, cache_key: str, result: dict[str, Any]) -> None:
    ensure_data_dirs()
    (ANALYSES_DIR / f"{repo_id}-{cache_key}.json").write_text(json.dumps(result, indent=2))
    (ANALYSES_DIR / f"{repo_id}.json").write_text(json.dumps(result, indent=2))


def load_analysis(repo_id: str, cache_key: str | None = None) -> dict[str, Any] | None:
    path = ANALYSES_DIR / f"{repo_id}{'-' + cache_key if cache_key else ''}.json"
    return json.loads(path.read_text()) if path.exists() else None


def export_csv(result: dict[str, Any]) -> str:
    fields = ["repo", "ref_sha", "commit_set", "commit_count", "object_type", "path", "author", "added", "removed", "growth", "churn", "modifications", "modification_frequency", "churn_rate", "ownership"]
    base = {"repo": result["repo"]["name"], "ref_sha": result["ref_sha"], "commit_set": "all", "commit_count": result["commit_count"]}
    rows = [{**base, **obj, "author": "ALL", "ownership": ""} for obj in [result["repository"], *result["objects"]]]
    object_types = {"/": "repository", **{obj["path"]: obj["object_type"] for obj in result["objects"]}}
    rows.extend({**base, "object_type": object_types.get(metric["path"], "file"), **metric, "modification_frequency": "", "churn_rate": ""} for metric in result["author_metrics"])
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def datetime_to_timestamp(value: str) -> int | None:
    if not value:
        return None
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())
