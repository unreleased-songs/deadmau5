#!/usr/bin/env python3
"""
Audio-Aufräumer
- m4a / opus / wav prüfen: lossless (ALAC, PCM) -> FLAC, lossy (AAC, Opus) -> MP3
- Doppelte Titel (gleicher Dateiname, andere Endung, z. B. song.m4a + song.mp3)
  werden zusammengeführt: die beste Version bleibt, der Rest wird gelöscht.
- Originale werden erst gelöscht, wenn die neue Datei erfolgreich geprüft wurde.

Benötigt: ffmpeg + ffprobe
Nutzung:  python3 audio_aufraeumen.py ORDNER [--dry-run] [--rekursiv] [--aac-behalten]
"""
import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

QUELL_ENDUNGEN = {".m4a", ".opus", ".wav"}
ALLE_ENDUNGEN = QUELL_ENDUNGEN | {".mp3", ".flac"}
LOSSLESS_CODECS = {"alac", "flac", "wavpack", "ape", "tta", "mlp", "truehd"}  # + pcm_*
# AAC/Opus klingen bei gleicher Bitrate besser als MP3 -> grober Gewichtungsfaktor
CODEC_FAKTOR = {"mp3": 1.0, "aac": 1.3, "vorbis": 1.3, "opus": 1.6}
# Eine vorhandene MP3 muss nicht neu kodiert werden (kein weiterer Verlust) -> kleiner Bonus
MP3_BONUS = 1.25


def probe(pfad: Path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(pfad)],
        capture_output=True, text=True)
    if r.returncode != 0:
        return None
    d = json.loads(r.stdout)
    fmt = d.get("format", {})
    audio = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not audio:
        return None
    codec = audio.get("codec_name", "")
    try:
        bits = int(audio.get("bits_per_raw_sample") or audio.get("bits_per_sample") or 16) or 16
    except ValueError:
        bits = 16
    return {
        "pfad": pfad,
        "ext": pfad.suffix.lower(),
        "codec": codec,
        "lossless": codec in LOSSLESS_CODECS or codec.startswith("pcm_"),
        "bitrate": int(audio.get("bit_rate") or fmt.get("bit_rate") or 0),
        "samplerate": int(audio.get("sample_rate") or 0),
        "bits": bits,
        "dauer": float(fmt.get("duration") or audio.get("duration") or 0),
        "container": fmt.get("format_name", ""),
    }


def lossy_score(i):
    s = i["bitrate"] * CODEC_FAKTOR.get(i["codec"], 1.0)
    return s * MP3_BONUS if i["ext"] == ".mp3" else s


def beschreibung(i):
    if i["lossless"]:
        return f'{i["codec"]}, {i["bits"]} bit / {i["samplerate"] // 1000} kHz, lossless'
    return f'{i["codec"]}, {i["bitrate"] // 1000} kbit/s, lossy'


def umwandeln(info, ziel: Path) -> bool:
    tmp = ziel.with_name(ziel.name + ".tmp")
    # Bei Ogg/Opus stecken die Tags im Audiostream, sonst global
    meta = "0:s:a:0" if "ogg" in info["container"] else "0"
    basis = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(info["pfad"]), "-map_metadata", meta]
    if ziel.suffix == ".flac":
        codec, fmt = ["-c:a", "flac", "-compression_level", "8"], "flac"
    else:
        codec, fmt = ["-c:a", "libmp3lame", "-q:a", "0", "-id3v2_version", "3"], "mp3"

    versuche = [
        # 1. Versuch: mit Cover
        basis + ["-map", "0:a:0", "-map", "0:v?", "-c:v", "copy",
                 "-disposition:v", "attached_pic"] + codec + ["-f", fmt, str(tmp)],
        # 2. Versuch: ohne Cover (falls das Cover-Format nicht passt)
        basis + ["-map", "0:a:0", "-vn"] + codec + ["-f", fmt, str(tmp)],
    ]
    for cmd in versuche:
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            neu = probe(tmp)
            # Prüfen: lesbar und gleiche Länge (Toleranz 1 s)
            if neu and abs(neu["dauer"] - info["dauer"]) < 1.0:
                os.replace(tmp, ziel)
                return True
        tmp.unlink(missing_ok=True)
    return False


def ist_gleiche_datei(a: Path, b: Path) -> bool:
    # wichtig auf macOS (Groß-/Kleinschreibung egal): "Song.MP3" == "Song.mp3"
    try:
        return a == b or (a.exists() and b.exists() and os.path.samefile(a, b))
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser(description="m4a/opus/wav -> FLAC/MP3, Duplikate aufräumen")
    ap.add_argument("ordner", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts ändern")
    ap.add_argument("--rekursiv", action="store_true", help="auch Unterordner")
    ap.add_argument("--aac-behalten", action="store_true",
                    help="lossy AAC-m4a nicht zu MP3 umwandeln (vermeidet Qualitätsverlust)")
    args = ap.parse_args()

    if not args.ordner.is_dir():
        sys.exit(f"Ordner nicht gefunden: {args.ordner}")

    dateien = args.ordner.rglob("*") if args.rekursiv else args.ordner.iterdir()
    gruppen = defaultdict(list)
    for p in dateien:
        if p.is_file() and p.suffix.lower() in ALLE_ENDUNGEN:
            gruppen[(p.parent, p.stem)].append(p)

    stats = defaultdict(int)
    for (ordner, stamm), pfade in sorted(gruppen.items()):
        # Nichts zu tun: einzelne mp3/flac ohne Duplikat
        if len(pfade) == 1 and pfade[0].suffix.lower() not in QUELL_ENDUNGEN:
            continue

        infos = []
        for p in pfade:
            i = probe(p)
            if i is None:
                print(f"⚠️  Nicht lesbar, wird ignoriert: {p}")
                stats["fehler"] += 1
            else:
                infos.append(i)
        if not infos:
            continue

        lossless = [i for i in infos if i["lossless"]]
        if lossless:
            # höchste Auflösung gewinnt, bei Gleichstand eine vorhandene FLAC
            beste = max(lossless, key=lambda i: (i["samplerate"] * i["bits"], i["ext"] == ".flac"))
            ziel = ordner / f"{stamm}.flac"
        else:
            beste = max(infos, key=lossy_score)
            if args.aac_behalten and beste["codec"] == "aac" and beste["ext"] == ".m4a":
                ziel = beste["pfad"]
            else:
                ziel = ordner / f"{stamm}.mp3"

        rest = [i for i in infos if i is not beste]
        print(f"\n🎵 {ordner / stamm}")
        print(f"   beste Version: {beste['pfad'].name} ({beschreibung(beste)})")
        for i in rest:
            print(f"   schlechter:    {i['pfad'].name} ({beschreibung(i)})")

        braucht_umwandlung = not (beste["ext"] == ziel.suffix and ist_gleiche_datei(beste["pfad"], ziel))
        if braucht_umwandlung:
            print(f"   -> umwandeln zu {ziel.name}")
            if not args.dry_run:
                if not umwandeln(beste, ziel):
                    print("   ❌ Umwandlung fehlgeschlagen, nichts gelöscht")
                    stats["fehler"] += 1
                    continue
                stats["umgewandelt"] += 1
        else:
            print(f"   -> {ziel.name} bleibt wie sie ist")

        for i in infos:
            if ist_gleiche_datei(i["pfad"], ziel):
                continue
            print(f"   🗑  lösche {i['pfad'].name}")
            if not args.dry_run:
                i["pfad"].unlink()
                stats["gelöscht"] += 1

    print("\n" + ("(Dry-Run, nichts geändert) " if args.dry_run else "")
          + f"Umgewandelt: {stats['umgewandelt']}, gelöscht: {stats['gelöscht']}, Fehler: {stats['fehler']}")


if __name__ == "__main__":
    main()