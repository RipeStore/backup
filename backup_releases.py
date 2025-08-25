#!/usr/bin/env python3

import os
import json
import requests
import subprocess
import tempfile
import shutil
import re
from pathlib import Path
from datetime import datetime

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


def sanitize_for_tag(s: str, max_len: int = 80) -> str:
    if not s:
        return "unknown"
    s = str(s)
    s = re.sub(r"[^\w\.-]+", "_", s)
    s = re.sub(r"_{2,}", "_", s)
    s = s.strip("._-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("._-")
    return s or "unknown"


def get_latest_release(owner, repo, allow_prerelease=False):
    if not allow_prerelease:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/releases/latest"
        r = requests.get(url, headers=HEADERS)
        if r.status_code == 200:
            return r.json()
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


def create_github_release_and_upload(tag_name, release_name, archive_path: Path, body_text: str, prerelease: bool = False):
    url = f"{GITHUB_API}/repos/{BACKUP_REPO}/releases"
    payload = {
        "tag_name": tag_name,
        "name": release_name,
        "body": body_text,
        "draft": False,
        "prerelease": prerelease
    }
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


def build_release_body(owner: str, repo: str, release: dict, downloaded_assets: list):
    raw_tag = release.get("tag_name") or release.get("name") or "unknown"
    simple_tag = normalize_tag(raw_tag)
    author_login = (release.get("author") or {}).get("login") or (release.get("author") or {}).get("name") or "unknown"
    published_at = release.get("published_at") or release.get("created_at") or ""
    published_at_disp = published_at or ""
    if published_at_disp:
        try:
            published_at_disp = datetime.fromisoformat(published_at_disp.replace("Z", "+00:00")).isoformat()
        except Exception:
            pass
    assets_md = ""
    for a in release.get("assets", []):
        name = a.get("name") or "unnamed"
        size = a.get("size", 0)
        url = a.get("browser_download_url") or a.get("url") or ""
        assets_md += f"- `{name}` ({size} bytes) — {url}\n"
    if not assets_md:
        assets_md = "- (no assets in original release)\n"
    original_url = release.get("html_url") or f"https://github.com/{owner}/{repo}/releases/tag/{raw_tag}"
    body = (
        f"**Backup metadata**\n\n"
        f"- **Source:** `{owner}/{repo}`\n"
        f"- **Original release tag:** `{raw_tag}`\n"
        f"- **Original release name:** {release.get('name') or '(none)'}\n"
        f"- **Original release URL:** {original_url}\n"
        f"- **Author:** `{author_login}`\n"
        f"- **Published at:** {published_at_disp}\n"
        f"- **Prerelease:** {bool(release.get('prerelease', False))}\n\n"
        f"**Assets included in this backup (originally):**\n\n"
        f"{assets_md}\n"
        f"---\n\n"
        f"**Original release notes:**\n\n"
        f"{release.get('body') or '(none)'}\n\n"
        f"---\n\n"
        f"_This release contains an encrypted 7z archive of the original release assets and release notes. "
        f"Archive filename: `{Path(downloaded_assets[0]).name if downloaded_assets else 'archive.7z'}`_\n"
    )
    return body


def main():
    with open(TARGETS_FILE, "r", encoding="utf-8") as f:
        targets = json.load(f)
    for t in targets:
        owner, repo_name, allow_prerelease = parse_target(t)
        if not owner or not repo_name:
            print("Skipping invalid target entry.")
            continue
        print(f"Checking latest release for {owner}/{repo_name} (allow_prerelease={allow_prerelease})...")
        release = get_latest_release(owner, repo_name, allow_prerelease)
        if not release:
            print(f"No release found for {owner}/{repo_name}.\n")
            continue
        raw_tag = release.get("tag_name") or release.get("name") or "unknown"
        simple_tag = normalize_tag(raw_tag)
        author_login = (release.get("author") or {}).get("login") or (release.get("author") or {}).get("name") or "unknown"
        owner_s = sanitize_for_tag(owner)
        repo_s = sanitize_for_tag(repo_name)
        ver_s = sanitize_for_tag(simple_tag)
        author_s = sanitize_for_tag(author_login)
        backup_tag = f"{owner_s}_{repo_s}-v{ver_s}-by-{author_s}"
        if len(backup_tag) > 100:
            backup_tag = backup_tag[:100].rstrip("._-")
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
            notes_header = (
                f"Source: {owner}/{repo_name}\n"
                f"Original tag: {raw_tag}\n"
                f"Original name: {release.get('name') or ''}\n"
                f"Author: {(release.get('author') or {}).get('login') or (release.get('author') or {}).get('name') or ''}\n"
                f"Original URL: {release.get('html_url') or ''}\n"
                f"Published at: {release.get('published_at') or ''}\n"
                f"Prerelease: {bool(release.get('prerelease', False))}\n"
                f"\n---\n\n"
            )
            notes_body = release.get("body") or ""
            notes_file.write_text(notes_header + notes_body, encoding="utf-8")
            downloaded.insert(0, notes_file)
            archive_path = tmp_path / f"{backup_tag}.7z"
            try:
                create_7z_archive(tmp_path, archive_path, ZIP_PASSWORD)
            except subprocess.CalledProcessError as e:
                print("7z failed:", e)
                continue
            release_body = build_release_body(owner, repo_name, release, [archive_path])
            ok = create_github_release_and_upload(backup_tag, release_name, archive_path, release_body, prerelease=False)
            if ok:
                print(f"Backup for {owner}/{repo_name} ({raw_tag}) completed and uploaded as {archive_path.name}\n")
            else:
                print(f"Failed to upload backup for {owner}/{repo_name} ({raw_tag}).\n")
    print("All targets processed.")


if __name__ == "__main__":
    main()
