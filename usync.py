from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import json
import mimetypes
import os
import random
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".cache"
CONFIG_PATH = ROOT / "config.yaml"
BROWSER_STATE = ROOT / ".usync-browser-state.json"
USER_AGENT = "usync/0.3.0"


def safe_name(value: Any) -> str:
    text = html.unescape(str(value or "untitled"))
    text = re.sub(r'[<>:"/\\|?*]', "_", text)
    return re.sub(r"\s+", " ", text).strip()[:180]


def html_text(value: str | None) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    return soup.get_text("\n", strip=True)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from iter_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_strings(child)


def embedded_urls(value: Any) -> set[str]:
    found: set[str] = set()
    for text in iter_strings(value):
        text = html.unescape(text.replace("\\/", "/"))
        for match in re.findall(r'''https?://[^\s"'<>]+''', text):
            found.add(match.rstrip(".,);]"))
        if "<" in text:
            soup = BeautifulSoup(text, "html.parser")
            for tag in soup.find_all(True):
                for key in ("href", "src", "data-api-endpoint", "data-download-url"):
                    if tag.get(key):
                        found.add(str(tag[key]))
    return found


def content_filename(response: requests.Response, fallback: str) -> str:
    cd = response.headers.get("content-disposition", "")
    star = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.I)
    plain = re.search(r'filename="?([^";]+)', cd, re.I)
    name = unquote((star or plain).group(1)) if (star or plain) else fallback
    ctype = response.headers.get("content-type", "").split(";", 1)[0]
    if "." not in Path(name).name:
        name += mimetypes.guess_extension(ctype) or ""
    return safe_name(Path(name).name)


def looks_like_file(response: requests.Response) -> bool:
    ctype = response.headers.get("content-type", "").lower()
    return response.ok and not ("text/html" in ctype and b"<html" in response.content[:1000].lower())


def unique_destination(directory: Path, name: str, data: bytes) -> tuple[Path, bool]:
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    candidate = directory / safe_name(name)
    if candidate.exists() and hashlib.sha256(candidate.read_bytes()).hexdigest() == digest:
        return candidate, False
    if candidate.exists():
        candidate = candidate.with_name(f"{candidate.stem}_{digest[:8]}{candidate.suffix}")
    return candidate, True


def get_with_scoped_credentials(session: requests.Session, url: str, trusted_base: str, **kwargs: Any) -> requests.Response:
    """Never transmit API credentials to a different origin, even on redirects.

    Canvas/Ed can return signed CDN links. Each redirect is checked before
    following it, because requests may retain custom headers such as x-token
    across cross-origin redirects.
    """
    trusted = urlparse(trusted_base)
    follow = kwargs.pop("allow_redirects", True)
    current = url
    for _ in range(10):
        requested = urlparse(current)
        if requested.scheme != "https":
            raise ValueError(f"Refusing non-HTTPS download URL: {current}")
        same_origin = (requested.scheme, requested.netloc.lower()) == (trusted.scheme, trusted.netloc.lower())
        getter = session.get if same_origin else requests.get
        response = getter(current, allow_redirects=False, **kwargs)
        if follow and response.status_code in (301, 302, 303, 307, 308) and response.headers.get("Location"):
            current = urljoin(current, response.headers["Location"])
            continue
        return response
    raise RuntimeError("Too many download redirects")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"Missing configuration: {CONFIG_PATH}")
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}


def load_secrets() -> tuple[str | None, str | None]:
    load_dotenv(ROOT / "tokens.env", override=False)
    canvas = os.getenv("CANVAS_API_TOKEN") or os.getenv("canvas_api_token")
    return canvas, os.getenv("X_TOKEN")


def restore_browser_cookies(context: Any) -> None:
    """Restore explicitly exported cookies, including session-only SSO cookies."""
    if not BROWSER_STATE.exists():
        return
    try:
        state = json.loads(BROWSER_STATE.read_text(encoding="utf-8"))
        cookies = state.get("cookies", [])
        if cookies:
            context.add_cookies(cookies)
    except Exception:
        pass


class Http:
    def __init__(self, headers: dict[str, str]):
        self.s = requests.Session()
        self.s.headers.update(headers)

    def pages(self, url: str, params: dict[str, Any] | None = None) -> list[Any]:
        results: list[Any] = []
        trusted_origin = urlparse(url)
        first = True
        while url:
            requested = urlparse(url)
            if (requested.scheme, requested.netloc.lower()) != (trusted_origin.scheme, trusted_origin.netloc.lower()):
                raise RuntimeError("Refusing cross-origin API pagination")
            r = get_with_scoped_credentials(self.s, url, trusted_origin.geturl(), params=params if first else None, timeout=90)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                return [data]
            results.extend(data)
            url = r.links.get("next", {}).get("url", "")
            first = False
        return results


def pick_course(courses: list[dict[str, Any]], unit: str, year: int) -> dict[str, Any]:
    unit_upper = unit.upper()
    matches = []
    for course in courses:
        hay = " ".join(str(course.get(k, "")) for k in ("name", "title", "code", "course_code", "original_name")).upper()
        if unit_upper in hay:
            matches.append(course)
    year_matches = [c for c in matches if str(year) in json.dumps(c)]
    if len(year_matches) == 1:
        return year_matches[0]
    if len(matches) == 1:
        return matches[0]
    names = [f"{c.get('name')} (id={c.get('id')})" for c in (year_matches or matches)]
    raise RuntimeError(f"Course discovery for {unit} was {'ambiguous: ' + '; '.join(names) if names else 'empty'}")


def canvas_dump(unit: str, year: int, target: Path, token: str, base: str) -> dict[str, Any]:
    api = base.rstrip("/") + "/api/v1"
    http = Http({"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": USER_AGENT})
    courses = http.pages(f"{api}/courses", {"enrollment_state": "all", "state[]": ["available", "completed"], "include[]": ["term"], "per_page": 100})
    course = pick_course(courses, unit, year)
    cid = course["id"]
    out = target / "canvas"
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "course.json", course)

    warnings: list[str] = []

    def optional_pages(label: str, url: str, params: dict[str, Any] | None = None) -> list[Any]:
        """Canvas features may be disabled per course; do not abort the whole dump."""
        try:
            return http.pages(url, params)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (401, 403, 404):
                warnings.append(f"{label}: unavailable (HTTP {status})")
                return []
            raise

    modules = optional_pages("modules", f"{api}/courses/{cid}/modules", {"per_page": 100, "include[]": "items"})
    module_items = []
    for module in modules:
        items = optional_pages(
            f"module {module.get('name') or module['id']}",
            f"{api}/courses/{cid}/modules/{module['id']}/items",
            {"per_page": 100},
        )
        module_items.append({"module": module, "items": items})
    write_json(out / "modules.json", modules)
    write_json(out / "module_items.json", module_items)

    pages = optional_pages("pages", f"{api}/courses/{cid}/pages", {"per_page": 100})
    write_json(out / "pages_index.json", pages)
    # A course may disable the Pages index while pages linked from modules remain
    # readable.  Treat module item URLs as the authoritative content graph.
    page_targets: list[tuple[str, str]] = []
    for item in pages:
        slug = item.get("url")
        if slug: page_targets.append((item.get("title") or slug, f"{api}/courses/{cid}/pages/{slug}"))
    for group in module_items:
        for item in group["items"]:
            if item.get("type") == "Page" and item.get("url"):
                page_targets.append((item.get("title") or "Page", item["url"]))
    combined, page_records, seen_page_urls = [], [], set()
    for title_hint, page_url in page_targets:
        if page_url in seen_page_urls: continue
        seen_page_urls.add(page_url)
        try:
            r = get_with_scoped_credentials(http.s, page_url, base, timeout=60)
            r.raise_for_status()
            page = r.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            warnings.append(f"page {title_hint}: unavailable (HTTP {status})")
            continue
        page_records.append(page)
        title, body = page.get("title") or title_hint, page.get("body") or ""
        stem = f"{len(page_records):03d}_{safe_name(title)}"
        write_json(out / "pages" / f"{stem}.json", page)
        (out / "pages" / f"{stem}.html").write_text(body, encoding="utf-8")
        (out / "pages" / f"{stem}.txt").write_text(f"{title}\n{'=' * len(title)}\n\n{html_text(body)}\n", encoding="utf-8")
        combined.append(f"{'='*80}\n{title}\n{'='*80}\n\n{html_text(body)}")
    (out / "ALL_CANVAS_PAGES.txt").write_text("\n\n".join(combined), encoding="utf-8")

    files = optional_pages("files", f"{api}/courses/{cid}/files", {"per_page": 100})
    write_json(out / "files_index.json", files)
    file_records: dict[int, dict[str, Any]] = {int(x["id"]): x for x in files if x.get("id")}
    candidate_file_apis: set[str] = set()
    candidate_downloads: set[str] = set()
    for group in module_items:
        for item in group["items"]:
            if item.get("type") == "File":
                if item.get("url"): candidate_file_apis.add(item["url"])
                if item.get("content_id"): candidate_file_apis.add(f"{api}/courses/{cid}/files/{item['content_id']}")
    for record in [*page_records, *files, *[x for g in module_items for x in g["items"]]]:
        for link in embedded_urls(record):
            absolute = urljoin(base, link)
            if re.search(r"/api/v1/(?:courses/\d+/)?files/\d+", absolute): candidate_file_apis.add(absolute)
            elif re.search(r"/files/\d+/(?:download|preview)", absolute) or "download=" in absolute:
                candidate_downloads.add(absolute)
    for file_api in sorted(candidate_file_apis):
        try:
            r = get_with_scoped_credentials(http.s, file_api, base, timeout=60); r.raise_for_status()
            record = r.json()
            if record.get("id"): file_records[int(record["id"])] = record
        except (requests.RequestException, ValueError) as exc:
            warnings.append(f"file metadata {file_api}: {exc}")
    for record in file_records.values():
        if record.get("url"): candidate_downloads.add(record["url"])
    downloaded, download_log, seen_hashes = 0, [], set()
    for url in sorted(candidate_downloads):
        try:
            r = get_with_scoped_credentials(http.s, url, base, timeout=180); r.raise_for_status()
            if not looks_like_file(r): raise RuntimeError("returned HTML instead of a file")
            digest = hashlib.sha256(r.content).hexdigest()
            if digest in seen_hashes: continue
            seen_hashes.add(digest)
            fallback = Path(urlparse(r.url).path).name or "canvas_file"
            name = content_filename(r, fallback)
            dest, should_write = unique_destination(out / "files", name, r.content)
            if should_write: dest.write_bytes(r.content)
            downloaded += 1
            download_log.append({"source_url": url, "saved_as": str(dest.relative_to(out)), "sha256": digest})
        except Exception as exc:
            warnings.append(f"file download {url}: {exc}")
    write_json(out / "file_downloads.json", download_log)

    assignments = optional_pages("assignments", f"{api}/courses/{cid}/assignments", {"per_page": 100, "include[]": ["submission", "rubric_assessment"]})
    announcements = optional_pages("announcements", f"{api}/courses/{cid}/discussion_topics", {"only_announcements": "true", "per_page": 100})
    quizzes = optional_pages("quizzes", f"{api}/courses/{cid}/quizzes", {"per_page": 100})
    assignment_groups = optional_pages("assignment groups", f"{api}/courses/{cid}/assignment_groups", {"per_page": 100, "include[]": ["assignments", "submission"]})
    submissions = optional_pages("my submissions", f"{api}/courses/{cid}/students/submissions", {"per_page": 100, "include[]": ["assignment", "submission_comments", "rubric_assessment", "submission_history"]})
    enrollments = optional_pages("my enrollment", f"{api}/courses/{cid}/enrollments", {"user_id": "self", "type[]": "StudentEnrollment", "include[]": "current_points", "per_page": 100})
    # Assignment descriptions, submissions, comments and rubric feedback can
    # contain attachments that are absent from the forbidden course file index.
    feedback_links = set()
    for link in embedded_urls({"assignments": assignments, "submissions": submissions}):
        absolute = urljoin(base, link)
        if "/files/" in absolute or "download=" in absolute: feedback_links.add(absolute)
    for url in sorted(feedback_links - candidate_downloads):
        try:
            r = get_with_scoped_credentials(http.s, url, base, timeout=180); r.raise_for_status()
            if not looks_like_file(r): continue
            digest = hashlib.sha256(r.content).hexdigest()
            if digest in seen_hashes: continue
            seen_hashes.add(digest)
            name = content_filename(r, Path(urlparse(r.url).path).name or "canvas_attachment")
            dest, should_write = unique_destination(out / "files" / "assignment_and_feedback", name, r.content)
            if should_write: dest.write_bytes(r.content)
            downloaded += 1
            download_log.append({"source_url": url, "saved_as": str(dest.relative_to(out)), "sha256": digest})
        except Exception as exc:
            warnings.append(f"assignment/feedback file {url}: {exc}")
    write_json(out / "file_downloads.json", download_log)
    write_json(out / "assignments.json", assignments)
    write_json(out / "announcements.json", announcements)
    write_json(out / "quizzes.json", quizzes)
    write_json(out / "grades" / "assignment_groups.json", assignment_groups)
    write_json(out / "grades" / "submissions.json", submissions)
    write_json(out / "grades" / "enrollments.json", enrollments)
    marks = ["# Marks and submission status", "", "| Assessment | Score | Possible | Status | Submitted |", "|---|---:|---:|---|---|"]
    for sub in submissions:
        assignment = sub.get("assignment") or {}
        score = sub.get("score")
        if sub.get("workflow_state") == "graded" and score is None: score_text = "Withheld"
        elif score is None: score_text = "—"
        else: score_text = str(score)
        assessment_name = str(assignment.get("name") or sub.get("assignment_id")).replace("|", "\\|")
        marks.append(f"| {assessment_name} | {score_text} | {assignment.get('points_possible', '—')} | {sub.get('workflow_state', '—')} | {sub.get('submitted_at') or '—'} |")
    (out / "grades" / "MARKS_SUMMARY.md").write_text("\n".join(marks) + "\n", encoding="utf-8")
    for sub in submissions:
        assignment = sub.get("assignment") or {}
        comments = sub.get("submission_comments") or []
        rubric = sub.get("rubric_assessment")
        if not comments and not rubric: continue
        name = safe_name(assignment.get("name") or sub.get("assignment_id"))
        lines = [f"# {assignment.get('name') or name} — feedback", ""]
        for comment in comments:
            lines.extend([f"## Comment — {comment.get('created_at') or ''}", "", html_text(comment.get("comment")) or "(no text)", ""])
        if rubric:
            lines.extend(["## Rubric assessment", "", "```json", json.dumps(rubric, ensure_ascii=False, indent=2), "```", ""])
        (out / "grades" / "feedback" / f"{name}.md").parent.mkdir(parents=True, exist_ok=True)
        (out / "grades" / "feedback" / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")
    # Store quiz questions only where Canvas permits the student token to see them.
    question_count = 0
    for quiz in quizzes:
        qid = quiz.get("id")
        if not qid: continue
        questions = optional_pages(f"quiz {qid} questions", f"{api}/courses/{cid}/quizzes/{qid}/questions", {"per_page": 100})
        if questions:
            write_json(out / "quiz_questions" / f"{qid}_{safe_name(quiz.get('title'))}.json", questions)
            question_count += len(questions)
    return {
        "course_id": cid,
        "modules": len(modules),
        "pages": len(page_records),
        "files": downloaded,
        "assignments": len(assignments),
        "announcements": len(announcements),
        "quizzes": len(quizzes),
        "quiz_questions": question_count,
        "submissions": len(submissions),
        "grade_enrollments": len(enrollments),
        "warnings": warnings,
    }


def ed_polite_pause(s: requests.Session) -> None:
    """Apply light jitter and an occasional short burst cooldown.

    Defaults average about two requests per second. Actual rate-limit responses
    are handled separately by ed_get using Retry-After/exponential backoff.
    Session attributes are populated from config.yaml by ed_dump.
    """
    request_count = int(getattr(s, "_usync_ed_request_count", 0))
    if request_count:
        delay_min = float(getattr(s, "_usync_ed_delay_min", 0.25))
        delay_max = float(getattr(s, "_usync_ed_delay_max", 0.60))
        burst_every = int(getattr(s, "_usync_ed_burst_every", 50))
        if burst_every > 0 and request_count % burst_every == 0:
            burst_min = float(getattr(s, "_usync_ed_burst_min", 2.0))
            burst_max = float(getattr(s, "_usync_ed_burst_max", 4.0))
            delay = random.uniform(min(burst_min, burst_max), max(burst_min, burst_max))
            print(f"    Ed pacing pause after {request_count} requests: {delay:.1f}s...")
        else:
            delay = random.uniform(min(delay_min, delay_max), max(delay_min, delay_max))
        time.sleep(delay)
    setattr(s, "_usync_ed_request_count", request_count + 1)


def ed_get(s: requests.Session, url: str, params: dict[str, Any] | None = None) -> Any:
    """GET JSON from Ed with proactive pacing and reactive 429 backoff."""
    max_attempts = 7
    for attempt in range(max_attempts):
        ed_polite_pause(s)
        r = s.get(url, params=params, timeout=60)
        if r.status_code == 401:
            raise RuntimeError("Ed X_TOKEN expired")
        if r.status_code != 429:
            r.raise_for_status()
            return r.json()
        if attempt == max_attempts - 1:
            r.raise_for_status()
        retry_after = r.headers.get("Retry-After", "").strip()
        try:
            delay = float(retry_after)
        except ValueError:
            delay = min(60.0, 2.0 ** (attempt + 1))
        delay = max(1.0, min(delay, 120.0))
        print(f"    Ed rate limit reached; retrying in {delay:g}s ({attempt + 1}/{max_attempts - 1})...")
        time.sleep(delay)
    raise RuntimeError("Ed request retry loop ended unexpectedly")


def flatten_outline(tree: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    def walk(node: dict[str, Any], path: list[str]) -> None:
        title = node.get("title") or "Untitled"
        result.append({"id": node.get("id"), "title": title, "type": node.get("type"), "position": node.get("position"), "parent_id": node.get("parent_id"), "path": path + [title], "children_ids": [c.get("id") for c in node.get("children", [])]})
        for child in node.get("children", []) or []:
            walk(child, path + [title])
    for root in tree.get("lessons", []) or []:
        walk(root, [])
    return result


def find_slide_ids(value: Any) -> set[int]:
    ids: set[int] = set()
    if isinstance(value, dict):
        if value.get("id") and "lesson_id" in value and ("type" in value or "title" in value):
            try: ids.add(int(value["id"]))
            except (TypeError, ValueError): pass
        for child in value.values(): ids.update(find_slide_ids(child))
    elif isinstance(value, list):
        for child in value: ids.update(find_slide_ids(child))
    return ids


def ed_lesson_assets(slides: list[dict[str, Any]], nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return downloadable Ed-hosted files with their lesson/slide context.

    Ed lesson PDF slides expose a ``file_url`` such as
    ``https://static.edusercontent.com/files/<opaque id>``. The URL has no
    useful filename, so the response's Content-Disposition filename is stored
    beneath its lesson and slide. Searching the complete slide payload also
    catches rich-text attachments and future Ed slide schemas.
    """
    node_by_id = {str(node.get("id")): node for node in nodes if node.get("id") is not None}
    assets: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for slide in slides:
        lesson_id = str(slide.get("lesson_id") or "unknown")
        node = node_by_id.get(lesson_id, {})
        path = node.get("path") if isinstance(node.get("path"), list) else []
        lesson_title = path[-1] if path else node.get("title") or f"lesson_{lesson_id}"
        lesson_position = node.get("position")
        slide_id = str(slide.get("id") or "unknown")
        slide_title = slide.get("title") or slide.get("type") or f"slide_{slide_id}"
        slide_position = slide.get("index")
        lesson_prefix = f"{int(lesson_position) + 1:03d}_" if isinstance(lesson_position, int) else ""
        slide_prefix = f"{int(slide_position) + 1:03d}_" if isinstance(slide_position, int) else ""
        category = f"lessons/{lesson_prefix}{safe_name(lesson_title)}/{slide_prefix}{safe_name(slide_title)}"
        for url in sorted(embedded_urls(slide)):
            host = urlparse(url).netloc.lower()
            if not (host == "static.edusercontent.com" or host.endswith(".edusercontent.com")):
                continue
            key = (url, lesson_id, slide_id)
            if key in seen:
                continue
            seen.add(key)
            assets.append({
                "url": url,
                "lesson_id": lesson_id,
                "lesson_title": lesson_title,
                "slide_id": slide_id,
                "slide_title": slide_title,
                "category": category,
            })
    return assets


def find_workspace_paths(value: Any) -> set[str]:
    paths: set[str] = set()
    for text in iter_strings(value):
        if text.startswith("/home/") and not text.endswith("/"):
            paths.add(text.split("?", 1)[0])
        for match in re.findall(r"/home/[A-Za-z0-9_ .@+()\-/]+\.[A-Za-z0-9]{1,12}", text):
            paths.add(match.rstrip(".,);]"))
    return paths


def find_workspace_urls(value: Any, cid: int) -> set[str]:
    """Find public workspace routes in Ed API payloads and serialized SPA state."""
    found: set[str] = set()
    route = re.compile(rf"(?:https?://edstem\.org)?/au/courses/{cid}/workspaces/([A-Za-z0-9_-]{{16,}})")
    def walk(v: Any) -> None:
        if isinstance(v, str):
            for token in route.findall(v.replace("\\/", "/")):
                found.add(f"https://edstem.org/au/courses/{cid}/workspaces/{token}")
        elif isinstance(v, dict):
            blob = " ".join(str(k) for k in v).lower()
            for child in v.values(): walk(child)
            if "workspace" in blob or any("workspace" in str(x).lower() for x in v.values() if isinstance(x, str)):
                for key in ("public_id", "workspace_id", "slug", "token", "key", "url"):
                    candidate = v.get(key)
                    if isinstance(candidate, str):
                        for token in route.findall(candidate.replace("\\/", "/")):
                            found.add(f"https://edstem.org/au/courses/{cid}/workspaces/{token}")
                        if key != "url" and re.fullmatch(r"[A-Za-z0-9_-]{24,80}", candidate):
                            found.add(f"https://edstem.org/au/courses/{cid}/workspaces/{candidate}")
        elif isinstance(v, list):
            for child in v: walk(child)
    walk(value)
    return found


def ed_browser_discover(cid: int, target: Path, config: dict[str, Any], seed_urls: set[str] | None = None) -> tuple[set[str], dict[str, set[str]], list[str]]:
    """Use the reusable browser profile to discover Ed links and workspace paths.

    Ed's private APIs change; capturing JSON responses makes this deliberately
    schema-tolerant. A prior `usync login` keeps the browser authenticated.
    """
    warnings: list[str] = []
    workspace_urls: set[str] = set(seed_urls or set())
    paths: dict[str, set[str]] = {}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return workspace_urls, paths, ["Playwright not installed; run: pip install playwright && playwright install chromium"]
    profile = ROOT / ".usync-browser"
    headless = bool(config.get("browser", {}).get("headless", True))
    captured: list[tuple[str, Any]] = []
    response_urls: list[str] = []
    websocket_frames: list[str] = []
    active_workspace: list[str | None] = [None]
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(profile), headless=headless, accept_downloads=True)
        restore_browser_cookies(context)
        page = context.pages[0] if context.pages else context.new_page()
        def on_response(response):
            response_urls.append(response.url)
            parsed = urlparse(response.url)
            if active_workspace[0] and parsed.netloc.startswith("web.") and parsed.netloc.endswith("edusercontent.com"):
                parts = [unquote(x) for x in parsed.path.split("/") if x]
                # First component is an ephemeral workspace session/container id.
                if len(parts) >= 2:
                    paths.setdefault(active_workspace[0], set()).add("/home/" + "/".join(parts[1:]))
            if "edstem.org/api/" not in response.url: return
            try:
                ctype = response.headers.get("content-type", "")
                if "json" in ctype: captured.append((response.url, response.json()))
            except Exception: pass
        page.on("response", on_response)
        def on_websocket(ws):
            def frame_received(payload):
                try:
                    text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
                    websocket_frames.append(text[:2000])
                    if active_workspace[0]: paths.setdefault(active_workspace[0], set()).update(find_workspace_paths(text))
                except Exception: pass
            ws.on("framereceived", frame_received)
        page.on("websocket", on_websocket)
        try:
            page.goto(f"https://edstem.org/au/courses/{cid}", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            hrefs = page.locator("a").evaluate_all("els => els.map(e => e.href).filter(Boolean)")
            workspace_urls.update(x for x in hrefs if "/workspaces/" in x)
            # Common SPA route; harmless if the course has no workspace index.
            page.goto(f"https://edstem.org/au/courses/{cid}/workspaces", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            hrefs = page.locator("a").evaluate_all("els => els.map(e => e.href).filter(Boolean)")
            workspace_urls.update(x for x in hrefs if "/workspaces/" in x)
            workspace_urls.update(find_workspace_urls(page.content(), cid))
            for _, payload in captured: workspace_urls.update(find_workspace_urls(payload, cid))
        except Exception as exc:
            warnings.append(f"Ed browser discovery: {exc}")
        for url in sorted(workspace_urls):
            wid = urlparse(url).path.rstrip("/").split("/")[-1]
            active_workspace[0] = wid
            before = len(captured)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)
                for _, payload in captured[before:]: paths.setdefault(wid, set()).update(find_workspace_paths(payload))
                attrs = page.locator("[data-path]").evaluate_all("els => els.map(e => e.getAttribute('data-path')).filter(Boolean)")
                paths.setdefault(wid, set()).update(x for x in attrs if x.startswith("/home/") and not x.endswith("/"))
            except Exception as exc:
                warnings.append(f"workspace {wid} discovery: {exc}")
        active_workspace[0] = None
        write_json(target / "ed" / "ED_WORKSPACE_DISCOVERY_DEBUG.json", {
            "workspace_index": f"https://edstem.org/au/courses/{cid}/workspaces",
            "api_response_urls": list(dict.fromkeys(response_urls)),
            "workspace_urls": sorted(workspace_urls),
            "workspace_paths": {wid: sorted(values) for wid, values in paths.items()},
            "websocket_frames_seen": len(websocket_frames),
        })
        context.close()
    return workspace_urls, paths, warnings


def ed_dump(unit: str, year: int, target: Path, token: str, config: dict[str, Any]) -> dict[str, Any]:
    base = "https://edstem.org/api"
    s = requests.Session()
    s.headers.update({"x-token": token, "User-Agent": USER_AGENT, "Origin": "https://edstem.org", "Accept": "application/json"})
    rate = config.get("ed", {}).get("rate_limit", {})
    s._usync_ed_delay_min = float(rate.get("delay_min", 0.25))
    s._usync_ed_delay_max = float(rate.get("delay_max", 0.60))
    s._usync_ed_burst_every = int(rate.get("burst_every", 50))
    s._usync_ed_burst_min = float(rate.get("burst_delay_min", 2.0))
    s._usync_ed_burst_max = float(rate.get("burst_delay_max", 4.0))
    course_payload = ed_get(s, f"{base}/user")
    courses = []
    def hunt(v: Any) -> None:
        if isinstance(v, dict):
            if v.get("id") and any(k in v for k in ("code", "name", "title")): courses.append(v)
            for x in v.values(): hunt(x)
        elif isinstance(v, list):
            for x in v: hunt(x)
    hunt(course_payload)
    course = pick_course(courses, unit, year)
    cid = int(course["id"])
    out = target / "ed"
    out.mkdir(parents=True, exist_ok=True)

    threads = []
    offset = 0
    while True:
        data = ed_get(s, f"{base}/courses/{cid}/threads", {"limit": 100, "offset": offset, "sort": "new"})
        batch = data.get("threads", []) if isinstance(data, dict) else []
        if not batch: break
        for summary in batch:
            tid = summary.get("id")
            if tid:
                try: threads.append(ed_get(s, f"{base}/threads/{tid}"))
                except requests.HTTPError: threads.append({"thread": summary})
        offset += len(batch)
        if len(batch) < 100: break
    with (out / "discussions.jsonl").open("w", encoding="utf-8") as f:
        for row in threads: f.write(json.dumps(row, ensure_ascii=False) + "\n")

    outline = ed_get(s, f"{base}/courses/{cid}/lessons", {"view": 1})
    nodes = flatten_outline(outline)
    slides, challenges = [], []
    seen_challenges = set()
    for node in nodes:
        lid = node.get("id")
        if not lid: continue
        try: detail = ed_get(s, f"{base}/lessons/{lid}", {"view": 1})
        except requests.HTTPError: continue
        for sid in sorted(find_slide_ids(detail)):
            try:
                payload = ed_get(s, f"{base}/lessons/slides/{sid}", {"view": 1})
                slide = payload.get("slide", payload)
                slides.append({"unit": unit, "lesson_id": lid, **slide})
                chid = slide.get("challenge_id")
                if chid and chid not in seen_challenges:
                    seen_challenges.add(chid)
                    try:
                        cp = ed_get(s, f"{base}/challenges/{chid}", {"view": 1})
                        challenges.append({"unit": unit, "lesson_id": lid, "slide_id": sid, **cp.get("challenge", cp)})
                    except requests.HTTPError: pass
            except requests.HTTPError: pass
    for filename, rows in (("tasks.jsonl", [{"unit": unit, **x} for x in nodes]), ("slides.jsonl", slides), ("challenges.jsonl", challenges)):
        with (out / filename).open("w", encoding="utf-8") as f:
            for row in rows: f.write(json.dumps(row, ensure_ascii=False) + "\n")
    all_payload = {"threads": threads, "outline": outline, "slides": slides, "challenges": challenges}
    urls = embedded_urls(all_payload)
    resource_urls = {u for u in urls if "edstem.org" in u and "/resources?" in u and "download=" in u}
    static_urls = {u for u in urls if urlparse(u).netloc.lower() == "static.edusercontent.com" or urlparse(u).netloc.lower().endswith(".edusercontent.com")}
    lesson_assets = ed_lesson_assets(slides, nodes)
    lesson_asset_urls = {asset["url"] for asset in lesson_assets}
    workspace_urls = {u for u in urls if "edstem.org" in u and "/workspaces/" in u}
    browser_paths: dict[str, set[str]] = {}
    asset_warnings: list[str] = []
    # Proven by Ed's own workspace page: this endpoint is the authoritative
    # course workspace index, including JavaScript-rendered public cards.
    try:
        workspace_index = ed_get(s, f"{base}/courses/{cid}/workspaces")
        write_json(out / "workspaces_index.json", workspace_index)
        for item in workspace_index.get("workspaces", []) if isinstance(workspace_index, dict) else []:
            wid = item.get("id")
            if wid: workspace_urls.add(f"https://edstem.org/au/courses/{cid}/workspaces/{wid}")
    except requests.RequestException as exc:
        asset_warnings.append(f"workspace index: {exc}")
    # Seed browser discovery with any previously confirmed workspace URLs.
    known = config.get("ed", {}).get("workspaces", {}).get(unit, [])
    for entry in known:
        if isinstance(entry, str): workspace_urls.add(entry)
        elif isinstance(entry, dict) and entry.get("url"):
            workspace_urls.add(entry["url"])
            wid = entry["url"].rstrip("/").split("/")[-1]
            browser_paths.setdefault(wid, set()).update(entry.get("paths", []))
    if config.get("ed", {}).get("browser_discovery", True):
        discovered, discovered_paths, browser_warnings = ed_browser_discover(cid, target, config, workspace_urls)
        workspace_urls.update(discovered); asset_warnings.extend(browser_warnings)
        for wid, values in discovered_paths.items(): browser_paths.setdefault(wid, set()).update(values)
    downloaded, unresolved, log, seen_hashes = 0, [], [], set()
    def save_response(response: requests.Response, fallback: str, category: str, source: str) -> bool:
        nonlocal downloaded
        if not looks_like_file(response): return False
        digest = hashlib.sha256(response.content).hexdigest()
        if digest in seen_hashes: return True
        seen_hashes.add(digest)
        name = content_filename(response, fallback)
        dest, should_write = unique_destination(out / "resources" / category, name, response.content)
        if should_write: dest.write_bytes(response.content)
        downloaded += 1
        log.append({"source": source, "saved_as": str(dest.relative_to(out)), "sha256": digest})
        return True
    lesson_file_count = 0
    lesson_file_index: list[dict[str, Any]] = []
    for asset in lesson_assets:
        url = asset["url"]
        record = {key: value for key, value in asset.items() if key != "category"}
        try:
            r = get_with_scoped_credentials(s, url, "https://edstem.org", timeout=180)
            before = len(log)
            if save_response(r, Path(urlparse(url).path).name or f"lesson_{asset['slide_id']}_file", asset["category"], url):
                lesson_file_count += 1
                record["status"] = "downloaded"
                if len(log) > before:
                    record["saved_as"] = log[-1]["saved_as"]
            else:
                record["status"] = "unresolved"
                unresolved.append(url)
        except Exception as exc:
            record["status"] = "error"
            record["reason"] = str(exc)
            asset_warnings.append(f"Ed lesson file {url}: {exc}")
        lesson_file_index.append(record)
    write_json(out / "lesson_files_index.json", lesson_file_index)
    # Download Ed CDN files found outside lessons (for example discussion
    # attachments) into the general files directory.
    for url in sorted(static_urls - lesson_asset_urls):
        try:
            r = get_with_scoped_credentials(s, url, "https://edstem.org", timeout=180)
            if not save_response(r, Path(urlparse(url).path).name or "ed_file", "files", url): unresolved.append(url)
        except Exception as exc: asset_warnings.append(f"Ed file {url}: {exc}")
    # The public-looking resource URL is a SPA route. Try known API shapes and
    # record it unresolved when Ed changes/denies them; never save HTML as PDF.
    for url in sorted(resource_urls):
        rid = (parse_qs(urlparse(url).query).get("download") or [""])[0]
        candidates = [f"{base}/resources/{rid}/download", f"{base}/courses/{cid}/resources/{rid}/download", url]
        ok = False
        for candidate in candidates:
            try:
                r = get_with_scoped_credentials(s, candidate, "https://edstem.org", timeout=180, allow_redirects=True)
                if save_response(r, f"resource_{rid}", "resources", url): ok = True; break
            except requests.RequestException: pass
        if not ok: unresolved.append(url)
    for workspace_url in sorted(workspace_urls):
        wid = urlparse(workspace_url).path.rstrip("/").split("/")[-1]
        root_downloaded = False
        # Ed's download endpoint also accepts directories. Prefer /home as a
        # single archive: it is exhaustive and preserves the workspace tree.
        try:
            root_response = s.post(f"{base}/workspaces/{wid}/download", json={"path": "/home"}, timeout=300)
            if root_response.ok and root_response.content[:4] == b"PK\x03\x04":
                with zipfile.ZipFile(io.BytesIO(root_response.content)) as archive:
                    for member in archive.infolist():
                        if member.is_dir(): continue
                        relative = Path(member.filename.lstrip("/"))
                        if ".." in relative.parts: continue
                        data = archive.read(member)
                        digest = hashlib.sha256(data).hexdigest()
                        if digest in seen_hashes: continue
                        seen_hashes.add(digest)
                        dest = out / "resources" / "workspaces" / safe_name(wid) / relative
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(data)
                        downloaded += 1
                        log.append({"source": f"{workspace_url}:/home", "saved_as": str(dest.relative_to(out)), "sha256": digest})
                root_downloaded = True
        except Exception as exc:
            asset_warnings.append(f"workspace {wid} root download: {exc}")
        for remote_path in sorted(browser_paths.get(wid, set())):
            try:
                r = s.post(f"{base}/workspaces/{wid}/download", json={"path": remote_path}, timeout=180)
                if not save_response(r, Path(remote_path).name, f"workspaces/{safe_name(wid)}", f"{workspace_url}:{remote_path}"):
                    unresolved.append(f"{workspace_url}:{remote_path}")
            except Exception as exc: asset_warnings.append(f"workspace {wid} {remote_path}: {exc}")
        if not root_downloaded and not browser_paths.get(wid): unresolved.append(f"{workspace_url} (file tree not discovered)")
    write_json(out / "resource_downloads.json", log)
    write_json(out / "UNRESOLVED_RESOURCES.json", sorted(set(unresolved)))
    return {"course_id": cid, "threads": len(threads), "outline_nodes": len(nodes), "slides": len(slides), "challenges": len(challenges), "lesson_files": lesson_file_count, "resource_links": len(resource_urls) + len(static_urls), "workspaces": len(workspace_urls), "downloaded_files": downloaded, "unresolved": len(set(unresolved)), "warnings": asset_warnings}


def echo_dump(unit: str, target: Path, har_path: Path) -> dict[str, Any]:
    har = json.loads(har_path.read_text(encoding="utf-8"))
    entries = har.get("log", {}).get("entries", [])
    syllabus = next((e for e in entries if "echo360.net.au" in e.get("request", {}).get("url", "") and e.get("request", {}).get("url", "").rstrip("/").endswith("syllabus")), None)
    if not syllabus: raise RuntimeError("No Echo360 syllabus response found in HAR")
    content = syllabus["response"]["content"]
    raw = content.get("text", "")
    if content.get("encoding") == "base64": raw = base64.b64decode(raw).decode("utf-8")
    lesson_rows = json.loads(raw).get("data", [])
    auth_entry = next((e for e in entries if "transcript-file?format=text" in e.get("request", {}).get("url", "")), None)
    auth_entry = auth_entry or next((e for e in entries if "echo360.net.au/api/" in e.get("request", {}).get("url", "")), None)
    if not auth_entry: raise RuntimeError("No authenticated Echo360 request found in HAR")
    allowed = {"authorization", "cookie", "user-agent", "accept"}
    headers = {h["name"]: h["value"] for h in auth_entry["request"].get("headers", []) if h.get("name", "").lower() in allowed}
    out = target / "lectures" / "transcripts"
    out.mkdir(parents=True, exist_ok=True)
    combined, count = [], 0
    for index, row in enumerate(lesson_rows, 1):
        wrapper = row.get("lesson", {})
        lesson = wrapper.get("lesson", {})
        media = next((m for m in wrapper.get("medias", []) if m.get("isAvailable") and m.get("mediaType") == "Video"), None)
        if not lesson or not media: continue
        title = lesson.get("displayName") or lesson.get("name") or f"Lecture {index}"
        url = f"https://echo360.net.au/api/ui/echoplayer/lessons/{lesson['id']}/medias/{media['id']}/transcript-file?format=text"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as response:
            transcript = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace").strip()
        header = f"{'='*80}\n{title}\n{'='*80}\n\n"
        (out / f"{index:02d}_{safe_name(title)}.txt").write_text(header + transcript + "\n", encoding="utf-8")
        combined.append(header + transcript)
        count += 1
    (target / "lectures" / "ALL_LECTURE_TRANSCRIPTS.txt").write_text("\n\n".join(combined), encoding="utf-8")
    return {"transcripts": count}


def echo_playwright_dump(unit: str, year: int, target: Path, canvas_token: str, base_url: str, config: dict[str, Any]) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright missing. Run: pip install playwright && playwright install chromium") from exc
    api = base_url.rstrip("/") + "/api/v1"
    http = Http({"Authorization": f"Bearer {canvas_token}", "Accept": "application/json", "User-Agent": USER_AGENT})
    courses = http.pages(f"{api}/courses", {"enrollment_state": "all", "state[]": ["available", "completed"], "include[]": ["term"], "per_page": 100})
    course = pick_course(courses, unit, year); cid = course["id"]
    candidates: list[str] = []
    try:
        tabs = http.pages(f"{api}/courses/{cid}/tabs", {"per_page": 100})
        for tab in tabs:
            hay = f"{tab.get('label','')} {tab.get('id','')}".lower()
            if any(x in hay for x in ("echo", "recorded", "lecture", "video")) and tab.get("html_url"):
                candidates.append(tab["html_url"])
    except requests.RequestException: pass
    try:
        modules = http.pages(f"{api}/courses/{cid}/modules", {"per_page": 100})
        for module in modules:
            for item in http.pages(f"{api}/courses/{cid}/modules/{module['id']}/items", {"per_page": 100}):
                hay = f"{item.get('title','')} {item.get('external_url','')}".lower()
                if any(x in hay for x in ("echo", "recorded lecture", "lecture recording")):
                    if item.get("html_url"): candidates.append(item["html_url"])
                    if item.get("external_url"): candidates.append(item["external_url"])
    except requests.RequestException: pass
    configured = config.get("echo360", {}).get("launch_urls", {}).get(unit)
    if configured: candidates.extend(configured if isinstance(configured, list) else [configured])
    if not candidates: candidates.append(f"{base_url.rstrip('/')}/courses/{cid}")
    # Canvas commonly returns LTI tab URLs as root-relative paths. Playwright's
    # page.goto requires an absolute URL.
    candidates = [urljoin(base_url.rstrip("/") + "/", str(url)) for url in candidates]
    payloads: list[Any] = []
    echo_response_urls: list[str] = []
    visited: list[dict[str, Any]] = []
    profile = ROOT / ".usync-browser"
    headless = bool(config.get("browser", {}).get("headless", True))
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(profile), headless=headless, accept_downloads=True)
        restore_browser_cookies(context)
        page = context.pages[0] if context.pages else context.new_page()
        def capture(response):
            if "echo360" not in response.url.lower(): return
            echo_response_urls.append(response.url)
            try:
                if "json" in response.headers.get("content-type", ""): payloads.append(response.json())
            except Exception: pass
        page.on("response", capture)
        for url in dict.fromkeys(candidates):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(10000)
                visited.append({"requested": url, "landed_on": page.url, "title": page.title()})
            except Exception as exc:
                visited.append({"requested": url, "error": str(exc)})
        lesson_rows: list[dict[str, Any]] = []
        def hunt(v: Any):
            if isinstance(v, dict):
                if isinstance(v.get("lesson"), dict) and isinstance(v.get("medias"), list): lesson_rows.append(v)
                for child in v.values(): hunt(child)
            elif isinstance(v, list):
                for child in v: hunt(child)
        for payload in payloads: hunt(payload)
        # De-duplicate lesson/media pairs captured from several Echo responses.
        unique: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
        for wrapper in lesson_rows:
            lesson = wrapper.get("lesson", {})
            media = next((m for m in wrapper.get("medias", []) if m.get("isAvailable") and m.get("mediaType") == "Video"), None)
            if lesson.get("id") and media and media.get("id"): unique[(str(lesson["id"]), str(media["id"]))] = (lesson, media)
        write_json(target / "lectures" / "ECHO_DISCOVERY_DEBUG.json", {
            "candidate_launch_urls": list(dict.fromkeys(candidates)),
            "visited": visited,
            "echo_response_urls": list(dict.fromkeys(echo_response_urls)),
            "captured_json_responses": len(payloads),
            "discovered_lecture_media": len(unique),
        })
        if not unique:
            context.close()
            raise RuntimeError("Echo360 lecture list was not discovered. Run `usync login`, or add this unit's Canvas Recorded Lectures URL under echo360.launch_urls.")
        out = target / "lectures" / "transcripts"; out.mkdir(parents=True, exist_ok=True)
        combined, failures = [], []
        for index, ((lesson_id, media_id), (lesson, media)) in enumerate(unique.items(), 1):
            title = lesson.get("displayName") or lesson.get("name") or f"Lecture {index}"
            url = f"https://echo360.net.au/api/ui/echoplayer/lessons/{lesson_id}/medias/{media_id}/transcript-file?format=text"
            response = context.request.get(url, timeout=90000)
            if not response.ok:
                failures.append(f"{title}: HTTP {response.status}"); continue
            transcript = response.text().strip()
            header = f"{'='*80}\n{title}\n{'='*80}\n\n"
            (out / f"{index:02d}_{safe_name(title)}.txt").write_text(header + transcript + "\n", encoding="utf-8")
            combined.append(header + transcript)
        context.close()
    (target / "lectures" / "ALL_LECTURE_TRANSCRIPTS.txt").write_text("\n\n".join(combined), encoding="utf-8")
    return {"transcripts": len(combined), "failures": failures, "authentication": "Playwright persistent browser profile"}


def browser_login(config: dict[str, Any], canvas_token: str | None) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("Playwright missing. Run: pip install playwright && playwright install chromium") from exc
    base = config.get("canvas", {}).get("base_url", "https://canvas.sydney.edu.au").rstrip("/")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(ROOT / ".usync-browser"), headless=False, accept_downloads=True)
        restore_browser_cookies(context)
        canvas_page = context.pages[0] if context.pages else context.new_page()
        canvas_page.goto(base, wait_until="domcontentloaded", timeout=90000)
        ed_page = context.new_page(); ed_page.goto("https://edstem.org/au", wait_until="domcontentloaded", timeout=90000)
        opened_echo = 0
        if canvas_token:
            try:
                api = base + "/api/v1"
                http = Http({"Authorization": f"Bearer {canvas_token}", "Accept": "application/json", "User-Agent": USER_AGENT})
                courses = http.pages(f"{api}/courses", {"enrollment_state": "all", "state[]": ["available", "completed"], "include[]": ["term"], "per_page": 100})
                semester = config.get("active_semester")
                semester_cfg = config.get("semesters", {}).get(semester, {})
                year = int(semester_cfg.get("calendar_year", datetime.now().year))
                for unit_value in semester_cfg.get("units", []):
                    unit = str(unit_value).upper()
                    try:
                        course = pick_course(courses, unit, year)
                        tabs = http.pages(f"{api}/courses/{course['id']}/tabs", {"per_page": 100})
                        launch = next((tab.get("html_url") for tab in tabs if any(x in f"{tab.get('label','')} {tab.get('id','')}".lower() for x in ("echo", "recorded", "lecture", "video")) and tab.get("html_url")), None)
                        if launch:
                            echo_page = context.new_page()
                            echo_page.goto(urljoin(base + "/", launch), wait_until="domcontentloaded", timeout=90000)
                            opened_echo += 1
                    except Exception as exc:
                        print(f"Could not open {unit} Recorded Lectures automatically: {exc}")
            except Exception as exc:
                print(f"Could not enumerate Canvas Recorded Lectures tabs: {exc}")
        print(f"Log into Canvas and Ed in the opened browser. Opened {opened_echo} Recorded Lectures tab(s); complete Sydney SSO in each until Echo content is visible.")
        input("When all three are logged in, return here and press Enter...")
        context.storage_state(path=str(BROWSER_STATE))
        context.close()
    print("Reusable browser session saved in .usync-browser and .usync-browser-state.json")
    return 0


def zip_tree(source: Path, destination: Path) -> None:
    tmp = destination.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(source.rglob("*")):
            if path.is_file() and path.suffix.lower() != ".har" and path.name != "tokens.env":
                zf.write(path, Path(source.name) / path.relative_to(source))
    tmp.replace(destination)


def build_gpt_ready_tree(dump: Path, output: Path, unit: str) -> dict[str, Any]:
    """Create a lean teaching corpus without changing the archival dump.

    Only redundant renderings, runtime diagnostics, download ledgers and
    unmistakable Canvas navigation artwork are omitted. Original academic
    resources are copied byte-for-byte. The generated manifest documents every
    omission and, where applicable, the retained file carrying the same data.
    """
    output.mkdir(parents=True, exist_ok=True)
    excluded: list[dict[str, str]] = []
    included_files = 0
    included_bytes = 0
    excluded_bytes = 0

    diagnostics = {
        "ed/ED_WORKSPACE_DISCOVERY_DEBUG.json",
        "lectures/ECHO_DISCOVERY_DEBUG.json",
    }
    ledgers = {
        "canvas/file_downloads.json",
        "ed/resource_downloads.json",
        "ed/lesson_files_index.json",
    }
    navigation_icon = re.compile(
        r"(?:^|[_ -])(?:badge|contact|discussion|events|information|journey|lecture|"
        r"notesreadings|scaffolding|seminar)[_ -]?icon|^Ed_(?:Discussion|Lessons)_Icon\.",
        re.I,
    )

    def omit(relative: Path) -> tuple[str, str] | None:
        rel = relative.as_posix()
        if rel in diagnostics:
            return "runtime diagnostic; not unit-learning content", "MANIFEST.json"
        if rel in ledgers:
            return "download provenance ledger; resources themselves are retained", "GPT_READY_MANIFEST.json"
        if rel == "lectures/ALL_LECTURE_TRANSCRIPTS.txt":
            return "exact aggregate of retained individual transcripts", "lectures/transcripts/"
        if rel.startswith("canvas/pages/") and relative.suffix.lower() in {".html", ".txt"}:
            page_json = dump / relative.with_suffix(".json")
            try:
                payload = json.loads(page_json.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and isinstance(payload.get("body"), str) and payload["body"]:
                    return "redundant page rendering; full Canvas HTML body and metadata retained in JSON", relative.with_suffix(".json").as_posix()
            except Exception:
                pass
        if rel.startswith("canvas/files/") and navigation_icon.search(relative.name):
            return "known Canvas navigation icon with no teaching content", "canvas/pages/ and canvas/modules.json"
        return None

    for source in sorted(dump.rglob("*")):
        if not source.is_file() or source.suffix.lower() == ".har" or source.name == "tokens.env":
            continue
        relative = source.relative_to(dump)
        decision = omit(relative)
        if decision:
            reason, represented_by = decision
            excluded.append({
                "path": relative.as_posix(),
                "reason": reason,
                "represented_by": represented_by,
                "bytes": str(source.stat().st_size),
            })
            excluded_bytes += source.stat().st_size
            continue
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        included_files += 1
        included_bytes += source.stat().st_size

    manifest = {
        "unit": unit,
        "purpose": "Lean GPT teaching corpus generated from the complete local latest_dump",
        "policy": "Academic content is retained byte-for-byte; only redundant renderings, runtime diagnostics, provenance ledgers and known navigation icons are omitted.",
        "included_files": included_files,
        "included_bytes": included_bytes,
        "excluded_files": len(excluded),
        "excluded_bytes": excluded_bytes,
        "omissions": excluded,
    }
    write_json(output / "GPT_READY_MANIFEST.json", manifest)
    (output / "README_GPT.md").write_text(
        f"# {unit} GPT-ready unit corpus\n\n"
        "This ZIP is the lean retrieval copy. The complete archival copy remains in `latest_dump/` locally.\n\n"
        "Start with `MANIFEST.json`, `canvas/ALL_CANVAS_PAGES.txt`, individual lecture transcripts, "
        "assessment/grade JSON and Markdown, Ed JSONL files, and the original PDFs/documents. "
        "See `GPT_READY_MANIFEST.json` for the exact omission audit.\n",
        encoding="utf-8",
    )
    return manifest


def pack_existing(unit: str, semester: str) -> Path:
    """Rebuild only the lean GPT ZIP from an existing complete latest_dump."""
    unit_root = ROOT / semester / unit
    dump = unit_root / "latest_dump"
    if not dump.exists():
        raise SystemExit(f"No existing dump for {unit}: {dump}")
    stage = Path(tempfile.mkdtemp(prefix=f".{unit}.packing-", dir=unit_root))
    try:
        gpt_ready = stage / unit
        manifest = build_gpt_ready_tree(dump, gpt_ready, unit)
        staged_zip = stage / f"{unit}.zip"
        zip_tree(gpt_ready, staged_zip)
        destination = unit_root / f"{unit}.zip"
        staged_zip.replace(destination)
        print(f"Packed GPT-ready ZIP: {destination}")
        print(f"  {manifest['included_files']} files kept; {manifest['excluded_files']} redundant/non-teaching files omitted")
        return destination
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def sync_unit(unit: str, semester: str, semester_cfg: dict[str, Any], config: dict[str, Any], canvas_token: str | None, ed_token: str | None) -> bool:
    unit_root = ROOT / semester / unit
    unit_root.mkdir(parents=True, exist_ok=True)
    previous_dump = unit_root / "latest_dump"
    stage = Path(tempfile.mkdtemp(prefix=f".{unit}.building-", dir=unit_root))
    dump = stage / "latest_dump"
    dump.mkdir()
    sources, successes, had_errors = {}, 0, False
    year = int(semester_cfg["calendar_year"])
    operations = []
    if canvas_token: operations.append(("canvas", lambda: canvas_dump(unit, year, dump, canvas_token, config.get("canvas", {}).get("base_url", "https://canvas.sydney.edu.au"))))
    else: sources["canvas"] = {"status": "skipped", "reason": "missing CANVAS_API_TOKEN"}
    if ed_token: operations.append(("ed", lambda: ed_dump(unit, year, dump, ed_token, config)))
    else: sources["ed"] = {"status": "skipped", "reason": "missing X_TOKEN"}
    if canvas_token and config.get("echo360", {}).get("enabled", True):
        operations.append(("echo360", lambda: echo_playwright_dump(unit, year, dump, canvas_token, config.get("canvas", {}).get("base_url", "https://canvas.sydney.edu.au"), config)))
    else:
        sources["echo360"] = {"status": "skipped", "reason": "disabled or missing CANVAS_API_TOKEN"}
    for name, operation in operations:
        print(f"  [{unit}] {name}...")
        try:
            sources[name] = {"status": "ok", **operation()}
            successes += 1
        except Exception as exc:
            had_errors = True
            source_directory = {"canvas": "canvas", "ed": "ed", "echo360": "lectures"}.get(name)
            previous_source = previous_dump / source_directory if source_directory else None
            staged_source = dump / source_directory if source_directory else None
            preserved = bool(previous_source and previous_source.exists() and staged_source)
            # Never publish a half-written source tree. Replace it with the
            # last complete copy when one exists; otherwise omit it.
            if staged_source and staged_source.exists():
                shutil.rmtree(staged_source)
            if preserved:
                shutil.copytree(previous_source, staged_source)
            sources[name] = {
                "status": "error_preserved" if preserved else "error",
                "reason": str(exc),
                "previous_data_preserved": preserved,
            }
            print(f"    warning: {exc}")
            if preserved:
                print(f"    previous {name} data preserved")
    manifest = {"unit": unit, "semester": semester, "created_at": datetime.now(timezone.utc).isoformat(), "sources": sources}
    write_json(dump / "MANIFEST.json", manifest)
    (dump / "README.md").write_text(f"# {unit} resource dump\n\nGenerated by usync for {semester}. See `MANIFEST.json` for source status.\n", encoding="utf-8")
    if not successes:
        shutil.rmtree(stage)
        print(f"  [{unit}] no sources succeeded; previous dump preserved")
        return False
    gpt_ready = stage / unit
    gpt_manifest = build_gpt_ready_tree(dump, gpt_ready, unit)
    staged_zip = stage / f"{unit}.zip"
    zip_tree(gpt_ready, staged_zip)
    live_dump, live_zip = unit_root / "latest_dump", unit_root / f"{unit}.zip"
    backup = unit_root / ".previous_dump"
    if backup.exists(): shutil.rmtree(backup)
    if live_dump.exists(): live_dump.replace(backup)
    dump.replace(live_dump)
    staged_zip.replace(live_zip)
    if backup.exists(): shutil.rmtree(backup)
    shutil.rmtree(stage)
    if had_errors:
        print(f"  [{unit}] ready with preserved-source warnings: {live_zip}")
    else:
        print(f"  [{unit}] ready: {live_zip}")
    print(f"  [{unit}] GPT ZIP: {gpt_manifest['included_files']} files kept; {gpt_manifest['excluded_files']} redundant/non-teaching files omitted")
    return not had_errors


def check(config: dict[str, Any], canvas: str | None, ed: str | None) -> int:
    semester = config.get("active_semester")
    cfg = config.get("semesters", {}).get(semester, {})
    print(f"Root: {ROOT}")
    print(f"Active semester: {semester}")
    print(f"Units: {', '.join(cfg.get('units', []))}")
    print(f"Canvas token: {'found' if canvas else 'missing'}")
    print(f"Ed token: {'found' if ed else 'missing'}")
    try:
        import playwright  # noqa: F401
        playwright_state = "installed"
    except ImportError:
        playwright_state = "missing"
    print(f"Playwright: {playwright_state}")
    print(f"Browser session: {'found' if (ROOT / '.usync-browser').exists() and BROWSER_STATE.exists() else 'not initialized (run: usync login)'}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="usync",
        usage="usync [UNITCODE | help | check | login | pack UNITCODE] [--check]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Sync university course resources from Canvas, Ed and Echo360.",
        epilog="""COMMANDS
  usync
      Sync every unit configured for the active semester.

  usync <UNITCODE>
      Sync one unit, for example: usync SOFT2201.
      Unit codes are case-insensitive and must be configured for the active
      semester in config.yaml. Do not type the angle brackets.

  usync check
      Show the active semester, configured units, token availability,
      Playwright installation and saved browser-session status.
      This checks local setup only; it does not validate remote logins.
      Alias: usync --check.

  usync login
      Open a browser to sign into Canvas, Ed and Recorded Lectures/Echo360.
      Complete sign-in, then return to the terminal and press Enter to save
      the session for future syncs. Requires Playwright and Chromium.

  usync pack <UNITCODE>
      Rebuild a unit's lean GPT-ready ZIP from its existing latest_dump.
      No resources are downloaded; the complete local dump is preserved.

  usync help
      Show this help page. Aliases: usync -h, usync --help.

EXAMPLES
  usync check            Check local setup before syncing
  usync login            Save a browser session
  usync                  Sync all active-semester units
  usync SOFT2201         Sync only SOFT2201
  usync pack SOFT2201    Rebuild SOFT2201.zip from the local dump

CONFIGURATION AND OUTPUT
  Paths are relative to the usync installation directory.
  config.yaml selects the active semester and its units.
  tokens.env or environment variables supply CANVAS_API_TOKEN (also accepted:
  canvas_api_token) and X_TOKEN (Ed).
  Complete resources: <semester>/<UNITCODE>/latest_dump/
  Lean GPT-ready ZIP: <semester>/<UNITCODE>/<UNITCODE>.zip
  Source results and errors are recorded in latest_dump/MANIFEST.json.
  A sync publishes output when at least one source succeeds; if none succeed,
  the previous dump is preserved.
""",
    )
    parser.add_argument("unit", nargs="?", metavar="COMMAND_OR_UNIT", help="unit code, help, check, login, pack, or omit to sync all units")
    parser.add_argument("pack_unit", nargs="?", metavar="UNITCODE", help="unit code for the pack command")
    parser.add_argument("--check", action="store_true", help="check local setup without syncing (same as usync check)")
    args = parser.parse_args()
    if args.unit == "help":
        if args.pack_unit:
            parser.error("usync help takes no unit code; use: usync help")
        parser.print_help()
        return
    config = load_config()
    canvas, ed = load_secrets()
    if args.unit == "login":
        raise SystemExit(browser_login(config, canvas))
    if args.check or args.unit == "check":
        raise SystemExit(check(config, canvas, ed))
    semester = config["active_semester"]
    semester_cfg = config["semesters"][semester]
    configured = [str(u).upper() for u in semester_cfg.get("units", [])]
    if args.unit == "pack":
        if not args.pack_unit:
            raise SystemExit("Usage: usync pack UNIT")
        pack_unit = args.pack_unit.upper()
        if pack_unit not in configured:
            raise SystemExit(f"Not configured in {semester}: {pack_unit}")
        pack_existing(pack_unit, semester)
        raise SystemExit(0)
    if args.pack_unit:
        raise SystemExit("Unexpected second argument. Use: usync pack UNIT")
    units = [args.unit.upper()] if args.unit else configured
    unknown = [u for u in units if u not in configured]
    if unknown: raise SystemExit(f"Not configured in {semester}: {', '.join(unknown)}")
    ok = 0
    for unit in units:
        print(f"\nSyncing {unit}")
        ok += int(sync_unit(unit, semester, semester_cfg, config, canvas, ed))
    print(f"\nCompleted {ok}/{len(units)} unit(s).")
    raise SystemExit(0 if ok == len(units) else 1)


if __name__ == "__main__":
    main()
