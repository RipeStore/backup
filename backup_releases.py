#!/usr/bin/env python3


import os
import json
import requests
import subprocess
import tempfile
import shutil
from pathlib import Path

GITHUB_API = "https://api.github.com"
BACKUP_REPO = os.environ.get("BACKUP_REPO") or os.environ.get("GITHUB_REPOSITORY")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
ZIP_PASSWORD = os.environ.get("BACKUP_ZIP_PASSWORD")
TARGETS_FILE = Path("targets.json")

if not GITHUB_TOKEN:
    raise RuntimeError("GITHUB_TOKEN environment variable not set")
if not ZIP_PASSWORD:
    raise RuntimeError("BACKUP_ZIP_PASSWORD environment variable not set")
if not TARGETS_FILE.exists():
    raise RuntimeError(f"{TARGETS_FILE} not found in repo root")

HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}


def parse_target(t):
    allow_keys = ["allow_prerelease", "allow_prereleases", "include_prerelease", "include_prereleases", "prerelease"]
    allow = False
    for k in allow_keys:
        if k in t:
            allow = bool(t.get(k))
            break

    if "repo" in t and isinstance(t["repo"], str):
        r = t["repo"].strip()
        if "/" in r:
            owner, repo = r.split("/", 1)
            return owner.strip(), repo.strip(), allow
        else:
            owner = t.get("owner") or t.get("user") or None
            if owner:
                return owner.strip(), r, allow
            return None, None, None
    elif "owner" in t and "repo" in t:
        return t["owner"].strip(), t["repo"].strip(), allow
    else:
        return None, None, None


def normalize_tag(raw_tag: str) -> str:
    if not raw_tag:
        return "unknown"
    rt = str(raw_tag).strip()
    return rt[1:] if rt.lower().startswith("v") else rt


def get_latest_release(owner, repo, allow_prerelease=False):
    if not allow_prerelease:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/releases/latest"
        r = requests.get(url, headers=HEADERS)
        if r.status_code == 200:
            return r.json()
        # fall through if not found
    url = f"{GITHUB_API}/repos/{owner}/{repo}/releases"
    r = requests.get(url, headers=HEADERS)
    if r.status_code != 200:
        print(f"Failed to list releases for {owner}/{repo}: {r.status_code} {r.text}")
        return None
    for rel in r.json():
        if rel.get("draft"):
            continue
        if rel.get("prerelease") and not allow_prerelease:
            continue
        return rel
    return None


def release_exists_in_backup(tag_name):
    url = f"{GITHUB_API}/repos/{BACKUP_REPO}/releases/tags/{tag_name}"
    r = requests.get(url, headers=HEADERS)
    return r.status_code == 200


def download_asset_to_dir(asset, dest_dir):
    name = asset.get("name") or "unnamed"
    dl = asset.get("browser_download_url") or asset.get("url")
    if not dl:
        print(f" - asset {name} has no download url; skipping")
        return None
    out_path = Path(dest_dir) / name
    stream_headers = HEADERS.copy()
    if not asset.get("browser_download_url"):
        stream_headers = {**stream_headers, "Accept": "application/octet-stream"}
    with requests.get(dl, headers=stream_headers, stream=True) as r:
        if r.status_code not in (200, 302, 307):
            print(f" - failed to download {name}: {r.status_code} {r.text}")
            return None
        with open(out_path, "wb") as fh:
            for chunk in r.iter_content(1024 * 32):
                if chunk:
                    fh.write(chunk)
    return out_path


def create_7z_archive(files_dir: Path, archive_path: Path, password: str):
    seven = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
    if not seven:
        raise RuntimeError("7z not found on PATH. Install p7zip-full in your workflow.")
    cmd = [
        seven, "a", "-t7z", str(archive_path),
        str(files_dir) + os.sep,
        f"-p{password}", "-mhe=on", "-mx=9"
    ]
    print("Running 7z:", " ".join(cmd))
    subprocess.check_call(cmd)


def create_github_release_and_upload(tag_name, release_name, archive_path: Path):
    url = f"{GITHUB_API}/repos/{BACKUP_REPO}/releases"
    payload = {"tag_name": tag_name, "name": release_name, "body": "", "draft": False, "prerelease": False}
    r = requests.post(url, headers=HEADERS, json=payload)
    if r.status_code not in (200, 201):
        print(f"Failed to create release {tag_name} in {BACKUP_REPO}: {r.status_code} {r.text}")
        return False
    upload_url = r.json().get("upload_url", "").split("{")[0]
    if not upload_url:
        print("Upload URL missing from release creation response.")
        return False

    mimetype = "application/x-7z-compressed"
    with open(archive_path, "rb") as fh:
        upload_r = requests.post(f"{upload_url}?name={archive_path.name}",
                                 headers={**HEADERS, "Content-Type": mimetype}, data=fh)
    if upload_r.status_code not in (200, 201):
        print(f"Failed to upload {archive_path.name}: {upload_r.status_code} {upload_r.text}")
        return False
    return True


def main():
    with open(TARGETS_FILE, "r", encoding="utf-8") as f:
        targets = json.load(f)

    for t in targets:
        owner, repo_name, allow_prerelease = parse_target(t)
        if not owner or not repo_name:
            print("Skipping invalid target entry (need 'repo': 'owner/repo' or 'owner' + 'repo').")
            continue

        print(f"Checking latest release for {owner}/{repo_name} (allow_prerelease={allow_prerelease})...")
        release = get_latest_release(owner, repo_name, allow_prerelease)
        if not release:
            print(f"No release found for {owner}/{repo_name}.\n")
            continue

        raw_tag = release.get("tag_name") or release.get("name") or "unknown"
        simple_tag = normalize_tag(raw_tag)
        backup_tag = f"{owner}_{repo_name}-v{simple_tag}"
        release_name = backup_tag

        if release_exists_in_backup(backup_tag):
            print(f"Release {backup_tag} already backed up — skipping.\n")
            continue

        print(f"Found new release {raw_tag} -> creating backup {backup_tag} ...")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            downloaded = []
            for a in release.get("assets", []):
                got = download_asset_to_dir(a, tmp_path)
                if got:
                    downloaded.append(got)

            notes_file = tmp_path / "release-notes.txt"
            notes_file.write_text(release.get("body") or "", encoding="utf-8")
            downloaded.append(notes_file)

            archive_path = tmp_path / f"{backup_tag}.7z"
            try:
                create_7z_archive(tmp_path, archive_path, ZIP_PASSWORD)
            except subprocess.CalledProcessError as e:
                print("7z failed:", e)
                continue

            ok = create_github_release_and_upload(backup_tag, release_name, archive_path)
            if ok:
                print(f"Backup for {owner}/{repo_name} ({raw_tag}) completed and uploaded as {archive_path.name}\n")
            else:
                print(f"Failed to upload backup for {owner}/{repo_name} ({raw_tag}).\n")

    print("All targets processed.")


if __name__ == "__main__":
    main()
