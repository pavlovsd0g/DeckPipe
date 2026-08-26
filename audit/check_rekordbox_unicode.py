"""Read-only aggregate audit of a copied Rekordbox master.db.

The script intentionally emits counts only: no track titles, artists, paths,
or other library contents are printed.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import unicodedata
from pathlib import Path

from pyrekordbox import Rekordbox6Database


CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
MOJIBAKE_RE = re.compile(r"(?:[РС][\u0400-\u04ff]){3,}")
BAD_GLYPHS = {"\ufffd", "\u25a1", "\u25a0", "\u25af"}


def text(value: object) -> str:
    return "" if value is None else str(value)


def has_bad_glyph(value: object) -> bool:
    return any(char in text(value) for char in BAD_GLYPHS)


def has_mojibake(value: object) -> bool:
    return bool(MOJIBAKE_RE.search(text(value)))


def _is_utf8(value: bytes) -> bool:
    try:
        value.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _utf8_has_cyrillic(value: bytes) -> bool:
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return bool(CYRILLIC_RE.search(decoded))


def _utf8_has_replacement(value: bytes) -> bool:
    try:
        return "\ufffd" in value.decode("utf-8")
    except UnicodeDecodeError:
        return False


def glyph_counts(values: list[str]) -> dict[str, int]:
    return {
        f"U+{ord(char):04X}": sum(value.count(char) for value in values)
        for char in sorted(BAD_GLYPHS)
    }


def extension_counts(rows: list[dict[str, str]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if field == "missing" and row["title"].strip():
            continue
        if field == "bad" and not has_bad_glyph(row["title"]):
            continue
        suffix = Path(row["filename"]).suffix.lower() or "(none)"
        counts[suffix] = counts.get(suffix, 0) + 1
    return dict(sorted(counts.items()))


def value_counts(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = value or "(empty)"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def riff_info_values(path: Path) -> list[bytes]:
    """Return RIFF INFO values without decoding or exposing them."""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return []
    values: list[bytes] = []
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        size = struct.unpack_from("<I", data, offset + 4)[0]
        payload = data[offset + 8 : offset + 8 + size]
        if chunk_id == b"LIST" and payload[:4] == b"INFO":
            inner = 4
            while inner + 8 <= len(payload):
                info_size = struct.unpack_from("<I", payload, inner + 4)[0]
                value = payload[inner + 8 : inner + 8 + info_size].rstrip(b"\x00")
                values.append(value)
                inner += 8 + info_size + (info_size % 2)
        offset += 8 + size + (size % 2)
    return values


def normalized_path(path: Path | str) -> str:
    return str(path).replace("/", "\\").lower()


def sidecar_stats(root: Path | None, bad_rows: list[dict[str, str]]) -> dict[str, object]:
    if root is None:
        return {"checked": False}
    root = root.resolve(strict=True)
    index: dict[str, dict[str, object]] = {}
    sidecar_count = 0
    invalid_sidecars = 0
    for sidecar in root.rglob(".deckpipe.json"):
        sidecar_count += 1
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            invalid_sidecars += 1
            continue
        for entry in data.get("tracks", {}).values():
            filename = entry.get("file")
            if not filename:
                continue
            source = sidecar.parent / str(filename)
            actual = source.with_suffix(".wav") if entry.get("flipped_to") == "wav" else source
            row = {
                "title": text(entry.get("title")),
                "artist": text(entry.get("artist")),
                "flipped": entry.get("flipped_to") == "wav",
                "provider": text(entry.get("provider") or "unknown"),
                "source_ext": source.suffix.lower() or "(none)",
                "source_exists": source.is_file(),
            }
            index[normalized_path(actual)] = row
            index.setdefault(normalized_path(source.with_suffix(".wav")), row)

    matches = [index.get(normalized_path(row["path"])) for row in bad_rows]
    matches = [row for row in matches if row is not None]
    source_ext: dict[str, int] = {}
    providers: dict[str, int] = {}
    for row in matches:
        ext = str(row["source_ext"])
        source_ext[ext] = source_ext.get(ext, 0) + 1
        provider = str(row["provider"])
        providers[provider] = providers.get(provider, 0) + 1
    return {
        "checked": True,
        "sidecar_count": sidecar_count,
        "invalid_sidecars": invalid_sidecars,
        "bad_db_rows_matched": len(matches),
        "matched_flipped_to_wav": sum(bool(row["flipped"]) for row in matches),
        "matched_sidecar_title_cyrillic": sum(
            bool(CYRILLIC_RE.search(str(row["title"]))) for row in matches
        ),
        "matched_sidecar_title_bad_glyph": sum(
            has_bad_glyph(row["title"]) for row in matches
        ),
        "matched_source_still_exists": sum(bool(row["source_exists"]) for row in matches),
        "matched_source_extensions": dict(sorted(source_ext.items())),
        "matched_providers": dict(sorted(providers.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--sidecar-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    database = args.database.resolve(strict=True)
    db = Rekordbox6Database(path=database)
    try:
        contents = list(db.get_content())
        playlists = [p for p in db.get_playlist() if p.Attribute == 0]

        rows: list[dict[str, str]] = []
        for item in contents:
            try:
                artist = text(item.ArtistName)
            except Exception:
                artist = ""
            rows.append(
                {
                    "title": text(item.Title),
                    "artist": artist,
                    "filename": text(item.FileNameL),
                    "path": text(item.FolderPath),
                    "date_created": text(item.DateCreated),
                    "stock_date": text(item.StockDate),
                }
            )

        music_rows = [
            row
            for row in rows
            if row["path"].lower().replace("/", "\\").startswith("e:\\music\\")
        ]
        bad_title_rows = [row for row in rows if has_bad_glyph(row["title"])]
        missing_title_rows = [row for row in rows if not row["title"].strip()]
        bad_wav_info = [riff_info_values(Path(row["path"])) for row in bad_title_rows]
        sidecar_evidence = sidecar_stats(args.sidecar_root, bad_title_rows)
        missing_sidecar_evidence = sidecar_stats(args.sidecar_root, missing_title_rows)
        output = {
            "database_bytes": database.stat().st_size,
            "content_count": len(rows),
            "playlist_count": len(playlists),
            "title_missing": sum(not row["title"].strip() for row in rows),
            "artist_missing": sum(not row["artist"].strip() for row in rows),
            "title_cyrillic": sum(bool(CYRILLIC_RE.search(row["title"])) for row in rows),
            "artist_cyrillic": sum(bool(CYRILLIC_RE.search(row["artist"])) for row in rows),
            "filename_cyrillic": sum(bool(CYRILLIC_RE.search(row["filename"])) for row in rows),
            "title_bad_glyph": sum(has_bad_glyph(row["title"]) for row in rows),
            "artist_bad_glyph": sum(has_bad_glyph(row["artist"]) for row in rows),
            "filename_bad_glyph": sum(has_bad_glyph(row["filename"]) for row in rows),
            "title_bad_glyph_occurrences": glyph_counts([row["title"] for row in rows]),
            "artist_bad_glyph_occurrences": glyph_counts([row["artist"] for row in rows]),
            "bad_title_with_cyrillic_filename": sum(
                has_bad_glyph(row["title"]) and bool(CYRILLIC_RE.search(row["filename"]))
                for row in rows
            ),
            "bad_title_and_bad_artist": sum(
                has_bad_glyph(row["title"]) and has_bad_glyph(row["artist"])
                for row in rows
            ),
            "bad_title_extensions": extension_counts(rows, "bad"),
            "bad_title_date_created": value_counts(
                [row["date_created"] for row in bad_title_rows]
            ),
            "bad_title_stock_date": value_counts([row["stock_date"] for row in bad_title_rows]),
            "bad_title_file_exists": sum(Path(row["path"]).is_file() for row in bad_title_rows),
            "bad_title_wav_with_riff_info": sum(bool(values) for values in bad_wav_info),
            "bad_title_wav_info_utf8_valid": sum(
                bool(values)
                and all(_is_utf8(value) for value in values)
                for values in bad_wav_info
            ),
            "bad_title_wav_info_utf8_cyrillic": sum(
                any(_utf8_has_cyrillic(value) for value in values)
                for values in bad_wav_info
            ),
            "bad_title_wav_info_utf8_replacement": sum(
                any(_utf8_has_replacement(value) for value in values)
                for values in bad_wav_info
            ),
            "bad_title_wav_info_non_ascii": sum(
                any(any(byte > 0x7F for byte in value) for value in values)
                for values in bad_wav_info
            ),
            "missing_title_extensions": extension_counts(rows, "missing"),
            "missing_title_date_created": value_counts(
                [row["date_created"] for row in missing_title_rows]
            ),
            "missing_title_stock_date": value_counts(
                [row["stock_date"] for row in missing_title_rows]
            ),
            "missing_title_sidecar_evidence": missing_sidecar_evidence,
            "title_mojibake_candidates": sum(has_mojibake(row["title"]) for row in rows),
            "artist_mojibake_candidates": sum(has_mojibake(row["artist"]) for row in rows),
            "non_nfc_title": sum(
                bool(row["title"]) and unicodedata.normalize("NFC", row["title"]) != row["title"]
                for row in rows
            ),
            "music_root_content_count": len(music_rows),
            "music_root_title_missing": sum(not row["title"].strip() for row in music_rows),
            "music_root_filename_cyrillic": sum(
                bool(CYRILLIC_RE.search(row["filename"])) for row in music_rows
            ),
            "music_root_title_bad_glyph": sum(has_bad_glyph(row["title"]) for row in music_rows),
            "playlist_name_cyrillic": sum(
                bool(CYRILLIC_RE.search(text(p.Name))) for p in playlists
            ),
            "playlist_name_bad_glyph": sum(has_bad_glyph(p.Name) for p in playlists),
            "sidecar_evidence": sidecar_evidence,
        }
        rendered = json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output is None:
            print(rendered, end="")
        else:
            destination = args.output.resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".tmp")
            temporary.write_text(rendered, encoding="utf-8", newline="\n")
            temporary.replace(destination)
            print(f"RB_UNICODE_JSON {destination}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
