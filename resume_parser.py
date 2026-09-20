#!/usr/bin/env python3
"""Resume profile parser using pypdf.

Extracts text from a text-based PDF and derives a candidate PROFILE so the
matcher stays in sync with the resume. Run with the project venv:
    .venv/bin/python resume_parser.py [path-to-resume.pdf]
"""

import re
import sys

from pypdf import PdfReader

# Skills we know how to match, mapped to weights. If a skill appears in the
# resume text, it goes into the profile with this weight.
KNOWN_SKILLS = {
    "java": 3, "spring boot": 3, "spring": 2, "microservices": 3,
    "rest": 2, "grpc": 2, "distributed systems": 3, "event-driven": 2,
    "mongodb": 2, "docker": 2, "kubernetes": 2, "ci/cd": 1,
    "jenkins": 1, "backend": 3, "python": 1, "api": 1,
    "javascript": 1, "typescript": 1, "angular": 1, "junit": 1,
    "playwright": 1, "github actions": 1,
}

INDIA_LOCS = ["india", "bangalore", "bengaluru", "hyderabad", "pune",
              "chennai", "mumbai", "delhi", "gurgaon", "noida"]


def extract_text(pdf_path):
    """Return plain text from a text-based PDF."""
    reader = PdfReader(pdf_path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def parse_contact(text):
    """Extract contact details for building application kits."""
    flat = re.sub(r"\s+", " ", text)
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", flat)
    phone = re.search(r"\+?\d[\d\s-]{8,}\d", flat)
    links = re.findall(r"(?:linkedin\.com/\S+|github\.com/\S+)", flat)
    return {
        "name": _parse_name(text),
        "email": email.group() if email else "",
        "phone": phone.group() if phone else "",
        "links": links,
    }


def _parse_name(text):
    """Derive the candidate name from the first line of the raw resume text.

    The header runs the name straight into the phone/email/links (tab- or
    space-separated), so cut at the first contact marker: a digit, '@', '|',
    or 'linkedin'/'github'. Whatever remains is the name.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Split the raw first line on tabs first (the resume's real separator),
        # then trim at the first token that looks like contact info.
        head = re.split(r"\t| {2,}", line)[0]
        head = re.split(r"[|@]|\+?\d|linkedin|github", head, flags=re.I)[0]
        name = head.strip(" -|")
        if name:
            return name[:60]
        return "Candidate"
    return "Candidate"


def parse_profile(text):
    """Derive a matcher profile from resume text."""
    low = re.sub(r"\s+", " ", text.lower())  # normalize tabs/newlines to spaces

    skills = {s: w for s, w in KNOWN_SKILLS.items()
              if re.search(r"\b" + re.escape(s) + r"\b", low)}

    # Years of experience: look for "N+ years" / "N years".
    yoe = 0
    m = re.search(r"(\d+)\s*\+?\s*years", low)
    if m:
        yoe = int(m.group(1))

    # Titles: seed with common backend titles present in the text.
    title_seeds = ["backend software engineer", "software engineer",
                   "backend engineer"]
    titles = [t for t in title_seeds if re.search(r"\b" + re.escape(t) + r"\b", low)] or ["software engineer"]

    # Locations: any known India location mentioned.
    locations = sorted({l for l in INDIA_LOCS if l in low}) or ["india"]

    avoid = ["intern", "principal", "director", "vp", "manager", "sales",
             "marketing", "content", "recruiter", "designer"]

    return {
        "titles": titles,
        "years_experience": yoe,
        "locations": locations,
        "skills": skills,
        "avoid": avoid,
    }


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else \
        "/Users/I527878/Documents/Arpitha/Personal/ArpithaMJ.pdf"
    txt = extract_text(path)
    print(f"Extracted {len(txt)} chars of text.\n")
    print("SAMPLE:", txt[:400], "\n")
    prof = parse_profile(txt)
    import json
    print("DERIVED PROFILE:")
    print(json.dumps(prof, indent=2))
