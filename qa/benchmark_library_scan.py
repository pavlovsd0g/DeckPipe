from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import library


def fixture_track(index: int) -> dict[str, object]:
    return {
        "id": str(index),
        "title": f"Title {index:04d}",
        "artist": f"Artist {index:04d}",
        "album": "Synthetic",
        "duration": 180,
    }


def timed_scan(directory: Path, tracks: list[dict[str, object]]) -> tuple[float, list[dict]]:
    started = time.perf_counter()
    result = library.scan_playlist(directory, tracks)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return round(elapsed_ms, 3), result


def run_probe(track_count: int, file_count: int) -> dict[str, object]:
    tracks = [fixture_track(index) for index in range(track_count)]
    with tempfile.TemporaryDirectory(prefix="deckpipe-scan-benchmark-") as temporary:
        root = Path(temporary)
        missing_dir = root / "missing"
        missing_dir.mkdir()
        for index in range(file_count):
            (missing_dir / f"Noise {index:05d}.flac").write_bytes(b"")
        missing_ms, missing = timed_scan(missing_dir, tracks)

        adoption_dir = root / "adoption"
        adoption_dir.mkdir()
        matching_files = min(track_count, file_count)
        for index in range(matching_files):
            (adoption_dir / f"Artist {index:04d} - Title {index:04d}.flac").write_bytes(b"")
        for index in range(matching_files, file_count):
            (adoption_dir / f"Noise {index:05d}.flac").write_bytes(b"")
        adoption_ms, adopted = timed_scan(adoption_dir, tracks)
        warm_ms, warm = timed_scan(adoption_dir, tracks)

        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "mode": "isolated-synthetic-library-scan",
            "dataset": {
                "track_count": track_count,
                "file_count": file_count,
                "estimated_pair_comparisons": track_count * file_count,
            },
            "metrics_ms": {
                "missing_scan": missing_ms,
                "adoption_scan": adoption_ms,
                "warm_sidecar_scan": warm_ms,
            },
            "outcomes": {
                "missing_count": sum(item["status"] == "missing" for item in missing),
                "adopted_ok_count": sum(item["status"] == "ok" for item in adopted),
                "warm_ok_count": sum(item["status"] == "ok" for item in warm),
                "sidecar_utf8": (adoption_dir / library.SIDECAR_NAME)
                .read_bytes()
                .decode("utf-8")
                is not None,
            },
        }


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def write_artifacts(result: dict[str, object], output_directory: Path) -> tuple[Path, Path]:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    stem = f"library-scan-{stamp}"
    json_path = output_directory / f"{stem}.json"
    markdown_path = output_directory / f"{stem}.md"
    metrics = result["metrics_ms"]
    dataset = result["dataset"]
    outcomes = result["outcomes"]
    markdown = "\n".join(
        [
            "# DeckPipe isolated library-scan baseline",
            "",
            f"Generated: {result['generated_at']}",
            "",
            "| Tracks | Files | Estimated comparisons |",
            "|---:|---:|---:|",
            f"| {dataset['track_count']} | {dataset['file_count']} | {dataset['estimated_pair_comparisons']} |",
            "",
            "| Metric | Milliseconds |",
            "|---|---:|",
            f"| Missing scan | {metrics['missing_scan']:.3f} |",
            f"| Adoption scan | {metrics['adoption_scan']:.3f} |",
            f"| Warm sidecar scan | {metrics['warm_sidecar_scan']:.3f} |",
            "",
            "| Outcome | Count |",
            "|---|---:|",
            f"| Missing | {outcomes['missing_count']} |",
            f"| Adopted OK | {outcomes['adopted_ok_count']} |",
            f"| Warm OK | {outcomes['warm_ok_count']} |",
            "",
            "All files were synthetic and created under an automatically removed temporary directory.",
            "",
        ]
    )
    atomic_write(json_path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    atomic_write(markdown_path, markdown)
    return json_path, markdown_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tracks", type=int, default=200)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    if args.tracks <= 0 or args.files <= 0:
        raise SystemExit("tracks and files must be positive")

    result = run_probe(args.tracks, args.files)
    json_path, markdown_path = write_artifacts(result, args.output_directory)
    print(f"BENCH_JSON {json_path.resolve()}")
    print(f"BENCH_MARKDOWN {markdown_path.resolve()}")


if __name__ == "__main__":
    main()
