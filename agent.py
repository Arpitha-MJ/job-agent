#!/usr/bin/env python3
"""Job-matching agent v1 (stdlib-only, human-in-the-loop).

Stage 1: fetch live jobs from a company's Workday CXS endpoint.
Stage 2: score each job against a hardcoded candidate profile.
Stage 3: print a ranked shortlist for the human to review + apply.

No auto-submit. No paid API. No pip installs.
"""

import html
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import lru_cache

from resume_parser import extract_text, parse_profile, parse_contact

# --- Candidate profile: auto-parsed from the resume PDF ----------------------
RESUME_PATH = "/Users/I527878/Documents/Arpitha/Personal/ArpithaMJ.pdf"

# Populated in main() so --help works without needing the PDF / venv.
PROFILE = {}
CONTACT = {}

# Manual overrides: take precedence over parsed values. The resume summary
# still reads "3+ years" but the candidate actually has 4.
YEARS_EXPERIENCE_OVERRIDE = 4

# Only keep jobs posted within this many hours. Sources differ in date
# precision: Lever/Greenhouse give exact timestamps; Workday gives relative
# text ("Posted 2 Days Ago") that we approximate at day granularity. Jobs with
# no parseable date are KEPT (we can't prove they're stale).
MAX_AGE_HOURS = 72

# --- Source config: each source has a "type" that selects the fetch logic ----
SOURCES = {
    "salesforce": {
        "type": "workday",
        "url": "https://salesforce.wd12.myworkdayjobs.com/wday/cxs/salesforce/External_Career_Site/jobs",
        "detail_base": "https://salesforce.wd12.myworkdayjobs.com/wday/cxs/salesforce/External_Career_Site",
        "job_url_prefix": "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site",
    },
    "visa": {
        "type": "workday",
        "url": "https://visa.wd5.myworkdayjobs.com/wday/cxs/visa/Visa/jobs",
        "detail_base": "https://visa.wd5.myworkdayjobs.com/wday/cxs/visa/Visa",
        "job_url_prefix": "https://visa.wd5.myworkdayjobs.com/Visa",
    },
    # WF India tech is Hyderabad-based; kept behind the Bangalore-only filter
    # so it auto-surfaces if/when they post Bangalore roles.
    "wellsfargo": {
        "type": "workday",
        "url": "https://wd1.myworkdaysite.com/wday/cxs/wf/WellsFargoJobs/jobs",
        "detail_base": "https://wd1.myworkdaysite.com/wday/cxs/wf/WellsFargoJobs",
        "job_url_prefix": "https://wd1.myworkdaysite.com/en-US/recruiting/wf/WellsFargoJobs",
        # Fetch India roles directly (166 of them) so the Bengaluru-only filter
        # has the ~21 Bengaluru postings to narrow to. Without this facet the
        # US-heavy catalog buries them past the prefilter sample.
        "applied_facets": {"locationCountry": ["c4f78be1a8f14da0ab49ce1162348a5e"]},
    },
    # JPMorgan runs Oracle Cloud recruiting (ORC). 7390 total reqs, so we pass
    # keyword=java in the finder to narrow to ~800 before the Bangalore filter.
    "jpmorgan": {
        "type": "oracle",
        "host": "https://jpmc.fa.oraclecloud.com",
        "site_number": "CX_1001",
        "keyword": "java",
        "job_url_prefix": "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/",
    },
    # American Express: careers.americanexpress.com is a proxy that 302s to the
    # Oracle pod egug.fa.us2.oraclecloud.com. The API only answers on the pod
    # host; apply links use the public careers domain candidates actually see.
    "amex": {
        "type": "oracle",
        "host": "https://egug.fa.us2.oraclecloud.com",
        "site_number": "CX_1",
        "keyword": "java",
        "job_url_prefix": "https://careers.americanexpress.com/en/sites/CX_1/job/",
    },
    # Major Workday-hosted companies with Bengaluru/India engineering roles.
    # All verified reachable with java results. Datacenter (wd1/wd5) is per-tenant.
    "nvidia": {
        "type": "workday",
        "url": "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs",
        "detail_base": "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite",
        "job_url_prefix": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
    },
    "adobe": {
        "type": "workday",
        "url": "https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/external_experienced/jobs",
        "detail_base": "https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/external_experienced",
        "job_url_prefix": "https://adobe.wd5.myworkdayjobs.com/external_experienced",
    },
    "paypal": {
        "type": "workday",
        "url": "https://paypal.wd1.myworkdayjobs.com/wday/cxs/paypal/jobs/jobs",
        "detail_base": "https://paypal.wd1.myworkdayjobs.com/wday/cxs/paypal/jobs",
        "job_url_prefix": "https://paypal.wd1.myworkdayjobs.com/jobs",
    },
    "hp": {
        "type": "workday",
        "url": "https://hp.wd5.myworkdayjobs.com/wday/cxs/hp/ExternalCareerSite/jobs",
        "detail_base": "https://hp.wd5.myworkdayjobs.com/wday/cxs/hp/ExternalCareerSite",
        "job_url_prefix": "https://hp.wd5.myworkdayjobs.com/ExternalCareerSite",
    },
    "broadcom": {
        "type": "workday",
        "url": "https://broadcom.wd1.myworkdayjobs.com/wday/cxs/broadcom/External_Career/jobs",
        "detail_base": "https://broadcom.wd1.myworkdayjobs.com/wday/cxs/broadcom/External_Career",
        "job_url_prefix": "https://broadcom.wd1.myworkdayjobs.com/External_Career",
    },
    "workday": {
        "type": "workday",
        "url": "https://workday.wd5.myworkdayjobs.com/wday/cxs/workday/Workday/jobs",
        "detail_base": "https://workday.wd5.myworkdayjobs.com/wday/cxs/workday/Workday",
        "job_url_prefix": "https://workday.wd5.myworkdayjobs.com/Workday",
    },
    "mastercard": {
        "type": "workday",
        "url": "https://mastercard.wd1.myworkdayjobs.com/wday/cxs/mastercard/CorporateCareers/jobs",
        "detail_base": "https://mastercard.wd1.myworkdayjobs.com/wday/cxs/mastercard/CorporateCareers",
        "job_url_prefix": "https://mastercard.wd1.myworkdayjobs.com/CorporateCareers",
    },
    "morganstanley": {
        "type": "workday",
        "url": "https://ms.wd5.myworkdayjobs.com/wday/cxs/ms/External/jobs",
        "detail_base": "https://ms.wd5.myworkdayjobs.com/wday/cxs/ms/External",
        "job_url_prefix": "https://ms.wd5.myworkdayjobs.com/External",
    },
    "citi": {
        "type": "workday",
        "url": "https://citi.wd5.myworkdayjobs.com/wday/cxs/citi/2/jobs",
        "detail_base": "https://citi.wd5.myworkdayjobs.com/wday/cxs/citi/2",
        "job_url_prefix": "https://citi.wd5.myworkdayjobs.com/2",
    },
    "target": {
        "type": "workday",
        "url": "https://target.wd5.myworkdayjobs.com/wday/cxs/target/targetcareers/jobs",
        "detail_base": "https://target.wd5.myworkdayjobs.com/wday/cxs/target/targetcareers",
        "job_url_prefix": "https://target.wd5.myworkdayjobs.com/targetcareers",
        # India (Bangalore) location facet — pulls the 13 India roles directly so
        # the Bengaluru-only filter isn't starved by Target's US-heavy catalog.
        "applied_facets": {"locations": ["daccab9f1d2501dacfb791d63b57fc1f"]},
    },
    "autodesk": {
        "type": "workday",
        "url": "https://autodesk.wd1.myworkdayjobs.com/wday/cxs/autodesk/Ext/jobs",
        "detail_base": "https://autodesk.wd1.myworkdayjobs.com/wday/cxs/autodesk/Ext",
        "job_url_prefix": "https://autodesk.wd1.myworkdayjobs.com/Ext",
    },
    # Nasdaq: India roles skew Mumbai but include Bengaluru backend/data eng.
    # India facet key here is "Location_Country" (not "locationCountry").
    "nasdaq": {
        "type": "workday",
        "url": "https://nasdaq.wd1.myworkdayjobs.com/wday/cxs/nasdaq/Global_External_Site/jobs",
        "detail_base": "https://nasdaq.wd1.myworkdayjobs.com/wday/cxs/nasdaq/Global_External_Site",
        "job_url_prefix": "https://nasdaq.wd1.myworkdayjobs.com/Global_External_Site",
        "applied_facets": {"Location_Country": ["c4f78be1a8f14da0ab49ce1162348a5e"]},
    },
    "fidelity": {
        "type": "workday",
        "url": "https://wd1.myworkdaysite.com/wday/cxs/fmr/FidelityCareers/jobs",
        "detail_base": "https://wd1.myworkdaysite.com/wday/cxs/fmr/FidelityCareers",
        "job_url_prefix": "https://wd1.myworkdaysite.com/en-US/recruiting/fmr/FidelityCareers",
    },
    # Deutsche Bank: large Bengaluru (Velankani Tech Park) engineering center;
    # no India facet exposed, so we rely on the 5-page fetch + Bangalore filter.
    "deutschebank": {
        "type": "workday",
        "url": "https://db.wd3.myworkdayjobs.com/wday/cxs/db/DBWebsite/jobs",
        "detail_base": "https://db.wd3.myworkdayjobs.com/wday/cxs/db/DBWebsite",
        "job_url_prefix": "https://db.wd3.myworkdayjobs.com/DBWebsite",
    },
    # Warner Bros Discovery: Bengaluru (Embassy Tech Village) engineering center,
    # strong Java/Angular full-stack roles.
    "warnerbros": {
        "type": "workday",
        "url": "https://warnerbros.wd5.myworkdayjobs.com/wday/cxs/warnerbros/global/jobs",
        "detail_base": "https://warnerbros.wd5.myworkdayjobs.com/wday/cxs/warnerbros/global",
        "job_url_prefix": "https://warnerbros.wd5.myworkdayjobs.com/global",
    },
    "meesho": {
        "type": "lever",
        "url": "https://api.lever.co/v0/postings/meesho?mode=json",
        "job_url_prefix": "https://jobs.lever.co/meesho",
    },
    # Paytm: Lever-based. 198 postings, ~20 Bangalore/India engineering roles.
    "paytm": {
        "type": "lever",
        "url": "https://api.lever.co/v0/postings/paytm?mode=json",
        "job_url_prefix": "https://jobs.lever.co/paytm",
    },
    # Swiggy: SmartRecruiters-based. Bangalore HQ; confirmed 68 live roles including
    # backend SDE roles in Bengaluru via api.smartrecruiters.com.
    "swiggy": {
        "type": "smartrecruiters",
        "company": "Swiggy",
        "job_url_prefix": "https://careers.smartrecruiters.com/Swiggy",
    },
    # Freshworks: SmartRecruiters. Chennai HQ but strong Bengaluru engineering center;
    # confirmed 152 postings, ~9 Bangalore roles.
    "freshworks": {
        "type": "smartrecruiters",
        "company": "Freshworks",
        "job_url_prefix": "https://careers.smartrecruiters.com/Freshworks",
    },
    # PhonePe: SmartRecruiters slug PHONEPELIMITED. Bengaluru HQ; confirmed 28 postings,
    # 18 Bangalore roles (engineering + product).
    "phonepe": {
        "type": "smartrecruiters",
        "company": "PHONEPELIMITED",
        "job_url_prefix": "https://careers.smartrecruiters.com/PHONEPELIMITED",
    },
    # Razorpay: Greenhouse board. Bengaluru HQ; token verified from careers page.
    "razorpay": {
        "type": "greenhouse",
        "token": "razorpaysoftwareprivatelimited",
        "job_url_prefix": "https://job-boards.greenhouse.io/razorpaysoftwareprivatelimited",
    },
    # Atlassian: custom iCIMS-backed API. Bengaluru office (Embassy Tech Village);
    # confirmed 250 postings, 29 India/Bangalore roles.
    "atlassian": {
        "type": "atlassian",
        "url": "https://www.atlassian.com/endpoint/careers/listings",
    },
    # Palo Alto Networks: Bangalore Bagmane Tech Park office; 123 roles when filtered to
    # Bangalore. Mix of PS/consulting and real engineering (Staff SWE, Principal Backend,
    # Staff SRE, Staff AI Engineer). Location facets narrow the fetch server-side.
    "paloalto": {
        "type": "workday",
        "url": "https://paloaltonetworks.wd5.myworkdayjobs.com/wday/cxs/paloaltonetworks/panwexternalcareers/jobs",
        "detail_base": "https://paloaltonetworks.wd5.myworkdayjobs.com/wday/cxs/paloaltonetworks/panwexternalcareers",
        "job_url_prefix": "https://paloaltonetworks.wd5.myworkdayjobs.com/panwexternalcareers",
        # Bangalore Bagmane Tech Park + Bangalore plain — avoids fetching 1500 global jobs.
        "applied_facets": {"locations": ["a4e5dc5cfe170161250a9bd1b000e413", "0925b66a7d40107de95dc57546152c93"]},
    },
    # Barclays: Bengaluru (Maruthi Onyx / Tesco TSA office) engineering center;
    # 885 total roles, ~3 Bangalore Java roles in the search window.
    "barclays": {
        "type": "workday",
        "url": "https://barclays.wd3.myworkdayjobs.com/wday/cxs/barclays/External_Career_Site_Barclays/jobs",
        "detail_base": "https://barclays.wd3.myworkdayjobs.com/wday/cxs/barclays/External_Career_Site_Barclays",
        "job_url_prefix": "https://barclays.wd3.myworkdayjobs.com/External_Career_Site_Barclays",
    },
    # MongoDB: Greenhouse board; 18 India/Bengaluru roles including Application Engineer,
    # Director Solutions Architecture, and TSE roles.
    "mongodb": {
        "type": "greenhouse",
        "token": "mongodb",
        "job_url_prefix": "https://job-boards.greenhouse.io/mongodb",
    },
    # Samsara: Greenhouse board; 9 Bangalore engineering roles including Staff SWE
    # (Platform & Infrastructure), Security Engineer, Data Scientist. BLR1 office.
    "samsara": {
        "type": "greenhouse",
        "token": "samsara",
        "job_url_prefix": "https://job-boards.greenhouse.io/samsara",
    },
    # Zscaler: Greenhouse board; 35 engineering roles in Bangalore (Bangalore IND office).
    # Strong backend/cloud roles: Principal SDE, Manager SDE, Vulnerability Management Engineer.
    "zscaler": {
        "type": "greenhouse",
        "token": "zscaler",
        "job_url_prefix": "https://job-boards.greenhouse.io/zscaler",
    },
    # Commvault: Greenhouse board; 10 engineering roles in Bangalore/India including
    # Backend Senior Engineer, Senior Engineer C++/SQL, Data Protection Engineer, DevOps.
    "commvault": {
        "type": "greenhouse",
        "token": "commvault",
        "job_url_prefix": "https://job-boards.greenhouse.io/commvault",
    },
    # Twilio: Greenhouse board; 9 engineering roles in Remote - India including
    # Principal Engineer, Staff SWE, Senior Engineering Manager, Software Architect.
    "twilio": {
        "type": "greenhouse",
        "token": "twilio",
        "job_url_prefix": "https://job-boards.greenhouse.io/twilio",
    },
}

# --- Description cache -------------------------------------------------------
_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".job_cache.json")
_CACHE_TTL = 6 * 3600  # seconds

def _load_cache():
    try:
        with open(_CACHE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_cache(cache):
    tmp = _CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, _CACHE_PATH)

_DESC_CACHE = _load_cache()
_CACHE_LOCK = threading.Lock()

def _cache_get(key):
    with _CACHE_LOCK:
        entry = _DESC_CACHE.get(key)
    if entry and time.time() - entry["ts"] < _CACHE_TTL:
        return entry["text"]
    return None

def _cache_put(key, text):
    with _CACHE_LOCK:
        _DESC_CACHE[key] = {"text": text, "ts": time.time()}
        _save_cache(_DESC_CACHE)

# --- Word-boundary skill/title/avoid patterns --------------------------------
@lru_cache(maxsize=256)
def _wb(term):
    """Compile a case-insensitive word-boundary regex for a skill/title term."""
    return re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s):
    return _TAG_RE.sub(" ", s).replace("&nbsp;", " ").replace("&amp;", "&")


def fetch_description(company, external_path):
    """Fetch the full job description text for one posting. Returns lower-cased text."""
    cfg = SOURCES[company]
    if cfg["type"] in ("lever", "atlassian"):
        # These feeds embed the description; it's carried on the job dict already.
        return ""  # handled inline in fetch_jobs

    cache_key = f"{company}::{external_path}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    text = _fetch_description_uncached(company, external_path, cfg)
    _cache_put(cache_key, text)
    return text


def _fetch_description_uncached(company, external_path, cfg):
    if cfg["type"] == "smartrecruiters":
        return fetch_description_smartrecruiters(company, external_path)
    if cfg["type"] == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{cfg['token']}/jobs/{external_path}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
        except Exception:
            return ""
        # Greenhouse `content` is HTML-entity-encoded, so unescape BEFORE stripping tags.
        return strip_html(html.unescape(data.get("content", ""))).lower()
    if cfg["type"] == "oracle":
        base = f"{cfg['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
        finder = f'ById;Id="{external_path}",siteNumber={cfg["site_number"]}'
        url = f"{base}?onlyData=true&expand=all&finder={urllib.parse.quote(finder, safe=';=,')}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
        except Exception:
            return ""
        items = data.get("items", [])
        if not items:
            return ""
        r = items[0]
        # Combine the description + qualifications/responsibilities for skill matching.
        text = " ".join(r.get(k) or "" for k in (
            "ExternalDescriptionStr", "ExternalQualificationsStr",
            "ExternalResponsibilitiesStr"))
        return strip_html(html.unescape(text)).lower()
    url = cfg["detail_base"] + external_path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception:
        return ""
    info = data.get("jobPostingInfo", {})
    return strip_html(info.get("jobDescription", "")).lower()



def fetch_jobs_lever(company):
    """Fetch jobs from a Lever board. Description is embedded (no second fetch)."""
    cfg = SOURCES[company]
    req = urllib.request.Request(cfg["url"], headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    jobs = []
    for p in data:
        cats = p.get("categories") or {}
        jobs.append({
            "title": p.get("text", ""),
            "location": cats.get("location", "") or "",
            "posted": str(p.get("createdAt", "")),
            "external_path": "",
            "url": p.get("hostedUrl", ""),
            "description": strip_html(p.get("descriptionPlain", "")).lower(),
        })
    return jobs


def fetch_jobs_smartrecruiters(company, pages=5, per_page=20):
    """Fetch jobs from SmartRecruiters. Description is fetched per-job."""
    cfg = SOURCES[company]
    slug = cfg["company"]
    jobs = []
    for page in range(pages):
        url = (f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
               f"?limit={per_page}&offset={page * per_page}")
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                    "User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
        except Exception:
            break
        content = data.get("content", [])
        if not content:
            break
        for p in content:
            loc = p.get("location") or {}
            jobs.append({
                "title": p.get("name", ""),
                "location": f"{loc.get('city', '')} {loc.get('region', '')}".strip(),
                "posted": p.get("releasedDate", ""),
                "external_path": str(p.get("id", "")),
                "url": f"{cfg['job_url_prefix']}/{p.get('id', '')}",
            })
        if len(jobs) >= data.get("totalFound", 0):
            break
    return jobs


def fetch_description_smartrecruiters(company, job_id):
    """Fetch full job description from SmartRecruiters detail endpoint."""
    slug = SOURCES[company]["company"]
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}"
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception:
        return ""
    sections = data.get("jobAd", {}).get("sections", {})
    text = " ".join(
        strip_html(s.get("text", ""))
        for s in sections.values() if isinstance(s, dict)
    )
    return text.lower()


def fetch_jobs_greenhouse(company):
    """Fetch jobs from a Greenhouse board. Description needs a per-job detail fetch."""
    cfg = SOURCES[company]
    url = f"https://boards-api.greenhouse.io/v1/boards/{cfg['token']}/jobs?content=false"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    jobs = []
    for p in data.get("jobs", []):
        loc = p.get("location") or {}
        jobs.append({
            "title": p.get("title", ""),
            "location": loc.get("name", "") or "",
            "posted": p.get("updated_at", ""),
            "external_path": str(p.get("id", "")),  # carries the GH job id for the detail fetch
            "url": p.get("absolute_url", ""),
        })
    return jobs


def fetch_jobs_atlassian(company):
    """Fetch jobs from Atlassian's custom iCIMS-backed careers API.

    Returns all listings in one call (no pagination). Description is embedded
    in the 'overview' field — no second fetch needed.
    """
    cfg = SOURCES[company]
    req = urllib.request.Request(cfg["url"], headers={"Accept": "application/json",
                                                      "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    jobs = []
    for p in data if isinstance(data, list) else []:
        portal = p.get("portalJobPost") or {}
        # locations is a list; join into one string for the standard location field.
        locs = p.get("locations") or []
        location = ", ".join(locs) if locs else ""
        desc = strip_html(p.get("overview", "") or "").lower()
        jobs.append({
            "title": p.get("title", ""),
            "location": location,
            "posted": portal.get("updatedDate", ""),
            "external_path": str(p.get("id", "")),
            "url": p.get("applyUrl") or portal.get("portalUrl", ""),
            "description": desc,
        })
    return jobs


def fetch_jobs_oracle(company, pages=5, per_page=25):
    """Fetch jobs from Oracle Cloud recruiting (ORC). Description needs a detail fetch.

    The `requisitionList` only appears when expand=requisitionList is passed;
    pagination is via `offset` inside the finder clause. An optional `keyword`
    narrows the (often huge) catalog before the Bangalore filter runs.
    """
    cfg = SOURCES[company]
    base = f"{cfg['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    kw = f",keyword={urllib.parse.quote(cfg['keyword'])}" if cfg.get("keyword") else ""
    jobs = []
    for page in range(pages):
        offset = page * per_page
        finder = f"findReqs;siteNumber={cfg['site_number']},limit={per_page},offset={offset}{kw}"
        url = f"{base}?onlyData=true&expand=requisitionList&finder={urllib.parse.quote(finder, safe=';=,')}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        items = data.get("items", [])
        reqs = items[0].get("requisitionList", []) if items else []
        if not reqs:
            break
        for p in reqs:
            jid = str(p.get("Id", ""))
            jobs.append({
                "title": p.get("Title", ""),
                "location": p.get("PrimaryLocation", "") or "",
                "posted": p.get("PostedDate", ""),
                "external_path": jid,  # carries the ORC req id for the detail fetch
                "url": cfg["job_url_prefix"] + jid,
            })
    return jobs


def fetch_jobs(company, pages=3, per_page=20, search_text=""):
    """Fetch job postings for a source. Dispatches by source type."""
    t = SOURCES[company]["type"]
    if t == "lever":
        return fetch_jobs_lever(company)
    if t == "greenhouse":
        return fetch_jobs_greenhouse(company)
    if t == "oracle":
        return fetch_jobs_oracle(company, pages=pages, per_page=per_page)
    if t == "smartrecruiters":
        return fetch_jobs_smartrecruiters(company, pages=pages, per_page=per_page)
    if t == "atlassian":
        return fetch_jobs_atlassian(company)
    cfg = SOURCES[company]
    jobs = []
    for page in range(pages):
        body = json.dumps({
            "appliedFacets": cfg.get("applied_facets", {}),
            "limit": per_page,
            "offset": page * per_page,
            "searchText": search_text,
        }).encode()
        req = urllib.request.Request(
            cfg["url"], data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        for p in data.get("jobPostings", []):
            jobs.append({
                "title": p.get("title", ""),
                "location": p.get("locationsText", ""),
                "posted": p.get("postedOn", ""),
                "external_path": p.get("externalPath", ""),
                "url": cfg["job_url_prefix"] + p.get("externalPath", ""),
            })
        if len(jobs) >= data.get("total", 0):
            break
    return jobs


def prefilter_score(job, profile):
    """Cheap title/location-only score to decide which jobs are worth a detail fetch."""
    title = job["title"].lower()
    loc = job["location"].lower()
    title_bonus = sum(4 for t in profile["titles"] if _wb(t).search(title))
    loc_bonus = 3 if any(_wb(l).search(loc) for l in profile["locations"]) else 0
    penalty = sum(6 for a in profile["avoid"] if _wb(a).search(title))
    return title_bonus + loc_bonus - penalty


def passes_filters(job, description, profile):
    """Hard filter: keep Bangalore or Remote-India roles the candidate isn't under-qualified for.

    Returns (keep: bool, reason: str). Location is checked against the posting
    label, the external path, and the description (to catch '2 Locations' that
    actually include Bangalore). Experience drops roles requiring MORE years
    than the candidate has; roles with no stated requirement are kept.
    """
    hay = (job["location"] + " " + job.get("external_path", "") + " " + description).lower()
    is_bangalore = "bangalore" in hay or "bengaluru" in hay
    is_remote_india = "remote" in hay and "india" in hay
    if not is_bangalore and not is_remote_india:
        return False, "not Bangalore/Remote-India"

    req = required_years(description)
    cand = profile.get("years_experience", 0)
    # Keep jobs with no stated requirement: Salesforce rarely writes "N+ years",
    # and requiring it drops strong matches. The title penalty in score_job
    # already sinks senior/architect roles that are a poor fit.
    if req is not None and req > cand:
        return False, f"needs {req}+ yrs (you have {cand})"
    return True, "ok"


def required_years(description):
    """Best-effort: extract the minimum years of experience a job asks for."""
    yrs = [int(m) for m in re.findall(r"(\d{1,2})\s*\+?\s*years?", description)]
    yrs = [y for y in yrs if 0 < y <= 20]  # ignore noise like "2024 years"
    return min(yrs) if yrs else None


def score_job(job, profile, description=""):
    """Full score using title + location + fetched description text."""
    title = job["title"].lower()
    loc = job["location"].lower()
    text = title + " " + loc + " " + description

    matched = [s for s in profile["skills"] if _wb(s).search(text)]
    skill_score = sum(profile["skills"][s] for s in matched)

    title_bonus = sum(4 for t in profile["titles"] if _wb(t).search(title))
    loc_bonus = 3 if any(_wb(l).search(loc) for l in profile["locations"]) else 0
    penalty = sum(6 for a in profile["avoid"] if _wb(a).search(title))

    # Experience-year fit vs the candidate's years.
    req = required_years(description)
    cand = profile.get("years_experience", 0)
    exp_note = None
    if req is not None and cand:
        gap = req - cand
        if gap <= 0:
            exp_note = f"meets exp ({req}+ req, you have {cand})"
        elif gap <= 2:
            exp_note = f"slight stretch ({req}+ req, you have {cand})"
            penalty += 3
        else:
            exp_note = f"under-qualified ({req}+ req, you have {cand})"
            penalty += 8

    score = skill_score + title_bonus + loc_bonus - penalty
    reasons = []
    if title_bonus:
        reasons.append("title match")
    if loc_bonus:
        reasons.append("location match")
    if description:
        reasons.append(f"{len(matched)} skills in description")
    if exp_note:
        reasons.append(exp_note)
    if penalty and not exp_note:
        reasons.append("off-target title (penalized)")
    return score, matched, reasons


def write_application_kit(ranked, contact, resume_path, out_dir):
    """Write an HTML application kit: per-job prep sheet + direct apply link.

    This does NOT submit anything. It makes manual applying fast by putting
    your details, matched skills, and talking points on one page to copy from.
    """
    os.makedirs(out_dir, exist_ok=True)
    links = "".join(
        f'<a href="https://{html.escape(l)}" target="_blank">{html.escape(l)}</a>'
        for l in contact["links"]
    )
    cards = []
    all_companies = set()
    all_skills = set()
    for i, (s, matched, reasons, j) in enumerate(ranked, 1):
        rating = rating_1_to_10(s)
        # Rating band: 1-3 = strong (green), 4-6 = decent (amber), 7-10 = weak (grey).
        band = "strong" if rating <= 3 else ("mid" if rating <= 6 else "weak")
        pills = "".join(f'<span class="pill">{html.escape(m)}</span>' for m in matched) \
            or '<span class="pill empty">no direct skill matches</span>'
        exp_line = next((r for r in reasons if "req, you have" in r), "not stated in posting")
        company = j.get('company', '')
        all_companies.add(company)
        skills_lc = [m.lower() for m in matched]
        all_skills.update(skills_lc)
        # data-* carry filter metadata so the client JS never re-parses card text.
        data_skills = html.escape(" ".join(skills_lc))
        data_search = html.escape(
            f"{j['title']} {company} {' '.join(matched)} {j['location']}".lower())
        cards.append(f"""
        <article class="job" data-company="{html.escape(company)}" data-rating="{rating}"
                 data-skills="{data_skills}" data-search="{data_search}"
                 data-url="{html.escape(j['url'])}">
          <div class="job-head">
            <span class="rank">#{i}</span>
            <h2 class="job-title">{html.escape(j['title'])}</h2>
            <span class="badge {band}" title="1 = best match">{rating}<small>/10</small></span>
          </div>
          <div class="meta">
            <span class="company">{html.escape(company)}</span>
            <span class="loc">{html.escape(j['location'])}</span>
          </div>
          <div class="skills">{pills}</div>
          <p class="exp"><span class="lbl">Exp</span>{html.escape(exp_line)}</p>
          <div class="actions">
            <a class="apply" href="{html.escape(j['url'])}" target="_blank">Apply &rarr;</a>
            <button type="button" class="applied-btn">Mark applied</button>
            <button type="button" class="na-btn">Not applicable</button>
          </div>
        </article>""")

    shown = len(ranked)
    company_opts = "".join(
        f'<option value="{html.escape(c)}">{html.escape(c)}</option>'
        for c in sorted(all_companies) if c)
    skill_chips = "".join(
        f'<button type="button" class="chip" data-skill="{html.escape(sk)}">{html.escape(sk)}</button>'
        for sk in sorted(all_skills))
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Application kit &mdash; {html.escape(contact['name'])}</title><style>
    :root{{--ink:#12151b;--muted:#5b6472;--line:#e6e9ef;--bg:#f7f8fb;--accent:#0b5cff}}
    *{{box-sizing:border-box}}
    body{{font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
      margin:0;background:var(--bg);color:var(--ink)}}
    .wrap{{max-width:1400px;margin:0 auto;padding:24px 24px 56px}}
    header.top{{background:linear-gradient(135deg,#0b5cff,#5b8cff);color:#fff;
      border-radius:16px;padding:18px 24px;margin-bottom:14px;
      display:flex;align-items:baseline;gap:14px}}
    header.top h1{{margin:0;font-size:22px;letter-spacing:-.3px}}
    header.top .who{{opacity:.9;font-size:14px}}
    .note{{background:#fff4f4;border:1px solid #ffd7d7;color:#a11;
      border-radius:10px;padding:8px 14px;font-size:12px;margin-bottom:14px}}
    .contact{{display:flex;flex-wrap:wrap;gap:4px 22px;
      background:#fff;border:1px solid var(--line);border-radius:12px;
      padding:12px 18px;margin-bottom:12px;font-size:13px}}
    .contact b{{color:var(--muted);font-weight:600;margin-right:6px}}
    .contact a{{color:var(--accent);text-decoration:none;margin-right:12px}}
    .summary{{color:var(--muted);font-size:13px;margin:12px 2px}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));
      gap:14px;align-items:start}}
    .job{{background:#fff;border:1px solid var(--line);border-radius:12px;
      padding:14px 16px;box-shadow:0 1px 2px rgba(16,20,30,.04);
      display:flex;flex-direction:column;gap:8px}}
    .job.done{{opacity:.55;background:#f4f8f4;border-color:#cfe6d3}}
    .job.skip{{opacity:.45;background:#fdf4f4;border-color:#f0cece}}
    .job.skip .job-title{{text-decoration:line-through;color:var(--muted)}}
    .job-head{{display:flex;align-items:center;gap:10px}}
    .rank{{font-weight:700;color:var(--muted);font-size:13px}}
    .job-title{{flex:1;min-width:0;margin:0;font-size:15px;line-height:1.3}}
    .meta{{font-size:12px;color:var(--muted);display:flex;gap:8px;flex-wrap:wrap;
      align-items:center}}
    .company{{background:#eef2ff;color:#3355cc;border-radius:6px;padding:1px 8px;
      font-weight:600;text-transform:capitalize}}
    .badge{{flex-shrink:0;font-weight:700;font-size:16px;color:#fff;border-radius:10px;
      padding:3px 9px;min-width:44px;text-align:center}}
    .badge small{{font-size:10px;font-weight:600;opacity:.85}}
    .badge.strong{{background:#1a9e5c}} .badge.mid{{background:#e08a00}}
    .badge.weak{{background:#8a94a6}}
    .lbl{{font-size:11px;text-transform:uppercase;letter-spacing:.4px;
      color:var(--muted);font-weight:600;margin-right:6px}}
    .skills{{display:flex;flex-wrap:wrap;gap:5px}}
    .pill{{display:inline-block;background:#eef7f1;color:#1a7a48;border:1px solid #cfead9;
      border-radius:999px;padding:1px 9px;font-size:11px}}
    .pill.empty{{background:#f2f2f4;color:var(--muted);border-color:var(--line)}}
    .exp{{margin:0;font-size:12px;color:var(--muted)}}
    .actions{{display:flex;gap:8px;margin-top:2px;align-items:center}}
    .apply{{display:inline-block;background:var(--accent);color:#fff;
      padding:7px 14px;border-radius:8px;text-decoration:none;font-weight:600;font-size:13px}}
    .apply:hover{{background:#0a4fd6}}
    .applied-btn{{font:inherit;font-size:13px;cursor:pointer;background:#fff;
      color:var(--muted);border:1px solid var(--line);border-radius:8px;padding:6px 12px}}
    .applied-btn:hover{{border-color:#1a9e5c;color:#1a9e5c}}
    .job.done .applied-btn{{background:#1a9e5c;color:#fff;border-color:#1a9e5c}}
    .na-btn{{font:inherit;font-size:13px;cursor:pointer;background:#fff;
      color:var(--muted);border:1px solid var(--line);border-radius:8px;padding:6px 12px}}
    .na-btn:hover{{border-color:#c0392b;color:#c0392b}}
    .job.skip .na-btn{{background:#c0392b;color:#fff;border-color:#c0392b}}
    .filters{{position:sticky;top:0;z-index:5;background:rgba(247,248,251,.92);
      backdrop-filter:blur(6px);border:1px solid var(--line);border-radius:12px;
      padding:12px 14px;margin-bottom:14px}}
    .filters .row{{display:flex;flex-wrap:wrap;gap:10px;align-items:center}}
    .filters select,.filters input[type=search]{{font:14px inherit;color:var(--ink);
      background:#fff;border:1px solid var(--line);border-radius:8px;padding:7px 10px}}
    .filters input[type=search]{{flex:1;min-width:160px}}
    .chips{{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}}
    .chip{{cursor:pointer;background:#eef7f1;color:#1a7a48;border:1px solid #cfead9;
      border-radius:999px;padding:3px 11px;font:inherit;font-size:12px}}
    .chip.active{{background:var(--accent);color:#fff;border-color:var(--accent)}}
    .chip.clear{{background:#f2f2f4;color:var(--muted);border-color:var(--line)}}
    .count{{font-size:13px;color:var(--muted);margin-left:auto}}
    .hint{{font-size:12px;color:var(--muted);margin-top:8px}}
    .empty-state{{display:none;text-align:center;color:var(--muted);
      background:#fff;border:1px dashed var(--line);border-radius:14px;padding:32px}}
    .job[hidden]{{display:none}}
    </style></head><body><div class="wrap">
    <header class="top">
      <h1>Application kit</h1>
      <span class="who">{html.escape(contact['name'])}</span>
    </header>
    <div class="note">Human-in-the-loop: this page does <b>not</b> submit anything. Review each role and click apply yourself.</div>
    <div class="contact">
      <span><b>Email</b>{html.escape(contact['email'])}</span>
      <span><b>Phone</b>{html.escape(contact['phone'])}</span>
      <span><b>Links</b>{links}</span>
      <span><b>Resume</b>{html.escape(resume_path)}</span>
    </div>
    <p class="summary">Showing all {shown} matches &middot; rating <b>1 = best</b>, 10 = weakest. Applied state is saved in this browser.</p>
    <div class="filters">
      <div class="row">
        <select id="f-company">
          <option value="">All companies</option>
          {company_opts}
        </select>
        <select id="f-rating">
          <option value="">All ratings</option>
          <option value="1-3">Strong (1-3)</option>
          <option value="4-6">Decent (4-6)</option>
          <option value="7-10">Weak (7-10)</option>
        </select>
        <select id="f-applied">
          <option value="">All</option>
          <option value="no">Not applied</option>
          <option value="yes">Applied</option>
          <option value="na">Not applicable</option>
        </select>
        <input type="search" id="f-search" placeholder="Search title, company, skills&hellip;">
        <span class="count" id="f-count"></span>
      </div>
      <div class="chips" id="f-chips">
        {skill_chips}
        <button type="button" class="chip clear" id="f-clear">clear skills</button>
      </div>
      <div class="hint">Selecting multiple skills narrows to roles matching <b>all</b> of them.</div>
    </div>
    <p class="empty-state" id="f-empty">No roles match these filters.</p>
    <div class="grid">
    {''.join(cards)}
    </div>
    </div>
    <script>
    (function(){{
      var KEY = 'jobagent.applied';
      var applied = {{}};
      try {{ applied = JSON.parse(localStorage.getItem(KEY)) || {{}}; }} catch(e) {{ applied = {{}}; }}
      function save(){{ try {{ localStorage.setItem(KEY, JSON.stringify(applied)); }} catch(e){{}} }}

      var jobs = Array.prototype.slice.call(document.querySelectorAll('.job'));
      var company = document.getElementById('f-company');
      var rating = document.getElementById('f-rating');
      var appliedSel = document.getElementById('f-applied');
      var search = document.getElementById('f-search');
      var chipBox = document.getElementById('f-chips');
      var clearBtn = document.getElementById('f-clear');
      var count = document.getElementById('f-count');
      var empty = document.getElementById('f-empty');

      // Restore applied/na state onto cards.
      jobs.forEach(function(job){{
        var url = job.getAttribute('data-url');
        var state = applied[url];
        if(state === 1 || state === 'yes'){{
          job.classList.add('done');
          var b = job.querySelector('.applied-btn');
          if(b) b.textContent = 'Applied ✓';
        }} else if(state === 'na'){{
          job.classList.add('skip');
          var b = job.querySelector('.na-btn');
          if(b) b.textContent = 'N/A ✓';
        }}
      }});

      function activeSkills(){{
        return Array.prototype.slice.call(chipBox.querySelectorAll('.chip.active'))
          .map(function(c){{ return c.getAttribute('data-skill'); }});
      }}
      function apply(){{
        var co = company.value;
        var band = rating.value;
        var appl = appliedSel.value;
        var q = search.value.trim().toLowerCase();
        var skills = activeSkills();
        var lo = 0, hi = 10;
        if(band){{ var p = band.split('-'); lo = +p[0]; hi = +p[1]; }}
        var visible = 0;
        jobs.forEach(function(job){{
          var r = +job.getAttribute('data-rating');
          var jobSkills = job.getAttribute('data-skills').split(' ');
          var isDone = job.classList.contains('done');
          var isSkip = job.classList.contains('skip');
          var ok = (!co || job.getAttribute('data-company') === co)
            && (r >= lo && r <= hi)
            && (!appl || (appl === 'yes' ? isDone : appl === 'na' ? isSkip : (!isDone && !isSkip)))
            && (!q || job.getAttribute('data-search').indexOf(q) !== -1)
            && skills.every(function(sk){{ return jobSkills.indexOf(sk) !== -1; }});
          job.hidden = !ok;
          if(ok) visible++;
        }});
        count.textContent = 'showing ' + visible + ' of ' + jobs.length;
        empty.style.display = visible ? 'none' : 'block';
      }}
      company.addEventListener('change', apply);
      rating.addEventListener('change', apply);
      appliedSel.addEventListener('change', apply);
      search.addEventListener('input', apply);
      chipBox.addEventListener('click', function(e){{
        if(e.target.classList.contains('chip') && !e.target.classList.contains('clear')){{
          e.target.classList.toggle('active');
          apply();
        }}
      }});
      clearBtn.addEventListener('click', function(){{
        chipBox.querySelectorAll('.chip.active').forEach(function(c){{ c.classList.remove('active'); }});
        apply();
      }});
      // Mark-applied toggle.
      document.querySelector('.grid').addEventListener('click', function(e){{
        var job = e.target.closest('.job');
        if(!job) return;
        var url = job.getAttribute('data-url');
        var naBtn = job.querySelector('.na-btn');
        var applBtn = job.querySelector('.applied-btn');
        if(e.target.classList.contains('applied-btn')){{
          // Clear N/A if set, then toggle applied.
          job.classList.remove('skip');
          if(naBtn) naBtn.textContent = 'Not applicable';
          if(job.classList.toggle('done')){{
            applied[url] = 'yes'; applBtn.textContent = 'Applied ✓';
          }} else {{
            delete applied[url]; applBtn.textContent = 'Mark applied';
          }}
          save(); apply();
        }} else if(e.target.classList.contains('na-btn')){{
          // Clear applied if set, then toggle N/A.
          job.classList.remove('done');
          if(applBtn) applBtn.textContent = 'Mark applied';
          if(job.classList.toggle('skip')){{
            applied[url] = 'na'; naBtn.textContent = 'N/A ✓';
          }} else {{
            delete applied[url]; naBtn.textContent = 'Not applicable';
          }}
          save(); apply();
        }}
      }});
      apply();
    }})();
    </script>
    </body></html>"""

    path = os.path.join(out_dir, "application_kit.html")
    with open(path, "w") as f:
        f.write(doc)
    return path


def rating_1_to_10(raw_score):
    """Map an unbounded raw score to a 1-10 rating where 1 = best match.

    Thresholds tuned to the current weighting (a strong backend role scores
    ~20+, a weak/penalized one scores low or negative).
    """
    thresholds = [20, 16, 12, 9, 6, 4, 2, 0, -4]  # rating 1..9; below all = 10
    for i, t in enumerate(thresholds, start=1):
        if raw_score >= t:
            return i
    return 10


def posted_age_hours(job):
    """Best-effort age of a posting in hours, or None if the date isn't parseable.

    Sources differ: Lever carries epoch-ms, Greenhouse/RSS carry a date string,
    Workday carries relative text ("Posted 2 Days Ago") we approximate by day.
    None means 'unknown' — callers keep such jobs rather than guess them stale.
    """
    raw = str(job.get("posted", "")).strip()
    if not raw:
        return None
    now = time.time()

    # Lever: epoch milliseconds (a long run of digits).
    if raw.isdigit():
        return max(0.0, (now - int(raw) / 1000) / 3600)

    low = raw.lower()

    # Workday relative text: day granularity is all we get.
    if "today" in low or "just posted" in low:
        return 0.0
    if "yesterday" in low:
        return 24.0
    m = re.search(r"(\d+)\+?\s*day", low)
    if m:
        return int(m.group(1)) * 24.0
    if re.search(r"(\d+)\+?\s*hour", low):
        return int(re.search(r"(\d+)", low).group(1))
    if re.search(r"(\d+)\+?\s*(week|month|year)", low):
        return 10000.0  # clearly older than any short window

    # Greenhouse ISO 8601 (updated_at) — fromisoformat handles the 'Z'/offset.
    from datetime import datetime, timezone
    try:
        iso = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt.timestamp()) / 3600)
    except (ValueError, TypeError):
        pass

    # RSS pubDate (e.g. "Mon, 18 Aug 2026 09:00:00 GMT").
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (now - dt.timestamp()) / 3600)
        except (ValueError, TypeError):
            continue
    return None


def is_recent(job, max_hours=MAX_AGE_HOURS):
    """True if the job is within max_hours OR its date is unknown (kept for recall)."""
    age = posted_age_hours(job)
    return age is None or age <= max_hours


def collect_ranked(company, max_age=MAX_AGE_HOURS):
    """Fetch, filter, and score one company's jobs. Returns (ranked, dropped)."""
    print(f"Fetching live jobs from {company}...")
    try:
        jobs = fetch_jobs(company, pages=5, per_page=20)
    except Exception as e:
        print(f"  {company}: FETCH FAILED ({e}), skipping")
        return [], 0
    n_fetched = len(jobs)
    jobs = [j for j in jobs if is_recent(j, max_age)]
    n_stale = n_fetched - len(jobs)
    for j in jobs:
        j["company"] = company
        j["_pref"] = prefilter_score(j, PROFILE)
    # Prioritise Bangalore/Bengaluru jobs, then Remote-India, then the rest,
    # so the cap doesn't cut targeted roles in favour of untargeted ones.
    def _location_priority(j):
        loc = (j["location"] + " " + j.get("external_path", "")).lower()
        if "bangalore" in loc or "bengaluru" in loc:
            return 0
        if "remote" in loc and "india" in loc:
            return 1
        return 2
    jobs.sort(key=lambda j: (_location_priority(j), -j["_pref"]))
    top = jobs[:50]

    ranked = []
    dropped = 0
    for j in top:
        # lever/atlassian carry the description inline; workday/greenhouse need a detail fetch.
        desc = j.get("description") or fetch_description(company, j["external_path"])
        keep, why = passes_filters(j, desc, PROFILE)
        if not keep:
            dropped += 1
        else:
            s, matched, reasons = score_job(j, PROFILE, desc)
            ranked.append((s, matched, reasons, j))
        # Only sleep for types that do a per-job detail fetch (lever/atlassian embed it).
        if SOURCES[company]["type"] in ("workday", "greenhouse", "oracle", "smartrecruiters"):
            time.sleep(0.3)  # polite rate-limit for per-job detail calls
    print(f"  {company}: kept {len(ranked)}, filtered out {dropped}"
          f" ({n_stale} dropped as older than {max_age}h)")
    return ranked, dropped


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Job-matching agent")
    parser.add_argument("--max-age", type=int, default=MAX_AGE_HOURS, metavar="HOURS",
                        help=f"Only include jobs posted within this many hours (default: {MAX_AGE_HOURS})")
    parser.add_argument("--companies", metavar="NAME", nargs="+",
                        choices=list(SOURCES.keys()), default=None,
                        help="Limit to specific companies (default: all)")
    parser.add_argument("--top", type=int, default=10, metavar="N",
                        help="Number of top matches to print to terminal (default: 10)")
    args = parser.parse_args()

    # Load and parse the resume now (after --help, so it doesn't block arg parsing).
    global PROFILE, CONTACT
    try:
        _resume_text = extract_text(RESUME_PATH)
    except Exception as e:
        print(f"ERROR: could not read resume at {RESUME_PATH}: {e}")
        raise SystemExit(1)
    PROFILE = parse_profile(_resume_text)
    CONTACT = parse_contact(_resume_text)
    if YEARS_EXPERIENCE_OVERRIDE is not None:
        PROFILE["years_experience"] = YEARS_EXPERIENCE_OVERRIDE

    companies = args.companies or list(SOURCES.keys())
    max_age = args.max_age
    ranked = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(collect_ranked, c, max_age): c for c in companies}
        for fut in as_completed(futures):
            r, _ = fut.result()
            ranked.extend(r)
    elapsed = time.time() - t0
    ranked.sort(key=lambda x: x[0], reverse=True)
    print(f"\nTotal kept across {len(companies)} companies: {len(ranked)} ({elapsed:.0f}s)\n")

    print("=" * 78)
    print("TOP MATCHES: Bangalore + experience-fit for Arpitha (4 yrs, Java/Spring)")
    print("=" * 78)
    for i, (s, matched, reasons, j) in enumerate(ranked[:args.top], 1):
        print(f"\n#{i}  [match {rating_1_to_10(s)}/10, 1=best]  {j['title']}  ({j.get('company','?')})")
        print(f"    Location : {j['location']}")
        print(f"    Skills   : {', '.join(matched) if matched else '(none matched)'}")
        if reasons:
            print(f"    Why      : {', '.join(reasons)}")
        print(f"    Apply    : {j['url']}")
    print("\n" + "=" * 78)
    print("HUMAN-IN-THE-LOOP: review above, open the Apply link to submit yourself.")

    kit = write_application_kit(ranked, CONTACT, RESUME_PATH,
                                os.path.dirname(os.path.abspath(__file__)))
    print(f"\nApplication kit written: {kit}")
    # Open in Chrome explicitly (default browser here is Edge).
    subprocess.run(["open", "-a", "Google Chrome", kit], check=False)
    print("Opened in Chrome (still human-submitted).")


if __name__ == "__main__":
    main()
