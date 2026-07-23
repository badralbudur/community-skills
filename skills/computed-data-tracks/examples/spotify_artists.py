import json
import datetime
import hashlib
import uuid
import sys
import subprocess
import os

def generate_deterministic_uuid(fields):
    combined = "||".join(str(f) for f in fields)
    m = hashlib.md5()
    m.update(combined.encode('utf-8'))
    return str(uuid.UUID(bytes=m.digest()))

def process_spotify_files(files_to_process, output_file):
    source_identifier = "com.spotify.computed_artists"
    remote_path = "local_upload"
    agent_id = "agent.orchestrator"
    sources = [source_identifier, remote_path, agent_id]

    print("Fetching all existing tags from Fulcra...", file=sys.stderr)
    tag_cache = {}
    try:
        result = subprocess.run(["uv", "tool", "run", "fulcra-api", "tag", "list"], capture_output=True, text=True, check=True)
        tags_data = json.loads(result.stdout)
        for t in tags_data:
            tag_cache[t["name"].lower()] = t["id"]
        print(f"Loaded {len(tag_cache)} tags into cache.", file=sys.stderr)
    except Exception as e:
        print(f"Failed to fetch tags: {e}", file=sys.stderr)
        sys.exit(1)

    records = []
    for filepath in files_to_process:
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
        except Exception as e:
            print(f"Error reading {filepath}: {e}", file=sys.stderr)
            continue
        
        for item in data:
            end_time_str = item.get("endTime")
            artist_name = item.get("artistName", "")
            track_name = item.get("trackName", "")
            
            if not end_time_str or not artist_name:
                continue
                
            try:
                if len(end_time_str) == 16:
                    end_dt = datetime.datetime.strptime(end_time_str, "%Y-%m-%d %H:%M")
                else:
                    end_dt = datetime.datetime.strptime(end_time_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
                
            end_dt = end_dt.replace(tzinfo=datetime.timezone.utc)
            iso_time = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            
            id_fields = [source_identifier, end_time_str, artist_name, track_name]
            record_id = generate_deterministic_uuid(id_fields)
            
            tag_id = tag_cache.get(artist_name.lower())
            
            records.append({
                "id": record_id,
                "tags": [tag_id] if tag_id else [],
                "recorded_at": iso_time,
                "sources": sources,
                "note": track_name
            })

    print(f"Writing {len(records)} records to {output_file}...", file=sys.stderr)
    with open(output_file, 'w') as out:
        for r in records:
            out.write(json.dumps(r) + "\n")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python spotify_artists.py <output_jsonl> <input_json1> [input_json2 ...]", file=sys.stderr)
        sys.exit(1)
        
    output_file = sys.argv[1]
    input_files = sys.argv[2:]
    process_spotify_files(input_files, output_file)
