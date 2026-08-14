---
name: fulcra-watch-together
description: "Compare two consenting people's viewing histories and recommend mutually appealing movies with Fulcra."
---

# Fulcra Watch Together

Build explainable recommendations that are fair to both participants. Treat viewing history as private personal data.

## Dependencies & References

This skill depends on the `fulcra-ingest`, `fulcra-analytics`, and `fulcra-workspaces` skills (fulcradynamics/agent-skills).

## Workflow

1. Identify both participants and whether the request is mutual or single-person.
2. Obtain explicit consent from each participant for the source/export, exact Fulcra data types, date range, analysis recipient, enrichment provider and title disclosure, retention, and any saved output.
3. Never infer consent from a user ID, an existing relationship, or one participant's approval. Never use `--share-all`.
4. Ingest each owner's export independently with `fulcra-ingest`. Prefer Netflix and Letterboxd for the MVP. Preserve provenance and match confidence.
5. Have each owner create a narrow, preferably time-bounded share. Show the exact command before execution. Read collaborator records with `fulcra get-records <TYPE> <RANGE> --user-id <UUID>`.
6. Keep raw histories and title-level feature vectors local. Do not write them to Fulcra workspaces. Explain owner revocation with `fulcra share delete <SHARE_ID>` and recipient departure with `fulcra share leave <SHARE_ID>`.
7. Enrich canonical titles only after both participants approve external title disclosure. Cache provider IDs, media type, year, features, runtime, language, creators, franchise, region availability, provenance, and confidence. Never silently merge ambiguous titles or remakes.
8. For detailed Netflix exports, run `python3 scripts/prepare_netflix.py --input-a A.csv --profile-a PROFILE --input-b B.csv --profile-b PROFILE --catalog catalog.json --output prepared.json`. The approved local catalog must contain canonical watched titles and unseen candidates with feature lists. Inspect coverage and unmatched titles before scoring.
9. Use `--split-profile PROFILE` only to validate the pipeline with one public or consenting history. It creates deterministic chronological pseudo-participants; never describe their output as real compatibility.
10. Use `fulcra-analytics file` or CLI-backed summaries for coverage checks only. Run `python3 scripts/score_candidates.py prepared.json` for ranking.
11. Model explicit ratings as strongest evidence, then rewatches, completed films, and series engagement. Apply mild recency decay. Treat Netflix viewing as implicit positive but ambiguous. Infer negative preference only from explicit low ratings or dislikes.
12. Report overlap, shared favorites, complementary differences, catalog coverage, unmatched titles, and uncertainty. Avoid pseudo-precise compatibility scores for sparse histories.
13. Rank unseen candidates with `0.45 * min(person_a, person_b) + 0.35 * mean(person_a, person_b) + 0.20 * shared_feature_confidence`. Enforce explicit dislike/veto thresholds, then rerank for novelty, diversity, availability, runtime, mood, and franchise fatigue.
14. Return 5–10 recommendations with separate reasons for each participant, confidence, assumptions, and revocation guidance. Save aggregate annotations only with both participants' consent; never persist partner-derived raw features.

## Netflix Preparation Contract

The catalog is local JSON with `titles` and `candidates` arrays. Every row has a canonical `title` and `features`; candidates may also include `confidence`. Add aliases only when the match is unambiguous. The preparation script:

- accepts Netflix's detailed `ViewingActivity.csv` columns;
- rejects missing profiles and histories with fewer than five qualifying sessions;
- drops sessions shorter than five minutes;
- canonicalizes common season/series episode suffixes conservatively;
- weights engagement by total minutes and session count;
- emits scorer-compatible JSON plus a `diagnostics` object;
- keeps source rows and raw titles out of its output.

Do not rank when either participant has under 20% catalog coverage or fewer than two matched canonical titles. Improve the approved catalog or use the sparse-history fallback.

## Fallbacks

- One history: request the second share/export or use a short preference questionnaire.
- Sparse histories: combine questionnaires with a diverse popular shortlist and lower confidence.
- No enrichment consent: use approved local metadata or title/genre evidence and state the limitation.
- Conflicting tastes: offer compromise genres, alternating picks, or explicit veto-safe choices.
- No availability data: omit availability claims.
- No Fulcra access: analyze two explicitly supplied local files without upload.

## Validation

Use synthetic fixtures by default. Test shared favorites, divergent genres, explicit dislikes, rewatches, episodic titles, malformed dates, duplicates, missing metadata, sparse histories, and detailed Netflix columns.

Verify deterministic parsing, symmetric ranking under participant swap, watched-title exclusion, veto behavior, stable provenance, exact share scope, revocation instructions, and absence of raw histories in logs.

Run:

```bash
python3 scripts/prepare_netflix.py --input-a examples/netflix_detailed.csv --profile-a Alex --input-b examples/netflix_detailed.csv --profile-b Blair --catalog examples/netflix_catalog.json --output /tmp/prepared.json
python3 scripts/score_candidates.py /tmp/prepared.json
```
