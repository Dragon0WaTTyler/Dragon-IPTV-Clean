#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES = ROOT / "config" / "iptv_rules.json"
SOURCE_REPO = "https://github.com/mesbahikarim63-commits/hot-dodo"
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
    health_status: str = "not_checked"
    health_checked_urls: int = 0
    health_promoted: bool = False


def load_rules(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def iter_source_files(source_dir: Path, max_files: int | None = None) -> list[Path]:
    files = sorted(
        (p for p in source_dir.rglob(SOURCE_PATTERN) if p.is_file()),
        key=lambda path: path.name,
        reverse=True,
    )
    if max_files and max_files > 0:
        return files[:max_files]
    return files


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


def extract_country_prefix(text: str) -> str | None:
    match = re.match(r"^\s*[^A-Za-z0-9]*([A-Za-z]{2,3})(?=\s*[:|._-])", text)
    return match.group(1).upper() if match else None


def contains_blocked_country_marker(text: str, rules: dict) -> bool:
    markers = rules["language"].get("blocked_country_markers", [])
    if not markers:
        return False
    marker_pattern = "|".join(re.escape(marker) for marker in markers)
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9])(?:{marker_pattern})(?:[\s|:._()\[\]-])*$",
            text,
            flags=re.IGNORECASE,
        )
    )


def infer_language(text: str, rules: dict) -> str | None:
    language_rules = rules["language"]
    if contains_keyword(text, language_rules.get("blocked_language_keywords", [])):
        return None
    if contains_blocked_country_marker(text, rules):
        return None

    prefix = extract_country_prefix(text)
    arabic_prefixes = set(language_rules.get("arabic_prefixes", []))
    english_prefixes = set(language_rules.get("english_prefixes", []))
    if prefix and prefix not in arabic_prefixes | english_prefixes:
        return None

    lowered = text.casefold()
    explicit_english = bool(re.search(r"\b(?:english|eng)\b", lowered))
    explicit_arabic = bool(re.search(r"\b(?:arabic|arab|arabia)\b", lowered)) or contains_arabic_script(text)
    if explicit_english and not explicit_arabic:
        return "english"
    if explicit_arabic and not explicit_english:
        return "arabic"

    if prefix in english_prefixes:
        return "english"
    if prefix in arabic_prefixes:
        return "arabic"

    if explicit_english:
        return "english"
    if explicit_arabic:
        return "arabic"
    if contains_cyrillic(text):
        return None
    if contains_keyword(text, language_rules["arabic_keywords"]):
        return "arabic"
    if contains_keyword(text, language_rules["english_keywords"]):
        return "english"
    return None


def infer_categories(text: str, rules: dict) -> list[str]:
    category_rules = rules["categories"]
    exclusions = category_rules.get("exclude_terms", {})
    detected: list[str] = []
    for category in category_rules["order"]:
        if contains_keyword(text, category_rules[category]) and not contains_keyword(
            text,
            exclusions.get(category, []),
        ):
            detected.append(category)
    return detected


def known_channel_category(name: str, rules: dict) -> str | None:
    identity = channel_identity_key(name)
    category_rules = rules.get("known_channel_categories", {})
    for category in rules["categories"]["order"]:
        aliases = category_rules.get(category, [])
        if any(channel_identity_key(alias) in identity for alias in aliases):
            return category
    return None


def is_vod_entry(name: str, url: str, rules: dict) -> bool:
    cleanup_rules = rules["cleanup"]
    url_path = urlsplit(url).path.casefold()
    if any(marker.casefold() in url_path for marker in cleanup_rules.get("vod_url_markers", [])):
        return True
    if any(url_path.endswith(extension.casefold()) for extension in cleanup_rules.get("vod_extensions", [])):
        return True
    return any(
        re.search(pattern, name, flags=re.IGNORECASE)
        for pattern in cleanup_rules.get("vod_name_patterns", [])
    )


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


def channel_identity_key(value: str) -> str:
    text = normalize_whitespace(value)
    text = re.sub(
        r"^\s*[A-Za-z]{2,3}(?:\s*[.|_-]\s*(?:NEWS|DOCU|SPORTS?))?\s*[:|._-]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\((?:\d{3,4}p|HD|FHD|UHD|4K)\)", " ", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(?:VIP|FHD|UHD|HD|SD|4K|HEVC|H\.26[45]|BACKUP|ALT)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\baljazeera\b", "al jazeera", text, flags=re.IGNORECASE)
    text = re.sub(r"\bmubashar\b", "mubasher", text, flags=re.IGNORECASE)
    return normalize_name_key(normalize_whitespace(text))


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
    playback_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    return family_key, playback_url, parsed.query


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
        primary_category=groups[0] if groups else None,
        name_key=best.name_key,
    )


def record_key(record: ChannelRecord) -> tuple[str, str]:
    return (record.name_key or channel_identity_key(record.name), record.language)


def build_entries(source_dir: Path, rules: dict) -> tuple[list[Entry], Counter]:
    counts = Counter()
    token_params = {value.casefold() for value in rules["cleanup"]["token_query_params"]}
    suffixes = rules["cleanup"]["free_iptv_world_suffixes"]
    drop_terms = [term.casefold() for term in rules["cleanup"]["drop_name_terms"]]
    drop_url_terms = [term.casefold() for term in rules["cleanup"]["drop_url_terms"]]
    unsupported_protocols = [term.casefold() for term in rules["cleanup"]["unsupported_protocols"]]
    entries: list[Entry] = []
    sequence = 0
    max_files = int(rules.get("source_selection", {}).get("max_files", 0)) or None
    for source_file in iter_source_files(source_dir, max_files=max_files):
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

                language_text = searchable_text(original_name, cleaned_name, tvg_name, tvg_id)
                search_text = searchable_text(language_text, raw_group)
                lowered = search_text.casefold()
                if any(url.lower().startswith(protocol) for protocol in unsupported_protocols):
                    counts["dropped_unsupported_protocol"] += 1
                    continue
                if not url.lower().startswith(("http://", "https://")):
                    counts["dropped_unsupported_protocol"] += 1
                    continue
                if any(term in url.casefold() for term in drop_url_terms):
                    counts["dropped_promo"] += 1
                    continue
                if any(term in lowered for term in drop_terms) or "free iptv world promo" in lowered:
                    counts["dropped_promo"] += 1
                    continue
                if is_vod_entry(cleaned_name or original_name, url, rules):
                    counts["dropped_vod"] += 1
                    continue

                language = infer_language(language_text, rules)
                if language not in {"arabic", "english"}:
                    counts["dropped_language"] += 1
                    continue

                categories = infer_categories(search_text, rules)
                known_category = known_channel_category(cleaned_name or original_name, rules)
                if known_category:
                    categories = [known_category]
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
                    name_key=channel_identity_key(cleaned_name or original_name),
                )
                entries.append(entry)
    return entries, counts


def group_by_url(entries: list[Entry]) -> list[ChannelRecord]:
    grouped: dict[tuple[str, str], list[Entry]] = defaultdict(list)
    for entry in entries:
        grouped[(entry.url_key, entry.language)].append(entry)
    records: list[ChannelRecord] = []
    for group_entries in grouped.values():
        records.append(representative_record(group_entries))
    records.sort(key=lambda record: (record.sequence, record.name.casefold(), record.primary_url))
    return records


def merge_by_name(records: list[ChannelRecord]) -> tuple[list[ChannelRecord], int]:
    grouped: dict[tuple[str, str], list[ChannelRecord]] = defaultdict(list)
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
        combined_groups = sorted(
            {group for record in group_records for group in record.groups},
            key=lambda item: CATEGORY_ORDER.index(item) if item in CATEGORY_ORDER else 999,
        )
        merged.append(
            ChannelRecord(
                sequence=min(record.sequence for record in group_records),
                name=best.name,
                original_name=best.original_name,
                language=best.language,
                groups=combined_groups,
                raw_group=next((record.raw_group for record in group_records if record.raw_group), ""),
                logo=best.logo,
                primary_url=primary_url,
                alternates=alternates,
                source_file=best.source_file,
                primary_category=combined_groups[0] if combined_groups else None,
                name_key=best.name_key or channel_identity_key(best.name),
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


@lru_cache(maxsize=4096)
def public_hostname_addresses(hostname: str) -> tuple[bool, str]:
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            }
        except (OSError, UnicodeError):
            return False, "dns_error"
        if not addresses:
            return False, "dns_error"
        try:
            parsed_addresses = [ipaddress.ip_address(address) for address in addresses]
        except ValueError:
            return False, "dns_error"
    else:
        parsed_addresses = [literal]

    if not all(address.is_global for address in parsed_addresses):
        return False, "non_public_address"
    return True, "ok"


def validate_public_http_target(url: str) -> tuple[bool, str]:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return False, "unsupported_scheme"
    if not parsed.hostname:
        return False, "missing_hostname"
    return public_hostname_addresses(parsed.hostname.rstrip(".").casefold())


def is_public_http_target(url: str) -> bool:
    return validate_public_http_target(url)[0]


class SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        allowed, reason = validate_public_http_target(newurl)
        if not allowed:
            raise URLError(f"blocked_redirect:{reason}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_stream_url(url: str, timeout_seconds: float, read_bytes: int) -> tuple[bool, str]:
    allowed, reason = validate_public_http_target(url)
    if not allowed:
        return False, reason

    request = Request(
        url,
        headers={
            "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, video/*, */*;q=0.5",
            "User-Agent": "Dragon-IPTV-Clean/2.1",
        },
    )
    try:
        opener = build_opener(SafeRedirectHandler())
        with opener.open(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", response.getcode())
            if status is not None and int(status) >= 400:
                return False, f"http_{status}"
            sample = response.read(max(read_bytes, 1))
            if not sample:
                return False, "empty_response"
            content_type = response.headers.get("Content-Type", "").casefold()
            stripped_sample = sample.lstrip().lower()
            if "text/html" in content_type or stripped_sample.startswith((b"<!doctype html", b"<html")):
                return False, "html_response"
    except HTTPError as exc:
        return False, f"http_{exc.code}"
    except (TimeoutError, socket.timeout):
        return False, "timeout"
    except (URLError, OSError, ValueError):
        return False, "network_error"
    return True, "ok"


HealthChecker = Callable[[str, float, int], tuple[bool, str]]


def should_health_check(record: ChannelRecord, rules: dict) -> bool:
    health_rules = rules.get("health_check", {})
    categories = set(health_rules.get("categories", []))
    priorities = rules.get("priority_channels", {}).get(record.language, [])
    is_priority = priority_rank(record, rules) < len(priorities)
    return record.primary_category in categories or is_priority


def health_check_records(
    records: list[ChannelRecord],
    rules: dict,
    checker: HealthChecker = check_stream_url,
) -> Counter:
    health_rules = rules.get("health_check", {})
    timeout_seconds = float(health_rules.get("timeout_seconds", 5))
    read_bytes = max(1, int(health_rules.get("read_bytes", 1024)))
    max_urls = max(1, int(health_rules.get("max_urls_per_channel", 3)))
    configured_workers = max(1, int(health_rules.get("max_workers", 40)))
    targets = [record for record in records if should_health_check(record, rules)]
    results = Counter()
    if not targets:
        return results

    def evaluate(record: ChannelRecord) -> tuple[ChannelRecord, str | None, int]:
        candidates = list(dict.fromkeys([record.primary_url, *record.alternates]))[:max_urls]
        checked = 0
        for candidate in candidates:
            checked += 1
            try:
                reachable, _ = checker(candidate, timeout_seconds, read_bytes)
            except Exception:
                reachable = False
            if reachable:
                return record, candidate, checked
        return record, None, checked

    with ThreadPoolExecutor(max_workers=min(configured_workers, len(targets))) as executor:
        futures = [executor.submit(evaluate, record) for record in targets]
        for future in as_completed(futures):
            record, reachable_url, checked = future.result()
            record.health_checked_urls = checked
            results["checked"] += 1
            if reachable_url is None:
                record.health_status = "unreachable"
                results["unreachable"] += 1
                continue

            record.health_status = "reachable"
            results["reachable"] += 1
            if reachable_url != record.primary_url:
                old_primary = record.primary_url
                record.primary_url = reachable_url
                record.alternates = list(dict.fromkeys([old_primary, *record.alternates]))
                record.alternates = [url for url in record.alternates if url != reachable_url]
                record.health_promoted = True
                results["promoted"] += 1
    return results


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
        "health_status": record.health_status,
        "health_checked_urls": record.health_checked_urls,
        "health_promoted": record.health_promoted,
    }
    return payload


def stable_id(record: ChannelRecord) -> str:
    digest = hashlib.sha1(
        f"{record.language}|{record.primary_category or ''}|{record.name_key or channel_identity_key(record.name)}".encode("utf-8")
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
    health_check_enabled: bool,
) -> dict:
    files = {
        "catalog": "dragon_iptv_catalog.json",
        language: f"{language}.m3u",
        "news": "news.m3u",
        "documentary": "documentary.m3u",
        "sports": "sports.m3u",
    }
    return {
        "schema_version": rules["schema_version"],
        "revision_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source_repo": SOURCE_REPO,
        "source_pattern": SOURCE_PATTERN,
        "language": language,
        "generated_by": rules["generated_by"],
        "health_check": {
            "enabled": health_check_enabled,
            "categories": rules.get("health_check", {}).get("categories", []),
            "max_urls_per_channel": rules.get("health_check", {}).get("max_urls_per_channel", 0),
        },
        "limits": rules["limits"][language],
        "counts": {
            "raw_files": counts["raw_files"],
            "raw_channels": counts["raw_channels"],
            "dropped_promo": counts["dropped_promo"],
            "dropped_vod": counts["dropped_vod"],
            "dropped_unsupported_protocol": counts["dropped_unsupported_protocol"],
            "dropped_language": counts["dropped_language"],
            "deduped": counts["deduped"],
            "kept_channels": len(catalog_records),
            language: len(language_records),
            "news": len(category_records["news"]),
            "documentary": len(category_records["documentary"]),
            "sports": len(category_records["sports"]),
            "health_checked": sum(record.health_status != "not_checked" for record in catalog_records),
            "health_reachable": sum(record.health_status == "reachable" for record in catalog_records),
            "health_unreachable": sum(record.health_status == "unreachable" for record in catalog_records),
            "health_promoted": sum(record.health_promoted for record in catalog_records),
        },
        "files": files,
    }


def select_language_records(records: list[ChannelRecord], language: str, limit: int) -> list[ChannelRecord]:
    selected = [record for record in records if record.language == language]
    return selected[:limit]


def priority_rank(record: ChannelRecord, rules: dict) -> int:
    priorities = rules.get("priority_channels", {}).get(record.language, [])
    haystack = record.name_key or channel_identity_key(record.name)
    for index, channel_name in enumerate(priorities):
        if channel_identity_key(channel_name) in haystack:
            return index
    return len(priorities) + 1


def sort_language_records(records: list[ChannelRecord], language: str, rules: dict) -> list[ChannelRecord]:
    selected = [record for record in records if record.language == language]
    return sorted(
        selected,
        key=lambda record: (
            priority_rank(record, rules),
            record.sequence,
            record.name.casefold(),
        ),
    )


def select_category_records(records: list[ChannelRecord], category: str, limit: int, language: str) -> list[ChannelRecord]:
    selected = [
        record
        for record in records
        if record.language == language and record.primary_category == category
    ]
    return selected[:limit]


def build_dist(
    source_dir: Path,
    output_dir: Path,
    rules: dict,
    health_check: bool = False,
    health_checker: HealthChecker | None = None,
) -> dict[str, dict]:
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

        catalog_records = sort_language_records(merged_records, language, rules)[: language_rules["catalog"]]
        if health_check:
            health_check_records(catalog_records, rules, health_checker or check_stream_url)
        language_records = select_language_records(catalog_records, language, language_rules["main"])
        category_records = {
            category: select_category_records(catalog_records, category, language_rules[category], language)
            for category in ("news", "documentary", "sports")
        }

        write_json(language_dir / "dragon_iptv_catalog.json", [record_to_json(record) for record in catalog_records])
        write_m3u(language_dir / f"{language}.m3u", language_records, language)
        for category, records in category_records.items():
            write_m3u(language_dir / f"{category}.m3u", records, category)

        manifest = build_manifest(
            language,
            rules,
            counts,
            catalog_records,
            language_records,
            category_records,
            language_dir,
            health_check,
        )
        write_json(language_dir / "manifest.json", manifest)
        manifests[language] = manifest

    return manifests


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Dragon IPTV Clean dist outputs.")
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument(
        "--health-check",
        action="store_true",
        help="Check priority/news/documentary streams and promote reachable alternates.",
    )
    args = parser.parse_args()

    rules = load_rules(args.rules)
    if args.output_dir.exists():
        shutil.rmtree(args.output_dir)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifests = build_dist(args.source_dir, args.output_dir, rules, health_check=args.health_check)
    if args.health_check:
        for language, manifest in manifests.items():
            health = manifest["counts"]
            print(
                f"{language}: checked={health['health_checked']} "
                f"reachable={health['health_reachable']} "
                f"unreachable={health['health_unreachable']} "
                f"promoted={health['health_promoted']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
