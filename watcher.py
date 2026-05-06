#!/usr/bin/env python3
"""Valencia College Banner course availability watcher with Discord notifications."""

import argparse
import fcntl
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
    cfg.setdefault("online_only", True)
    cfg.setdefault("notify_new_sections", True)
    if "term" not in cfg:
        log("FATAL", "config missing required 'term' field")
        sys.exit(1)
    if "courses" not in cfg or not cfg["courses"]:
        log("FATAL", "config missing required 'courses' list")
        sys.exit(1)
    return cfg


def load_state(path):
    """Load persisted seat state. Returns None if file doesn't exist or is corrupt."""
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log("WARN", f"State file {path} has unexpected type {type(data).__name__}, treating as first run")
            return None
        return data
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, ValueError) as e:
        log("WARN", f"State file {path} is corrupt ({e}), treating as first run")
        return None


def save_state(path, state):
    """Atomic write: write to .tmp then os.replace."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def make_session():
    """Create a requests.Session with our User-Agent and default timeout."""
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})

    # Patch send to enforce default timeout (10s connect, 30s read)
    original_send = s.send

    def send_with_timeout(*args, **kwargs):
        kwargs.setdefault("timeout", (10, 30))
        return original_send(*args, **kwargs)

    s.send = send_with_timeout
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


def _fetch_sections(session, params):
    """Single Banner search request. Returns list of section dicts or raises."""
    resp = session.get(f"{BASE_URL}/searchResults/searchResults", params=params)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("success") or data.get("data") is None:
        raise RuntimeError(
            f"Banner returned success={data.get('success')}, data={data.get('data')} "
            f"(totalCount={data.get('totalCount')}). Likely stale session or bad query."
        )

    total = data.get("totalCount", 0)
    fetched = len(data["data"])
    if total > fetched:
        log("WARN", f"Banner returned {fetched}/{total} sections — results truncated. "
                     f"Consider narrowing your search (use courseNumber or CRN).")

    return data["data"]


def search_sections(session, term, subject, course_number=None, crn=None):
    """Search Banner for sections, merging open and full results.

    Banner quirks:
    - Default search returns only sections with open seats.
    - chk_open_only=false INVERTS this, returning only full sections.
    - Banner caches the chk_open_only setting per session, so the second
      request in the same session ignores the parameter change.
    To get ALL sections we make the open-seats request on the provided
    session, then create a fresh session for the full-sections request.
    """
    base_params = {
        "txt_term": term,
        "pageOffset": 0,
        "pageMaxSize": 200,
        "sortColumn": "subjectDescription",
        "sortDirection": "asc",
    }
    if crn:
        base_params["txt_crn"] = crn
    else:
        base_params["txt_subject"] = subject
        if course_number:
            base_params["txt_courseNumber"] = course_number

    open_sections = []
    full_sections = []
    open_ok = False
    full_ok = False

    # Request 1: open seats (default), using existing session
    try:
        open_sections = _fetch_sections(session, base_params)
        open_ok = True
    except (requests.RequestException, RuntimeError) as e:
        log("WARN", f"Failed to fetch open sections: {e}")

    # Request 2: full sections only — needs its own session because
    # Banner caches chk_open_only per session
    try:
        full_session = make_session()
        init_banner_session(full_session, term)
        full_params = {**base_params, "chk_open_only": "false"}
        full_sections = _fetch_sections(full_session, full_params)
        full_ok = True
    except (requests.RequestException, RuntimeError) as e:
        log("WARN", f"Failed to fetch full sections: {e}")

    if not open_ok and not full_ok:
        raise RuntimeError("Both open and full section searches failed")

    # Merge, deduping by CRN (prefer open-seats data since it has current counts)
    seen = {}
    for section in open_sections:
        seen[section["courseReferenceNumber"]] = section
    for section in full_sections:
        if section["courseReferenceNumber"] not in seen:
            seen[section["courseReferenceNumber"]] = section

    return list(seen.values())


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
    """Send a Discord webhook with an embed. Retries once on 429. Returns True on success."""
    payload = {"embeds": [embed]}
    if dry_run:
        log("DRY-RUN", f"Would send: {embed['title']}")
        return True
    for attempt in range(2):
        try:
            resp = requests.post(webhook_url, json=payload, headers={"User-Agent": USER_AGENT}, timeout=10)
            if resp.status_code == 204:
                return True
            if resp.status_code == 429 and attempt == 0:
                retry_after = 5
                try:
                    retry_after = resp.json().get("retry_after", 5)
                except (ValueError, KeyError):
                    pass
                log("WARN", f"Discord rate limited, retrying after {retry_after}s")
                time.sleep(retry_after)
                continue
            log("WARN", f"Discord webhook returned {resp.status_code}: {resp.text[:200]}")
            return False
        except requests.RequestException as e:
            log("ERROR", f"Discord webhook failed: {e}")
            return False
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


def is_online_section(section):
    """A section is online if instructionalMethod == 'X' (Online Course).
    Mixed Mode ('M') and Onsite ('N') are NOT online.
    See banner_api_notes.md 'Identifying Online Sections' for probe data."""
    method = section.get("instructionalMethod")
    if method is None:
        log("WARN", f"CRN {section.get('courseReferenceNumber', '?')}: "
                     "instructionalMethod is missing — treating as non-online")
    return method == "X"


def build_new_section_embed(section):
    """Build a Discord embed for a newly added section."""
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
        "title": f"\U0001f195 New section added: {subject} {course_num}",
        "color": 0x3498DB,  # blue
        "fields": fields,
        "footer": {"text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
    }


def poll_cycle(cfg, state, webhook_url, dry_run=False):
    """Run one poll cycle. Returns updated state dict and whether any notifications were sent."""
    term = cfg["term"]
    first_run = state is None or not state
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
    online_only = cfg.get("online_only", True)
    notify_new = cfg.get("notify_new_sections", True)
    watched = []
    all_matched_crns = set()  # CRNs matching watch list before online filter
    for section in all_sections:
        for course in cfg["courses"]:
            if matches_watch(section, course):
                all_matched_crns.add(section["courseReferenceNumber"])
                if online_only and not is_online_section(section):
                    log("INFO", f"Excluding {section['courseReferenceNumber']} ({get_instructor(section)}, "
                                f"{section.get('campusDescription', 'N/A')}): not online")
                    break
                watched.append(section)
                break

    if not watched:
        if not first_run and state:
            log("WARN", "No sections matched watch list this cycle — skipping state update "
                        "(possible stale session or Banner outage)")
            return state, False
        log("WARN", "No matching sections this cycle (online_only={})".format(online_only))

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

        is_new = crn not in state
        old = state.get(crn, {})
        old_seats = old.get("seats", 0)
        old_wait = old.get("wait", 0)

        new_state[crn] = {"seats": seats, "wait": wait}

        if first_run:
            log("INFO", f"{label}: seats {seats}/{seats_total}, wait {wait}/{wait_cap} (initial load, no notification)")
            continue

        # New-section detection: CRN not in previous state
        if is_new and notify_new:
            log("INFO", f"{label}: NEW SECTION DETECTED — seats {seats}/{seats_total} (NOTIFY)")
            embed = build_new_section_embed(section)
            if webhook_url:
                if not send_discord(webhook_url, embed, dry_run=dry_run):
                    log("ERROR", f"{label}: failed to deliver new-section notification to Discord")
            notified = True
            continue  # Don't also fire seat notification for brand-new sections

        # Seat notification: <=0 → >0
        if old_seats <= 0 and seats > 0:
            log("INFO", f"{label}: seats {old_seats} → {seats} (NOTIFY)")
            embed = build_discord_embed(section, "seats", old_seats, seats)
            if webhook_url:
                if not send_discord(webhook_url, embed, dry_run=dry_run):
                    log("ERROR", f"{label}: failed to deliver seat notification to Discord")
            notified = True
        elif seats != old_seats:
            log("INFO", f"{label}: seats {old_seats} → {seats}")

        # Waitlist notification (if enabled): 0 → >0
        if cfg.get("notify_waitlist") and wait_cap > 0:
            if old_wait <= 0 and wait > 0:
                log("INFO", f"{label}: waitlist {old_wait} → {wait} (NOTIFY)")
                embed = build_discord_embed(section, "waitlist", old_wait, wait)
                if webhook_url:
                    if not send_discord(webhook_url, embed, dry_run=dry_run):
                        log("ERROR", f"{label}: failed to deliver waitlist notification to Discord")
                notified = True
            elif wait != old_wait:
                log("INFO", f"{label}: waitlist {old_wait} → {wait}")

    # Detect sections that disappeared (cancelled or removed)
    # Only carry forward CRNs that were in the pre-filter matched set.
    # Don't carry forward CRNs excluded by online_only — they'd persist forever.
    if not first_run:
        for crn in state:
            if crn not in new_state and crn in all_matched_crns:
                log("WARN", f"CRN {crn} disappeared from results — section may have been cancelled. "
                            "Carrying forward last state to avoid false trigger on reappearance.")
                new_state[crn] = state[crn]

    return new_state, notified


def startup_checks(cfg):
    """Validate term and watch targets against live Banner data."""
    log("INFO", "Running startup checks...")
    session = make_session()

    # Validate term exists
    validate_term(session, cfg["term"])

    # Init session and check each watch target returns sections
    init_banner_session(session, cfg["term"])

    online_only = cfg.get("online_only", True)

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

        # Apply online filter for reporting
        if online_only:
            online_matched = [s for s in matched if is_online_section(s)]
            excluded = [s for s in matched if not is_online_section(s)]
            for s in excluded:
                log("INFO", f"Excluding CRN {s['courseReferenceNumber']} "
                            f"({get_instructor(s)}, {s.get('campusDescription', 'N/A')}): not online")
            if not online_matched:
                log("WARN", f"No online sections of {label} currently exist for term {cfg['term']}. "
                            f"Watcher will continue running and notify you if one is added.")
            else:
                log("INFO", f"Watch target '{label}': {len(online_matched)} online section(s) "
                            f"({len(excluded)} non-online excluded)")
                for s in online_matched:
                    log("INFO", f"  CRN {s['courseReferenceNumber']}: {s['courseTitle']} — "
                                f"{s['seatsAvailable']}/{s['maximumEnrollment']} seats, "
                                f"instructor: {get_instructor(s)}")
        else:
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

    # Acquire lockfile to prevent concurrent instances
    lock_path = state_file + ".lock"
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("FATAL", f"Another instance is running (lockfile {lock_path}). Exiting.")
        sys.exit(1)

    # Startup validation
    startup_checks(cfg)

    state = load_state(state_file)
    consecutive_failures = 0

    while True:
        try:
            new_state, _ = poll_cycle(cfg, state, webhook_url, dry_run=args.dry_run)
            state = new_state
            if state:
                save_state(state_file, state)
            consecutive_failures = 0
        except requests.RequestException as e:
            consecutive_failures += 1
            backoff = min(900, 30 * (2 ** (consecutive_failures - 1)))
            backoff += random.uniform(0, backoff * 0.25)  # jitter up to 25%
            log("ERROR", f"HTTP error (attempt {consecutive_failures}): {e}. Backoff {backoff:.0f}s.")
            time.sleep(backoff)
            continue
        except json.JSONDecodeError as e:
            consecutive_failures += 1
            backoff = min(900, 30 * (2 ** (consecutive_failures - 1)))
            backoff += random.uniform(0, backoff * 0.25)
            log("ERROR", f"JSON decode error (attempt {consecutive_failures}): {e}. Backoff {backoff:.0f}s.")
            time.sleep(backoff)
            continue
        except RuntimeError as e:
            # Banner returned success=false or data=null — skip cycle, don't update state
            consecutive_failures += 1
            backoff = min(900, 30 * (2 ** (consecutive_failures - 1)))
            backoff += random.uniform(0, backoff * 0.25)
            log("ERROR", f"Banner error (attempt {consecutive_failures}): {e}. Backoff {backoff:.0f}s.")
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
