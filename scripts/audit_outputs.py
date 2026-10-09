"""Read-only inventory of preserved outputs. Presence is NOT a success-rate metric.

No dependencies. Optional --probe uses a locally installed ffprobe to inspect
stream metadata (not full decode). --report creates a new JSON file exclusively.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import shutil
import subprocess


def scene_info(path: Path, expected: list[str]) -> dict:
    info = {"json_parseable": False, "schema_ok": False, "canonical_labels": False, "duration": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        info["json_parseable"] = True
        scenes = data.get("scenes") if isinstance(data, dict) else data
        if not isinstance(scenes, list) or not scenes or any(not isinstance(x,dict) for x in scenes):
            raise ValueError("Scene list missing or invalid")
        labels = [str(x.get("id", x.get("label", x.get("letter", "")))).strip().upper() for x in scenes]
        durations = []
        for x in scenes:
            raw = x.get("duration") if "duration" in x else float(x["end"])-float(x["start"])
            if isinstance(raw, bool):
                raise ValueError("Boolean duration")
            value = float(raw)
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Non-positive or non-finite duration")
            durations.append(value)
        info.update(schema_ok=True, duration=sum(durations), labels=labels,
                    canonical_labels=labels == expected,
                    missing=[x for x in expected if x not in labels],
                    extra=[x for x in labels if x not in expected],
                    duplicate=[x for x,n in Counter(labels).items() if n>1])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        info["error"] = str(exc)
    return info


def srt_info(path: Path, letters: str) -> dict:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    matches = re.findall(r"\d{2,}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})",text)
    ends = [int(h)*3600+int(m)*60+int(s)+int(ms)/1000 for h,m,s,ms in matches]
    return {"last_cue_end": max(ends) if ends else None,
            "missing_anchor_heuristic": [x for x in letters if not re.search(rf"\b{x}\s+is\s+for\b",text,re.I)]}


def probe(path: Path) -> dict:
    try:
        result = subprocess.run(["ffprobe","-v","error","-show_entries",
            "stream=codec_type,duration:format=duration","-of","json",str(path)],capture_output=True,text=True,timeout=30)
        if result.returncode:
            return {"metadata_readable":False}
        data = json.loads(result.stdout)
        kinds = [x.get("codec_type") for x in data.get("streams",[])]
        return {"metadata_readable":True, "stream_types":kinds,
                "has_audio_and_video":"audio" in kinds and "video" in kinds,
                "duration":data.get("format",{}).get("duration")}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"metadata_readable":False}


def audit(root: Path, letters: str = "ABCDEFGHIJKLMNOPQRSTUVWXYZ", threshold: float = 3, with_probe=False):
    rows, counts, videos_per_job = [], Counter(), Counter()
    directories = sorted(x for x in root.iterdir() if x.is_dir())
    observed_jobs = 0
    for directory in directories:
        job_rows = []
        for version in ("v0", "v1"):
            if (directory / version / "branch.json").exists():
                branch = directory / version
                wav = branch / "audio.wav"
                audio = wav if wav.exists() else branch / "audio.mp3"
                paths = {"audio":audio,"srt":branch/"subtitles.srt","json":branch/"scene_plan.json","video":branch/"final.mp4"}
            else:
                paths = {"audio":directory/f"audio_{version}.mp3","srt":directory/f"audio_{version}.srt",
                         "json":directory/f"audio_{version}.json","video":directory/f"final_video_{version}.mp4"}
            present = {key:path.is_file() for key,path in paths.items()}
            if not any(present.values()):
                continue
            row = {"job":directory.name,"branch":version,"present":present,
                   "bytes":{key:path.stat().st_size for key,path in paths.items() if present[key]}}
            for key, exists in present.items():
                counts[f"{key}_present"] += int(exists)
                counts[f"{key}_nonempty"] += int(exists and paths[key].stat().st_size>0)
            if present["json"]:
                row["scene"] = scene_info(paths["json"],["INTRO",*letters,"OUTRO"])
            if present["srt"]:
                row["transcript"] = srt_info(paths["srt"],letters)
            total = row.get("scene",{}).get("duration")
            end = row.get("transcript",{}).get("last_cue_end")
            row["duration_difference_flag"] = total is not None and end is not None and abs(total-end)>threshold
            if total is not None and end is not None:
                row["scene_minus_last_subtitle_seconds"] = round(total-end,6)
            if with_probe and present["video"]:
                row["video_probe"] = probe(paths["video"])
            job_rows.append(row)
        if job_rows:
            observed_jobs += 1
            videos_per_job[sum(x["present"]["video"] for x in job_rows)] += 1
            rows.extend(job_rows)
    flags = {
        "json_not_parseable": sum(not x["scene"]["json_parseable"] for x in rows if "scene" in x),
        "scene_schema_flags": sum(not x["scene"]["schema_ok"] for x in rows if "scene" in x),
        "noncanonical_scene_sequence": sum(not x["scene"]["canonical_labels"] for x in rows if "scene" in x),
        "srt_anchor_heuristic": sum(bool(x["transcript"]["missing_anchor_heuristic"]) for x in rows if "transcript" in x),
        "duration_difference": sum(x["duration_difference_flag"] for x in rows),
        "video_present_with_noncanonical_scene_sequence": sum(x["present"]["video"] and not x.get("scene",{}).get("canonical_labels",False) for x in rows),
        "video_present_with_duration_difference": sum(x["present"]["video"] and x["duration_difference_flag"] for x in rows),
    }
    return {"interpretation":"Preserved-artifact inventory, NOT attempts, success rate, availability or acoustic quality.",
            "observed_directories":len(directories), "directories_with_artifacts":observed_jobs,
            "observed_branches":len(rows),"artifact_counts":dict(counts),"videos_present_per_job":dict(videos_per_job),
            "flags":flags,"duration_threshold_seconds":threshold,"branches":rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root",type=Path)
    parser.add_argument("--letters",default="ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    parser.add_argument("--threshold",type=float,default=3)
    parser.add_argument("--probe",action="store_true")
    parser.add_argument("--report",type=Path)
    args = parser.parse_args()
    if not args.root.is_dir():
        parser.error("The output directory does not exist")
    if not args.letters or any(x not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for x in args.letters):
        parser.error("--letters must contain uppercase English letters")
    if not math.isfinite(args.threshold) or args.threshold < 0:
        parser.error("--threshold must be finite and nonnegative")
    if args.probe and not shutil.which("ffprobe"):
        parser.error("--probe requires ffprobe on PATH")
    report = audit(args.root,args.letters,args.threshold,args.probe)
    summary = {k:v for k,v in report.items() if k!="branches"}
    print(json.dumps(summary,indent=2))
    if args.report:
        # Exclusive creation avoids accidentally overwriting any historical artifact.
        with args.report.open("x",encoding="utf-8") as stream:
            json.dump(report,stream,indent=2,ensure_ascii=False,allow_nan=False)
        print(f"Created report: {args.report}")
    print("No input files were changed. Missing artifacts have unknown causes.")

if __name__ == "__main__":
    main()
