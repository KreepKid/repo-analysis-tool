from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
REPOS_DIR = DATA_DIR / "repos"
ANALYSES_DIR = DATA_DIR / "analyses"
UPLOADS_DIR = DATA_DIR / "uploads"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


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
    proc = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "git command failed")
    return proc.stdout


def _safe_repo_name(value: str) -> str:
    base = value.rstrip("/").split("/")[-1] or "repository"
    if base.endswith(".git"):
        base = base[:-4]
    clean = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in base).strip(".-_")
    return clean or "repository"


def _repo_db_path() -> Path:
    return DATA_DIR / "repos.json"


def load_repos() -> list[RepoRecord]:
    ensure_data_dirs()
    path = _repo_db_path()
    if not path.exists():
        return []
    records = json.loads(path.read_text())
    return [RepoRecord(**record) for record in records]


def save_repos(records: list[RepoRecord]) -> None:
    ensure_data_dirs()
    _repo_db_path().write_text(json.dumps([asdict(r) for r in records], indent=2))


def get_repo(repo_id: str) -> RepoRecord | None:
    return next((repo for repo in load_repos() if repo.repo_id == repo_id), None)


def clone_repository(url: str) -> RepoRecord:
    ensure_data_dirs()
    repo_id = uuid.uuid4().hex[:10]
    name = _safe_repo_name(url)
    target = REPOS_DIR / f"{repo_id}-{name}"
    proc = subprocess.run(
        ["git", "clone", "--no-single-branch", url, str(target)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=900,
        check=False,
    )
    if proc.returncode != 0:
        if target.exists():
            shutil.rmtree(target)
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "clone failed")
    record = RepoRecord(repo_id=repo_id, name=name, source_type="url", source=url, path=str(target))
    repos = load_repos()
    repos.append(record)
    save_repos(repos)
    return record


def import_zip(zip_bytes: bytes, filename: str) -> RepoRecord:
    ensure_data_dirs()
    repo_id = uuid.uuid4().hex[:10]
    name = _safe_repo_name(filename.rsplit(".", 1)[0])
    upload_path = UPLOADS_DIR / f"{repo_id}-{name}.zip"
    upload_path.write_bytes(zip_bytes)
    extract_root = REPOS_DIR / f"{repo_id}-{name}"
    extract_root.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(upload_path) as zf:
        zf.extractall(extract_root)
    repo_path = _find_git_repo(extract_root)
    if repo_path is None:
        shutil.rmtree(extract_root)
        raise RuntimeError("Uploaded ZIP does not contain a Git repository with a .git directory/file")
    record = RepoRecord(repo_id=repo_id, name=name, source_type="zip", source=filename, path=str(repo_path))
    repos = load_repos()
    repos.append(record)
    save_repos(repos)
    return record


def _find_git_repo(root: Path) -> Path | None:
    if (root / ".git").exists():
        return root
    for current, dirs, files in os.walk(root):
        if ".git" in dirs or ".git" in files:
            return Path(current)
    return None


def list_branches_and_refs(repo_path: Path) -> list[dict[str, str]]:
    refs = []
    try:
        out = _run_git(repo_path, ["for-each-ref", "--format=%(refname:short)|%(objectname)", "refs/heads", "refs/remotes"], timeout=60)
        for line in out.splitlines():
            if not line or "|" not in line:
                continue
            name, sha = line.split("|", 1)
            refs.append({"name": name, "sha": sha})
    except Exception:
        pass
    try:
        head = _run_git(repo_path, ["rev-parse", "HEAD"], timeout=30).strip()
        refs.insert(0, {"name": "HEAD", "sha": head})
    except Exception:
        pass
    return refs


def analyze_repository(repo: RepoRecord, ref: str = "HEAD", start_ts: int | None = None, end_ts: int | None = None, selected_commits: set[str] | None = None) -> dict[str, Any]:
    repo_path = Path(repo.path)
    resolved_ref = _run_git(repo_path, ["rev-parse", ref], timeout=30).strip()
    commit_rows = _commit_rows(repo_path, resolved_ref)
    if selected_commits:
        wanted = {sha.strip() for sha in selected_commits if sha.strip()}
        commit_rows = [row for row in commit_rows if row["sha"] in wanted]
    elif start_ts is not None or end_ts is not None:
        commit_rows = [row for row in commit_rows if _in_time_range(row["date"], start_ts, end_ts)]

    file_totals: dict[str, dict[str, Any]] = {}
    dir_totals: dict[str, dict[str, Any]] = {"/": _blank_total("directory", "/")}
    author_totals: dict[tuple[str, str], dict[str, Any]] = {}
    authors: dict[str, dict[str, Any]] = {}

    for idx, commit in enumerate(commit_rows, 1):
        parent = commit["parent"] or EMPTY_TREE
        diffs = _numstat(repo_path, parent, commit["sha"])
        changed_objects: set[str] = set()
        for path, added, removed in diffs:
            growth = added - removed
            churn = added + removed
            file_total = file_totals.setdefault(path, _blank_total("file", path))
            _add_change(file_total, added, removed, growth, churn)
            changed_objects.add(path)
            for directory in _directories_for(path):
                dir_total = dir_totals.setdefault(directory, _blank_total("directory", directory))
                _add_change(dir_total, added, removed, growth, churn)
                changed_objects.add(directory)
            for object_path in [path, *_directories_for(path)]:
                key = (object_path, commit["author"])
                author_total = author_totals.setdefault(key, _blank_author_total(object_path, commit["author"]))
                _add_author_change(author_total, added, removed, growth, churn)
        for object_path in changed_objects:
            if object_path in file_totals:
                file_totals[object_path]["modifications"] += 1
            if object_path in dir_totals:
                dir_totals[object_path]["modifications"] += 1
            key = (object_path, commit["author"])
            if key in author_totals:
                author_totals[key]["modifications"] += 1
        author = authors.setdefault(commit["author"], {"author": commit["author"], "commits": 0})
        author["commits"] += 1

    all_objects = list(dir_totals.values()) + list(file_totals.values())
    for total in all_objects:
        _finalize_total(total, len(commit_rows))
    author_metrics = list(author_totals.values())
    by_path_churn = {obj["path"]: obj["churn"] for obj in all_objects}
    for metric in author_metrics:
        base_churn = by_path_churn.get(metric["path"], 0)
        metric["ownership"] = metric["churn"] / base_churn if base_churn else 0
    repo_row = dir_totals.get("/", _blank_total("directory", "/"))
    repo_row = {**repo_row, "object_type": "repository"}
    display_objects = [obj for obj in all_objects if obj["path"] != "/"]

    result = {
        "repo": asdict(repo),
        "ref": ref,
        "ref_sha": resolved_ref,
        "commit_count": len(commit_rows),
        "commits": commit_rows,
        "repository": repo_row,
        "objects": sorted(display_objects, key=lambda row: (row["object_type"], row["path"])),
        "authors": sorted(authors.values(), key=lambda row: row["commits"], reverse=True),
        "author_metrics": sorted(author_metrics, key=lambda row: row["churn"], reverse=True),
    }
    _write_analysis(repo.repo_id, result)
    return result


def _commit_rows(repo_path: Path, ref: str) -> list[dict[str, Any]]:
    fmt = "%H%x1f%P%x1f%aN <%aE>%x1f%ct"
    out = _run_git(repo_path, ["log", "--use-mailmap", "--no-merges", "--reverse", f"--format={fmt}", ref], timeout=300)
    rows = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        sha, parents, author, ts = parts
        parent = parents.split()[0] if parents else ""
        rows.append({"sha": sha, "short_sha": sha[:8], "parent": parent, "author": author, "date": int(ts)})
    return rows


def _numstat(repo_path: Path, parent: str, sha: str) -> list[tuple[str, int, int]]:
    out = _run_git(repo_path, ["diff", "--numstat", "-M50%", parent, sha], timeout=300)
    rows: list[tuple[str, int, int]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added_raw, removed_raw, path_raw = parts[0], parts[1], "\t".join(parts[2:])
        if added_raw == "-" or removed_raw == "-":
            continue
        path = _clean_diff_path(path_raw)
        rows.append((path, int(added_raw), int(removed_raw)))
    return rows


def _clean_diff_path(path: str) -> str:
    if " => " not in path:
        return path.strip()
    if path.startswith("{") and "}" in path:
        prefix, rest = path[1:].split("}", 1)
        _, new_name = prefix.split(" => ", 1)
        return (new_name + rest).strip().strip("/")
    return path.split(" => ", 1)[1].strip()


def _directories_for(path: str) -> list[str]:
    pieces = Path(path).parts[:-1]
    dirs = ["/"]
    current = ""
    for piece in pieces:
        current = f"{current}/{piece}" if current else piece
        dirs.append(current + "/")
    return dirs


def _blank_total(object_type: str, path: str) -> dict[str, Any]:
    return {"object_type": object_type, "path": path, "added": 0, "removed": 0, "growth": 0, "churn": 0, "modifications": 0, "modification_frequency": 0, "churn_rate": 0}


def _blank_author_total(path: str, author: str) -> dict[str, Any]:
    return {"path": path, "author": author, "added": 0, "removed": 0, "growth": 0, "churn": 0, "modifications": 0, "ownership": 0}


def _add_change(total: dict[str, Any], added: int, removed: int, growth: int, churn: int) -> None:
    total["added"] += added
    total["removed"] += removed
    total["growth"] += growth
    total["churn"] += churn


def _add_author_change(total: dict[str, Any], added: int, removed: int, growth: int, churn: int) -> None:
    total["added"] += added
    total["removed"] += removed
    total["growth"] += growth
    total["churn"] += churn


def _finalize_total(total: dict[str, Any], commit_count: int) -> None:
    if commit_count:
        total["modification_frequency"] = total["modifications"] / commit_count
        total["churn_rate"] = total["churn"] / commit_count


def _in_time_range(value: int, start_ts: int | None, end_ts: int | None) -> bool:
    if start_ts is not None and value < start_ts:
        return False
    if end_ts is not None and value >= end_ts:
        return False
    return True


def _write_analysis(repo_id: str, result: dict[str, Any]) -> None:
    ensure_data_dirs()
    (ANALYSES_DIR / f"{repo_id}.json").write_text(json.dumps(result, indent=2))


def load_analysis(repo_id: str) -> dict[str, Any] | None:
    path = ANALYSES_DIR / f"{repo_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def export_csv(result: dict[str, Any]) -> str:
    fields = ["repo", "ref_sha", "commit_set", "commit_count", "object_type", "path", "author", "added", "removed", "growth", "churn", "modifications", "modification_frequency", "churn_rate", "ownership"]
    rows: list[dict[str, Any]] = []
    repo_name = result["repo"]["name"]
    base = {"repo": repo_name, "ref_sha": result["ref_sha"], "commit_set": "all", "commit_count": result["commit_count"]}
    for obj in [result["repository"], *result["objects"]]:
        rows.append({**base, **obj, "author": "ALL", "ownership": ""})
    by_type = {obj["path"]: obj["object_type"] for obj in result["objects"]}
    by_type["/"] = "repository"
    for metric in result["author_metrics"]:
        rows.append({**base, "object_type": by_type.get(metric["path"], "file"), **metric, "modification_frequency": "", "churn_rate": ""})
    import io
    handle = io.StringIO()
    writer = csv.DictWriter(handle, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return handle.getvalue()
