---
name: spotify-artists-track
description: "Ingests a raw Spotify data export, creating a custom Moment track that tags each listen by the artist name so you can view your listening history categorically."
homepage: "https://github.com/fulcradynamics/community-skills"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🎵" } }
---

# Spotify Artists Track Generator

This skill processes a raw Spotify data export (specifically the `StreamingHistory_music_*.json` files) and creates a computed "Moment Annotation" track in Fulcra. 

Unlike a standard duration ingest, this specialized skill extracts every unique artist you've listened to, dynamically registers them as Fulcra Tags, and then ingests the listening history. This allows the user to filter their timeline by specific artists.

## Prerequisites

1. **The Data:** The user must provide the path to their extracted Spotify data (or have uploaded the zip to their `ingest/` folder).
2. **Authentication:** You must be authenticated to the Fulcra CLI. If you are not, run `uv tool run fulcra-api auth login --get-auth-url` and guide the user through the headless login flow.

## The Workflow

### 1. Schema Creation
Check if the `Spotify Artists` schema already exists by running `uv tool run fulcra-api catalog --user-only`. 
If it does not exist, create it as a `MomentAnnotation`:
```bash
uv tool run fulcra-api data-type create MomentAnnotation "Spotify Artists" --description "com.fulcradynamics.annotation.computed.spotify_artists" --add-to-timeline
```
Capture the returned JSON schema ID (e.g., `com.fulcradynamics.annotation...`).

### 2. Processing and Tagging
Execute the provided Python script `scripts/process_spotify_artists.py` against the user's Spotify JSON files. 

This script:
- Parses the streaming history to extract all unique artist names.
- Uses the `fulcra-api tag list` command to cache all existing tags.
- Safely generates a deterministic UUID for each record.
- Matches the artist name to the tag UUID (case-insensitive) or assigns an empty array if the tag was not found.
- Outputs a fully formatted JSONL file ready for batch ingestion.

*Note: Before running the final ingestion, you must ensure all necessary tags actually exist. You can write a quick loop to run `uv tool run fulcra-api tag create "Artist 1" "Artist 2"` for any unique artists found in the dataset before running the main processing script.*

### 3. Ingestion
Run the batch record command against the generated JSONL file using the Schema ID you captured in Step 1. Because datasets are often large, run this in the background with notifications enabled.
```bash
uv tool run fulcra-api record MomentAnnotation/<SCHEMA_ID> -f output_records.jsonl
```

### 4. Handoff
Notify the user that their data is uploading. Remind them that large datasets (like years of Spotify history) may take several minutes to fully render on the web timeline.
