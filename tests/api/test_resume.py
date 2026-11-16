import hashlib

import pytest

from workwise.core.security import auth_settings, create_access_token
from workwise.db.models import Company, Job, JobApplication, User

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@pytest.fixture(autouse=True)
def resume_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_settings, "resume_dir", str(tmp_path / "resumes"))
    monkeypatch.setattr(auth_settings, "resume_max_bytes", 5000)
    return tmp_path / "resumes"


@pytest.fixture
def people(db_session):
    users = {}
    for name in ("candidate", "employer", "outsider"):
        user = User(username=name, password_hash="fixture-not-for-login", role="member")
        db_session.add(user)
        db_session.flush()
        users[name] = user
    company = Company(name="Resume Co", owner_id=users["employer"].id)
    db_session.add(company)
    db_session.flush()
    job = Job(title="Engineer", company_id=company.id, location="Remote")
    db_session.add(job)
    db_session.flush()
    application = JobApplication(job_id=job.id, applicant_id=users["candidate"].id, status="applied")
    db_session.add(application)
    db_session.commit()
    return {name: {"Authorization": "Bearer " + create_access_token(user.id)} for name, user in users.items()}, application.id


def upload(client, headers, content=PDF, name="cv.pdf", kind="application/pdf"):
    return client.put("/v1/resume", headers=headers, files={"file": (name, content, kind)})


def test_upload_download_replace_and_delete(anonymous_client, people, resume_dir):
    headers, _ = people
    me = headers["candidate"]
    created = upload(anonymous_client, me)
    assert created.status_code == 200
    assert created.json()["size_bytes"] == len(PDF) and created.json()["sha256"] == hashlib.sha256(PDF).hexdigest()

    download = anonymous_client.get("/v1/resume", headers=me)
    assert download.status_code == 200 and download.content == PDF
    assert download.headers["content-type"] == "application/pdf"
    assert "attachment" in download.headers["content-disposition"] and "resume.pdf" in download.headers["content-disposition"]

    newer = PDF + b"% second version\n%%EOF\n"
    assert upload(anonymous_client, me, newer).status_code == 200
    assert [path.suffix for path in resume_dir.iterdir()] == [".pdf"]  # old file removed, no temp leftovers
    assert anonymous_client.get("/v1/resume", headers=me).content == newer

    assert anonymous_client.delete("/v1/resume", headers=me).status_code == 204
    assert list(resume_dir.iterdir()) == []
    assert anonymous_client.get("/v1/resume", headers=me).status_code == 404
    assert anonymous_client.delete("/v1/resume", headers=me).status_code == 404


@pytest.mark.parametrize("content,status", [
    (b"", 422),
    (b"GIF89a not a pdf at all\n%%EOF", 415),
    (b"%PDF-1.4 truncated, no end marker", 415),
    (PDF + b"0" * 6000, 413),
])
def test_bad_files_are_rejected_and_leave_nothing_behind(anonymous_client, people, resume_dir, content, status):
    assert upload(anonymous_client, people[0]["candidate"], content).status_code == status
    assert not resume_dir.exists() or list(resume_dir.iterdir()) == []


def test_resume_needs_a_login(anonymous_client, people):
    assert upload(anonymous_client, {}).status_code == 401
    assert anonymous_client.get("/v1/resume").status_code == 401


def test_only_applicant_company_owner_and_admins_read_an_applicants_resume(anonymous_client, people):
    headers, application_id = people
    upload(anonymous_client, headers["candidate"])
    path = f"/v1/applications/{application_id}/resume"
    for who in ("candidate", "employer"):
        response = anonymous_client.get(path, headers=headers[who])
        assert response.status_code == 200 and response.content == PDF
    assert anonymous_client.get(path, headers=headers["outsider"]).status_code == 404
    assert anonymous_client.get(path).status_code == 401
    anonymous_client.delete("/v1/resume", headers=headers["candidate"])
    assert anonymous_client.get(path, headers=headers["employer"]).status_code == 404
