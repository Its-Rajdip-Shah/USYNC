# Contributing to USYNC

Thanks for helping make USYNC easier and safer for students to use.

## Before opening a pull request

1. Check whether an issue already covers the change. For new integrations, describe the service and its documented access requirements.
2. Create a branch from `main` with a descriptive name.
3. Use a local virtual environment, then install `python -m pip install -e ".[browser,dev]"`.
4. Run `python -m pytest` and `python -m compileall -q usync.py`.
5. Explain what changed, how you tested it, and which services/operating systems it affects.

Keep changes reasonably scoped. Avoid refactoring the entire downloader at the same time as changing its behaviour. Prefer tests using local fixtures and mocked HTTP rather than live university services.

**Never upload** access tokens, browser profiles, HARs, downloaded course resources, personal grades, student details, or private API responses. Use invented/sample fixture data. Do not assist in bypassing access controls or rate limits.

For sensitive security issues, use the private reporting path described in [SECURITY.md](SECURITY.md), not a public issue.
