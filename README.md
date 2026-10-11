# USYNC

**Sync university learning resources into organised offline archives.**

USYNC is an experimental command-line tool that collects accessible course materials from Canvas, Ed, and Echo360 into a local semester/unit directory, and creates a lean ZIP for study workflows. It was originally developed for the University of Sydney.

> **Status: alpha (v0.3.0).** Tested integration behaviour is currently limited; this is a source-install development release, not yet a supported PyPI release. Ed and Echo360 interfaces can change. Don't treat an API response as permission to distribute someone else's materials.

## Features

- Sync an entire configured semester or one unit.
- Collect Canvas modules, pages, downloadable files, assignments, announcements, quizzes and accessible personal grades/feedback.
- Collect Ed lesson structures, slides, discussions, challenges and discovered workspace resources.
- Attempt Echo360 transcript collection through a saved browser session.
- Keep `latest_dump/` as the complete local collection, and generate a lean `<UNIT>.zip` for study use.
- Log source status/warnings to `MANIFEST.json` and preserve the previous source snapshot on an individual-source failure.

## Quick start (macOS / Linux)

Requires Python 3.11+ and Git. The setup script installs Chromium for browser-based sign-in.

```sh
git clone https://github.com/Its-Rajdip-Shah/USYNC.git
cd USYNC
./install.sh
source .venv/bin/activate
```

Then edit `config.yaml` for **your** university, semester, and unit codes; it's created from `config.example.yaml` on first installation.

If your sources require tokens, copy `tokens.env.example` to `tokens.env` and add your own credentials. Do **not** share or commit tokens. To set up browser authentication, run `usync login` and complete all required sign-ins yourself.

```sh
usync check
usync login
usync SOFT2201  # replace with one of your configured unit codes
usync             # sync all configured units
usync pack SOFT2201  # regenerate a ZIP without redownloading
```

For command descriptions, run `usync --help`. `usync check` only checks local setup, not server-side permissions or login validity.

### Windows

`install.sh` is intended for POSIX shells. On Windows, create/activate a Python virtual environment, install `pip install -e ".[browser]"`, install Chromium with `python -m playwright install chromium`, and copy the config example manually. Windows end-to-end support is not yet tested.

## Output and privacy

```text
<semester>/<UNIT>/latest_dump/       complete local capture
<semester>/<UNIT>/<UNIT>.zip          lean study archive
```

Archives may contain grades, submission history, assignment feedback, course material, and other personal or copyrighted data. **Do not publish these archives or session credentials.** `.gitignore` intentionally excludes them. Review the data-sharing and usage rules of each university service before using USYNC.

## Known limitations

- Initially built for Sydney Canvas, Ed Australia (`edstem.org/au`) and Echo360 Australia; other institutions are not verified.
- Authentication and source-discovery flows depend on external services and may break.
- There is no automatic setup wizard or background/scheduled sync yet.
- The existing single-file implementation will be modularised incrementally after regression coverage is established.
- When one source fails, its previous saved data may be retained; check `MANIFEST.json` for freshness.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and the [roadmap](docs/ROADMAP.md). Please do not include real tokens, HARs, session cookies, grades or course materials in issues or test fixtures. Security disclosures: [SECURITY.md](SECURITY.md).

## Licence

MIT. See [LICENSE](LICENSE). USYNC is an independent project, not an official Canvas, Ed or Echo360 product.
