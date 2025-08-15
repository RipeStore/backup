#!/usr/bin/env python3
"""Backup upstream releases into this repo's releases as passworded zips.

Behavior summary:
- Read ./targets.json (list of {owner, repo, include_prereleases})
- For each repo, fetch its latest release (optionally allow prereleases)
- If a backup release with tag `{repo}-{tag}` already exists in BACKUP_REPO, skip
- Download assets + write release-notes.txt
- Create an encrypted zip (prefers 7z AES256 if available, falls back to zip -P)
- Create a release in BACKUP_REPO and upload the zip

Environment variables used:
- GITHUB_TOKEN (required) -- use the Actions-provided token or a PAT with repo scope
- BACKUP_ZIP_PASSWORD (required) -- repo secret for the zip encryption password
- BACKUP_REPO (optional) -- publish backups to this owner/repo (defaults to the current repo)

"""

import os
import sys
import json
import requests
import tempfile
import shutil
import subprocess
from pathlib import Path

GITHUB_API = "https://api.github.com"

TOKEN = os.environ.get("GITHUB_TOKEN")
if not TOKEN:
    print("ERROR: GITHUB_TOKEN is required via env (use Actions secrets).", file=sys.stderr)
    sys.exit(2)

ZIP_PASSWORD = os.environ.get("BACKUP_ZIP_PASSWORD")
if not ZIP_PASSWORD:
    print("ERROR: BACKUP_ZIP_PASSWORD must be set as a repository secret.", file=sys.stderr)
    sys.exit(2)

BACKUP_REPO = os.environ.get("BACKUP_REPO") or os.environ.get("GITHUB_REPOSITORY")
if not BACKUP_REPO:
    print("ERROR: BACKUP_REPO not set and GITHUB_REPOSITORY not present.", file=sys.stderr)
    sys.exit(2)

HEADERS = {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github.v3+json"}

ROOT = Path.cwd()
TARGETS_FILE = ROOT / "targets.json"

if not TARGETS_FILE.exists():
    print(f"No targets.json found at {TARGETS_FILE}. place a file like targets.json.example", file=sys.stderr)
    sys.exit(0)

with TARGETS_FILE.open() as f:
    targets = json.load(f)


def get_latest_release(owner, repo, include_prereleases=False):
    # If include_prereleases is False, try /releases/latest which excludes prereleases.
    if not include_prereleases:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/releases/latest"
        r = requests.get(url, headers=HEADERS)
        if r.status_code == 200:
            return r.json()
        # fallthrough if no "latest" (no releases)
    # Otherwise list releases and pick the first non-draft that suits the prerelease flag
    url = f"{GITHUB_API}/repos/{owner}/{repo}/releases"
    r = requests.get(url, headers=HEADERS)
    if r.status_code != 200:
        print(f"Failed to list releases for {owner}/{repo}: {r.status_code} {r.text}")
        return None
    for rel in r.json():
        if rel.get("draft"):
            continue
        if not include_prereleases and rel.get("prerelease"):
            continue
        return rel
    return None


def release_exists_in_backup(tag_name):
    owner, repo = BACKUP_REPO.split("/")
    url = f"{GITHUB_API}/repos/{owner}/{repo}/releases/tags/{tag_name}"
    r = requests.get(url, headers=HEADERS)
    return r.status_code == 200


def create_backup_release(tag_name, title, body):
    owner, repo = BACKUP_REPO.split("/")
    url = f"{GITHUB_API}/repos/{owner}/{repo}/releases"
    payload = {"tag_name": tag_name, "name": title, "body": body, "prerelease": False}
    r = requests.post(url, headers=HEADERS, json=payload)
    if r.status_code not in (200, 201):
        print(f"Failed to create release {tag_name} in {BACKUP_REPO}: {r.status_code} {r.text}")
        return None
    return r.json()


def upload_asset(upload_url_template, file_path, label=None):
    # upload_url_template looks like: https://uploads.github.com/repos/:owner/:repo/releases/:id/assets{?name,label}
    name = Path(file_path).name
    upload_url = upload_url_template.split("{")[0] + f"?name={name}"
    if label:
        upload_url += f"&label={label}"
    headers = {"Authorization": f"token {TOKEN}", "Content-Type": "application/zip"}
    with open(file_path, "rb") as fh:
        r = requests.post(upload_url, headers=headers, data=fh)
    if r.status_code not in (200, 201):
        print(f"Failed to upload asset {name}: {r.status_code} {r.text}")
        return False
    return True


for t in targets:
    owner = t.get("owner")
    repo = t.get("repo")
    include_prereleases = bool(t.get("include_prereleases"))
    if not owner or not repo:
        print("Skipping invalid target entry (missing owner or repo)")
        continue

    print(f"Checking latest release for {owner}/{repo} (allow prerelease={include_prereleases})...")
    latest = get_latest_release(owner, repo, include_prereleases)
    if not latest:
        print(f"No release found for {owner}/{repo}.\n")
        continue

    tag = latest.get("tag_name") or latest.get("name") or "unknown"
    safe_tag = tag.replace("/", "-")
    backup_tag = f"{repo}-{safe_tag}"

    if release_exists_in_backup(backup_tag):
        print(f"Backup release {backup_tag} already exists in {BACKUP_REPO}. Skipping.\n")
        continue

    print(f"Found new release {tag} -> creating backup release {backup_tag}...")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        # download assets
        assets = latest.get("assets", [])
        downloaded = []
        for a in assets:
            url = a.get("browser_download_url")
            name = a.get("name")
            if not url:
                print(f"Skipping asset {name} (no download url)")
                continue
            out_path = tmp / name
            print(f"Downloading asset {name}...")
            with requests.get(url, headers=HEADERS, stream=True) as r:
                if r.status_code != 200:
                    print(f"Failed to download asset {name}: {r.status_code}")
                    continue
                with open(out_path, "wb") as fh:
                    for chunk in r.iter_content(1024 * 32):
                        if chunk:
                            fh.write(chunk)
            downloaded.append(out_path)

        # release notes inside the zip (but not in the GitHub release body)
        notes = latest.get("body") or ""
        notes_file = tmp / "release-notes.txt"
        notes_file.write_text(notes, encoding="utf-8")
        downloaded.append(notes_file)

        # create archive
        archive_name = f"{repo}-{safe_tag}.zip"
        archive_path = Path.cwd() / "artifacts" / archive_name
        archive_path.parent.mkdir(parents=True, exist_ok=True)

        # prefer 7z if available
        seven = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
        if seven:
            print("Using 7z for AES256 encrypted zip")
            # 7z a -tzip -pPASSWORD -mem=AES256 archive.zip files...
            cmd = [seven, "a", "-tzip", str(archive_path), f"-p{ZIP_PASSWORD}", "-mem=AES256"]
            cmd += [str(p) for p in downloaded]
            subprocess.check_call(cmd)
        else:
            print("7z not found, falling back to zip -P (legacy encryption)")
            # zip -j -r -P password archive.zip files...
            cmd = ["zip", "-j", "-r", "-P", ZIP_PASSWORD, str(archive_path)]
            cmd += [str(p) for p in downloaded]
            subprocess.check_call(cmd)

        # create release in backup repo
        body = f"Backup of {owner}/{repo} release {tag}\nOriginal url: {latest.get('html_url')}\nPublished at: {latest.get('published_at')}"
        created = create_backup_release(backup_tag, f"{repo}-{tag}", body)
        if not created:
            print("Failed to create backup release; moving to next target.")
            continue

        upload_url = created.get("upload_url")
        print(f"Uploading archive {archive_path.name} to {BACKUP_REPO} release {backup_tag}...")
        ok = upload_asset(upload_url, archive_path)
        if not ok:
            print("Upload failed.")
        else:
            print(f"Backup for {owner}/{repo} ({tag}) completed and uploaded as {archive_path.name}\n")

print("All targets processed.")
