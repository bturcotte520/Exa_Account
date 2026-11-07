import hashlib
import ipaddress
import re
from datetime import date, datetime, timezone
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


GEOGRAPHY = "US West Coast (California, Oregon, Washington)"
ACCOUNTS = "Regional fitness chains"
QUALIFICATIONS = "2+ operating locations and expansion"


class Criteria(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    geography: str = Field(default=GEOGRAPHY, min_length=1, max_length=1000)
    accounts: str = Field(default=ACCOUNTS, min_length=1, max_length=1000)
    qualifications: str = Field(default=QUALIFICATIONS, min_length=1, max_length=4000)
    window_days: int = Field(default=90, ge=1, le=3650, strict=True)
    window_start: date | None = None
    window_end: date | None = None

    @model_validator(mode="after")
    def dates(self):
        if bool(self.window_start) != bool(self.window_end):
            raise ValueError("Supply both research dates or neither")
        if self.window_start and (self.window_start > self.window_end or self.window_end > datetime.now(timezone.utc).date()):
            raise ValueError("Research dates must be ordered and not in the future")
        if self.window_start:
            self.window_days = max(1, (self.window_end - self.window_start).days)
            if self.window_days > 3650:
                raise ValueError("Research window cannot exceed 3650 days")
        return self


def criteria_snapshot(record):
    return {key: record.get(key, default) for key, default in (
        ("geography", "Texas"), ("accounts", ACCOUNTS), ("qualifications", QUALIFICATIONS),
        ("window_days", 90), ("window_start", None), ("window_end", None))}


def deterministic_criteria(record):
    return (record.get("geography", "Texas") in REGION_STATES
            and record.get("accounts", ACCOUNTS) == ACCOUNTS
            and record.get("qualifications", QUALIFICATIONS) == QUALIFICATIONS)
# Records without geography predate the West Coast workflow and were Texas-only.
REGION_STATES = {GEOGRAPHY: {"ca", "california", "or", "oregon", "wa", "washington"},
                 "Texas": {"tx", "texas"}}


def now():
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix):
    return f"{prefix}_{uuid4().hex}"


def stable_id(prefix, *parts):
    return prefix + "_" + hashlib.sha256("\x00".join(str(p) for p in parts).encode()).hexdigest()[:24]


def normalize_domain(value):
    parsed = urlsplit(value if "://" in value else "https://" + value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    try:
        ipaddress.ip_address(host)
        raise ValueError("Use a public company domain, not an IP address")
    except ValueError as exc:
        if "public company" in str(exc):
            raise
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or parsed.port or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", host)
            or len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split("."))
            or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid"))):
        raise ValueError("Use a public company domain")
    return host


def normalize_url(value):
    parsed = urlsplit(value)
    host = normalize_domain(value)
    query = [(k, v) for k, v in parse_qsl(parsed.query) if not k.lower().startswith("utm_") and k not in {"fbclid", "gclid"}]
    return urlunsplit(("https", host, parsed.path.rstrip("/"), urlencode(sorted(query)), ""))


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    address: str | None = None
    state: str | None = Field(default=None, description="State, province or region; use US state name or postal abbreviation for US criteria; null if unknown")
    operating_status: Literal["operating", "announced", "unclear"] | None = None
    operating_entity: str | None = None


class ExpansionSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["opening", "new_location", "expansion", "expansion_announcement", "announcement", "lease", "acquisition"] | None = None
    description: str | None = None
    state: str | None = Field(default=None, description="State, province or region; use US state name or postal abbreviation for US criteria; null if unknown")
    event_date: str | None = None
    publication_date: str | None = None
    date_status: Literal["actual", "announced", "planned", "unknown"] | None = None
    announcement_date: str | None = None
    planned_opening_date: str | None = None
    actual_opening_date: str | None = None


class CriterionAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["met", "not_met", "unknown"]
    explanation: str


class ResearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    company_name: str | None = None
    domain: str | None = None
    entity_verification_status: Literal["verified", "unclear", "mismatch"] | None = None
    locations: list[Location] = Field(default_factory=list)
    expansion_signals: list[ExpansionSignal] = Field(default_factory=list)
    employee_count: int | str | None = Field(default=None, description="Company-level headcount from Fiber if available; null if unknown")
    fit: Literal["qualifies", "does_not_qualify", "needs_review"] | None = None
    fit_explanation: str | None = None
    unresolved_questions: list[str] = Field(default_factory=list)
    criteria_assessment: dict[str, CriterionAssessment] = Field(default_factory=dict, description="Keys geography, accounts, qualifications, research_window. Assess each saved criterion, including every qualification, with status and explanation. Ground each status and explanation with citations. Unknown if incomplete. These are source-grounded assessments, not deterministic verification.")
