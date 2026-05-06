import requests
import time
import json
import os

# --- CONFIG ---
BASE = "https://banner.aws.valenciacollege.edu/StudentRegistrationSsb/ssb"
TERM = "202610"  # check the term dropdown for the real code
WATCH = [
    {"subject": "COP", "courseNumber": "2800"},  # add CRNs or subject+number
]
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]
POLL_SECONDS = 180  # 3 minutes — be polite
STATE_FILE = "seats.json"

def get_session():
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (course-watcher; personal use)"})
    # 1. Hit term selection to get cookies
    s.get(f"{BASE}/term/termSelection?mode=search")
    # 2. Set the term in session
    s.post(f"{BASE}/term/search?mode=search",
           data={"term": TERM, "studyPath": "", "studyPathText": "",
                 "startDatepicker": "", "endDatepicker": ""})
    return s

def search(s, subject, course_number):
    params = {
        "txt_subject": subject,
        "txt_courseNumber": course_number,
        "txt_term": TERM,
        "pageOffset": 0,
        "pageMaxSize": 50,
    }
    r = s.get(f"{BASE}/searchResults/searchResults", params=params)
    r.raise_for_status()
    return r.json().get("data", [])

def notify(msg):
    requests.post(DISCORD_WEBHOOK, json={"content": msg})

def load_state():
    try:
        return json.load(open(STATE_FILE))
    except FileNotFoundError:
        return {}

def save_state(state):
    json.dump(state, open(STATE_FILE, "w"))

def main():
    state = load_state()
    while True:
        try:
            s = get_session()
            for course in WATCH:
                sections = search(s, course["subject"], course["courseNumber"])
                for sec in sections:
                    crn = sec["courseReferenceNumber"]
                    seats = sec.get("seatsAvailable", 0)
                    prev = state.get(crn, 0)
                    if seats > 0 and prev == 0:
                        notify(f"🟢 OPEN: {sec['subject']} {sec['courseNumber']} "
                               f"CRN {crn} — {seats} seats — {sec.get('faculty', [{}])[0].get('displayName', 'TBA')}")
                    state[crn] = seats
            save_state(state)
        except Exception as e:
            print(f"error: {e}")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()