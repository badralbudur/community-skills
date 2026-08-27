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

def process_files(files_to_process, output_file):
    # TODO: Set these variables based on the specific data source
    source_identifier = "com.custom.computed_source"
    remote_path = "local_upload"
    agent_id = "agent.orchestrator"
    sources = [source_identifier, remote_path, agent_id]

    print("Fetching all existing tags from Fulcra...", file=sys.stderr)
    tag_cache = {}
    try:
        result = subprocess.run(["uvx", "fulcra-api", "tag", "list"], capture_output=True, text=True, check=True)
        tags_data = json.loads(result.stdout)
        for t in tags_data:
            tag_cache[t["name"].lower()] = t["id"]
        print(f"Loaded {len(tag_cache)} tags into cache.", file=sys.stderr)
    except Exception as e:
        print(f"Failed to fetch tags: {e}", file=sys.stderr)
        sys.exit(1)

    records = []
    # TODO: Agent must implement the custom parsing loop here
    # Example flow:
    # 1. Open each file and parse JSON/CSV
    # 2. Extract timestamp, tag_name (e.g. Artist, Genre), and note (e.g. Track, Movie)
    # 3. Format timestamp to ISO 8601 UTC
    # 4. id_fields = [source_identifier, timestamp, tag_name, note]
    # 5. record_id = generate_deterministic_uuid(id_fields)
    # 6. tag_id = tag_cache.get(tag_name.lower())
    # 7. Append to `records` array.
    
    # ------------------
    # CUSTOM PARSING LOGIC HERE
    # ------------------
    
    print(f"Writing {len(records)} records to {output_file}...", file=sys.stderr)
    with open(output_file, 'w') as out:
        for r in records:
            out.write(json.dumps(r) + "\n")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python process_custom.py <output_jsonl> <input_file1> [input_file2 ...]", file=sys.stderr)
        sys.exit(1)
        
    output_file = sys.argv[1]
    input_files = sys.argv[2:]
    process_files(input_files, output_file)
