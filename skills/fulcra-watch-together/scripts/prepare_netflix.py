#!/usr/bin/env python3
import argparse
import csv
import json
import math
import re
from collections import Counter
from datetime import datetime


EPISODE_MARKERS = (
    r": Season \d+:",
    r": Series \d+:",
    r": Limited Series:",
    r": Part \d+:",
)


def duration_seconds(value):
    parts = [int(p) for p in value.split(":")]
    if len(parts) != 3:
        raise ValueError("Duration must be HH:MM:SS")
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def canonicalize(title):
    for pattern in EPISODE_MARKERS:
        match = re.search(pattern, title, flags=re.IGNORECASE)
        if match:
            return title[:match.start()].strip()
    return title.strip()


def parse_time(value):
    for fmt in ("%m/%d/%y %H:%M", "%m/%d/%Y %H:%M", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError("Unsupported Start Time: " + value)


def read_profile(path, profile):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    
    # The instant export only requires Profile Name and Title.
    # The detailed export also includes Start Time and Duration.
    is_detailed = {"Profile Name", "Start Time", "Duration", "Title"}.issubset(rows[0])
    is_instant = {"Profile Name", "Title"}.issubset(rows[0])
    
    if not is_detailed and not is_instant:
        raise ValueError("Expected either detailed or instant Netflix ViewingActivity.csv columns")
        
    selected = []
    for row in rows:
        if row["Profile Name"] != profile:
            continue
            
        if is_detailed:
            seconds = duration_seconds(row["Duration"])
            if seconds < 300:
                continue
            time_val = parse_time(row["Start Time"])
        else:
            # Instant export lacks duration and start time; infer constant weight and epoch time
            seconds = 1800  # Assume 30 mins as a baseline
            time_val = datetime.fromtimestamp(0)
            
        selected.append({
            "canonical": canonicalize(row["Title"]),
            "seconds": seconds,
            "time": time_val,
        })
    if len(selected) < 5:
        raise ValueError("Profile %s has fewer than five qualifying sessions" % profile)
    return selected


def split_profile(rows):
    ordered = sorted(rows, key=lambda row: (row["time"], row["canonical"]))
    midpoint = len(ordered) // 2
    return ordered[:midpoint], ordered[midpoint:]


def load_catalog(path):
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    known = {}
    for row in payload.get("titles", []):
        names = [row["title"]] + row.get("aliases", [])
        for name in names:
            if name in known:
                raise ValueError("Duplicate catalog title or alias: " + name)
            known[name] = row
    candidates = payload.get("candidates", [])
    if not candidates:
        raise ValueError("Catalog needs at least one candidate")
    return known, candidates


def summarize(person_id, rows, known):
    sessions = Counter(row["canonical"] for row in rows)
    minutes = Counter()
    for row in rows:
        minutes[row["canonical"]] += row["seconds"] / 60.0
    features = Counter()
    matched_sessions = 0
    matched_titles = set()
    for title, count in sessions.items():
        item = known.get(title)
        if not item:
            continue
        matched_sessions += count
        matched_titles.add(item["title"])
        weight = math.log1p(minutes[title]) + 0.35 * math.log1p(count)
        for feature in item.get("features", []):
            features[feature] += weight
    total = sum(features.values())
    preferences = {key: value / total for key, value in features.items()} if total else {}
    coverage = matched_sessions / len(rows)
    return {
        "id": person_id,
        "watched": sorted(matched_titles),
        "preferences": preferences,
        "diagnostics": {
            "qualifying_sessions": len(rows),
            "canonical_titles": len(sessions),
            "matched_titles": len(matched_titles),
            "catalog_coverage": round(coverage, 6),
            "unmatched_canonical_titles": len(set(sessions) - set(known)),
        },
    }


def candidate_score(preferences, features):
    if not preferences or not features:
        return 0.5
    affinity = sum(preferences.get(feature, 0.0) for feature in features)
    baseline = 1.0 / max(1, len(preferences))
    return max(0.2, min(0.95, 0.5 + 1.5 * (affinity - baseline)))


def build(args):
    known, candidates = load_catalog(args.catalog)
    if args.split_profile:
        source = read_profile(args.input_a, args.split_profile)
        rows_a, rows_b = split_profile(source)
        ids = ("pseudo-early", "pseudo-late")
        validation_only = True
    else:
        if not args.input_b or not args.profile_a or not args.profile_b:
            raise ValueError("Two inputs/profiles are required unless --split-profile is used")
        rows_a = read_profile(args.input_a, args.profile_a)
        rows_b = read_profile(args.input_b, args.profile_b)
        ids = (args.profile_a, args.profile_b)
        validation_only = False
    people = [summarize(ids[0], rows_a, known), summarize(ids[1], rows_b, known)]
    for person in people:
        d = person["diagnostics"]
        if d["catalog_coverage"] < 0.2 or d["matched_titles"] < 2:
            raise ValueError("Insufficient catalog coverage for " + person["id"])
    ranked_candidates = []
    for item in candidates:
        features = item.get("features", [])
        ranked_candidates.append({
            "title": item["title"],
            "scores": {
                person["id"]: round(candidate_score(person["preferences"], features), 6)
                for person in people
            },
            "shared_feature_confidence": float(item.get("confidence", 0.7)),
        })
    return {
        "veto_threshold": args.veto_threshold,
        "people": [{"id": p["id"], "watched": p["watched"]} for p in people],
        "candidates": ranked_candidates,
        "diagnostics": {
            "validation_only": validation_only,
            "method": "chronological split" if validation_only else "two histories",
            "participants": {p["id"]: p["diagnostics"] for p in people},
            "feature_preferences": {p["id"]: p["preferences"] for p in people},
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-a", required=True)
    parser.add_argument("--profile-a")
    parser.add_argument("--input-b")
    parser.add_argument("--profile-b")
    parser.add_argument("--split-profile")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--veto-threshold", type=float, default=0.2)
    args = parser.parse_args()
    payload = build(args)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(payload["diagnostics"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

