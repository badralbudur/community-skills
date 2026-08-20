#!/usr/bin/env python3
import argparse
import json
import random
from collections import Counter

def load_catalog(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)

def propose_candidates(catalog_data, count=50):
    """
    Propose a larger candidate shortlist automatically.
    In a full implementation, this could draw from TMDB/OMDb "trending" lists
    or fetch genre-adjacent titles using an API.
    Here we generate candidates based on the most frequent features in the watched titles,
    simulating a broader pool than a hand-curated list.
    """
    watched = catalog_data.get("titles", [])
    if not watched:
        return []
    
    # Extract popular features from watched titles
    feature_counts = Counter()
    for item in watched:
        for feature in item.get("features", []):
            feature_counts[feature] += 1
            
    top_features = [f for f, c in feature_counts.most_common(10)]
    
    # In practice, you would query an external API (like OMDb or TMDB) for titles
    # matching these top_features. Since we cannot make live API calls without a key here,
    # this script acts as a stub to be integrated with `web_fetch` or `curl` via the agent.
    
    proposed = []
    # e.g., proposed.append({"title": "Example Auto-Candidate", "features": top_features[:3], "confidence": 0.8})
    return proposed

def main():
    parser = argparse.ArgumentParser(description="Automatically propose candidate titles based on watched history.")
    parser.add_argument("--catalog", required=True, help="Path to the enriched catalog JSON")
    parser.add_argument("--output", required=True, help="Output path for the expanded catalog JSON")
    args = parser.parse_args()
    
    catalog = load_catalog(args.catalog)
    candidates = catalog.get("candidates", [])
    
    new_candidates = propose_candidates(catalog)
    
    # Merge new candidates, avoiding duplicates
    existing_titles = {c["title"] for c in candidates}
    for nc in new_candidates:
        if nc["title"] not in existing_titles:
            candidates.append(nc)
            existing_titles.add(nc["title"])
            
    catalog["candidates"] = candidates
    
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(catalog, handle, indent=2, sort_keys=True)
        handle.write("\n")
    
    print(f"Proposed {len(new_candidates)} new candidates automatically.")
    print(f"Total candidates in catalog: {len(candidates)}")

if __name__ == "__main__":
    main()
