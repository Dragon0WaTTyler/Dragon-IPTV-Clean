#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES = ROOT / "config" / "iptv_rules.json"
SOURCE_REPO = "https://github.com/mesbahikarim03-svg/krimo.-Iptv"
SOURCE_PATTERN = "FIW_17*.m3u"
CATEGORY_ORDER: list[str] = []


@dataclass
class Entry:
    sequence: int
    source_file: str
    original_name: str
    cleaned_name: str
    tvg_name: str
    tvg_id: str
    logo: str
    raw_group: str
    language: str
    categories: list[str]
    primary_category: str | None
    url: str
    url_key: str
    name_key: str


@dataclass
class ChannelRecord:
    sequence: int
    name: str
    original_name: str
    language: str
    groups: list[str]
    raw_group: str
    logo: str
    primary_url: str
    alternates: list[str] = field(default_factory=list)
    source_file: str = ""
    primary_category: str | None = None
    name_key: str = ""


def load_rules(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def iter_source_files(source_dir: Path) -> list[Path]:
    return sorted(
        p for p in source_dir.rglob(SOURCE_PATTERN) if p.is_file()
    )


def find_unquoted_comma(value: str) -> int:
    in_quotes = False
    escape = False
    for index, char in enumerate(value):
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_quotes = not in_quotes
            continue
        if char == "," and not in_quotes:
            return index
    return -1


def parse_extinf(line: str) -> tuple[dict[str, str], str]:
    body = line.strip()
    if body.upper().startswith("\ufeff#EXTINF"):
        body = body.lstrip("\ufeff")
    if not body.startswith("#EXTINF:"):
        return {}, ""
    payload = body[len("#EXTINF:") :]
    comma_index = find_unquoted_comma(payload)
    if comma_index == -1:
        attr_blob = payload
        display_name = ""
    else:
        attr_blob = payload[:comma_index]
        display_name = payload[comma_index + 1 :].strip()
    attrs = {}
    for key, value in re.findall(r'([A-Za-z0-9_-]+)="([^"]*)"', attr_blob):
        attrs[key.lower()] = value
    return attrs, display_name


def normalize_whitespace(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def strip_suffixes(value: str, suffixes: list[str]) -> str:
    result = value
    for suffix in suffixes:
        result = result.replace(suffix, "")
    return result


def clean_name(value: str, suffixes: list[str]) -> str:
    cleaned = normalize_whitespace(value)
    cleaned = strip_suffixes(cleaned, suffixes)
    cleaned = normalize_whitespace(cleaned)
    cleaned = re.sub(r"\s*\|\s*$", "", cleaned)
    cleaned = re.sub(r"^\s*[\-\|]+\s*", "", cleaned)
    cleaned = re.sub(r"\s*[\-\|]+\s*$", "", cleaned)
    return normalize_whitespace(cleaned)


def searchable_text(*values: str) -> str:
    return normalize_whitespace(" ".join(v for v in values if v))


def contains_arabic_script(value: str) -> bool:
    return bool(
        re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]", value)
    )


def contains_cyrillic(value: str) -> bool:
    return bool(re.search(r"[\u0400-\u04FF]", value))


def contains_keyword(text: str, keywords: Iterable[str]) -> bool:
    haystack = text.casefold()
    for keyword in keywords:
        if keyword.casefold() in haystack:
            return True
    return False


def infer_language(text: str, rules: dict) -> str | None:
    language_rules = rules["language"]
    if contains_keyword(text, language_rules.get("blocked_language_keywords", [])):
        return None
    if contains_arabic_script(text) or contains_keyword(text, language_rules["arabic_keywords"]):
        return "arabic"
    if contains_cyrillic(text):
        return None
    if contains_keyword(text, language_rules["english_keywords"]):
        return "english"
    if re.search(r"[A-Za-z]", text) and not re.search(r"[\u0370-\u1FFF\u2C00-\uD7FF]", text):
        return None
    return None


def infer_categories(text: str, rules: dict) -> list[str]:
    category_rules = rules["categories"]
    detected: list[str] = []
    for category in category_rules["order"]:
        if contains_keyword(text, category_rules[category]):
            detected.append(category)
    return detected


def normalize_name_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    parts: list[str] = []
    previous_space = False
    for char in text:
        category = unicodedata.category(char)
        if category.startswith("L") or category.startswith("N"):
            parts.append(char)
            previous_space = False
        elif char.isspace():
            if not previous_space:
                parts.append(" ")
            previous_space = True
    return "".join(parts).strip()


def canonicalize_url(url: str, token_params: set[str]) -> tuple[str, str, str]:
    raw = url.strip()
    raw = raw.split("#", 1)[0]
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    if "@" in netloc:
        userinfo, hostpart = netloc.rsplit("@", 1)
        netloc = f"{userinfo}@{hostpart}"
    host, sep, port = netloc.rpartition(":")
    if sep and port.isdigit():
        if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
            netloc = host
    filtered_query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.casefold() in token_params:
            continue
        filtered_query.append((key, value))
    canonical_query = urlencode(filtered_query, doseq=True)
    family_key = urlunsplit(("", netloc, parsed.path, canonical_query, ""))
    normalized_url = urlunsplit((scheme, netloc, parsed.path, canonical_query, ""))
    return family_key, normalized_url, parsed.query


def url_sort_key(url: str) -> tuple[int, int, int, int, str]:
    parsed = urlsplit(url)
    scheme_score = 1 if parsed.scheme.lower() == "https" else 0
    path = parsed.path.lower()
    m3u8_score = 1 if path.endswith(".m3u8") or ".m3u8" in path else 0
    no_token_score = 1 if not parsed.query else 0
    shorter = -len(url)
    return (scheme_score, m3u8_score, no_token_score, shorter, url)


def entry_quality(entry: Entry) -> tuple[int, int, int, int, int, str]:
    name = entry.cleaned_name or entry.original_name or entry.tvg_name
    letters = sum(1 for char in name if char.isalnum() or contains_arabic_script(char))
    punctuation = sum(1 for char in name if not (char.isalnum() or char.isspace() or contains_arabic_script(char)))
    category_bonus = len(entry.categories)
    https_bonus = 1 if urlsplit(entry.url).scheme.lower() == "https" else 0
    m3u8_bonus = 1 if urlsplit(entry.url).path.lower().endswith(".m3u8") else 0
    query_penalty = len(urlsplit(entry.url).query)
    return (
        category_bonus,
        https_bonus,
        m3u8_bonus,
        letters,
        -punctuation,
        -query_penalty,
        name.casefold(),
    )


def representative_record(entries: list[Entry]) -> ChannelRecord:
    best = max(entries, key=entry_quality)
    urls = sorted({entry.url for entry in entries}, key=url_sort_key, reverse=True)
    primary_url = urls[0]
    alternates = [url for url in urls[1:]]
    category_order = CATEGORY_ORDER
    groups = sorted(
        {category for entry in entries for category in entry.categories},
        key=lambda item: category_order.index(item) if item in category_order else 999,
    )
    raw_groups = [entry.raw_group for entry in entries if entry.raw_group]
    return ChannelRecord(
        sequence=min(entry.sequence for entry in entries),
        name=best.cleaned_name or best.original_name,
        original_name=best.original_name or best.cleaned_name,
        language=best.language,
        groups=groups,
        raw_group=raw_groups[0] if raw_groups else "",
        logo=best.logo,
        primary_url=primary_url,
        alternates=alternates,
        source_file=best.source_file,
        primary_category=best.primary_category,
        name_key=best.name_key,
    )


def record_key(record: ChannelRecord) -> tuple[str, str, str]:
    primary_category = record.primary_category or ""
    return (normalize_name_key(record.name), record.language, primary_category)


def build_entries(source_dir: Path, rules: dict) -> tuple[list[Entry], Counter]:
    counts = Counter()
    token_params = {value.casefold() for value in rules["cleanup"]["token_query_params"]}
    suffixes = rules["cleanup"]["free_iptv_world_suffixes"]
    drop_terms = [term.casefold() for term in rules["cleanup"]["drop_name_terms"]]
    drop_url_terms = [term.casefold() for term in rules["cleanup"]["drop_url_terms"]]
    unsupported_protocols = [term.casefold() for term in rules["cleanup"]["unsupported_protocols"]]
    entries: list[Entry] = []
    sequence = 0
    for source_file in iter_source_files(source_dir):
        counts["raw_files"] += 1
        with source_file.open("r", encoding="utf-8-sig", errors="replace") as fh:
            pending_attrs: dict[str, str] | None = None
            pending_display = ""
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("#EXTINF"):
                    pending_attrs, pending_display = parse_extinf(stripped)
                    continue
                if stripped.startswith("#"):
                    continue
                if pending_attrs is None:
                    continue
                url = normalize_whitespace(stripped)
                attrs = pending_attrs
                pending_attrs = None

                sequence += 1
                counts["raw_channels"] += 1

                tvg_id = normalize_whitespace(attrs.get("tvg-id", ""))
                tvg_name = normalize_whitespace(attrs.get("tvg-name", ""))
                tvg_logo = normalize_whitespace(attrs.get("tvg-logo", ""))
                raw_group = normalize_whitespace(attrs.get("group-title", ""))
                original_name = normalize_whitespace(pending_display or tvg_name or tvg_id or "")
                cleaned_name = clean_name(original_name, suffixes)

                search_text = searchable_text(original_name, cleaned_name, tvg_name, tvg_id, raw_group)
                lowered = search_text.casefold()
                if any(url.lower().startswith(protocol) for protocol in unsupported_protocols):
                    counts["dropped_unsupported_protocol"] += 1
                    continue
                if not url.lower().startswith(("http://", "https://")):
                    counts["dropped_unsupported_protocol"] += 1
                    continue
                if any(term in lowered for term in drop_url_terms):
                    counts["dropped_promo"] += 1
                    continue
                if any(term in lowered for term in drop_terms) or "free iptv world promo" in lowered:
                    counts["dropped_promo"] += 1
                    continue

                language = infer_language(search_text, rules)
                if language not in {"arabic", "english"}:
                    counts["dropped_language"] += 1
                    continue

                categories = infer_categories(search_text, rules)
                primary_category = categories[0] if categories else None

                family_key, normalized_url, _ = canonicalize_url(url, token_params)
                entry = Entry(
                    sequence=sequence,
                    source_file=source_file.name,
                    original_name=original_name or cleaned_name,
                    cleaned_name=cleaned_name or original_name,
                    tvg_name=tvg_name,
                    tvg_id=tvg_id,
                    logo=tvg_logo,
                    raw_group=raw_group,
                    language=language,
                    categories=categories,
                    primary_category=primary_category,
                    url=normalized_url,
                    url_key=family_key,
                    name_key=normalize_name_key(cleaned_name or original_name),
                )
                entries.append(entry)
    return entries, counts


def group_by_url(entries: list[Entry]) -> list[ChannelRecord]:
    grouped: dict[str, list[Entry]] = defaultdict(list)
    for entry in entries:
        grouped[entry.url_key].append(entry)
    records: list[ChannelRecord] = []
    for group_entries in grouped.values():
        records.append(representative_record(group_entries))
    records.sort(key=lambda record: (record.sequence, record.name.casefold(), record.primary_url))
    return records


def merge_by_name(records: list[ChannelRecord]) -> tuple[list[ChannelRecord], int]:
    grouped: dict[tuple[str, str, str], list[ChannelRecord]] = defaultdict(list)
    for record in records:
        grouped[record_key(record)].append(record)
    merged: list[ChannelRecord] = []
    deduped = 0
    for group_records in grouped.values():
        if len(group_records) > 1:
            deduped += len(group_records) - 1
        best = max(group_records, key=lambda record: (
            len(record.groups),
            1 if urlsplit(record.primary_url).scheme.lower() == "https" else 0,
            1 if urlsplit(record.primary_url).path.lower().endswith(".m3u8") else 0,
            -len(record.name),
            -len(record.primary_url),
            record.name.casefold(),
        ))
        urls: list[str] = []
        seen = set()
        for record in sorted(group_records, key=lambda item: (item.sequence, item.name.casefold())):
            if record.primary_url not in seen:
                urls.append(record.primary_url)
                seen.add(record.primary_url)
            for alternate in record.alternates:
                if alternate not in seen:
                    urls.append(alternate)
                    seen.add(alternate)
        urls.sort(key=url_sort_key, reverse=True)
        primary_url = urls[0]
        alternates = urls[1:]
        merged.append(
            ChannelRecord(
                sequence=min(record.sequence for record in group_records),
                name=best.name,
                original_name=best.original_name,
                language=best.language,
                groups=sorted(
                    {group for record in group_records for group in record.groups},
                    key=lambda item: CATEGORY_ORDER.index(item) if item in CATEGORY_ORDER else 999,
                ),
                raw_group=next((record.raw_group for record in group_records if record.raw_group), ""),
                logo=best.logo,
                primary_url=primary_url,
                alternates=alternates,
                source_file=best.source_file,
                primary_category=best.primary_category,
                name_key=best.name_key or normalize_name_key(best.name),
            )
        )
    merged.sort(key=lambda record: (record.sequence, record.name.casefold(), record.primary_url))
    return merged, deduped


def write_m3u(path: Path, records: list[ChannelRecord], group_title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("#EXTM3U\n")
        for record in records:
            tvg_name = escape_attr(record.name)
            tvg_logo = escape_attr(record.logo)
            display_name = record.name.replace("\n", " ").replace("\r", " ").strip()
            fh.write(
                f'#EXTINF:-1 tvg-name="{tvg_name}" tvg-logo="{tvg_logo}" group-title="{escape_attr(group_title)}",{display_name}\n'
            )
            fh.write(f"{record.primary_url}\n")


def escape_attr(value: str) -> str:
    return normalize_whitespace(value).replace("\\", "\\\\").replace('"', '\\"')


def record_to_json(record: ChannelRecord) -> dict:
    payload = {
        "id": stable_id(record),
        "name": record.name,
        "original_name": record.original_name,
        "language": record.language,
        "groups": record.groups,
        "raw_group": record.raw_group,
        "logo": record.logo,
        "primary_url": record.primary_url,
        "alternates": record.alternates,
        "source_file": record.source_file,
    }
    return payload


def stable_id(record: ChannelRecord) -> str:
    digest = hashlib.sha1(
        f"{record.language}|{record.primary_category or ''}|{record.name.casefold()}|{record.primary_url}".encode("utf-8")
    ).hexdigest()[:12]
    return f"dragon_{record.language}_{digest}"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def build_manifest(
    language: str,
    rules: dict,
    counts: Counter,
    catalog_records: list[ChannelRecord],
    language_records: list[ChannelRecord],
    category_records: dict[str, list[ChannelRecord]],
    output_dir: Path,
) -> dict:
    files = {
        "catalog": "dragon_iptv_catalog.json",
        language: f"{language}.m3u",
        "news": "news.m3u",
        "documentary": "documentary.m3u",
        "sports": "sports.m3u",
        "netflix": "netflix.m3u",
    }
    return {
        "schema_version": rules["schema_version"],
        "revision_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source_repo": SOURCE_REPO,
        "source_pattern": SOURCE_PATTERN,
        "language": language,
        "generated_by": rules["generated_by"],
        "limits": rules["limits"][language],
        "counts": {
            "raw_files": counts["raw_files"],
            "raw_channels": counts["raw_channels"],
            "dropped_promo": counts["dropped_promo"],
            "dropped_unsupported_protocol": counts["dropped_unsupported_protocol"],
            "dropped_language": counts["dropped_language"],
            "deduped": counts["deduped"],
            "kept_channels": len(catalog_records),
            language: len(language_records),
            "news": len(category_records["news"]),
            "documentary": len(category_records["documentary"]),
            "sports": len(category_records["sports"]),
            "netflix": len(category_records["netflix"]),
        },
        "files": files,
    }


def select_language_records(records: list[ChannelRecord], language: str, limit: int) -> list[ChannelRecord]:
    selected = [record for record in records if record.language == language]
    return selected[:limit]


def select_category_records(records: list[ChannelRecord], category: str, limit: int, language: str) -> list[ChannelRecord]:
    selected = [
        record
        for record in records
        if record.language == language and record.primary_category == category
    ]
    return selected[:limit]


def build_dist(source_dir: Path, output_dir: Path, rules: dict) -> dict[str, dict]:
    entries, counts = build_entries(source_dir, rules)
    global CATEGORY_ORDER
    CATEGORY_ORDER = list(rules["categories"]["order"])
    url_records = group_by_url(entries)
    merged_records, deduped = merge_by_name(url_records)
    counts["deduped"] = deduped

    manifests = {}

    for language in ("arabic", "english"):
        language_rules = rules["limits"][language]
        language_dir = output_dir / language
        if language_dir.exists():
            shutil.rmtree(language_dir)
        language_dir.mkdir(parents=True, exist_ok=True)

        catalog_records = [record for record in merged_records if record.language == language][: language_rules["catalog"]]
        language_records = select_language_records(catalog_records, language, language_rules["main"])
        category_records = {
            category: select_category_records(catalog_records, category, language_rules[category], language)
            for category in ("news", "documentary", "sports", "netflix")
        }

        write_json(language_dir / "dragon_iptv_catalog.json", [record_to_json(record) for record in catalog_records])
        write_m3u(language_dir / f"{language}.m3u", language_records, language)
        for category, records in category_records.items():
            write_m3u(language_dir / f"{category}.m3u", records, category)

        manifest = build_manifest(language, rules, counts, catalog_records, language_records, category_records, language_dir)
        write_json(language_dir / "manifest.json", manifest)
        manifests[language] = manifest

    return manifests


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Dragon IPTV Clean dist outputs.")
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    args = parser.parse_args()

    rules = load_rules(args.rules)
    if args.output_dir.exists():
        shutil.rmtree(args.output_dir)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    build_dist(args.source_dir, args.output_dir, rules)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
