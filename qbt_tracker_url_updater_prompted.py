import argparse
import getpass
import os
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

import qbittorrentapi


@dataclass(frozen=True)
class QBConnection:
    host: str
    port: int
    username: str
    password: str


DEFAULT_QBT_HOST = "localhost"
DEFAULT_QBT_PORT = 9090


def prompt_value(prompt: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        print("A value is required.")


def prompt_port(prompt: str, default: int) -> int:
    while True:
        raw_value = prompt_value(prompt, str(default))
        try:
            return int(raw_value)
        except ValueError:
            print("Please enter a valid port number.")


def prompt_password(prompt: str) -> str:
    while True:
        password = getpass.getpass(f"{prompt}: ").strip()
        if password:
            return password
        print("A value is required.")


def prompt_yes_no(message: str, default_no: bool = True) -> bool:
    suffix = "[y/N]" if default_no else "[Y/n]"
    while True:
        ans = input(f"{message} {suffix} ").strip().lower()
        if not ans:
            return not default_no
        if ans in {"y", "yes"}:
            return True
        if ans in {"n", "no"}:
            return False
        print("Please answer y or n.")


def get_connection_from_args(args: argparse.Namespace) -> QBConnection:
    host = (
        args.qbt_host
        or os.environ.get("QBT_HOST")
        or prompt_value("Enter the qBittorrent host or NAS/Docker hostname or IP", DEFAULT_QBT_HOST)
    )
    port = int(
        args.qbt_port
        or os.environ.get("QBT_PORT")
        or prompt_port("Enter the qBittorrent WebUI port (use the mapped Docker/NAS port)", DEFAULT_QBT_PORT)
    )
    username = args.qbt_username or os.environ.get("QBT_USERNAME") or prompt_value("Enter the qBittorrent username")
    password = args.qbt_password or os.environ.get("QBT_PASSWORD") or prompt_password("Enter the qBittorrent password")

    return QBConnection(host=host, port=port, username=username, password=password)


def connect_qb(conn: QBConnection) -> qbittorrentapi.Client:
    qb = qbittorrentapi.Client(host=conn.host, port=conn.port)
    qb.auth_log_in(username=conn.username, password=conn.password)
    return qb


def iter_matching_torrents(
    qb: qbittorrentapi.Client,
    original_url: str,
) -> Iterable[Tuple[str, str]]:
    for torrent in qb.torrents_info():
        torrent_hash = getattr(torrent, "hash", None)
        torrent_name = getattr(torrent, "name", None) or str(torrent_hash)
        if not torrent_hash:
            continue

        trackers = qb.torrents_trackers(torrent_hash=torrent_hash)
        for tracker in trackers:
            tracker_url = getattr(tracker, "url", None)
            if tracker_url == original_url:
                yield torrent_hash, torrent_name
                break


def replace_tracker_urls(
    qb: qbittorrentapi.Client,
    original_url: str,
    new_url: str,
    dry_run: bool,
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str, str]]]:
    updated: List[Tuple[str, str]] = []
    failed: List[Tuple[str, str, str]] = []

    for torrent_hash, torrent_name in iter_matching_torrents(qb, original_url):
        if dry_run:
            updated.append((torrent_hash, torrent_name))
            continue

        try:
            qb.torrents_edit_tracker(
                torrent_hash=torrent_hash,
                original_url=original_url,
                new_url=new_url,
            )
            updated.append((torrent_hash, torrent_name))
        except qbittorrentapi.APIError as exc:
            failed.append((torrent_hash, torrent_name, str(exc)))

    return updated, failed


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prompt for qBittorrent connection details, find torrents using a tracker "
            "announce URL, and replace that tracker URL with a new one after showing a dry-run preview."
        )
    )
    parser.add_argument("--qbt-host", dest="qbt_host", help="qBittorrent host, NAS hostname, or IP (or env QBT_HOST)")
    parser.add_argument("--qbt-port", dest="qbt_port", help="qBittorrent WebUI port or mapped Docker/NAS port (or env QBT_PORT)")
    parser.add_argument("--qbt-username", dest="qbt_username", help="qBittorrent username (or env QBT_USERNAME)")
    parser.add_argument("--qbt-password", dest="qbt_password", help="qBittorrent password (or env QBT_PASSWORD)")
    parser.add_argument("--old-url", dest="old_url", help="Existing tracker announce URL to replace")
    parser.add_argument("--new-url", dest="new_url", help="New tracker announce URL")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the preview only and exit without applying changes",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    conn = get_connection_from_args(args)

    print(f"Connecting to qBittorrent at {conn.host}:{conn.port}...")
    try:
        qb = connect_qb(conn)
    except qbittorrentapi.LoginFailed as exc:
        print(f"Failed to log in to qBittorrent: {exc}")
        return 1
    except qbittorrentapi.APIConnectionError as exc:
        print(
            "Failed to connect to qBittorrent. "
            "For Docker or NAS setups, make sure you are using the NAS hostname/IP and the exposed WebUI port."
        )
        print(f"Connection error: {exc}")
        return 1

    old_url = args.old_url or prompt_value("Enter the existing tracker announce URL")
    new_url = args.new_url or prompt_value("Enter the new tracker announce URL")

    if old_url == new_url:
        print("The existing and new tracker URLs are the same. Nothing to change.")
        return 1

    preview_matches, _ = replace_tracker_urls(qb, old_url, new_url, dry_run=True)
    if not preview_matches:
        print("No torrents were found with that tracker announce URL.")
        return 0

    print(f"Dry run: {len(preview_matches)} torrent(s) would be updated:")
    for _, torrent_name in preview_matches:
        print(f" - {torrent_name}")

    if args.dry_run:
        print("Dry run complete. No changes were made.")
        return 0

    if not prompt_yes_no("Apply these tracker URL changes?", default_no=True):
        print("Aborted.")
        return 0

    updated, failed = replace_tracker_urls(qb, old_url, new_url, dry_run=False)

    print(f"Updated {len(updated)} torrent(s).")
    if failed:
        print(f"Failed to update {len(failed)} torrent(s):")
        for _, torrent_name, error in failed:
            print(f" - {torrent_name}: {error}")
        return 1

    print("Tracker URL replacement complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
