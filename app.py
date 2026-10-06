from __future__ import annotations

import cgi
import html
import json
import mimetypes
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from rat_core import analyze_repository, clone_repository, export_csv, get_repo, import_zip, list_branches_and_refs, load_analysis, load_repos

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)} · RAT</title><link rel="stylesheet" href="/static/styles.css"><script defer src="/static/app.js"></script></head><body><header><a class="brand" href="/"><span class="brand-mark">↗</span> Repo Analysis Tool</a><nav><a href="/">Repository library</a></nav></header><main>{body}</main><div id="loading-modal" class="modal" hidden role="dialog" aria-modal="true" aria-live="polite"><div class="modal-card"><div class="spinner"></div><p class="eyebrow">ANALYSIS IN PROGRESS</p><h2 id="loading-title">Reading Git history</h2><p id="loading-copy">This can take a moment for repositories with many commits.</p></div></div></body></html>""".encode()


def fmt(value: object) -> str:
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def metrics_table(rows: list[dict], table_id: str) -> str:
    if not rows:
        return "<p class='muted'>No metrics match this analysis.</p>"
    columns = ["object_type", "path", "author", "added", "removed", "growth", "churn", "modifications", "modification_frequency", "churn_rate", "ownership"]
    header = "".join(f"<th>{esc(column.replace('_', ' '))}</th>" for column in columns)
    table_rows = "".join("<tr>" + "".join(f"<td>{esc(fmt(row.get(column, '')))}</td>" for column in columns) + "</tr>" for row in rows)
    return f"<label class='table-search'>Filter results <input data-table-filter='{table_id}' placeholder='Filter by path or author'></label><div class='table-wrap'><table id='{table_id}'><thead><tr>{header}</tr></thead><tbody>{table_rows}</tbody></table></div>"


class App(BaseHTTPRequestHandler):
    server_version = "RAT/0.2"

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/static/"):
                self.serve_static(parsed.path)
            elif parsed.path == "/":
                self.index()
            elif parsed.path == "/repo":
                self.repo_detail(parse_qs(parsed.query))
            elif parsed.path == "/export.csv":
                self.export(parse_qs(parsed.query))
            else:
                self.not_found()
        except Exception as exc:
            self.error_page(exc)

    def do_POST(self) -> None:
        try:
            route = urlparse(self.path).path
            if route == "/clone": self.clone()
            elif route == "/upload": self.upload()
            elif route == "/analyze": self.analyze()
            else: self.not_found()
        except (ValueError, RuntimeError) as exc:
            self.request_error(str(exc))
        except Exception as exc:
            self.error_page(exc)

    def index(self) -> None:
        cards = []
        for repo in load_repos():
            analysis = load_analysis(repo.repo_id)
            status = f"{analysis['commit_count']:,} commits analyzed" if analysis else "Ready to analyze"
            cards.append(f"<li class='repo-card'><div><p class='eyebrow'>{esc(repo.source_type)}</p><h3><a href='/repo?id={esc(repo.repo_id)}'>{esc(repo.name)}</a></h3><p>{esc(repo.source)}</p></div><span class='status'>{esc(status)}</span></li>")
        repository_list = "<ul class='repo-list'>" + "".join(cards) + "</ul>" if cards else "<div class='empty'><strong>Your library is empty.</strong><span>Clone a Git URL or upload a repository archive to start.</span></div>"
        body = f"""<section class='hero'><div><p class='eyebrow'>GIT INTELLIGENCE</p><h1>Make project history <em>visible.</em></h1><p>Explore repository change, contributor ownership, and volatile code paths from one focused dashboard.</p></div><aside><span>Built for</span><strong>Git repositories</strong><small>Files · directories · authors · commits</small></aside></section><section class='import-grid'><form class='panel import-panel' method='post' action='/clone'><p class='eyebrow'>01 / REMOTE</p><h2>Clone from a URL</h2><p>Bring in a remote Git repository with its complete history.</p><label>Remote Git URL<input required name='url' placeholder='https://github.com/org/repo.git'></label><button>Clone repository <span>→</span></button></form><form class='panel import-panel' method='post' action='/upload' enctype='multipart/form-data'><p class='eyebrow'>02 / ARCHIVE</p><h2>Upload a repository</h2><p>Upload a ZIP archive that includes its <code>.git</code> directory.</p><label>Repository ZIP<input required type='file' name='repozip' accept='.zip'></label><button class='gold'>Upload archive <span>↑</span></button></form></section><section class='section-heading'><div><p class='eyebrow'>YOUR LIBRARY</p><h2>Repositories</h2></div><span>{len(load_repos())} imported</span></section>{repository_list}"""
        self.send_html(page("Repositories", body))

    def repo_detail(self, query: dict[str, list[str]]) -> None:
        repo = get_repo(query.get("id", [""])[0])
        if not repo:
            return self.not_found("Repository not found")
        refs = list_branches_and_refs(Path(repo.path))
        options = "".join(f"<option value='{esc(item['name'])}'>{esc(item['name'])} · {esc(item['sha'][:8])}</option>" for item in refs)
        form = f"""<form class='panel analysis-form' method='post' action='/analyze'><input type='hidden' name='repo_id' value='{esc(repo.repo_id)}'><div class='panel-heading'><div><p class='eyebrow'>SCOPE</p><h2>Run an analysis</h2></div><button>Analyze history <span>→</span></button></div><div class='form-row'><label>Reference<select name='ref'>{options}</select></label><label>Start date <input type='datetime-local' name='start_datetime'><small>Include commits from this date.</small></label><label>End date <input type='datetime-local' name='end_datetime'><small>Exclude commits from this date.</small></label></div><label>Manual commit list<textarea name='commits' rows='3' placeholder='Optional full commit SHAs, one per line. This overrides the dates.'></textarea></label><p class='form-note'>Dates filter by a commit’s committer date. Leave both blank to analyze all non-merge commits.</p></form>"""
        analysis = load_analysis(repo.repo_id)
        if not analysis:
            return self.send_html(page(repo.name, f"<a class='back' href='/'>← Repository library</a><section class='repo-title'><p class='eyebrow'>REPOSITORY</p><h1>{esc(repo.name)}</h1><p>{esc(repo.source)}</p></section>{form}"))
        root = analysis["repository"]
        summary = "".join(f"<article><span>{label}</span><strong>{value:,}</strong></article>" for label, value in [("Commit set", analysis["commit_count"]), ("Lines added", root["added"]), ("Lines removed", root["removed"]), ("Total churn", root["churn"])])
        rows = [analysis["repository"], *analysis["objects"]]
        for row in rows:
            row.setdefault("author", "ALL"); row.setdefault("ownership", "")
        authors = "".join(f"<li><span>{esc(item['author'])}</span><strong>{item['commits']}</strong></li>" for item in analysis["authors"][:10])
        body = f"""<a class='back' href='/'>← Repository library</a><section class='repo-title'><div><p class='eyebrow'>REPOSITORY ANALYSIS</p><h1>{esc(repo.name)}</h1><p>Reference <code>{esc(analysis['ref_sha'][:12])}</code> · {esc(analysis['ref'])}</p></div><a class='button-link gold' href='/export.csv?id={esc(repo.repo_id)}'>Download CSV</a></section><section class='summary'>{summary}</section>{form}<section class='insight-grid'><article class='panel'><p class='eyebrow'>TOP CONTRIBUTORS</p><h2>Commit activity</h2><ol class='authors'>{authors}</ol></article><article class='panel metric-guide'><p class='eyebrow'>METRIC GUIDE</p><h2>Read the signals</h2><dl><dt>Growth</dt><dd>Added lines minus removed lines.</dd><dt>Churn</dt><dd>All added and removed lines.</dd><dt>Ownership</dt><dd>An author’s share of an object’s churn.</dd></dl></article></section><section class='panel'><p class='eyebrow'>CHANGE MAP</p><h2>Object metrics</h2>{metrics_table(rows, 'object-metrics')}</section><section class='panel'><p class='eyebrow'>CONTRIBUTOR MAP</p><h2>Author ownership</h2>{metrics_table(analysis['author_metrics'], 'author-metrics')}</section>"""
        self.send_html(page(repo.name, body))

    def clone(self) -> None:
        data = self.read_form()
        repo = clone_repository(data.get("url", [""])[0])
        self.redirect(f"/repo?id={repo.repo_id}")

    def upload(self) -> None:
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type")})
        item = form["repozip"] if "repozip" in form else None
        if not item or not item.filename: raise ValueError("Choose a ZIP archive first.")
        repo = import_zip(item.file.read(), item.filename)
        self.redirect(f"/repo?id={repo.repo_id}")

    def analyze(self) -> None:
        data = self.read_form()
        repo = get_repo(data.get("repo_id", [""])[0])
        if not repo: raise ValueError("Repository not found.")
        start = self.datetime_field(data.get("start_datetime", [""])[0])
        end = self.datetime_field(data.get("end_datetime", [""])[0])
        commits = {line.strip() for line in data.get("commits", [""])[0].splitlines() if line.strip()} or None
        analyze_repository(repo, data.get("ref", ["HEAD"])[0] or "HEAD", start, end, commits)
        if self.wants_json(): self.send_json({"redirect": f"/repo?id={repo.repo_id}"})
        else: self.redirect(f"/repo?id={repo.repo_id}")

    def export(self, query: dict[str, list[str]]) -> None:
        analysis = load_analysis(query.get("id", [""])[0])
        if not analysis: return self.not_found("No analysis found")
        payload = export_csv(analysis).encode()
        self.send_response(200); self.send_header("Content-Type", "text/csv; charset=utf-8"); self.send_header("Content-Disposition", f"attachment; filename={analysis['repo']['name']}-metrics.csv"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)

    def read_form(self) -> dict[str, list[str]]:
        return parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())

    @staticmethod
    def datetime_field(value: str) -> int | None:
        if not value: return None
        from datetime import datetime
        return int(datetime.fromisoformat(value).timestamp())

    def wants_json(self) -> bool:
        return self.headers.get("Accept", "").startswith("application/json")

    def serve_static(self, path: str) -> None:
        target = STATIC / path.removeprefix("/static/")
        if not target.is_file(): return self.not_found()
        payload = target.read_bytes(); self.send_response(200); self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)

    def request_error(self, message: str) -> None:
        if self.wants_json(): return self.send_json({"error": message}, 400)
        self.send_response(400); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(page("Input error", f"<section class='panel'><h1>We could not start that.</h1><p>{esc(message)}</p><p><a href='javascript:history.back()'>Go back</a></p></section>"))

    def send_json(self, data: dict, status: int = 200) -> None:
        payload = json.dumps(data).encode(); self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)

    def not_found(self, message: str = "Not found") -> None:
        self.send_response(404); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(page("Not found", f"<section class='panel'><h1>{esc(message)}</h1><a href='/'>Back home</a></section>"))

    def error_page(self, exc: Exception) -> None:
        traceback.print_exc(); self.send_response(500); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(page("Error", "<section class='panel'><h1>Unexpected server error</h1><p>Please try again.</p><a href='/'>Back home</a></section>"))

    def send_html(self, payload: bytes) -> None:
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)

    def redirect(self, location: str) -> None:
        self.send_response(303); self.send_header("Location", location); self.end_headers()


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8000), App)
    print("RAT running at http://127.0.0.1:8000")
    server.serve_forever()


if __name__ == "__main__": main()
