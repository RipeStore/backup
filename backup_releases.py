import os
import json
import requests
import subprocess
import tempfile
import shutil

GITHUB_API_URL = "https://api.github.com"
BACKUP_REPO = "RipeStore/backup"  # your repo
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
ZIP_PASSWORD = os.environ.get("BACKUP_ZIP_PASSWORD")

if not GITHUB_TOKEN:
    raise RuntimeError("GITHUB_TOKEN environment variable not set")
if not ZIP_PASSWORD:
    raise RuntimeError("BACKUP_ZIP_PASSWORD environment variable not set")

headers = {"Authorization": f"token {GITHUB_TOKEN}"}


def load_targets():
    with open("targets.json", "r", encoding="utf-8") as f:
        return json.load(f)


def get_latest_release(repo, allow_prerelease):
    url = f"{GITHUB_API_URL}/repos/{repo}/releases"
    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        print(f"Failed to list releases for {repo}: {r.status_code} {r.text}")
        return None
    releases = r.json()
    if not releases:
        return None
    for release in releases:
        if release.get("prerelease") and not allow_prerelease:
            continue
        return release
    return None


def release_exists(backup_repo, tag):
    url = f"{GITHUB_API_URL}/repos/{backup_repo}/releases/tags/{tag}"
    r = requests.get(url, headers=headers)
    return r.status_code == 200


def download_assets(release, temp_dir):
    assets = release.get("assets", [])
    for asset in assets:
        asset_url = asset["url"]
        asset_name = asset["name"]
        print(f"Downloading asset {asset_name}...")
        r = requests.get(asset_url, headers={**headers, "Accept": "application/octet-stream"}, stream=True)
        if r.status_code == 200:
            file_path = os.path.join(temp_dir, asset_name)
            with open(file_path, "wb") as f:
                shutil.copyfileobj(r.raw, f)
        else:
            print(f"Failed to download {asset_name}: {r.status_code} {r.text}")


def create_7z_archive(src_dir, archive_path, password):
    cmd = [
        "7z", "a", "-t7z", archive_path, src_dir + os.sep,
        f"-p{password}", "-mhe=on", "-mx=9"
    ]
    subprocess.run(cmd, check=True)


def create_backup_release(backup_repo, tag_name, name, archive_path):
    url = f"{GITHUB_API_URL}/repos/{backup_repo}/releases"
    data = {"tag_name": tag_name, "name": name, "draft": False, "prerelease": False}
    r = requests.post(url, headers=headers, json=data)
    if r.status_code != 201:
        print(f"Failed to create release {name} in {backup_repo}: {r.status_code} {r.text}")
        return None
    upload_url = r.json()["upload_url"].split("{")[0]
    with open(archive_path, "rb") as f:
        upload_headers = {
            **headers,
            "Content-Type": "application/x-7z-compressed"
        }
        upload_r = requests.post(f"{upload_url}?name={os.path.basename(archive_path)}",
                                 headers=upload_headers, data=f)
        if upload_r.status_code != 201:
            print(f"Failed to upload asset: {upload_r.status_code} {upload_r.text}")
            return False
    return True


def main():
    targets = load_targets()
    for target in targets:
        repo = target["repo"]
        allow_prerelease = target.get("allow_prerelease", False)

        print(f"Checking latest release for {repo} (allow prerelease={allow_prerelease})...")
        release = get_latest_release(repo, allow_prerelease)
        if not release:
            print(f"No release found for {repo}.")
            continue

        tag_name = f"{repo.replace('/', '_')}-v{release['tag_name']}"
        if release_exists(BACKUP_REPO, tag_name):
            print(f"Release {tag_name} already backed up — skipping.")
            continue

        print(f"Found new release {release['tag_name']} -> creating backup release {tag_name}...")
        with tempfile.TemporaryDirectory() as tmpdir:
            download_assets(release, tmpdir)

            archive_path = os.path.join(tmpdir, f"{tag_name}.7z")
            create_7z_archive(tmpdir, archive_path, ZIP_PASSWORD)

            success = create_backup_release(BACKUP_REPO, tag_name, tag_name, archive_path)
            if success:
                print(f"Backup for {tag_name} completed and uploaded.")
            else:
                print(f"Backup for {tag_name} failed.")


if __name__ == "__main__":
    main()
