# Repo Analysis Tool

A zero-dependency web dashboard for exploring how a Git repository changes over time. It calculates file, directory, repository, commit-set, and author metrics from non-merge commits.

## Features

- Import a repository by cloning an HTTP(S)/SSH URL or uploading a ZIP containing `.git`
- Analyze any available branch or reference
- Filter a commit set by a start/end date or an explicit list of commit SHAs
- Uses Git's `.mailmap` support to normalize contributor identities
- Measures added lines, removed lines, growth, churn, modifications, modification frequency, churn rate, and author ownership
- Detects renames with Git's 50% similarity threshold
- Exports the active analysis as CSV
- Caches identical analysis scopes locally for faster repeat lookups
- Includes searchable metric tables and a loading state for longer analyses

## Requirements

- Python 3.10+
- Git

No Python packages or Node dependencies are required.

## Run locally

```bash
git clone https://github.com/KreepKid/repo-analysis-tool
cd repo-analysis-tool
python3 app.py
```

Open `http://127.0.0.1:8000` in a browser.

## How to use it

1. On the home page, clone a remote repository or upload a repository ZIP archive.
2. Open the imported repository from the library.
3. Select a reference, optionally select a date range, and run the analysis.
4. Inspect repository summaries, object metrics, author ownership, and contributor activity.
5. Use **Download CSV** to export the displayed analysis.

### Commit filters

- **Start date:** includes commits at or after that date.
- **End date:** excludes commits at or after that date.
- **Manual commit list:** one full SHA per line; this overrides the dates.
- Leaving all filters empty analyzes all reachable non-merge commits for the selected reference.

## Metric definitions

| Metric | Definition |
| --- | --- |
| Added | Lines added by commits in the selected set |
| Removed | Lines removed by commits in the selected set |
| Growth | `added - removed` |
| Churn | `added + removed` |
| Modifications | Number of commits that changed at least one line in the object |
| Modification frequency | `modifications / commit count` |
| Churn rate | `churn / commit count` |
| Ownership | An author's churn divided by total churn for an object |

Directory metrics aggregate their immediate files and subdirectories. Repository metrics are the directory metrics for the root directory.

## Tests

Run the standard-library test suite:

```bash
python3 -m unittest discover -s tests
```

The test fixtures cover directory aggregation, URL validation, CSV export, mailmap identity normalization, rename-only commits, date boundaries, and manual commit selection.

## Data and privacy

Imported repositories and cached analyses are saved under `data/`. This directory is ignored by Git and is not included in commits.

## Publish to GitHub

The repository is configured to push to:

```text
https://github.com/KreepKid/repo-analysis-tool.git
```

After creating that public GitHub repository and authenticating Git locally, publish the current branch with:

```bash
git push -u origin main
```
