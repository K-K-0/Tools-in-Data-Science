import json
import subprocess
import sys


def fetch_metadata(url: str) -> dict | None:
    """Fetch video metadata via yt-dlp --dump-json. Returns None on failure."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--dump-json", url],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        print(f"Warning: failed to fetch metadata for {url}: {e}", file=sys.stderr)
        return None


def passes_duration(meta: dict, min_s: int, max_s: int) -> bool:
    duration = meta.get("duration")
    if duration is None:
        return False
    return min_s <= duration <= max_s


def combined_text(meta: dict) -> str:
    title = meta.get("title", "") or ""
    description = meta.get("description", "") or ""
    return f"{title} {description}".lower()


def passes_inclusion(meta: dict, required_words: list[str]) -> bool:
    text = combined_text(meta)
    return all(word.lower() in text for word in required_words)


def passes_exclusion(meta: dict, forbidden_words: list[str]) -> bool:
    text = combined_text(meta)
    return not any(word.lower() in text for word in forbidden_words)


def curate_playlist(
    source_urls: list[str],
    min_duration_seconds: int,
    max_duration_seconds: int,
    required_words: list[str],
    forbidden_words: list[str],
    limit: int,
) -> list[str]:
    candidates = []

    for url in source_urls:
        meta = fetch_metadata(url)
        if meta is None:
            continue

        if not passes_duration(meta, min_duration_seconds, max_duration_seconds):
            continue
        if not passes_inclusion(meta, required_words):
            continue
        if not passes_exclusion(meta, forbidden_words):
            continue

        candidates.append({
            "url": url,
            "id": meta.get("id", ""),
            "upload_date": meta.get("upload_date", ""),  # format: YYYYMMDD
        })

    # Sort by upload_date DESC, then id ASC as tiebreaker
    candidates.sort(key=lambda v: (v["upload_date"], v["id"]))  # id ASC, date ASC first
    candidates.sort(key=lambda v: v["upload_date"], reverse=True)  # then date DESC (stable sort preserves id ASC within ties)

    top = candidates[:limit]
    return [v["url"] for v in top]


if __name__ == "__main__":
  
    with open("q-youtube-metadata-filter-server.json") as f:
        params = json.load(f)

    urls = curate_playlist(
        source_urls=params["source_urls"],
        min_duration_seconds=params["min_duration_seconds"],
        max_duration_seconds=params["max_duration_seconds"],
        required_words=params["required_words"],
        forbidden_words=params["forbidden_words"],
        limit=params["limit"],
    )

    output = {"urls": urls}
    with open("output.json", "w") as f:
        json.dump(output, f, indent=2)

    print(json.dumps(output, indent=2))