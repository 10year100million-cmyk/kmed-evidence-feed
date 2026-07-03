#!/usr/bin/env python3
"""Fetch recent Korean medicine related papers from PubMed.

This script is designed for GitHub Actions. It keeps the site static by
generating papers.json ahead of time, including optional Korean clinical
summaries when OPENAI_API_KEY is available.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


BASE_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")

SUMMARY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title_ko": {
            "type": "string",
            "description": "A natural Korean translation of the English paper title.",
        },
        "question": {
            "type": "string",
            "description": "The study question in one concise Korean sentence.",
        },
        "methods": {
            "type": "string",
            "description": "The study design and method in one concise Korean sentence.",
        },
        "results": {
            "type": "string",
            "description": "The main result in one concise Korean sentence.",
        },
        "clinical_takeaway": {
            "type": "string",
            "description": "A cautious clinical learning point for Korean medicine doctors.",
        },
        "limitations": {
            "type": "string",
            "description": "The main limitation or caution in one concise Korean sentence.",
        },
        "pico": {
            "type": "object",
            "properties": {
                "p": {"type": "string", "description": "Population/problem."},
                "i": {"type": "string", "description": "Intervention/exposure."},
                "c": {"type": "string", "description": "Comparator/control."},
                "o": {"type": "string", "description": "Outcome."},
            },
            "required": ["p", "i", "c", "o"],
            "additionalProperties": False,
        },
    },
    "required": [
        "title_ko",
        "question",
        "methods",
        "results",
        "clinical_takeaway",
        "limitations",
        "pico",
    ],
    "additionalProperties": False,
}

CATEGORIES: list[dict[str, Any]] = [
    {
        "id": "acupuncture",
        "label": "침구/전침",
        "query": '("Acupuncture Therapy"[MeSH Terms] OR acupuncture[Title/Abstract] OR electroacupuncture[Title/Abstract] OR moxibustion[Title/Abstract])',
        "keywords": ["acupuncture", "electroacupuncture", "moxibustion", "acupoint"],
    },
    {
        "id": "herbal",
        "label": "한약/본초",
        "query": '("Medicine, Korean Traditional"[MeSH Terms] OR "Herbal Medicine"[Title/Abstract] OR phytotherapy[Title/Abstract] OR "traditional Korean medicine"[Title/Abstract] OR "traditional Chinese medicine"[Title/Abstract])',
        "keywords": ["herbal", "phytotherapy", "traditional korean medicine", "traditional chinese medicine", "decoction"],
    },
    {
        "id": "musculoskeletal",
        "label": "근골격",
        "query": '(acupuncture[Title/Abstract] OR electroacupuncture[Title/Abstract] OR "herbal medicine"[Title/Abstract]) AND ("low back pain"[Title/Abstract] OR "neck pain"[Title/Abstract] OR osteoarthritis[Title/Abstract] OR "shoulder pain"[Title/Abstract] OR tendinopathy[Title/Abstract])',
        "keywords": ["low back pain", "neck pain", "osteoarthritis", "shoulder", "tendinopathy", "knee pain"],
    },
    {
        "id": "women",
        "label": "여성질환",
        "query": '(acupuncture[Title/Abstract] OR "herbal medicine"[Title/Abstract] OR "traditional medicine"[Title/Abstract]) AND (infertility[Title/Abstract] OR dysmenorrhea[Title/Abstract] OR PCOS[Title/Abstract] OR endometriosis[Title/Abstract] OR menopause[Title/Abstract])',
        "keywords": ["infertility", "dysmenorrhea", "pcos", "endometriosis", "menopause", "pregnancy"],
    },
    {
        "id": "metabolic",
        "label": "대사/비만",
        "query": '(acupuncture[Title/Abstract] OR "herbal medicine"[Title/Abstract] OR "traditional medicine"[Title/Abstract]) AND (obesity[Title/Abstract] OR diabetes[Title/Abstract] OR "metabolic syndrome"[Title/Abstract] OR NAFLD[Title/Abstract])',
        "keywords": ["obesity", "diabetes", "metabolic syndrome", "nafld", "weight"],
    },
    {
        "id": "sleep_mental",
        "label": "수면/정신",
        "query": '(acupuncture[Title/Abstract] OR "herbal medicine"[Title/Abstract] OR "traditional medicine"[Title/Abstract]) AND (insomnia[Title/Abstract] OR anxiety[Title/Abstract] OR depression[Title/Abstract] OR sleep[Title/Abstract] OR autonomic[Title/Abstract])',
        "keywords": ["insomnia", "anxiety", "depression", "sleep", "autonomic"],
    },
]

SAFETY_TERMS = [
    "adverse",
    "safety",
    "toxicity",
    "liver injury",
    "hepatotoxicity",
    "herb-drug",
    "interaction",
    "contraindication",
]


class RateLimiter:
    def __init__(self, per_second: float) -> None:
        self.interval = 1.0 / per_second
        self.last_call = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self.last_call
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self.last_call = time.monotonic()


def request_json(url: str, params: dict[str, str], limiter: RateLimiter) -> Any:
    limiter.wait()
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "kmed-evidence-feed/1.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def request_xml(url: str, params: dict[str, str], limiter: RateLimiter) -> ET.Element:
    limiter.wait()
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "kmed-evidence-feed/1.0"})
    with urllib.request.urlopen(req, timeout=45) as response:
        return ET.fromstring(response.read())


def base_params(ncbi_api_key: str | None, email: str | None) -> dict[str, str]:
    params = {"tool": "kmed_evidence_feed"}
    if email:
        params["email"] = email
    if ncbi_api_key:
        params["api_key"] = ncbi_api_key
    return params


def esearch(category: dict[str, Any], retmax: int, lookback_days: int, limiter: RateLimiter, ncbi_api_key: str | None, email: str | None) -> list[str]:
    params = {
        **base_params(ncbi_api_key, email),
        "db": "pubmed",
        "term": category["query"],
        "retmode": "json",
        "retmax": str(retmax),
        "sort": "pub date",
        "datetype": "pdat",
        "reldate": str(lookback_days),
    }
    data = request_json(f"{BASE_EUTILS}/esearch.fcgi", params, limiter)
    return data.get("esearchresult", {}).get("idlist", [])


def text_at(node: ET.Element | None, path: str, default: str = "") -> str:
    if node is None:
        return default
    found = node.find(path)
    if found is None:
        return default
    return "".join(found.itertext()).strip() or default


def all_text_at(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ""
    parts = []
    for found in node.findall(path):
        value = " ".join("".join(found.itertext()).split())
        if value:
            parts.append(value)
    return "\n".join(parts)


def parse_pub_date(article: ET.Element) -> str:
    pub_date = article.find(".//JournalIssue/PubDate")
    if pub_date is None:
        article_date = article.find(".//ArticleDate")
        if article_date is None:
            return ""
        year = text_at(article_date, "Year")
        month = text_at(article_date, "Month", "01").zfill(2)
        day = text_at(article_date, "Day", "01").zfill(2)
        return f"{year}-{month}-{day}" if year else ""

    medline = text_at(pub_date, "MedlineDate")
    if medline:
        match = re.search(r"(19|20)\d{2}", medline)
        return f"{match.group(0)}-01-01" if match else ""

    year = text_at(pub_date, "Year")
    month_raw = text_at(pub_date, "Month", "01")
    day = text_at(pub_date, "Day", "01").zfill(2)
    month_map = {
        "jan": "01",
        "feb": "02",
        "mar": "03",
        "apr": "04",
        "may": "05",
        "jun": "06",
        "jul": "07",
        "aug": "08",
        "sep": "09",
        "oct": "10",
        "nov": "11",
        "dec": "12",
    }
    month = month_map.get(month_raw[:3].lower(), month_raw.zfill(2) if month_raw.isdigit() else "01")
    return f"{year}-{month}-{day}" if year else ""


def parse_authors(article: ET.Element) -> str:
    names = []
    for author in article.findall(".//AuthorList/Author")[:6]:
        collective = text_at(author, "CollectiveName")
        if collective:
            names.append(collective)
            continue
        last = text_at(author, "LastName")
        initials = text_at(author, "Initials")
        if last:
            names.append(f"{last} {initials}".strip())
    suffix = " et al." if len(article.findall(".//AuthorList/Author")) > 6 else ""
    return ", ".join(names) + suffix


def parse_article_ids(pubmed_article: ET.Element) -> tuple[str, str]:
    pmid = text_at(pubmed_article, ".//MedlineCitation/PMID")
    doi = ""
    for article_id in pubmed_article.findall(".//PubmedData/ArticleIdList/ArticleId"):
        if article_id.attrib.get("IdType") == "doi":
            doi = "".join(article_id.itertext()).strip()
            break
    return pmid, doi


def infer_categories(title: str, abstract: str, source_category: str) -> list[str]:
    text = f"{title} {abstract}".lower()
    labels = {source_category}
    for category in CATEGORIES:
        if any(keyword in text for keyword in category["keywords"]):
            labels.add(category["label"])
    return sorted(labels)


def infer_evidence(publication_types: list[str], title: str, abstract: str) -> str:
    joined_types = " ".join(publication_types).lower()
    text = f"{title} {abstract}".lower()
    if "meta-analysis" in joined_types or "systematic review" in joined_types or "meta-analysis" in text or "systematic review" in text:
        return "Meta-analysis"
    if "randomized controlled trial" in joined_types or "randomized" in text or "randomised" in text:
        return "RCT"
    if "cohort" in text or "population-based" in text:
        return "Cohort"
    if "case reports" in joined_types or "case report" in text:
        return "Case report"
    if any(term in text for term in SAFETY_TERMS):
        return "Safety"
    if "protocol" in text:
        return "Protocol"
    if "animals" in joined_types and "humans" not in joined_types:
        return "Animal/basic"
    return "Clinical study"


def infer_levels(evidence_type: str, title: str, abstract: str) -> tuple[str, str, str]:
    text = f"{title} {abstract}".lower()
    if evidence_type in {"Meta-analysis", "RCT"}:
        evidence_level = "높음"
    elif evidence_type in {"Cohort", "Clinical study", "Safety"}:
        evidence_level = "중간"
    else:
        evidence_level = "낮음"

    if evidence_type in {"Meta-analysis", "RCT"} and not any(term in text for term in ["protocol", "animal", "mice", "rat model"]):
        applicability = "높음"
    elif evidence_type in {"Cohort", "Clinical study", "Safety"}:
        applicability = "보통"
    else:
        applicability = "낮음"

    if evidence_type in {"Meta-analysis", "RCT"} and applicability == "높음":
        priority = "필독"
    elif evidence_level == "중간":
        priority = "참고"
    else:
        priority = "보류"
    return evidence_level, applicability, priority


def extract_safety_flags(title: str, abstract: str) -> list[str]:
    text = f"{title} {abstract}".lower()
    return [term for term in SAFETY_TERMS if term in text]


def efetch(pmids: list[str], limiter: RateLimiter, ncbi_api_key: str | None, email: str | None) -> list[dict[str, Any]]:
    if not pmids:
        return []
    params = {
        **base_params(ncbi_api_key, email),
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
    }
    root = request_xml(f"{BASE_EUTILS}/efetch.fcgi", params, limiter)
    papers: list[dict[str, Any]] = []
    for pubmed_article in root.findall(".//PubmedArticle"):
        pmid, doi = parse_article_ids(pubmed_article)
        article = pubmed_article.find(".//MedlineCitation/Article")
        if article is None or not pmid:
            continue
        title = html.unescape(all_text_at(article, "ArticleTitle"))
        abstract = html.unescape(all_text_at(article, ".//AbstractText"))
        journal = html.unescape(text_at(article, ".//Journal/Title") or text_at(article, ".//Journal/ISOAbbreviation"))
        pub_date = parse_pub_date(article)
        publication_types = [html.unescape("".join(node.itertext()).strip()) for node in article.findall(".//PublicationTypeList/PublicationType")]
        evidence_type = infer_evidence(publication_types, title, abstract)
        evidence_level, clinical_applicability, priority = infer_levels(evidence_type, title, abstract)
        papers.append(
            {
                "id": f"pmid-{pmid}",
                "pmid": pmid,
                "doi": doi,
                "title": " ".join(title.split()),
                "journal": journal,
                "pub_date": pub_date,
                "authors": parse_authors(article),
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "abstract": abstract,
                "title_ko": "",
                "publication_types": publication_types,
                "evidence_type": evidence_type,
                "evidence_level": evidence_level,
                "clinical_applicability": clinical_applicability,
                "priority": priority,
                "safety_flags": extract_safety_flags(title, abstract),
                "summary_status": "missing",
                "summary": empty_summary(),
            }
        )
    return papers


def empty_summary() -> dict[str, Any]:
    return {
        "question": "",
        "methods": "",
        "results": "",
        "clinical_takeaway": "",
        "limitations": "",
        "pico": {"p": "", "i": "", "c": "", "o": ""},
    }


def extract_response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    chunks: list[str] = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "\n".join(chunks)


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.I).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.S)
    if not match:
        raise ValueError("No JSON object found in model response")
    return json.loads(match.group(0))


def normalize_summary(raw: dict[str, Any]) -> dict[str, Any]:
    pico = raw.get("pico") if isinstance(raw.get("pico"), dict) else {}
    return {
        "question": clean_generated_text(raw.get("question", "")),
        "methods": clean_generated_text(raw.get("methods", "")),
        "results": clean_generated_text(raw.get("results", "")),
        "clinical_takeaway": clean_generated_text(raw.get("clinical_takeaway", "")),
        "limitations": clean_generated_text(raw.get("limitations", "")),
        "pico": {
            "p": clean_generated_text(pico.get("p", "")),
            "i": clean_generated_text(pico.get("i", "")),
            "c": clean_generated_text(pico.get("c", "")),
            "o": clean_generated_text(pico.get("o", "")),
        },
    }


def clean_generated_text(value: Any) -> str:
    return (
        str(value)
        .replace("초록에서 명확하지 않음", "명확한 정보 없음")
        .replace("초록에서 명확하지 않습니다", "명확한 정보 없음")
        .strip()
    )


def complete_ai_fields(paper: dict[str, Any]) -> bool:
    summary = paper.get("summary") if isinstance(paper.get("summary"), dict) else {}
    pico = summary.get("pico") if isinstance(summary.get("pico"), dict) else {}
    required_summary = ["question", "methods", "results", "clinical_takeaway", "limitations"]
    required_pico = ["p", "i", "c", "o"]
    return (
        bool(str(paper.get("title_ko", "")).strip())
        and all(bool(str(summary.get(key, "")).strip()) for key in required_summary)
        and all(bool(str(pico.get(key, "")).strip()) for key in required_pico)
    )


def normalize_ai_output(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "title_ko": clean_generated_text(raw.get("title_ko", "")),
        "summary": normalize_summary(raw),
    }


def normalized_existing_ai(paper: dict[str, Any]) -> dict[str, Any]:
    return {
        "title_ko": clean_generated_text(paper.get("title_ko", "")),
        "summary": normalize_summary(paper.get("summary") or {}),
    }


def summarize_with_openai(paper: dict[str, Any], api_key: str, model: str) -> dict[str, Any]:
    prompt = f"""
Title: {paper["title"]}
Journal: {paper["journal"]}
Publication date: {paper.get("pub_date", "")}
Evidence type: {paper["evidence_type"]}
Categories: {", ".join(paper.get("categories", []))}
Abstract:
{paper["abstract"]}
""".strip()
    body = {
        "model": model,
        "input": [
            {
                "role": "developer",
                "content": (
                    "You help Korean medicine doctors study PubMed papers. "
                    "Translate and summarize only from the provided title, metadata, and abstract. "
                    "Write Korean in a concise clinical-learning style. "
                    "Do not give medical advice or treatment instructions. "
                    "Do not invent sample sizes, outcomes, or claims not present in the abstract. "
                    "If a PICO element is unclear, write '명확한 정보 없음'. "
                    "Keep each summary field to one short Korean sentence."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "kmed_paper_summary",
                "strict": True,
                "schema": SUMMARY_RESPONSE_SCHEMA,
            }
        },
        "max_output_tokens": 1200,
    }
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "kmed-evidence-feed/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return normalize_ai_output(parse_json_object(extract_response_text(payload)))


def attach_summaries(papers: list[dict[str, Any]], existing: dict[str, dict[str, Any]], max_summaries: int, skip_ai: bool) -> None:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    completed = 0
    for paper in papers:
        old = existing.get(paper["pmid"], {})
        if old.get("title_ko"):
            paper["title_ko"] = old["title_ko"]
        if old.get("summary_status") == "complete" and complete_ai_fields(old):
            normalized_old = normalized_existing_ai(old)
            paper["title_ko"] = normalized_old["title_ko"]
            paper["summary_status"] = "complete"
            paper["summary"] = normalized_old["summary"]
            continue
        if not paper.get("abstract"):
            paper["summary_status"] = "missing"
            continue
        if skip_ai or not api_key or completed >= max_summaries:
            paper["summary_status"] = "missing"
            continue
        try:
            output = summarize_with_openai(paper, api_key, DEFAULT_MODEL)
            paper["title_ko"] = output["title_ko"]
            paper["summary"] = output["summary"]
            paper["summary_status"] = "complete"
            completed += 1
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            paper["summary_status"] = "error"
            paper["summary_error"] = str(exc)[:240]


def load_dataset_papers(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [paper for paper in data.get("papers", []) if paper.get("pmid")]


def load_existing(path: Path, archive_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    if archive_dir and archive_dir.exists():
        for archive_path in sorted(archive_dir.glob("papers-*.json")):
            for paper in load_dataset_papers(archive_path):
                merged[str(paper["pmid"])] = paper
    for paper in load_dataset_papers(path):
        merged[str(paper["pmid"])] = paper
    return merged


def paper_sort_key(paper: dict[str, Any]) -> tuple[str, str]:
    return (str(paper.get("pub_date") or ""), str(paper.get("pmid") or ""))


def paper_year(paper: dict[str, Any]) -> str:
    match = re.match(r"((?:19|20)\d{2})", str(paper.get("pub_date") or ""))
    return match.group(1) if match else "unknown"


def dataset_for_papers(papers: list[dict[str, Any]], updated: str) -> dict[str, Any]:
    return {
        "updated": updated,
        "total": len(papers),
        "source": "PubMed (NCBI E-utilities)",
        "papers": papers,
    }


def build_archive_outputs(all_papers: list[dict[str, Any]], archive_dir: Path, updated: str) -> tuple[dict[str, Any], dict[Path, dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for paper in all_papers:
        grouped.setdefault(paper_year(paper), []).append(paper)

    archive_files: dict[Path, dict[str, Any]] = {}
    years = []
    for year, papers in sorted(grouped.items(), reverse=True):
        sorted_year_papers = sorted(papers, key=paper_sort_key, reverse=True)
        filename = f"papers-{year}.json"
        years.append({"year": year, "count": len(sorted_year_papers), "file": f"{archive_dir.name}/{filename}"})
        archive_files[archive_dir / filename] = {
            "updated": updated,
            "year": year,
            "total": len(sorted_year_papers),
            "source": "PubMed (NCBI E-utilities)",
            "papers": sorted_year_papers,
        }

    index = {
        "updated": updated,
        "total": len(all_papers),
        "source": "PubMed (NCBI E-utilities)",
        "years": years,
    }
    return index, archive_files


def collect_papers(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], dict[Path, dict[str, Any]]]:
    ncbi_api_key = os.environ.get("NCBI_API_KEY", "").strip() or None
    email = os.environ.get("NCBI_EMAIL", "").strip() or None
    requests_per_second = 9.5 if ncbi_api_key else 2.8
    limiter = RateLimiter(requests_per_second)
    output_path = Path(args.output)
    archive_dir = Path(args.archive_dir)
    existing = load_existing(output_path, archive_dir)
    seen: set[str] = set()
    papers: list[dict[str, Any]] = []

    for category in CATEGORIES:
        pmids = esearch(category, args.category_limit, args.lookback_days, limiter, ncbi_api_key, email)
        for paper in efetch(pmids[: args.category_limit], limiter, ncbi_api_key, email):
            if paper["pmid"] in seen:
                continue
            seen.add(paper["pmid"])
            paper["categories"] = infer_categories(paper["title"], paper["abstract"], category["label"])
            papers.append(paper)
            if args.limit and len(papers) >= args.limit:
                break
        if args.limit and len(papers) >= args.limit:
            break

    papers.sort(key=lambda item: item.get("pub_date") or "", reverse=True)
    attach_summaries(papers, existing, args.max_summaries, args.no_ai)
    merged = dict(existing)
    for paper in papers:
        merged[str(paper["pmid"])] = paper

    all_papers = sorted(merged.values(), key=paper_sort_key, reverse=True)
    latest_limit = args.latest_limit if args.latest_limit > 0 else len(all_papers)
    latest_papers = all_papers[:latest_limit]
    updated = dt.datetime.now(dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    latest_dataset = dataset_for_papers(latest_papers, updated)
    archive_index, archive_files = build_archive_outputs(all_papers, archive_dir, updated)
    return latest_dataset, archive_index, archive_files


def validate_dataset(dataset: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = {
        "id",
        "pmid",
        "doi",
        "title",
        "journal",
        "pub_date",
        "authors",
        "url",
        "abstract",
        "title_ko",
        "categories",
        "evidence_type",
        "evidence_level",
        "clinical_applicability",
        "priority",
        "safety_flags",
        "summary_status",
        "summary",
    }
    seen: set[str] = set()
    for idx, paper in enumerate(dataset.get("papers", []), start=1):
        missing = sorted(required - set(paper))
        if missing:
            errors.append(f"paper {idx} missing fields: {', '.join(missing)}")
        pmid = paper.get("pmid")
        if pmid in seen:
            errors.append(f"duplicate PMID: {pmid}")
        seen.add(pmid)
        if not str(paper.get("url", "")).startswith("https://pubmed.ncbi.nlm.nih.gov/"):
            errors.append(f"broken PubMed URL for PMID {pmid}")
        if not paper.get("pub_date"):
            errors.append(f"empty publication date for PMID {pmid}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Build papers.json for the Korean medicine evidence feed.")
    parser.add_argument("--output", default="papers.json", help="Output JSON file path.")
    parser.add_argument("--archive-dir", default="archive", help="Directory for cumulative archive JSON files.")
    parser.add_argument("--lookback-days", type=int, default=30, help="PubMed publication date lookback window.")
    parser.add_argument("--category-limit", type=int, default=30, help="Maximum PubMed IDs to fetch per category.")
    parser.add_argument("--limit", type=int, default=0, help="Optional total paper limit for testing.")
    parser.add_argument("--latest-limit", type=int, default=50, help="Maximum papers to keep in the latest feed.")
    parser.add_argument("--max-summaries", type=int, default=20, help="Maximum new AI summaries to generate per run.")
    parser.add_argument("--no-ai", action="store_true", help="Skip OpenAI summarization even when OPENAI_API_KEY exists.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and validate without writing output.")
    args = parser.parse_args()

    try:
        dataset, archive_index, archive_files = collect_papers(args)
    except Exception as exc:  # noqa: BLE001 - CLI should report unexpected network/parser failures.
        print(f"Fetch failed: {exc}", file=sys.stderr)
        return 1

    errors = validate_dataset(dataset)
    for archive_path, archive_dataset in archive_files.items():
        errors.extend(f"{archive_path}: {error}" for error in validate_dataset(archive_dataset))
    if errors:
        print("Validation errors:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    output = json.dumps(dataset, ensure_ascii=False, indent=2)
    if args.dry_run:
        print(f"Dry run OK: {dataset['total']} latest papers, {archive_index['total']} archived papers.")
        print(output[:2000])
        return 0

    Path(args.output).write_text(output + "\n", encoding="utf-8")
    archive_dir = Path(args.archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "index.json").write_text(json.dumps(archive_index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for archive_path, archive_dataset in archive_files.items():
        archive_path.write_text(json.dumps(archive_dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}: {dataset['total']} latest papers.")
    print(f"Wrote {archive_dir}: {archive_index['total']} archived papers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
