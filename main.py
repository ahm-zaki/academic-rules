import os
import json
import re
import uvicorn
from fastapi import FastAPI, UploadFile, File, HTTPException, Response
from pydantic import BaseModel
from typing import List, Dict, Any
from bs4 import BeautifulSoup

app = FastAPI(title="Student Academic Profile API")


# --- Pydantic Models ---
class HistoryCourse(BaseModel):
    course_code: str
    term: str
    credits_earned: int
    status: str


class HistoryUpdate(BaseModel):
    history: List[HistoryCourse]


class PlanCourse(BaseModel):
    course_code: str
    term: str


class PlanUpdate(BaseModel):
    planned_courses: List[PlanCourse]


# --- In-Memory Databases ---
# Structure: { "student_id": { "history": [...], "plan": [...] } }
db: Dict[str, Dict[str, Any]] = {}

# Structure: { "COSC3506": { "course_code": "COSC 3506", "credits": 3, ... } }
courses_db: Dict[str, Dict[str, Any]] = {}


# --- Helper Functions for Audit Engine ---
def normalize_code(code: str) -> str:
    """Format-insensitive course matching (COSC 3506 = COSC-3506 = cosc3506)."""
    return re.sub(r"[\s\-]", "", code).upper()


def format_code_for_msg(norm_code: str) -> str:
    """Formats a normalized code 'COSC3506' into 'COSC-3506' for clean JSON messaging.""" # noqa: E501
    match = re.match(r"([A-Z]{4})(\d{4})", norm_code)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return norm_code


def term_sort_key(term: str) -> tuple:
    """Term ordering: 2-digit-year + season (W < SP < S < F)."""
    season_map = {"W": 0, "SP": 1, "S": 2, "F": 3}
    match = re.match(r"(\d{2})([A-Z]+)", term.upper())
    if not match:
        return (99, 99)  # Fallback for invalid terms
    year = int(match.group(1))
    season = season_map.get(match.group(2), 99)
    return (year, season)


def extract_course_codes(text: str) -> List[str]:
    """Finds all potential course codes in a string (e.g. from prereq columns)."""
    return [normalize_code(m) for m in re.findall(r"[A-Za-z]{4}\s*[-]?\s*\d{4}", text)]


# --- Catalog Endpoints ---
@app.post("/api/v1/admin/catalog/import")
async def import_catalog(file: UploadFile = File(...)):
    """Imports course catalog from an HTML file."""
    contents = await file.read()
    soup = BeautifulSoup(contents, "html.parser")
    table = soup.find("table")

    if not table:
        raise HTTPException(
            status_code=400, detail="No table found in the uploaded HTML."
        )

    tbody = table.find("tbody")
    rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]

    extracted_count = 0
    for row in rows:
        cols = row.find_all("td")
        if len(cols) >= 5:
            original_code = cols[0].get_text(strip=True)
            lookup_key = normalize_code(original_code)

            try:
                credits_int = int(cols[2].get_text(strip=True))
            except ValueError:
                credits_int = 0

            courses_db[lookup_key] = {
                "course_code": original_code,
                "title": cols[1].get_text(strip=True),
                "credits": credits_int,
                "prerequisites": cols[3].get_text(strip=True),
                "cross_listed": cols[4].get_text(strip=True),
            }
            extracted_count += 1

    return {"message": f"Successfully imported {extracted_count} courses."}


@app.get("/api/v1/catalog/courses/{course_code}")
def get_course(course_code: str):
    lookup_key = normalize_code(course_code)
    course = courses_db.get(lookup_key)
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    return Response(content=json.dumps(course, indent=2), media_type="application/json")


# --- Student Profile Endpoints ---
@app.post("/api/v1/students/{student_id}/history/import", status_code=201)
async def import_history(student_id: str, file: UploadFile = File(...)):
    """Parses an uploaded HTML transcript."""
    content = await file.read()
    soup = BeautifulSoup(content, "html.parser")
    extracted = {}

    for table in soup.find_all("table"):
        thead = table.find("thead")
        if not thead:
            continue

        headers = thead.find_all("th")
        if len(headers) < 6:
            continue

        header_texts = [
            th.get_text(separator=" ", strip=True).lower() for th in headers
        ]
        if not (
            "status" in header_texts[0]
            and "course" in header_texts[1]
            and "grade" in header_texts[3]
            and "term" in header_texts[4]
            and "credits" in header_texts[5]
        ):
            continue

        tbody = table.find("tbody")
        if not tbody:
            continue

        for row in tbody.find_all("tr"):
            cols = row.find_all("td")
            if len(cols) < 6:
                continue

            raw_status = cols[0].get_text(separator=" ", strip=True)
            if "Completed" in raw_status:
                c_status = "Completed"
            elif "In-Progress" in raw_status:
                c_status = "In-Progress"
            elif "Attempted" in raw_status:
                c_status = "Attempted"
            else:
                continue

            term = cols[4].get_text(separator=" ", strip=True)
            if not term:
                continue

            a_tag = cols[1].find("a")
            if a_tag:
                course_code = a_tag.get_text(strip=True)
            else:
                text_parts = cols[1].get_text(separator=" ", strip=True).split()
                course_code = text_parts[0] if text_parts else ""

            if not course_code:
                continue

            raw_credits = cols[5].get_text(separator=" ", strip=True)
            try:
                credits_earned = int(raw_credits)
            except ValueError:
                credits_earned = 0

            raw_grade = cols[3].get_text(separator=" ", strip=True)
            try:
                float(raw_grade)
                grade_rank = 3
            except ValueError:
                if raw_grade.upper() not in ["P", ""]:
                    grade_rank = 2
                else:
                    grade_rank = 1

            key = (normalize_code(course_code), term)
            new_entry = {
                "course_code": course_code,
                "term": term,
                "credits_earned": credits_earned,
                "status": c_status,
            }

            if key in extracted:
                existing = extracted[key]
                if grade_rank > existing["grade_rank"] or (
                    grade_rank == existing["grade_rank"]
                    and credits_earned > existing["credits_earned"]
                ):
                    extracted[key] = {
                        "course": new_entry,
                        "grade_rank": grade_rank,
                        "credits_earned": credits_earned,
                    }
            else:
                extracted[key] = {
                    "course": new_entry,
                    "grade_rank": grade_rank,
                    "credits_earned": credits_earned,
                }

    parsed_courses = [v["course"] for v in extracted.values()]

    if student_id not in db:
        db[student_id] = {"history": [], "plan": []}

    db[student_id]["history"] = parsed_courses
    return {"status": "success", "past_courses_imported": len(parsed_courses)}


@app.put("/api/v1/students/{student_id}/history")
def update_history(student_id: str, payload: HistoryUpdate):
    if student_id not in db:
        raise HTTPException(status_code=404, detail="Student not found")
    db[student_id]["history"] = [c.dict() for c in payload.history]
    return {"status": "success", "message": "Academic history updated successfully"}


@app.delete("/api/v1/students/{student_id}/history")
def delete_history(student_id: str):
    if student_id not in db:
        raise HTTPException(status_code=404, detail="Student not found")
    db[student_id]["history"] = []
    return {"status": "success", "message": "Academic history cleared"}


@app.post("/api/v1/students/{student_id}/plan")
def create_plan(student_id: str, payload: PlanUpdate):
    if student_id not in db:
        raise HTTPException(status_code=404, detail="Student not found")
    db[student_id]["plan"] = [c.dict() for c in payload.planned_courses]
    return {"status": "success", "planned_courses_saved": len(payload.planned_courses)}


@app.put("/api/v1/students/{student_id}/plan")
def update_plan(student_id: str, payload: PlanUpdate):
    if student_id not in db:
        raise HTTPException(status_code=404, detail="Student not found")
    db[student_id]["plan"] = [c.dict() for c in payload.planned_courses]
    return {"status": "success", "message": "Plan updated successfully"}


@app.delete("/api/v1/students/{student_id}/plan")
def delete_plan(student_id: str):
    if student_id not in db:
        raise HTTPException(status_code=404, detail="Student not found")
    db[student_id]["plan"] = []
    return {"status": "success", "message": "Plan cleared"}


@app.get("/api/v1/students/{student_id}/profile")
def get_profile(student_id: str):
    if student_id not in db:
        raise HTTPException(status_code=404, detail="Student not found")
    return {
        "student_id": student_id,
        "history": db[student_id]["history"],
        "plan": db[student_id]["plan"],
    }


# --- NEW: Audit Engine Endpoint ---
@app.get("/api/v1/students/{student_id}/audit-report")
def get_audit_report(student_id: str, strict: bool = False):
    """Audits a student's history and plan against graduation requirements."""
    if student_id not in db:
        raise HTTPException(status_code=404, detail="Student not found")

    student_data = db[student_id]
    history = sorted(
        student_data.get("history", []), key=lambda c: term_sort_key(c["term"])
    )
    planned = student_data.get("plan", [])

    earned_credits_map = {}
    completed_terms = {}

    # 1. Process History (Chronological)
    for hc in history:
        code = normalize_code(hc["course_code"])
        if hc["status"] == "Completed":
            # A later pass overrides a failure; record earned credits and the earliest passed term # noqa: E501.
            earned_credits_map[code] = hc.get("credits_earned", 0)
            if code not in completed_terms:
                completed_terms[code] = hc["term"]
        else:
            # If not completed, counts as 0 (unless already passed previously)
            if code not in earned_credits_map:
                earned_credits_map[code] = 0

    total_earned = sum(earned_credits_map.values())
    total_planned = 0

    timeline_errors = {}
    cross_list_violations = []

    # 2. Process Plan
    for pc in planned:
        code = normalize_code(pc["course_code"])
        p_term = pc["term"]
        formatted_code = format_code_for_msg(code)

        cat_info = courses_db.get(code)
        if cat_info:
            total_planned += cat_info.get("credits", 0)

            # Prerequisite Checking
            prereq_raw = cat_info.get("prerequisites", "")
            prereq_codes = extract_course_codes(prereq_raw)

            for pq in prereq_codes:
                # Must be completed in a strictly earlier term
                if pq not in completed_terms or term_sort_key(
                    completed_terms[pq]
                ) >= term_sort_key(p_term):
                    if p_term not in timeline_errors:
                        timeline_errors[p_term] = []
                    timeline_errors[p_term].append(
                        {
                            "course_code": formatted_code,
                            "type": "MISSING_PREREQUISITE",
                            "message": f"Missing prerequisite: {format_code_for_msg(pq)}" # noqa: E501,
                        }
                    )

            # Cross-Listing Checking
            cross_raw = cat_info.get("cross_listed", "")
            cross_codes = extract_course_codes(cross_raw)
            for cross in cross_codes:
                if cross in completed_terms:
                    cross_list_violations.append(
                        {
                            "course_code": formatted_code,
                            "type": "CROSS_LIST_CONFLICT",
                            "message": f"Cross-listed with completed course {format_code_for_msg(cross)}" # noqa: E501,
                        }
                    )

    # 3. Compile Timeline Chronologically
    timeline_validation = [
        {"term": t, "errors": timeline_errors[t]}
        for t in sorted(timeline_errors.keys(), key=term_sort_key)
    ]

    # 4. Status Determination
    status_val = "ok"
    if timeline_validation or cross_list_violations:
        status_val = "failed" if strict else "warning"

    total_remaining = max(0, 120 - total_earned - total_planned)

    return {
        "student_id": student_id,
        "status": status_val,
        "timeline_validation": timeline_validation,
        "cross_list_violations": cross_list_violations,
        "credit_summary": {
            "total_earned": total_earned,
            "total_planned": total_planned,
            "total_remaining_for_graduation": total_remaining,
        },
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
