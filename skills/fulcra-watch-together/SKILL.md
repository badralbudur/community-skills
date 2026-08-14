---
name: fulcra-watch-together
description: "Compare two consenting people's viewing histories and recommend mutually appealing movies with Fulcra."
---

# Fulcra Watch Together

Build explainable recommendations that are fair to both participants. Treat viewing history as private personal data.

## Workflow

1. Identify both participants and whether the request is mutual or single-person.
2. Obtain explicit consent from each participant for:
   - source/export;
   - exact Fulcra data types;
   - date range;
   - analysis recipient;
   - enrichment provider and title disclosure;
   - retention and any saved output.
3. Never infer consent from a user ID, an existing relationship, or one participant's approval. Never use `--share-all`.
4. Ingest each owner's export independently with `fulcra-ingest`. Prefer Netflix and Letterboxd for the MVP. Normalize dates, duplicates, episodic titles, remakes, and locale differences. Preserve provenance and match confidence.
5. Have each owner create a narrow, preferably time-bounded share. Show the exact command before execution. Read collaborator records with `fulcra get-records <TYPE> <RANGE> --user-id <UUID>`.
6. Keep raw histories and title-level feature vectors local. Do not write them to team spaces. Explain owner revocation with `fulcra share delete <SHARE_ID>` and recipient departure with `fulcra share leave <SHARE_ID>`.
7. Enrich canonical titles only after both participants approve any external title disclosure. Cache provider IDs, media type, year, genres, runtime, language, creators, franchise, region availability, provenance, and confidence. Never silently merge ambiguous titles or remakes.
8. Use `fulcra-analytics file` or CLI-backed descriptive summaries for coverage checks only. Use the bundled deterministic scorer for preference and candidate ranking.
9. Model explicit ratings as strongest evidence, then rewatches, completed films, and series engagement. Apply mild recency decay. Treat Netflix viewing as implicit positive but ambiguous. Infer negative preference only from explicit low ratings or dislikes.
10. Report overlap, shared favorites, complementary differences, coverage, and uncertainty. Avoid pseudo-precise compatibility scores for sparse histories.
11. Rank unseen candidates with a fairness-aware objective:
    `0.45 * min(person_a, person_b) + 0.35 * mean(person_a, person_b) + 0.20 * shared_feature_confidence`.
    Enforce explicit dislike/veto thresholds, then rerank for novelty, diversity, availability, runtime, mood, and franchise fatigue.
12. Return 5–10 recommendations with separate reasons for each participant, confidence, assumptions, and revocation guidance. Save aggregate annotations only with both participants' consent; never persist partner-derived raw features.

## Fallbacks

- One history: request the second share/export or use a short preference questionnaire.
- Sparse histories: combine questionnaires with a diverse popular shortlist and lower confidence.
- No enrichment consent: use approved local metadata or title/genre evidence and state the limitation.
- Conflicting tastes: offer compromise genres, alternating picks, or explicit veto-safe choices.
- No availability data: omit availability claims.
- No Fulcra access: analyze two explicitly supplied local files without upload.

## Validation

Use only synthetic fixtures by default. Test shared favorites, divergent genres, explicit dislikes, rewatches, episodic titles, remake collisions, malformed dates, duplicates, missing metadata, and sparse histories.

Verify:

- deterministic parsing and IDs;
- symmetric compatibility and ranking under participant swap;
- exclusion of watched titles unless rewatching is requested;
- veto/fairness behavior;
- stable ranking and provenance;
- exact share scope and time bounds;
- absence of `--share-all`;
- revocation instructions;
- no raw titles or histories in team logs.

Run `python scripts/score_candidates.py examples/synthetic_preferences.json` and inspect the JSON result.
