"""End-to-end tests for the LLP intake tool.

Runs without Docker/Postgres/Tesseract: SQLite + a temp upload dir + a stubbed
OCR extractor. Drives the real ASGI app over HTTP.

Run:  python backend/tests/test_e2e.py
Exits non-zero if any check fails.
"""
import asyncio
import io
import os
import sys
import tempfile

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

tmp = tempfile.mkdtemp(prefix="llp_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tmp, 'test.db')}"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "secret123"
os.environ["BASE_URL"] = "http://testserver"
os.environ["MAX_UPLOAD_MB"] = "15"
from cryptography.fernet import Fernet  # noqa: E402

os.environ["FILE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import app.config as cfgmod  # noqa: E402

cfgmod.settings.upload_dir = os.path.join(tmp, "uploads")
import app.storage as storage  # noqa: E402

storage.settings.upload_dir = cfgmod.settings.upload_dir

# --- unit: parsers -----------------------------------------------------------
from app.ocr.parsers import (  # noqa: E402
    parse_aadhaar, parse_bank_statement, parse_pan, parse_utility_bill, verhoeff_valid,
)

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


print("== parsers ==")
pan = parse_pan("Name\nRAHUL KUMAR SHARMA\nFather's Name\nSURESH SHARMA\n15/08/1990\nABCDE1234F")
check("pan number", pan.get("pan_number") == "ABCDE1234F")
check("pan first/last split", pan.get("first_name") == "RAHUL" and pan.get("last_name") == "KUMAR SHARMA")
check("pan father split", pan.get("father_first_name") == "SURESH")
check("pan dob", pan.get("dob") == "15/08/1990")
aad = parse_aadhaar("Address:\n12 MG Road, Banjara Hills,\nHyderabad, Telangana - 500034\n2345 6789 0123")
check("aadhaar present_address", "500034" in (aad.get("present_address") or ""))
bank = parse_bank_statement("Address: 45 Jubilee Hills,\nHyderabad, Telangana 500033")
check("bank permanent_address", "500033" in (bank.get("permanent_address") or ""))
util = parse_utility_bill("Consumer Name: RAHUL SHARMA\nService Address: Plot 88, Gachibowli,\nHyderabad 500032")
check("utility office_address", "500032" in (util.get("office_address") or ""))
check("utility name", util.get("utility_name") == "RAHUL SHARMA")

# --- stub OCR for the web flow ----------------------------------------------
import app.routers.public as public  # noqa: E402


def fake_extract(extractor_name, data, content_type):
    canon = {
        "pan": {"first_name": "RAHUL", "last_name": "SHARMA", "father_first_name": "SURESH",
                "father_last_name": "SHARMA", "pan_number": "ABCDE1234F", "dob": "15/08/1990"},
        "aadhaar": {"present_address": "12 MG Road, Hyderabad 500034"},
        "bank_statement": {"permanent_address": "45 Jubilee Hills, Hyderabad 500033"},
        "utility_bill": {"office_address": "Plot 88, Gachibowli 500032", "utility_name": "RAHUL SHARMA"},
    }
    return ("raw ocr text", canon.get(extractor_name, {}))


public.extract = fake_extract

import httpx  # noqa: E402
from app.config_loader import get_form_config  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import SubmissionLink  # noqa: E402

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)


def make_client():
    ac = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

    class Sync:
        def get(self, *a, **k):
            return loop.run_until_complete(ac.get(*a, **k))

        def post(self, *a, **k):
            return loop.run_until_complete(ac.post(*a, **k))

    return Sync()


client = make_client()
CFG = get_form_config()
db = SessionLocal()
JPG = (b"\xff\xd8\xff\xe0jpeg", "image/jpeg")


def sample_value(field):
    t = field.get("type")
    if field.get("numeric"):
        # lakh-style grouping, as a client would actually type it
        return "1,00,000"
    if t == "email":
        return "person@example.com"
    if t == "tel":
        # leading zero: the export must not turn this into a number
        return "09998887777"
    if t == "select":
        # skip a leading "— Select —" placeholder: it is blank, so a required
        # select would fail validation
        return next(o for o in field["options"] if o)
    return "Sample " + field["key"]


def valid_data(skip=()):
    data = {}
    for section in CFG["sections"]:
        for field in section["fields"]:
            if field["name"] in skip:
                continue
            if field.get("required"):
                data[field["name"]] = sample_value(field)
    return data


def required_files():
    files = {}
    for section in CFG["sections"]:
        for up in section["uploads"]:
            if up.get("required"):
                files[up["name"]] = (up["key"] + ".jpg", JPG[0], JPG[1])
    return files


def new_link(label):
    client.post("/admin/links", data={"label": label}, follow_redirects=True)
    db.expire_all()
    return db.query(SubmissionLink).filter(SubmissionLink.label == label).first()


print("== auth + link ==")
check("dashboard needs auth", client.get("/admin", follow_redirects=False).status_code == 303)
check("bad login 401", client.post("/admin/login", data={"username": "admin", "password": "x"},
                                    follow_redirects=False).status_code == 401)
client.post("/admin/login", data={"username": "admin", "password": "secret123"}, follow_redirects=False)
link = new_link("LLP Client")
token = link.token
check("link created", token is not None)

print("== form renders ==")
body = client.get(f"/f/{token}").text
for needle in ["Proposed LLP", "First Designated Partner", "Second Designated Partner",
               "Registered Office", 'data-slot="partner1__pan"', 'data-extract-url="/f/']:
    check(f"form contains {needle}", needle in body)

print("== live extract ==")
r = client.post(f"/f/{token}/extract", data={"slot": "partner1__pan"},
                files={"file": ("pan.jpg", *JPG)})
j = r.json()
check("extract fills first_name", j["fields"].get("partner1__first_name") == "RAHUL")
check("extract no cross-partner leak", not any(k.startswith("partner2__") for k in j["fields"]))
check("unknown slot 400", client.post(f"/f/{token}/extract", data={"slot": "no__pe"},
                                       files={"file": ("x.jpg", *JPG)}).status_code == 400)
check("bad type 415", client.post(f"/f/{token}/extract", data={"slot": "partner1__pan"},
                                   files={"file": ("x.html", b"<h1>", "text/html")}).status_code == 415)

print("== full submission ==")
r = client.post(f"/f/{token}", data=valid_data(), files=required_files())
check("submit ok", r.status_code == 200 and "Thank you" in r.text)
db.expire_all()
sub = db.query(SubmissionLink).filter(SubmissionLink.token == token).first().submission
check("persisted", sub is not None and sub.form_data.get("partner1__first_name"))
check("every required upload stored", len(sub.uploads) == len(required_files()))
check("upload extracted", {u.field_key: u for u in sub.uploads}["partner1__pan"].extracted_data.get("pan_number") == "ABCDE1234F")
check("relink locked (410)", client.get(f"/f/{token}").status_code == 410)

print("== validation ==")
l2 = new_link("L2")
check("missing field 400", client.post(f"/f/{l2.token}", data=valid_data(skip=("partner1__first_name",)),
                                        files=required_files()).status_code == 400)
l3 = new_link("L3")
d = valid_data(skip=("partner1__permanent_address", "partner1__place_of_birth"))
d["partner1__din"] = "DIN123"
check("DIN makes fields optional", client.post(f"/f/{l3.token}", data=d, files=required_files()).status_code == 200)
l4 = new_link("L4")
check("no DIN keeps them required", client.post(f"/f/{l4.token}",
      data=valid_data(skip=("partner1__permanent_address",)), files=required_files()).status_code == 400)

print("== master workbook ==")
from openpyxl import load_workbook  # noqa: E402
from app.exporting import submission_workbook  # noqa: E402

sub_link = db.query(SubmissionLink).filter(SubmissionLink.token == token).first()
xlsx = submission_workbook(CFG, sub_link, sub_link.submission,
                           {u.field_key: u for u in sub_link.submission.uploads})
wbk = load_workbook(io.BytesIO(xlsx))
check("three sheets", wbk.sheetnames == ["Details", "LLP agreement", "ODI sheet"])
check("no Notes column", wbk["Details"]["C1"].value is None)

det = wbk["Details"]
labels = {det.cell(row=r, column=1).value: r for r in range(1, det.max_row + 1)}
cap_row = labels["First Partner — Capital Contribution"]
# a text cell counts as 0 inside SUM, so the capital total and every
# percentage on the agreement sheet would come out wrong
check("capital written as a number", isinstance(det.cell(row=cap_row, column=2).value, (int, float)))
phone_row = labels["LLP Contact Number (different from the ones above)"]
check("leading zero kept on phone", str(det.cell(row=phone_row, column=2).value).startswith("0"))

agr = wbk["LLP agreement"]
formulas = [c.value for r in agr.iter_rows() for c in r if isinstance(c.value, str) and c.value.startswith("=")]
check("agreement pulls from Details", any("Details!B" in f for f in formulas))
check("agreement totals the capital", any(f.startswith("=SUM(") for f in formulas))
check("blank source renders blank, not 0",
      all('=IF(Details!B' in f for f in formulas if "Details!B" in f))
# every reference must land on a row that exists, or the sheet shows #REF!
import re as _re
refs = [int(m) for f in formulas for m in _re.findall(r"Details!B(\d+)", f)]
check("no reference past the end of Details", refs and max(refs) <= det.max_row)
check("ODI keeps the team's blanks empty",
      all(wbk["ODI sheet"].cell(row=r, column=2).value is None
          for r in range(1, wbk["ODI sheet"].max_row + 1)
          if wbk["ODI sheet"].cell(row=r, column=1).value in ("SWIFT", "IBAN", "Foreign entity name")))

print("== security headers ==")
r = client.get("/admin")
check("CSP", "content-security-policy" in {k.lower() for k in r.headers})
check("X-Frame-Options", r.headers.get("x-frame-options") == "DENY")
check("nosniff", r.headers.get("x-content-type-options") == "nosniff")

print(f"\n==== {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
