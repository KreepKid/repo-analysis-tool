from __future__ import annotations

import html
import mimetypes
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import cgi

from rat_core import (
    analyze_repository,
    clone_repository,
    export_csv,
    get_repo,
    import_zip,
    list_branches_and_refs,
    load_analysis,
    load_repos,
)

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)} · RAT</title>
  <link rel="stylesheet" href="/static/styles.css">
</head>
<body>
  <header>
    <a class="brand" href="/">RAT</a>
    <nav><a href="/">Repositories</a></nav>
  </header>
  <main>{body}</main>
</body>
</html>""".encode()


def fmt_num(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def metrics_table(rows: list[dict], limit: int = 100) -> str:
    if not rows:
        return "<p>No metrics available yet.</p>"
    headers = ["object_type", "path", "author", "added", "removed", "growth", "churn", "modifications", "modification_frequency", "churn_rate", "ownership"]
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body_rows = []
    for row in rows[:limit]:
        body_rows.append("<tr>" + "".join(f"<td>{esc(fmt_num(row.get(h, '')))}</td>" for h in headers) + "</tr>")
    return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>"


class App(BaseHTTPRequestHandler):
    server_version = "RAT/0.1"

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
            parsed = urlparse(self.path)
            if parsed.path == "/clone":
                self.clone()
            elif parsed.path == "/upload":
                self.upload()
            elif parsed.path == "/analyze":
                self.analyze()
            else:
                self.not_found()
        except Exception as exc:
            self.error_page(exc)

    def index(self) -> None:
        repos = load_repos()
        cards = []
        for repo in repos:
            analysis = load_analysis(repo.repo_id)
            status = f"{analysis['commit_count']} commits analyzed" if analysis else "Not analyzed yet"
            cards.append(f"""
            <li class="repo-card">
              <h3><a href="/repo?id={esc(repo.repo_id)}">{esc(repo.name)}</a></h3>
              <p><strong>Source:</strong> {esc(repo.source_type)} · {esc(repo.source)}</p>
              <p><strong>Status:</strong> {esc(status)}</p>
            </li>""")
        repo_list = "<ul class='repo-list'>" + "".join(cards) + "</ul>" if cards else "<p>No repositories imported yet.</p>"
        body = f"""
        <section class="hero">
          <h1>Repo Analysis Tool</h1>
          <p>Analyze Git repository churn, growth, modification frequency, and author ownership.</p>
        </section>
        <section class="grid">
          <form class="panel" method="post" action="/clone">
            <h2>Clone repository URL</h2>
            <label>Remote Git URL <input required name="url" placeholder="https://github.com/DaveGamble/cJSON.git"></label>
            <button>Clone</button>
          </form>
          <form class="panel" method="post" action="/upload" enctype="multipart/form-data">
            <h2>Upload repository ZIP</h2>
            <label>ZIP containing .git <input required type="file" name="repozip" accept=".zip"></label>
            <button>Upload</button>
          </form>
        </section>
        <section class="panel">
          <h2>Repositories</h2>
          {repo_list}
        </section>"""
        self.send_html(page("Repositories", body))

    def repo_detail(self, query: dict[str, list[str]]) -> None:
        repo_id = query.get("id", [""])[0]
        repo = get_repo(repo_id)
        if repo is None:
            self.not_found("Repository not found")
            return
        refs = list_branches_and_refs(Path(repo.path))
        analysis = load_analysis(repo.repo_id)
        ref_options = "".join(f"<option value='{esc(ref['name'])}'>{esc(ref['name'])} · {esc(ref['sha'][:8])}</option>" for ref in refs)
        analyze_form = f"""
        <form class="panel" method="post" action="/analyze">
          <input type="hidden" name="repo_id" value="{esc(repo.repo_id)}">
          <h2>Analyze</h2>
          <div class="form-row">
            <label>Reference <select name="ref">{ref_options}</select></label>
            <label>Start UNIX time <input name="start_ts" placeholder="optional"></label>
            <label>End UNIX time <input name="end_ts" placeholder="optional"></label>
          </div>
          <label>Manual commit list <textarea name="commits" rows="4" placeholder="Optional full commit SHAs, one per line. If provided, date filters are ignored."></textarea></label>
          <button>Run analysis</button>
        </form>"""
        if not analysis:
            body = f"<a href='/'>← Back</a><h1>{esc(repo.name)}</h1>{analyze_form}"
            self.send_html(page(repo.name, body))
            return
        repo_row = analysis["repository"]
        summary = f"""
        <section class="summary">
          <div><span>Commits</span><strong>{esc(analysis['commit_count'])}</strong></div>
          <div><span>Added</span><strong>{esc(repo_row['added'])}</strong></div>
          <div><span>Removed</span><strong>{esc(repo_row['removed'])}</strong></div>
          <div><span>Churn</span><strong>{esc(repo_row['churn'])}</strong></div>
        </section>"""
        authors = "".join(f"<li>{esc(a['author'])} <span>{esc(a['commits'])} commits</span></li>" for a in analysis["authors"][:20])
        all_rows = [analysis["repository"], *analysis["objects"]]
        for row in all_rows:
            row.setdefault("author", "ALL")
            row.setdefault("ownership", "")
        body = f"""
        <a href='/'>← Back</a>
        <h1>{esc(repo.name)}</h1>
        <p class="muted">Ref {esc(analysis['ref'])} · {esc(analysis['ref_sha'])}</p>
        {summary}
        <p><a class="button-link" href="/export.csv?id={esc(repo.repo_id)}">Download CSV</a></p>
        {analyze_form}
        <section class="panel">
          <h2>Top authors</h2>
          <ul class="authors">{authors}</ul>
        </section>
        <section class="panel">
          <h2>Object metrics</h2>
          {metrics_table(all_rows, 150)}
        </section>
        <section class="panel">
          <h2>Author ownership metrics</h2>
          {metrics_table(analysis['author_metrics'], 150)}
        </section>"""
        self.send_html(page(repo.name, body))

    def clone(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        data = parse_qs(self.rfile.read(length).decode())
        url = data.get("url", [""])[0].strip()
        if not url:
            raise RuntimeError("Repository URL is required")
        repo = clone_repository(url)
        self.redirect(f"/repo?id={repo.repo_id}")

    def upload(self) -> None:
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type")})
        item = form["repozip"] if "repozip" in form else None
        if item is None or not item.filename:
            raise RuntimeError("ZIP upload is required")
        repo = import_zip(item.file.read(), item.filename)
        self.redirect(f"/repo?id={repo.repo_id}")

    def analyze(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        data = parse_qs(self.rfile.read(length).decode())
        repo_id = data.get("repo_id", [""])[0]
        repo = get_repo(repo_id)
        if repo is None:
            raise RuntimeError("Repository not found")
        ref = data.get("ref", ["HEAD"])[0] or "HEAD"
        start_ts = self.optional_int(data.get("start_ts", [""])[0])
        end_ts = self.optional_int(data.get("end_ts", [""])[0])
        commits_raw = data.get("commits", [""])[0]
        commits = {line.strip() for line in commits_raw.splitlines() if line.strip()} or None
        analyze_repository(repo, ref=ref, start_ts=start_ts, end_ts=end_ts, selected_commits=commits)
        self.redirect(f"/repo?id={repo.repo_id}")

    def export(self, query: dict[str, list[str]]) -> None:
        repo_id = query.get("id", [""])[0]
        analysis = load_analysis(repo_id)
        if analysis is None:
            self.not_found("No analysis found")
            return
        payload = export_csv(analysis).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename={analysis['repo']['name']}-metrics.csv")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def serve_static(self, path: str) -> None:
        target = STATIC / path.removeprefix("/static/")
        if not target.exists() or not target.is_file():
            self.not_found()
            return
        payload = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def not_found(self, message: str = "Not found") -> None:
        self.send_response(HTTPStatus.NOT_FOUND)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page("Not found", f"<section class='panel'><h1>{esc(message)}</h1><p><a href='/'>Back home</a></p></section>"))

    def error_page(self, exc: Exception) -> None:
        details = esc(traceback.format_exc())
        self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page("Error", f"<section class='panel error'><h1>{esc(exc)}</h1><pre>{details}</pre><p><a href='/'>Back home</a></p></section>"))

    def send_html(self, payload: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    @staticmethod
    def optional_int(value: str) -> int | None:
        value = value.strip()
        return int(value) if value else None


def main() -> None:
    host = "127.0.0.1"
    port = 8000
    server = ThreadingHTTPServer((host, port), App)
    print(f"RAT running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
