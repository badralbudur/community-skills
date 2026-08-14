#!/usr/bin/env python3
import json
import sys


def rank(payload):
    people = payload["people"]
    if len(people) != 2:
        raise ValueError("exactly two people are required")
    veto = float(payload.get("veto_threshold", 0.2))
    watched = set(people[0].get("watched", [])) | set(people[1].get("watched", []))
    rows = []
    for candidate in payload["candidates"]:
        title = candidate["title"]
        if title in watched and not candidate.get("allow_rewatch", False):
            continue
        scores = [float(candidate["scores"][p["id"]]) for p in people]
        if min(scores) < veto:
            continue
        confidence = float(candidate.get("shared_feature_confidence", 0.5))
        score = 0.45 * min(scores) + 0.35 * (sum(scores) / 2) + 0.20 * confidence
        rows.append({
            "title": title,
            "score": round(score, 6),
            "person_scores": {people[i]["id"]: scores[i] for i in range(2)},
            "shared_feature_confidence": confidence,
        })
    return sorted(rows, key=lambda x: (-x["score"], x["title"]))


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as handle:
        print(json.dumps(rank(json.load(handle)), indent=2, sort_keys=True))
