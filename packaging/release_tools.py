"""Build signed static-server releases. No server credentials or uploads."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import update_core as core

PRIVATE = ROOT / "packaging/signing-private/release-key.xml"
PUBLIC = ROOT / "update_public_key.json"
STAGE = ROOT / "packaging/stage/app"
RELEASES = ROOT / "packaging/releases"


def powershell(code, env):
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            '$ErrorActionPreference="Stop"; ' + code,
        ],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode:
        raise RuntimeError(
            "Windows 签名操作失败。请检查密钥路径及 PowerShell/.NET 环境。"
        )


def init_key(private=PRIVATE, public=PUBLIC):
    private, public = Path(private).resolve(), Path(public).resolve()
    if private.exists() or public.exists():
        raise ValueError(
            "密钥已经存在，拒绝覆盖。请备份已有私钥；不要为同一更新渠道反复换密钥。"
        )
    private.parent.mkdir(parents=True, exist_ok=True)
    public.parent.mkdir(parents=True, exist_ok=True)
    powershell(
        r"""
        $rsa = New-Object System.Security.Cryptography.RSACryptoServiceProvider(3072);
        try {
            $rsa.PersistKeyInCsp = $false;
            [IO.File]::WriteAllText($env:PDD_SIGN_PRIVATE, $rsa.ToXmlString($true), [Text.Encoding]::UTF8);
            $p = $rsa.ExportParameters($false);
            $json = @{n=[Convert]::ToBase64String($p.Modulus); e=[Convert]::ToBase64String($p.Exponent)} | ConvertTo-Json;
            [IO.File]::WriteAllText($env:PDD_SIGN_PUBLIC, $json, (New-Object Text.UTF8Encoding($false)));
        } finally { $rsa.Dispose() }
    """,
        {"PDD_SIGN_PRIVATE": str(private), "PDD_SIGN_PUBLIC": str(public)},
    )
    print("已创建发布密钥。私钥必须离线备份，不能上传服务器：" + str(private))


def sign(filename, private=PRIVATE, public=PUBLIC):
    filename, private, public = (
        Path(filename).resolve(),
        Path(private).resolve(),
        Path(public).resolve(),
    )
    signature = filename.with_suffix(filename.suffix + ".sig")
    powershell(
        r"""
        $rsa = New-Object System.Security.Cryptography.RSACryptoServiceProvider;
        try {
            $rsa.PersistKeyInCsp = $false;
            $rsa.FromXmlString([IO.File]::ReadAllText($env:PDD_SIGN_PRIVATE));
            $bytes = [IO.File]::ReadAllBytes($env:PDD_SIGN_FILE);
            $sig = $rsa.SignData($bytes, 'SHA256');
            [IO.File]::WriteAllText($env:PDD_SIGN_SIGNATURE, [Convert]::ToBase64String($sig), [Text.Encoding]::ASCII);
        } finally { $rsa.Dispose() }
    """,
        {
            "PDD_SIGN_PRIVATE": str(private),
            "PDD_SIGN_FILE": str(filename),
            "PDD_SIGN_SIGNATURE": str(signature),
        },
    )
    core.verify_signature(
        filename.read_bytes(), signature.read_bytes(), public.read_bytes()
    )
    return signature


def stamp(stage=None, version=None, build=None):
    stage = Path(stage or STAGE).resolve()
    config = core.loads((ROOT / "release_config.json").read_bytes())
    version = version or config["version"]
    build = build or datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    if not PUBLIC.is_file():
        raise ValueError("缺少发布公钥，请先运行 release_tools.py init-key。")
    shutil.copy2(PUBLIC, stage / "update_public_key.json")
    files = {}
    for path in sorted(stage.rglob("*")):
        if path.is_file() and path.name != "app_release.json":
            name = path.relative_to(stage).as_posix()
            core.program_path(name)
            files[name] = core.sha256_file(path)
    release = core.validate_release(
        {
            "schema_version": 1,
            "app_id": core.APP_ID,
            "version": version,
            "build": str(build),
            "runtime_id": core.runtime_id(stage.parent / "runtime"),
            "update_features": ["incremental_only", "universal_code"],
            "data_schema": 1,
            "files": files,
        }
    )
    snapshot = RELEASES / str(build)
    if snapshot.exists():
        raise ValueError("构建编号已存在，不能覆盖发布快照。请使用新的构建编号。")
    core.write_json(stage / "app_release.json", release)
    snapshot.mkdir(parents=True)
    shutil.copy2(stage / "app_release.json", snapshot / "app_release.json")
    print(f"发布构建：{version} / {build}；快照：{snapshot}")
    return release


def publish(installer=None, base=None, notes="", output=None, mode="full"):
    if mode not in ("full", "incremental", "universal"):
        raise ValueError("发布模式无效。")
    if mode == "incremental" and (
        not base
        or "incremental_only"
        not in core.loads(Path(base).read_bytes()).get("update_features", [])
    ):
        raise ValueError("仅增量发布需要支持该功能的旧基线。请先完整发布一次新更新器。")
    release = core.installed_release(STAGE)
    if release is None or not core.local_matches(STAGE, release):
        raise ValueError("暂存源码与构建清单不一致，请先重新构建安装包。")
    if mode == "universal" and not any(
        core.universal_compatible(core.loads(path.read_bytes()), release)
        and int(path.parent.name) < int(release["build"])
        for path in RELEASES.glob("*/app_release.json")
    ):
        raise ValueError(
            "当前更新器或运行环境尚无兼容的完整发布，请先完整发布一次通用更新器。"
        )
    # Prevent publishing a prior installer against a newer staged build.
    receipt = (
        core.loads((RELEASES / release["build"] / "installer.json").read_bytes())
        if mode == "full"
        else None
    )
    installer = Path(
        installer or ROOT / f"dist/pddchatSetup-{release['version']}-lite.exe"
    ).resolve()
    if mode == "full" and (
        receipt["sha256"] != core.sha256_file(installer)
        or receipt["size"] != installer.stat().st_size
    ):
        raise ValueError("安装包与本轮构建回执不匹配，停止发布。")
    destination = Path(output or ROOT / "dist/updates" / release["build"]).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    packages, increments = destination / "packages", destination / "incremental"
    packages.mkdir()
    increments.mkdir()
    full_name = f"pddchatSetup-{release['version']}-{release['build']}.exe"
    full = packages / full_name
    if mode == "full":
        shutil.copy2(installer, full)

    def entry(path, remote):
        return {
            "url": core.BASE_URL + remote,
            "sha256": core.sha256_file(path),
            "size": path.stat().st_size,
        }

    latest = {
        "schema_version": 1,
        "app_id": core.APP_ID,
        "channel": "stable",
        "version": release["version"],
        "build": release["build"],
        "release": release,
        "release_notes": notes.splitlines(),
        "publish_mode": mode,
        "full": entry(full, "packages/" + full_name) if mode == "full" else None,
        "incremental": [],
    }
    if mode in ("full", "universal"):
        name = f"pddchat-code-{release['build']}.zip"
        package = increments / name
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "code.json",
                core.dumps({"package_type": "universal_code", "release": release}),
            )
            for filename in sorted(release["files"]):
                archive.write(STAGE / filename, "files/" + filename)
        latest["universal"] = entry(package, "incremental/" + name)
    if base:
        base = Path(base)
        old = core.validate_release(core.loads(base.read_bytes()))
        if int(old["build"]) >= int(release["build"]):
            raise ValueError("旧构建必须小于新构建。")
        if old["runtime_id"] == release["runtime_id"] and old["files"].get(
            "update_public_key.json"
        ) == release["files"].get("update_public_key.json"):
            changed = {
                n: d for n, d in release["files"].items() if old["files"].get(n) != d
            }
            deleted = sorted(set(old["files"]) - set(release["files"]))
            case_changed = any(
                n.casefold() in {x.casefold() for x in old["files"]}
                and n not in old["files"]
                for n in changed
            )
            # Bootstrap protocol itself is updated by full installers only.
            bootstrap_changed = any(
                old["files"].get(n) != release["files"].get(n)
                for n in ("update_core.py", "update_agent.py", "update_launcher.py")
            )
            if not case_changed and not bootstrap_changed:
                name = f"pddchat-{old['build']}-to-{release['build']}.zip"
                package = increments / name
                with zipfile.ZipFile(
                    package, "w", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    archive.writestr(
                        "patch.json",
                        core.dumps(
                            {
                                "from_build": old["build"],
                                "release": release,
                                "delete": deleted,
                            }
                        ),
                    )
                    for filename in sorted(changed):
                        archive.write(STAGE / filename, "files/" + filename)
                latest["incremental"].append(
                    {
                        **entry(package, "incremental/" + name),
                        "from_build": old["build"],
                        "base_manifest_sha256": core.sha256_file(base),
                    }
                )
            else:
                print("引导更新器或路径大小写有变化，本轮只发布完整安装包。")
        else:
            print("运行时或公钥变化，本轮只发布完整安装包。")
    if mode == "incremental" and not latest["incremental"]:
        raise ValueError(
            "无法生成兼容增量包（更新器、运行时或公钥可能变化），请改用完整发布。"
        )
    core.validate_latest(latest)
    manifest = destination / (
        "latest-v3.json"
        if mode == "universal"
        else "latest-v2.json"
        if mode == "incremental"
        else "latest.json"
    )
    core.write_json(manifest, latest)
    signature = sign(manifest)
    if mode == "full":
        shutil.copy2(manifest, destination / "latest-v2.json")
        shutil.copy2(signature, destination / "latest-v2.json.sig")
        shutil.copy2(manifest, destination / "latest-v3.json")
        shutil.copy2(signature, destination / "latest-v3.json.sig")
    # Immutable signature removes the two-file publication race for latest.json.
    shutil.copy2(signature, packages / f"release-{release['build']}.json.sig")
    print("发布文件已生成（尚未上传）：" + str(destination))
    return destination


def receipt(installer):
    release = core.installed_release(STAGE)
    core.write_json(
        RELEASES / release["build"] / "installer.json",
        {"sha256": core.sha256_file(installer), "size": Path(installer).stat().st_size},
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-key")
    stamp_parser = commands.add_parser("stamp")
    stamp_parser.add_argument("--version")
    stamp_parser.add_argument("--build")
    receipt_parser = commands.add_parser("receipt")
    receipt_parser.add_argument("--installer", type=Path, required=True)
    publish_parser = commands.add_parser("publish")
    publish_parser.add_argument("--installer", type=Path)
    publish_parser.add_argument("--base", type=Path)
    publish_parser.add_argument("--notes-file", type=Path)
    publish_parser.add_argument("--output", type=Path)
    publish_parser.add_argument(
        "--mode", choices=("full", "incremental", "universal"), default="full"
    )
    args = parser.parse_args()
    try:
        if args.command == "init-key":
            init_key()
        elif args.command == "stamp":
            stamp(version=args.version, build=args.build)
        elif args.command == "receipt":
            receipt(args.installer)
        else:
            publish(
                args.installer,
                args.base,
                args.notes_file.read_text(encoding="utf-8-sig")
                if args.notes_file
                else "",
                args.output,
                args.mode,
            )
    except Exception as error:  # noqa: BLE001 - CLI boundary reports build/signing failures
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
