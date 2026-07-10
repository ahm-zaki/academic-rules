import pytest
from fastapi.testclient import TestClient

# Import your app and in-memory databases/helpers
from main import (
    app,
    db,
    courses_db,
    normalize_code,
    format_code_for_msg,
    term_sort_key,
    extract_course_codes,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_databases():
    """Clears the in-memory databases before each test runs to ensure isolation."""
    db.clear()
    courses_db.clear()


# --- 1. Test Helper Functions ---
def test_helpers():
    assert normalize_code("COSC 3506") == "COSC3506"
    assert normalize_code("cosc-3506") == "COSC3506"

    assert format_code_for_msg("COSC3506") == "COSC-3506"
    assert format_code_for_msg("NONSENSE") == "NONSENSE"

    assert term_sort_key("24W") == (24, 0)
    assert term_sort_key("24F") == (24, 3)
    assert term_sort_key("INVALID") == (99, 99)

    codes = extract_course_codes("Prereqs: COSC 1000 and MATH-2000.")
    assert codes == ["COSC1000", "MATH2000"]


# --- 2. Test Catalog Endpoints ---
def test_import_catalog_success():
    html_content = """
    <table>
        <tbody>
            <tr>
                <td>COSC 1000</td>
                <td>Intro to CS</td>
                <td>3</td>
                <td></td>
                <td></td>
            </tr>
        </tbody>
    </table>
    """
    response = client.post(
        "/api/v1/admin/catalog/import",
        files={"file": ("catalog.html", html_content, "text/html")},
    )
    assert response.status_code == 200
    assert "Successfully imported 1" in response.json()["message"]
    assert "COSC1000" in courses_db


def test_import_catalog_no_table():
    response = client.post(
        "/api/v1/admin/catalog/import",
        files={
            "file": ("bad.html", "<html><body>No data here</body></html>", "text/html")
        },
    )
    assert response.status_code == 400


def test_get_course():
    courses_db["COSC1000"] = {"course_code": "COSC 1000", "credits": 3}

    response = client.get("/api/v1/catalog/courses/COSC 1000")
    assert response.status_code == 200
    assert response.json()["credits"] == 3

    assert client.get("/api/v1/catalog/courses/INVALID").status_code == 404


# --- 3. Test Transcript Parsing ---
def test_import_history():
    html_content = """
    <table>
        <thead>
            <tr><th>Status</th><th>Course</th><th>Title</th><th>Grade</th><th>Term</th><th>Credits</th></tr>
        </thead>
        <tbody>
            <tr>
                <td>Completed</td>
                <td><a href="#">COSC 1000</a></td>
                <td>Intro to CS</td>
                <td>A</td>
                <td>23F</td>
                <td>3</td>
            </tr>
        </tbody>
    </table>
    """
    response = client.post(
        "/api/v1/students/S123/history/import",
        files={"file": ("transcript.html", html_content, "text/html")},
    )
    assert response.status_code == 201
    assert "S123" in db
    assert len(db["S123"]["history"]) == 1
    assert db["S123"]["history"][0]["course_code"] == "COSC 1000"


# --- 4. Test Student Profile CRUD Endpoints ---
def test_student_profile_crud():
    # Seed the DB
    db["S999"] = {"history": [], "plan": []}

    # Test GET Profile
    assert client.get("/api/v1/students/S999/profile").status_code == 200
    assert client.get("/api/v1/students/NOBODY/profile").status_code == 404

    # Test PUT History
    h_payload = {
        "history": [
            {
                "course_code": "MATH1000",
                "term": "23F",
                "credits_earned": 3,
                "status": "Completed",
            }
        ]
    }
    assert (
        client.put("/api/v1/students/S999/history", json=h_payload).status_code == 200
    )
    assert (
        client.put("/api/v1/students/NOBODY/history", json=h_payload).status_code == 404
    )

    # Test DELETE History
    assert client.delete("/api/v1/students/S999/history").status_code == 200
    assert client.delete("/api/v1/students/NOBODY/history").status_code == 404

    # Test POST/PUT/DELETE Plan
    p_payload = {"planned_courses": [{"course_code": "CS100", "term": "24W"}]}
    assert client.post("/api/v1/students/S999/plan", json=p_payload).status_code == 200
    assert client.put("/api/v1/students/S999/plan", json=p_payload).status_code == 200
    assert client.delete("/api/v1/students/S999/plan").status_code == 200

    assert (
        client.post("/api/v1/students/NOBODY/plan", json=p_payload).status_code == 404
    )
    assert client.put("/api/v1/students/NOBODY/plan", json=p_payload).status_code == 404
    assert client.delete("/api/v1/students/NOBODY/plan").status_code == 404


# --- 5. Test Audit Engine Logic ---
def test_audit_report():
    # Setup mock course catalog with prereqs and cross-lists
    courses_db["COSC2000"] = {
        "course_code": "COSC 2000",
        "credits": 3,
        "prerequisites": "COSC 1000",
        "cross_listed": "MATH 2000",
    }

    # Setup mock student data
    db["S_AUDIT"] = {
        "history": [
            # Passed the prerequisite
            {
                "course_code": "COSC 1000",
                "term": "23F",
                "credits_earned": 3,
                "status": "Completed",
            },
            # Passed the cross-listed equivalent
            {
                "course_code": "MATH 2000",
                "term": "23F",
                "credits_earned": 3,
                "status": "Completed",
            },
        ],
        "plan": [
            # Trying to take COSC 2000 before the prereq was completed (23W is before 23F)
            {"course_code": "COSC 2000", "term": "23W"},
            # Taking it at a valid time, but triggers cross-list conflict with MATH 2000
            {"course_code": "COSC 2000", "term": "24F"},
        ],
    }

    response = client.get("/api/v1/students/S_AUDIT/audit-report")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "warning"  # Because not strict
    assert data["credit_summary"]["total_earned"] == 6
    assert len(data["cross_list_violations"]) > 0
    assert len(data["timeline_validation"]) > 0

    # Strict mode test
    response_strict = client.get("/api/v1/students/S_AUDIT/audit-report?strict=true")
    assert response_strict.json()["status"] == "failed"

    # Missing student test
    assert client.get("/api/v1/students/NOBODY/audit-report").status_code == 404
