import os
import json
import requests
import subprocess
import shutil

GITHUB_API_URL = "https://api.github.com"

# Auth headers
headers = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}"
}

# Repo where backups are stored (this repo)
backup_repo = "RipeStore/backup"

# JSON file containing repos to monitor
TARGETS_FILE = "repos.json"

# Password for AES256 7z
zip_password = os.getenv("BACKUP_ZIP_PASSWORD")
if not zip_password:
    raise ValueError("BACKUP_ZIP_PASSWORD environment variable not set.")

# Paths
artifacts_dir = "artifacts"
os.makedirs(artifacts_dir, exist_ok=True)

def release_exists(backup_repo, tag):
    """Check if a release with the given tag already exists in the backup repo."""
    url = f"{GITHUB_API_URL}/repos/{backup_repo}/releases/tags/{tag}"
    r = requests.get(url, headers=headers)
    return r.status_code == 200

def download_asset(asset_url, dest_path):
    r = requests.get(asset_url, headers=headers, stream=True)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)

def seven_zip_with_password(archive_path, files):
    # Best compression (-mx=9), AES256, hide filenames (-mhe=on)
    cmd = [
        "7z", "a", "-t7z", "-mx=9", "-mhe=on",
        f"-p{zip_password}", archive_path
    ] + files
    subprocess.run(cmd, check=True)

def main():
    with open(TARGETS_FILE, "r") as f:
        targets = json.load(f)

    for target in targets:
        owner = target["owner"]
        repo = target["repo"]
        include_prereleases = target.get("include_prereleases", False)

        print(f"Checking latest release for {owner}/{repo} (allow prerelease={include_prereleases})...")
        releases_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/releases"
        r = requests.get(releases_url, headers=headers)
        if r.status_code != 200:
            print(f"Failed to list releases for {owner}/{repo}: {r.status_code} {r.text}")
            continue

        releases = r.json()
        if not releases:
            print(f"No releases found for {owner}/{repo}.")
            continue

        # Pick latest release
        release = None
        for rel in releases:
            if rel["prerelease"] and not include_prereleases:
                continue
            release = rel
            break

        if not release:
            print(f"No suitable release found for {owner}/{repo}.")
            continue

        tag_name = release["tag_name"].lstrip("v")
        backup_tag = f"{repo}-v{tag_name}"

        # Skip if already backed up
        if release_exists(backup_repo, backup_tag):
            print(f"Release {backup_tag} already backed up — skipping.")
            continue

        print(f"Found new release {release['tag_name']} -> creating backup release {backup_tag}...")

        # Download assets
        downloaded_files = []
        for asset in release.get("assets", []):
            asset_name = asset["name"]
            download_url = asset["url"]
            print(f"Downloading asset {asset_name}...")
            dest_file = os.path.join(artifacts_dir, asset_name)
            r = requests.get(download_url, headers={
                "Accept": "application/octet-stream",
                "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}"
            }, stream=True)
            r.raise_for_status()
            with open(dest_file, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            downloaded_files.append(dest_file)

        # Save release notes inside archive (still password-protected)
        notes_file = os.path.join(artifacts_dir, "release_notes.txt")
        with open(notes_file, "w", encoding="utf-8") as f:
            f.write(release.get("body", ""))
        downloaded_files.append(notes_file)

        # Create password-protected 7z
        archive_path = os.path.join(artifacts_dir, f"{backup_tag}.7z")
        seven_zip_with_password(archive_path, downloaded_files)

        # Create release in backup repo
        create_url = f"{GITHUB_API_URL}/repos/{backup_repo}/releases"
        create_data = {
            "tag_name": backup_tag,
            "name": backup_tag,
            "body": "",
            "draft": False,
            "prerelease": False
        }
        r = requests.post(create_url, headers=headers, json=create_data)
        if r.status_code != 201:
            print(f"Failed to create release {backup_tag} in {backup_repo}: {r.status_code} {r.text}")
            continue

        release_id = r.json()["id"]
        upload_url = r.json()["upload_url"].split("{")[0]

        with open(archive_path, "rb") as f:
            r2 = requests.post(
                f"{upload_url}?name={os.path.basename(archive_path)}",
                headers={
                    "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}",
                    "Content-Type": "application/x-7z-compressed"
                },
                data=f
            )
        if r2.status_code not in (200, 201):
            print(f"Failed to upload asset: {r2.status_code} {r2.text}")
            continue

        print(f"Backup for {backup_tag} completed.")

        # Clean up artifacts
        shutil.rmtree(artifacts_dir, ignore_errors=True)
        os.makedirs(artifacts_dir, exist_ok=True)

if __name__ == "__main__":
    main()
