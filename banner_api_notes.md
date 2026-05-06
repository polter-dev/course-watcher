# Valencia College Banner Self-Service API Notes

> Investigated 2026-05-06 against live production endpoints.

## Base URL

```
https://banner.aws.valenciacollege.edu/StudentRegistrationSsb/ssb
```

---

## Session Flow (ORDER MATTERS)

You **must** follow this sequence. Skipping step 1 or 2 causes the search endpoint to return `totalCount: 0` with `data: null`.

1. **GET** `/term/termSelection?mode=search` — initializes the session, returns `JSESSIONID` and `AWSALB`/`AWSALBCORS` cookies.
2. **POST** `/term/search?mode=search` — sets the active term in the session.
3. **GET** `/searchResults/searchResults?...` — returns course sections.

---

## Endpoints

### 1. Initialize Session

```
GET /term/termSelection?mode=search
```

**Response:** HTML page (ignored). The important part is the cookies.

**Set-Cookie headers returned:**
| Cookie | Purpose | Scope |
|--------|---------|-------|
| `JSESSIONID` | Server session ID | `Path=/StudentRegistrationSsb; Secure; HttpOnly` |
| `AWSALB` | AWS ALB sticky session | `Path=/; Expires=+7d` |
| `AWSALBCORS` | Same as AWSALB, with `SameSite=None; Secure` | Same |

All three cookies must be sent on subsequent requests.

---

### 2. Get Available Terms

```
GET /classSearch/getTerms?searchTerm=&offset=1&max=30
```

**Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `searchTerm` | string | No | Filter term descriptions (empty = all) |
| `offset` | int | Yes | 1-based offset |
| `max` | int | Yes | Max results to return |

**Response:** JSON array (not wrapped in an object):
```json
[
  { "code": "202710", "description": "Fall 2026 Credit Courses" },
  { "code": "202630", "description": "Summer 2026 Credit Courses" },
  { "code": "202620", "description": "Spring 2026 Credit Courses (View Only)" },
  ...
]
```

**Term code format:** `YYYYTT` where `YYYY` = academic year, `TT` = `10` (Fall), `20` (Spring), `30` (Summer).

**Key term codes:**
| Code | Description |
|------|-------------|
| `202710` | Fall 2026 |
| `202630` | Summer 2026 |
| `202620` | Spring 2026 (View Only) |

---

### 3. Set Active Term (REQUIRED before search)

```
POST /term/search?mode=search
Content-Type: application/x-www-form-urlencoded
```

**Form data:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `term` | string | Yes | Term code, e.g. `202620` |

**Response:**
```json
{
  "fwdURL": "/StudentRegistrationSsb/ssb/classSearch/classSearch"
}
```

**Critical quirk:** If you skip this POST, the search endpoint returns `{ "success": true, "totalCount": 0, "data": null }` — it looks successful but is empty. No error is raised.

---

### 4. Search for Sections

```
GET /searchResults/searchResults?txt_subject=COP&txt_term=202620&pageOffset=0&pageMaxSize=50&sortColumn=subjectDescription&sortDirection=asc
```

**Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `txt_subject` | string | Yes* | Subject code, e.g. `COP` |
| `txt_term` | string | Yes | Term code, e.g. `202620` |
| `txt_courseNumber` | string | No | Course number, e.g. `2210C` |
| `txt_courseTitle` | string | No | Keyword search in title |
| `txt_instructor` | string | No | Instructor name search |
| `txt_campus` | string | No | Campus code |
| `txt_crn` | string | No | Specific CRN |
| `pageOffset` | int | Yes | 0-based offset (number of records to skip) |
| `pageMaxSize` | int | Yes | Page size (tested up to 50) |
| `sortColumn` | string | No | e.g. `subjectDescription` |
| `sortDirection` | string | No | `asc` or `desc` |
| `startDatepicker` | string | No | Filter by start date |
| `endDatepicker` | string | No | Filter by end date |

*At least one search filter is required (subject, CRN, course number, etc.).

**Response envelope:**
```json
{
  "success": true,
  "totalCount": 46,
  "data": [ ... ],
  "pageOffset": 0,
  "pageMaxSize": 50,
  "sectionsFetchedCount": 46,
  "pathMode": "search",
  "searchResultsConfigs": null,
  "ztcEncodedImage": null
}
```

---

### 5. Get Subjects (helper endpoint)

```
GET /classSearch/get_subject?searchTerm=COP&term=202620&offset=1&max=10
```

**Response:**
```json
[
  { "code": "COP", "description": "COP: Computer Programming" }
]
```

---

## Section Object — Full Field Reference

Below is the complete JSON shape for one section, with types annotated.

```json
{
  "id": 512111,                                    // int — internal Banner ID
  "term": "202620",                                // string — term code
  "termDesc": "Spring 2026 Credit Courses",        // string
  "courseReferenceNumber": "21869",                 // string — THIS IS THE CRN
  "partOfTerm": "1",                               // string — "1"=full, "H1"=first half, etc.
  "courseNumber": "1000C",                          // string — course number
  "courseDisplay": "1000C",                         // string — display version (same)
  "subject": "COP",                                // string — subject code
  "subjectDescription": "COP: Computer Programming", // string
  "sequenceNumber": "0",                           // string — section sequence
  "campusDescription": "Osceola Campus",           // string — campus name
  "scheduleTypeDescription": "Combined Class/Lab", // string
  "courseTitle": "Intro to Programming Concepts",   // string — section title
  "creditHours": null,                             // int|null
  "creditHourLow": 3,                              // int — credit hours
  "creditHourHigh": null,                          // int|null
  "creditHourIndicator": null,                     // string|null
  "maximumEnrollment": 27,                         // int — SEATS TOTAL
  "enrollment": 26,                                // int — seats taken
  "seatsAvailable": 1,                             // int — SEATS AVAILABLE
  "waitCapacity": 0,                               // int — WAITLIST TOTAL
  "waitCount": 0,                                  // int — waitlist taken
  "waitAvailable": 0,                              // int — WAITLIST AVAILABLE
  "openSection": true,                             // bool — true if seats > 0
  "crossList": null,                               // string|null
  "crossListCapacity": null,                       // int|null
  "crossListCount": null,                          // int|null
  "crossListAvailable": null,                      // int|null
  "linkIdentifier": null,                          // string|null
  "isSectionLinked": false,                        // bool
  "subjectCourse": "COP1000C",                     // string — subject+courseNumber combined
  "instructionalMethod": "M",                      // string — "X"=Online, "M"=Mixed, etc.
  "instructionalMethodDescription": "Mixed Mode",  // string
  "reservedSeatSummary": null,                     // object|null
  "sectionAttributes": null,                       // array|null

  "faculty": [                                     // array — instructor(s)
    {
      "bannerId": "623326",                        // string
      "category": null,                            // string|null
      "class": "net.hedtech.banner.student.faculty.FacultyResultDecorator",
      "courseReferenceNumber": "21869",             // string
      "displayName": "Edgardo Roman Ruiz",         // string — INSTRUCTOR NAME
      "emailAddress": "eromanruiz1@valenciacollege.edu", // string
      "primaryIndicator": true,                    // bool
      "term": "202620"                             // string
    }
  ],

  "meetingsFaculty": [                             // array — MEETING TIMES
    {
      "category": "01",                            // string
      "class": "net.hedtech.banner.student.schedule.SectionSessionDecorator",
      "courseReferenceNumber": "21869",
      "faculty": [],                               // array (usually empty here)
      "term": "202620",
      "meetingTime": {
        "beginTime": "1000",                       // string|null — 24hr "HHMM" format
        "endTime": "1115",                         // string|null — 24hr "HHMM" format
        "building": "OC-003",                      // string — building code
        "buildingDescription": "Osceola Campus Building 3", // string
        "room": "301",                             // string|null
        "campus": "OC",                            // string — CAMPUS CODE
        "campusDescription": "Osceola Campus",     // string — CAMPUS NAME
        "startDate": "01/12/2026",                 // string — MM/DD/YYYY
        "endDate": "05/03/2026",                   // string — MM/DD/YYYY
        "monday": false,                           // bool — DAYS OF WEEK
        "tuesday": false,
        "wednesday": true,
        "thursday": false,
        "friday": false,
        "saturday": false,
        "sunday": false,
        "hoursWeek": 1.25,                         // float
        "creditHourSession": 3.0,                  // float
        "meetingType": "CLAS",                      // string — "CLAS", "WEB", etc.
        "meetingTypeDescription": "Class",          // string — "Class", "Online"
        "meetingScheduleType": "B",                 // string
        "category": "01",
        "class": "net.hedtech.banner.general.overall.MeetingTimeDecorator",
        "courseReferenceNumber": "21869",
        "term": "202620"
      }
    }
  ]
}
```

---

## Key Field Mapping

| What you need | Field path | Notes |
|---------------|-----------|-------|
| CRN | `courseReferenceNumber` | String, not int |
| Subject | `subject` | e.g. `"COP"` |
| Course number | `courseNumber` | e.g. `"1000C"` |
| Section title | `courseTitle` | e.g. `"Intro to Programming Concepts"` |
| Instructor name | `faculty[0].displayName` | Check `primaryIndicator` for primary |
| Seats available | `seatsAvailable` | int |
| Seats total | `maximumEnrollment` | int |
| Waitlist available | `waitAvailable` | int |
| Waitlist total | `waitCapacity` | int |
| Meeting days | `meetingsFaculty[].meetingTime.{monday..sunday}` | booleans |
| Meeting start | `meetingsFaculty[].meetingTime.beginTime` | `"HHMM"` 24hr or `null` for online |
| Meeting end | `meetingsFaculty[].meetingTime.endTime` | Same format |
| Campus | `meetingsFaculty[].meetingTime.campusDescription` | Also top-level `campusDescription` |
| Building/room | `meetingsFaculty[].meetingTime.building` + `.room` | |
| Open/closed | `openSection` | bool |

---

## Quirks and Gotchas

1. **Session ordering is mandatory.** You MUST POST to `/term/search?mode=search` before calling `/searchResults/searchResults`. Without it, you get `{ "success": true, "totalCount": 0, "data": null }` — **no error, just empty results**. This is the biggest footgun.

2. **No CSRF token required.** Tested all endpoints without any CSRF/XSRF tokens. Cookies alone are sufficient.

3. **`courseReferenceNumber` is a string**, not an integer, despite looking like one.

4. **Online sections have `null` for `beginTime`/`endTime`** and all day-of-week booleans are `false`. The `building` is `"ONLINE"` and `meetingType` is `"WEB"`.

5. **`meetingsFaculty` is an array** — a section can have multiple meeting patterns (e.g., lecture MW + lab F). Always iterate, don't just take `[0]`.

6. **Pagination:** `pageOffset` is 0-based and counts records (not pages). To get all 46 COP sections at once, use `pageMaxSize=50`. The API returns `sectionsFetchedCount` as the total fetched for the term/filter combo.

7. **Waitlist can be 0 capacity.** Many Valencia sections have `waitCapacity: 0`, meaning no waitlist exists. Check `waitCapacity > 0` before reporting waitlist status.

8. **Cookies expire:** `AWSALB` expires in 7 days. `JSESSIONID` will likely expire after server-side session timeout (typically 20-30 min of inactivity). For a long-running watcher, re-initialize the session periodically.

9. **No rate limiting observed** during polite testing (~2s between requests). However, no guarantees — the watcher should space polls at least 30-60 seconds apart.

10. **`(View Only)` terms** are still searchable — the label appears in `description` from `/getTerms` but the search still returns live enrollment data.

11. **The `txt_term` param on searchResults appears redundant** — the session already has the term set from the POST. However, the web UI sends it, so include it for safety.

---

## Minimal Working Example (curl)

```bash
BASE="https://banner.aws.valenciacollege.edu/StudentRegistrationSsb/ssb"

# Step 1: Get session cookies
COOKIES=$(curl -s -D - -o /dev/null "$BASE/term/termSelection?mode=search" | \
  grep -i 'set-cookie' | sed 's/set-cookie: //i' | cut -d';' -f1 | tr '\n' '; ')

# Step 2: Set term
curl -s -b "$COOKIES" -X POST "$BASE/term/search?mode=search" -d 'term=202620'

# Step 3: Search
curl -s -b "$COOKIES" "$BASE/searchResults/searchResults?txt_subject=COP&txt_term=202620&pageOffset=0&pageMaxSize=50"
```

---

## Minimal Working Example (Python requests)

```python
import requests

BASE = "https://banner.aws.valenciacollege.edu/StudentRegistrationSsb/ssb"
session = requests.Session()

# Step 1: Init session
session.get(f"{BASE}/term/termSelection?mode=search")

# Step 2: Set term
session.post(f"{BASE}/term/search?mode=search", data={"term": "202620"})

# Step 3: Search
resp = session.get(f"{BASE}/searchResults/searchResults", params={
    "txt_subject": "COP",
    "txt_term": "202620",
    "pageOffset": 0,
    "pageMaxSize": 50,
})
data = resp.json()
for section in data["data"]:
    print(f"CRN {section['courseReferenceNumber']}: "
          f"{section['courseTitle']} — "
          f"{section['seatsAvailable']}/{section['maximumEnrollment']} seats")
```
