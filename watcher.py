#!/usr/bin/env python3
"""Valencia College Banner course availability watcher with Discord notifications."""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime

import requests
import yaml

BASE_URL = "https://banner.aws.valenciacollege.edu/StudentRegistrationSsb/ssb"
USER_AGENT = "valencia-course-watcher/1.0 (personal; course availability monitor)"
BANNER_SEARCH_URL = "https://banner.aws.valenciacollege.edu/StudentRegistrationSsb/ssb/term/termSelection?mode=search"


def log(level, msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [{level}] {msg}", flush=True)


def load_config(path):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    # Defaults
    cfg.setdefault("poll_interval", 180)
    cfg.setdefault("poll_jitter", 15)
    cfg.setdefault("notify_waitlist", False)
    cfg.setdefault("state_file", "seats.json")
    if "term" not in cfg:
        log("FATAL", "config missing required 'term' field")
        sys.exit(1)
    if "courses" not in cfg or not cfg["courses"]:
        log("FATAL", "config missing required 'courses' list")
        sys.exit(1)
    return cfg


def load_state(path):
    """Load persisted seat state. Returns None if file doesn't exist (first run)."""
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def save_state(path, state):
    """Atomic write: write to .tmp then os.replace."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def make_session():
    """Create a requests.Session with our User-Agent."""
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def init_banner_session(session, term):
    """3-step Banner session flow: init cookies, set term, return session."""
    # Step 1: GET termSelection to get cookies
    resp = session.get(f"{BASE_URL}/term/termSelection", params={"mode": "search"})
    resp.raise_for_status()

    # Step 2: POST to set term
    resp = session.post(f"{BASE_URL}/term/search", params={"mode": "search"}, data={"term": term})
    resp.raise_for_status()

    return session


def validate_term(session, term):
    """Check that the term code exists in Banner's active terms list."""
    resp = session.get(f"{BASE_URL}/classSearch/getTerms", params={
        "searchTerm": "",
        "offset": 1,
        "max": 50,
    })
    resp.raise_for_status()
    terms = resp.json()
    for t in terms:
        if t["code"] == term:
            log("INFO", f"Term validated: {term} = {t['description']}")
            return t["description"]
    available = ", ".join(f"{t['code']} ({t['description']})" for t in terms[:10])
    log("FATAL", f"Term {term} not found in Banner. Available: {available}")
    sys.exit(1)


def search_sections(session, term, subject, course_number=None, crn=None):
    """Search Banner for sections. Returns list of section dicts or raises."""
    params = {
        "txt_term": term,
        "pageOffset": 0,
        "pageMaxSize": 200,
        "sortColumn": "subjectDescription",
        "sortDirection": "asc",
    }
    if crn:
        params["txt_crn"] = crn
    else:
        params["txt_subject"] = subject
        if course_number:
            params["txt_courseNumber"] = course_number

    resp = session.get(f"{BASE_URL}/searchResults/searchResults", params=params)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("success") or data.get("data") is None:
        raise RuntimeError(
            f"Banner returned success={data.get('success')}, data={data.get('data')} "
            f"(totalCount={data.get('totalCount')}). Likely stale session or bad query."
        )

    return data["data"]


def format_meeting_times(section):
    """Build a human-readable meeting time string from a section."""
    meetings = section.get("meetingsFaculty") or []
    parts = []
    day_abbrevs = [
        ("monday", "Mon"), ("tuesday", "Tue"), ("wednesday", "Wed"),
        ("thursday", "Thu"), ("friday", "Fri"), ("saturday", "Sat"), ("sunday", "Sun"),
    ]
    for mf in meetings:
        mt = mf.get("meetingTime") or {}
        if mt.get("building") == "ONLINE" or mt.get("meetingType") == "WEB":
            parts.append("Online")
            continue
        days = "".join(abbr for key, abbr in day_abbrevs if mt.get(key))
        begin = mt.get("beginTime") or ""
        end = mt.get("endTime") or ""
        time_str = ""
        if begin and end:
            time_str = f" {begin[:2]}:{begin[2:]}-{end[:2]}:{end[2:]}"
        loc = mt.get("buildingDescription") or mt.get("building") or ""
        room = mt.get("room")
        if room:
            loc += f" {room}"
        campus = mt.get("campusDescription") or ""
        line = f"{days}{time_str}"
        if loc:
            line += f", {loc}"
        if campus and campus not in loc:
            line += f" ({campus})"
        parts.append(line)
    return " | ".join(parts) if parts else "TBA"


def get_instructor(section):
    """Get primary instructor name."""
    for f in section.get("faculty") or []:
        if f.get("primaryIndicator"):
            return f.get("displayName", "TBA")
    # Fallback to first faculty if no primary
    faculty = section.get("faculty") or []
    if faculty:
        return faculty[0].get("displayName", "TBA")
    return "TBA"


def build_discord_embed(section, change_type, old_count, new_count):
    """Build a Discord embed for a seat/waitlist change notification."""
    crn = section["courseReferenceNumber"]
    subject = section["subject"]
    course_num = section["courseNumber"]
    title_text = section.get("courseTitle", "")
    instructor = get_instructor(section)
    meeting = format_meeting_times(section)
    seats_avail = section["seatsAvailable"]
    seats_total = section["maximumEnrollment"]
    wait_avail = section.get("waitAvailable", 0)
    wait_cap = section.get("waitCapacity", 0)

    if change_type == "seats":
        embed_title = f"\U0001f7e2 {subject} {course_num} — Seat opened"
        color = 0x2ECC71  # green
    else:
        embed_title = f"\U0001f7e1 {subject} {course_num} — Waitlist opened"
        color = 0xF1C40F  # yellow

    fields = [
        {"name": "CRN", "value": crn, "inline": True},
        {"name": "Title", "value": title_text or "N/A", "inline": True},
        {"name": "Instructor", "value": instructor, "inline": True},
        {"name": "Seats", "value": f"**{seats_avail}** / {seats_total} available", "inline": True},
    ]
    if wait_cap > 0:
        fields.append({"name": "Waitlist", "value": f"**{wait_avail}** / {wait_cap} available", "inline": True})
    fields.append({"name": "Schedule", "value": meeting, "inline": False})
    fields.append({
        "name": "Banner",
        "value": f"[Search page]({BANNER_SEARCH_URL})",
        "inline": False,
    })

    return {
        "title": embed_title,
        "color": color,
        "fields": fields,
        "footer": {"text": f"Change: {old_count} → {new_count} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"},
    }


def send_discord(webhook_url, embed, dry_run=False):
    """Send a Discord webhook with an embed. Returns True on success."""
    payload = {"embeds": [embed]}
    if dry_run:
        log("DRY-RUN", f"Would send: {embed['title']}")
        return True
    try:
        resp = requests.post(webhook_url, json=payload, headers={"User-Agent": USER_AGENT}, timeout=10)
        if resp.status_code == 204:
            return True
        log("WARN", f"Discord webhook returned {resp.status_code}: {resp.text[:200]}")
        return False
    except requests.RequestException as e:
        log("ERROR", f"Discord webhook failed: {e}")
        return False


def matches_watch(section, course_cfg):
    """Check if a section matches a watch config entry."""
    if "crn" in course_cfg:
        return section["courseReferenceNumber"] == str(course_cfg["crn"])
    subj_match = section["subject"].upper() == course_cfg.get("subject", "").upper()
    num_match = True
    if "courseNumber" in course_cfg:
        num_match = section["courseNumber"].upper() == course_cfg["courseNumber"].upper()
    return subj_match and num_match


def poll_cycle(cfg, state, webhook_url, dry_run=False):
    """Run one poll cycle. Returns updated state dict and whether any notifications were sent."""
    term = cfg["term"]
    first_run = state is None
    if first_run:
        state = {}

    session = make_session()
    init_banner_session(session, term)

    # Deduplicate search queries to avoid redundant API calls
    queries = []
    seen = set()
    for course in cfg["courses"]:
        if "crn" in course:
            key = ("crn", str(course["crn"]))
        else:
            key = ("subject", course.get("subject", ""), course.get("courseNumber", ""))
        if key not in seen:
            seen.add(key)
            queries.append(course)

    all_sections = []
    for course in queries:
        sections = search_sections(
            session, term,
            subject=course.get("subject"),
            course_number=course.get("courseNumber"),
            crn=course.get("crn"),
        )
        all_sections.extend(sections)

    # Filter to only sections matching our watch list
    watched = []
    for section in all_sections:
        for course in cfg["courses"]:
            if matches_watch(section, course):
                watched.append(section)
                break

    if not watched:
        log("WARN", "No sections matched watch list this cycle")

    notified = False
    new_state = {}

    for section in watched:
        crn = section["courseReferenceNumber"]
        subj = section["subject"]
        cnum = section["courseNumber"]
        seats = section["seatsAvailable"]
        seats_total = section["maximumEnrollment"]
        wait = section.get("waitAvailable", 0)
        wait_cap = section.get("waitCapacity", 0)
        label = f"{subj} {cnum} CRN {crn}"

        old = state.get(crn, {})
        old_seats = old.get("seats", 0)
        old_wait = old.get("wait", 0)

        new_state[crn] = {"seats": seats, "wait": wait}

        if first_run:
            log("INFO", f"{label}: seats {seats}/{seats_total}, wait {wait}/{wait_cap} (initial load, no notification)")
            continue

        # Seat notification: 0 → >0
        if old_seats == 0 and seats > 0:
            log("INFO", f"{label}: seats {old_seats} → {seats} (NOTIFY)")
            embed = build_discord_embed(section, "seats", old_seats, seats)
            if webhook_url:
                send_discord(webhook_url, embed, dry_run=dry_run)
            notified = True
        elif seats != old_seats:
            log("INFO", f"{label}: seats {old_seats} → {seats}")

        # Waitlist notification (if enabled): 0 → >0
        if cfg.get("notify_waitlist") and wait_cap > 0:
            if old_wait == 0 and wait > 0:
                log("INFO", f"{label}: waitlist {old_wait} → {wait} (NOTIFY)")
                embed = build_discord_embed(section, "waitlist", old_wait, wait)
                if webhook_url:
                    send_discord(webhook_url, embed, dry_run=dry_run)
                notified = True
            elif wait != old_wait:
                log("INFO", f"{label}: waitlist {old_wait} → {wait}")

    return new_state, notified


def startup_checks(cfg):
    """Validate term and watch targets against live Banner data."""
    log("INFO", "Running startup checks...")
    session = make_session()

    # Validate term exists
    validate_term(session, cfg["term"])

    # Init session and check each watch target returns sections
    init_banner_session(session, cfg["term"])

    for course in cfg["courses"]:
        label = course.get("crn") or f"{course.get('subject', '?')} {course.get('courseNumber', '?')}"
        sections = search_sections(
            session, cfg["term"],
            subject=course.get("subject"),
            course_number=course.get("courseNumber"),
            crn=course.get("crn"),
        )
        matched = [s for s in sections if matches_watch(s, course)]
        if not matched:
            log("FATAL", f"Watch target '{label}' returned 0 matching sections for term {cfg['term']}. "
                         f"Check subject/courseNumber/crn in config. Search returned {len(sections)} total sections.")
            sys.exit(1)
        log("INFO", f"Watch target '{label}': found {len(matched)} section(s)")
        for s in matched:
            log("INFO", f"  CRN {s['courseReferenceNumber']}: {s['courseTitle']} — "
                        f"{s['seatsAvailable']}/{s['maximumEnrollment']} seats, "
                        f"instructor: {get_instructor(s)}")

    log("INFO", "Startup checks passed")


def main():
    parser = argparse.ArgumentParser(description="Valencia College course availability watcher")
    parser.add_argument("--once", action="store_true", help="Single poll cycle, then exit")
    parser.add_argument("--dry-run", action="store_true", help="Log notifications instead of sending")
    parser.add_argument("--config", default="config.yaml", help="Config file path (default: config.yaml)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    state_file = cfg["state_file"]

    webhook_url = os.environ.get("DISCORD_WEBHOOK")
    if not webhook_url and not args.dry_run:
        log("FATAL", "DISCORD_WEBHOOK environment variable not set. Use --dry-run to test without it.")
        sys.exit(1)

    # Startup validation
    startup_checks(cfg)

    state = load_state(state_file)
    consecutive_failures = 0

    while True:
        try:
            new_state, _ = poll_cycle(cfg, state, webhook_url, dry_run=args.dry_run)
            state = new_state
            save_state(state_file, state)
            consecutive_failures = 0
        except requests.RequestException as e:
            consecutive_failures += 1
            backoff = min(900, 30 * (2 ** (consecutive_failures - 1)))  # 30s, 60s, 120s, ... cap 900s
            log("ERROR", f"HTTP error (attempt {consecutive_failures}): {e}. Backoff {backoff}s.")
            time.sleep(backoff)
            continue
        except json.JSONDecodeError as e:
            consecutive_failures += 1
            backoff = min(900, 30 * (2 ** (consecutive_failures - 1)))
            log("ERROR", f"JSON decode error (attempt {consecutive_failures}): {e}. Backoff {backoff}s.")
            time.sleep(backoff)
            continue
        except RuntimeError as e:
            # Banner returned success=false or data=null — skip cycle, don't update state
            consecutive_failures += 1
            backoff = min(900, 30 * (2 ** (consecutive_failures - 1)))
            log("ERROR", f"Banner error (attempt {consecutive_failures}): {e}. Backoff {backoff}s.")
            time.sleep(backoff)
            continue

        if args.once:
            log("INFO", "Single cycle complete (--once), exiting")
            break

        interval = cfg["poll_interval"] + random.uniform(-cfg["poll_jitter"], cfg["poll_jitter"])
        log("INFO", f"Sleeping {interval:.0f}s until next poll")
        time.sleep(interval)


if __name__ == "__main__":
    main()
