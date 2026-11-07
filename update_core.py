"""Windows signed application updates. Standard library only; shared by bootstrap.

The update server is a static HTTPS directory. Business data is never a payload.
RSA signatures are verified by Windows CNG, not a Python crypto implementation.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

APP_ID = "pdd_appeal"
BASE_URL = "https://flowtry.top/updates/pdd_appeal/"
PROTOCOL = 1
MAX_PACKAGE = 1024 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
SHA = re.compile(r"^[0-9a-f]{64}$")
PROTECTED = {
    "state",
    "output",
    "outputs",
    "debug",
    "evidence",
    "browsers",
    "__pycache__",
    ".git",
    ".venv",
    "node_modules",
}


def dumps(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def loads(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("更新清单含重复字段。")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=unique)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(dumps(value))
    temporary.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def program_path(name):
    if not isinstance(name, str) or "\\" in name or ":" in name:
        raise ValueError("更新文件路径不合法。")
    parts = PurePosixPath(name).parts
    if not parts or "/".join(parts) != name or name.startswith("/"):
        raise ValueError("更新文件路径不是规范相对路径。")
    for part in parts:
        low = part.casefold()
        if (
            low in PROTECTED
            or low in {".", ".."}
            or low.startswith(".")
            or part.endswith((" ", "."))
        ):
            raise ValueError("更新包试图修改受保护目录或文件。")
        if re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", low):
            raise ValueError("更新包含 Windows 保留文件名。")
    allowed = len(parts) == 1 and (
        name.endswith(".py") or name in {"app_release.json", "update_public_key.json"}
    )
    allowed |= (
        len(parts) >= 3
        and parts[:2] == ("gui", "app")
        and PurePosixPath(name).suffix.lower()
        in {
            ".py",
            ".json",
            ".qss",
            ".css",
            ".html",
            ".txt",
            ".png",
            ".ico",
            ".svg",
            ".jpg",
            ".jpeg",
        }
    )
    if not allowed:
        raise ValueError(f"不在程序更新白名单内：{name}")
    return name


def safe_target(root, name):
    program_path(name)
    root = Path(root).resolve()
    target = root.joinpath(*PurePosixPath(name).parts)
    for item in [root, *reversed(target.relative_to(root).parents), target]:
        if not item.is_absolute():
            item = root / item
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise ValueError("更新路径含符号链接或目录联接。")
    if not target.resolve().is_relative_to(root):
        raise ValueError("更新目标超出应用目录。")
    return target


def validate_release(release):
    if (
        not isinstance(release, dict)
        or release.get("app_id") != APP_ID
        or release.get("schema_version") != PROTOCOL
    ):
        raise ValueError("应用或更新协议不匹配。")
    if not re.fullmatch(r"\d{3,20}", str(release.get("build", ""))):
        raise ValueError("构建编号不合法。")
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(release.get("version", ""))):
        raise ValueError("版本号必须为 major.minor.patch。")
    if not SHA.fullmatch(str(release.get("runtime_id", ""))):
        raise ValueError("缺少运行时标识。")
    if (
        type(release.get("data_schema", 1)) is not int
        or release.get("data_schema", 1) < 1
    ):
        raise ValueError("数据版本不合法。")
    files = release.get("files")
    if not isinstance(files, dict) or not files or len(files) > 10000:
        raise ValueError("发布文件清单不合法。")
    seen = set()
    for name, digest in files.items():
        program_path(name)
        if (
            name == "app_release.json"
            or name.casefold() in seen
            or not SHA.fullmatch(str(digest))
        ):
            raise ValueError("发布清单含重复路径或无效哈希。")
        seen.add(name.casefold())
    return release


def verify_signature(raw, signature, public_key):
    """RSA PKCS#1 v1.5 SHA256 through BCryptVerifySignature (Windows CNG)."""
    if os.name != "nt":
        raise RuntimeError("更新签名验证需要 Windows。")
    key = loads(public_key) if isinstance(public_key, (bytes, str)) else public_key
    n, e = (base64.b64decode(key[k], validate=True) for k in ("n", "e"))
    sig = base64.b64decode(signature.strip(), validate=True)
    if (
        len(n) < 384
        or len(n) > 512
        or int.from_bytes(e, "big") != 65537
        or len(sig) != len(n)
    ):
        raise ValueError("更新签名公钥或签名长度无效。")
    dll = ctypes.WinDLL("bcrypt")
    handle, imported = ctypes.c_void_p(), ctypes.c_void_p()
    ptr, uint = ctypes.c_void_p, ctypes.c_ulong
    dll.BCryptOpenAlgorithmProvider.argtypes = [
        ctypes.POINTER(ptr),
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        uint,
    ]
    dll.BCryptImportKeyPair.argtypes = [
        ptr,
        ptr,
        ctypes.c_wchar_p,
        ctypes.POINTER(ptr),
        ptr,
        uint,
        uint,
    ]
    dll.BCryptVerifySignature.argtypes = [ptr, ptr, ptr, uint, ptr, uint, uint]
    dll.BCryptDestroyKey.argtypes = [ptr]
    dll.BCryptCloseAlgorithmProvider.argtypes = [ptr, uint]
    blob = struct.pack("<6I", 0x31415352, len(n) * 8, len(e), len(n), 0, 0) + e + n

    class Padding(ctypes.Structure):
        _fields_ = [("algorithm", ctypes.c_wchar_p)]

    try:
        if dll.BCryptOpenAlgorithmProvider(ctypes.byref(handle), "RSA", None, 0) != 0:
            raise RuntimeError("无法初始化 Windows 签名验证。")
        if (
            dll.BCryptImportKeyPair(
                handle,
                None,
                "RSAPUBLICBLOB",
                ctypes.byref(imported),
                blob,
                len(blob),
                0,
            )
            != 0
        ):
            raise ValueError("无法导入更新公钥。")
        digest = hashlib.sha256(raw).digest()
        if (
            dll.BCryptVerifySignature(
                imported,
                ctypes.byref(Padding("SHA256")),
                digest,
                len(digest),
                sig,
                len(sig),
                2,
            )
            != 0
        ):
            raise ValueError("更新清单签名无效，已拒绝更新。")
    finally:
        if imported.value:
            dll.BCryptDestroyKey(imported)
        if handle.value:
            dll.BCryptCloseAlgorithmProvider(handle, 0)


def allowed_url(url):
    parts, base = urllib.parse.urlsplit(url), urllib.parse.urlsplit(BASE_URL)
    if (
        parts.scheme != "https"
        or parts.netloc != base.netloc
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise ValueError("更新下载地址必须使用指定服务器的 HTTPS。")
    decoded = urllib.parse.unquote(parts.path)
    if (
        not decoded.startswith(base.path)
        or ".." in decoded.split("/")
        or "\\" in decoded
    ):
        raise ValueError("更新地址超出指定目录。")
    return url


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        allowed_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def download(
    url, destination, *, limit, expected_size=None, expected_sha=None, progress=None
):
    allowed_url(url)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    digest, size = hashlib.sha256(), 0
    deadline = time.monotonic() + 300
    opener = urllib.request.build_opener(_Redirect())
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "PddAppealUpdater/1", "Cache-Control": "no-cache"},
        )
        with opener.open(request, timeout=25) as response, partial.open("wb") as output:
            allowed_url(response.url)
            while True:
                chunk = response.read(128 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if time.monotonic() > deadline:
                    raise TimeoutError("下载超过最长等待时间，请稍后重试。")
                if size > limit:
                    raise ValueError("下载内容超过大小上限。")
                output.write(chunk)
                digest.update(chunk)
                if progress:
                    progress(size, expected_size)
        if expected_size is not None and size != expected_size:
            raise ValueError("更新包大小不匹配。")
        if expected_sha is not None and digest.hexdigest() != expected_sha:
            raise ValueError("更新包 SHA256 校验失败。")
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    return destination


def artifact(value):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("size"), int)
        or not 0 < value["size"] <= MAX_PACKAGE
    ):
        raise ValueError("更新包大小字段无效。")
    if not SHA.fullmatch(str(value.get("sha256", ""))):
        raise ValueError("更新包校验字段无效。")
    allowed_url(value.get("url", ""))
    return value


def validate_latest(value):
    if value.get("app_id") != APP_ID or value.get("schema_version") != PROTOCOL:
        raise ValueError("服务器更新清单协议不匹配。")
    validate_release(value["release"])
    if (
        value.get("build") != value["release"]["build"]
        or value.get("version") != value["release"]["version"]
    ):
        raise ValueError("更新清单版本不一致。")
    if value.get("channel") != "stable":
        raise ValueError("更新渠道不匹配。")
    mode = value.get("publish_mode", "full")
    if mode not in ("full", "incremental", "universal"):
        raise ValueError("发布模式无效。")
    if mode == "incremental":
        if value.get("full") is not None or not value.get("incremental"):
            raise ValueError("仅增量清单必须包含增量包且不能包含完整包。")
    elif mode == "universal":
        if value.get("full") is not None or value.get("incremental"):
            raise ValueError("通用代码清单不能包含完整包或按基线生成的增量包。")
        artifact(value.get("universal"))
    else:
        artifact(value["full"])
    if value.get("universal") is not None:
        artifact(value["universal"])
    for entry in value.get("incremental", []):
        artifact(entry)
        if not SHA.fullmatch(str(entry.get("base_manifest_sha256", ""))):
            raise ValueError("增量包缺少旧清单哈希。")
    return value


def installed_release(root):
    path = Path(root) / "app_release.json"
    return validate_release(loads(path.read_bytes())) if path.is_file() else None


def local_matches(root, release):
    for name, digest in release["files"].items():
        path = safe_target(root, name)
        if not path.is_file() or sha256_file(path) != digest:
            return False
    return True


def select_artifact(root, latest):
    current = installed_release(root)
    if current and int(current["build"]) >= int(latest["build"]):
        return None, None
    bootstrap = (
        "update_core.py",
        "update_agent.py",
        "update_launcher.py",
        "update_public_key.json",
    )
    same_bootstrap = current and all(
        current["files"].get(n) == latest["release"]["files"].get(n) for n in bootstrap
    )
    if (
        latest.get("universal")
        and universal_compatible(current, latest["release"])
        and bootstrap_intact(root, current)
    ):
        return "universal", latest["universal"]
    if (
        current
        and same_bootstrap
        and current["runtime_id"] == latest["release"]["runtime_id"]
        and local_matches(root, current)
    ):
        raw_hash = sha256_file(Path(root) / "app_release.json")
        for entry in latest.get("incremental", []):
            if (
                entry["from_build"] == current["build"]
                and entry["base_manifest_sha256"] == raw_hash
            ):
                return "incremental", entry
    if latest.get("full") is None:
        return "manual", None
    return "full", latest["full"]


def universal_compatible(current, target):
    return bool(
        current
        and "universal_code" in current.get("update_features", [])
        and "universal_code" in target.get("update_features", [])
        and current["runtime_id"] == target["runtime_id"]
        and current.get("data_schema", 1) == target.get("data_schema", 1)
        and all(
            current["files"].get(name) == target["files"].get(name)
            for name in (
                "update_core.py",
                "update_agent.py",
                "update_launcher.py",
                "update_public_key.json",
            )
        )
    )


def bootstrap_intact(root, current):
    for name in (
        "update_core.py",
        "update_agent.py",
        "update_launcher.py",
        "update_public_key.json",
    ):
        digest = current["files"].get(name)
        if digest:
            path = safe_target(root, name)
            if not path.is_file() or sha256_file(path) != digest:
                return False
    return True


def runtime_id(runtime_root):
    digest = hashlib.sha256()
    for path in sorted(Path(runtime_root).rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        name = path.relative_to(runtime_root).as_posix()
        digest.update(
            name.encode("utf-8") + b"\0" + sha256_file(path).encode("ascii") + b"\n"
        )
    return digest.hexdigest()


def prepare_patch(root, package, entry, latest, staging, *, universal=False):
    root, staging = Path(root), Path(staging)
    current = installed_release(root)
    if universal:
        if (
            not universal_compatible(current, latest["release"])
            or not bootstrap_intact(root, current)
            or int(current["build"]) >= int(latest["build"])
        ):
            raise ValueError(
                "通用代码包与当前更新器、运行环境或数据版本不兼容，请使用完整安装包。"
            )
    elif (
        not current
        or sha256_file(root / "app_release.json") != entry["base_manifest_sha256"]
        or current["build"] != entry["from_build"]
    ):
        raise ValueError("当前安装版本不匹配，不能使用此增量包。")
    if current["runtime_id"] != latest["release"]["runtime_id"] or (
        not universal and not local_matches(root, current)
    ):
        raise ValueError("当前程序文件或运行时不匹配，请使用完整安装包。")
    if any(
        current["files"].get(name) != latest["release"]["files"].get(name)
        for name in (
            "update_core.py",
            "update_agent.py",
            "update_launcher.py",
            "update_public_key.json",
        )
    ):
        raise ValueError("引导更新器变化，需要完整安装包。")
    if (
        sha256_file(package) != entry["sha256"]
        or Path(package).stat().st_size != entry["size"]
    ):
        raise ValueError("增量包完整性校验失败。")
    expected_changed = {
        n: d
        for n, d in latest["release"]["files"].items()
        if universal or current["files"].get(n) != d
    }
    for name in expected_changed:
        if (
            name.casefold() in {n.casefold() for n in current["files"]}
            and name not in current["files"]
        ):
            raise ValueError("程序路径发生大小写变更，请使用完整安装包。")
        if name not in current["files"] and safe_target(root, name).exists():
            raise ValueError("新程序文件位置已被本地文件占用，请使用完整安装包。")
    removed = sorted(set(current["files"]) - set(latest["release"]["files"]))
    staging.mkdir(parents=True, exist_ok=False)
    header = "code.json" if universal else "patch.json"
    with zipfile.ZipFile(package) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len({n.casefold() for n in names}) or set(names) != {
            header
        } | {"files/" + n for n in expected_changed}:
            raise ValueError("增量包文件集合不匹配或含重复路径。")
        if sum(i.file_size for i in infos) > MAX_EXPANDED or any(
            stat.S_ISLNK(i.external_attr >> 16) or i.is_dir() for i in infos
        ):
            raise ValueError("增量包解压大小或文件类型不合法。")
        if archive.getinfo(header).file_size > 2 * 1024 * 1024:
            raise ValueError("包内清单超过大小上限。")
        patch = loads(archive.read(header))
        expected = (
            {"package_type": "universal_code", "release": latest["release"]}
            if universal
            else {
                "from_build": current["build"],
                "release": latest["release"],
                "delete": removed,
            }
        )
        if patch != expected:
            raise ValueError("增量包内部清单不匹配。")
        for name, digest in expected_changed.items():
            path = safe_target(staging, name)
            safe_target(root, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open("files/" + name) as source, path.open("wb") as target:
                shutil.copyfileobj(source, target, 128 * 1024)
            if sha256_file(path) != digest:
                raise ValueError("增量文件哈希不匹配。")
    for name in removed:
        safe_target(root, name)
    return sorted(expected_changed), removed


def begin_transaction(root, staging, changed, removed, release, folder):
    """Back up everything before any modification; persist intent before each replace."""
    root, folder = Path(root), Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    names = sorted(set(changed) | set(removed) | {"app_release.json"})
    journal = {
        "root": str(root.resolve()),
        "phase": "prepared",
        "touched": [],
        "original": {},
    }
    for name in names:
        target = safe_target(root, name)
        if target.exists() and not target.is_file():
            raise ValueError("程序文件位置被目录占用。")
        journal["original"][name] = target.is_file()
        if target.is_file():
            backup = safe_target(folder / "backup", name)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
    write_json(folder / "journal.json", journal)
    write_json(Path(staging) / "app_release.json", release)
    try:
        journal["phase"] = "applying"
        for name in [*changed, *removed, "app_release.json"]:
            journal["touched"].append(name)
            write_json(folder / "journal.json", journal)
            target = safe_target(root, name)
            if name in removed:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".update-tmp")
                shutil.copy2(safe_target(staging, name), temporary)
                temporary.replace(target)
        journal["phase"] = "awaiting_health"
        write_json(folder / "journal.json", journal)
    except BaseException:
        rollback(folder)
        raise
    return journal


def rollback(folder):
    folder = Path(folder)
    journal = loads((folder / "journal.json").read_bytes())
    root = Path(journal["root"])
    for name in reversed(journal["touched"]):
        target = safe_target(root, name)
        if journal["original"][name]:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".restore-tmp")
            shutil.copy2(safe_target(folder / "backup", name), temporary)
            temporary.replace(target)
        else:
            target.unlink(missing_ok=True)
    journal["phase"] = "rolled_back"
    write_json(folder / "journal.json", journal)


def commit(folder):
    folder = Path(folder)
    journal = loads((folder / "journal.json").read_bytes())
    journal["phase"] = "committed"
    write_json(folder / "journal.json", journal)


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        # Access denied is not proof that a task has exited.
        return ctypes.get_last_error() == 5
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


def installed_layout(root):
    root = Path(root).resolve()
    return (root.parent / "runtime/gui/pythonw.exe").is_file() and (
        root / "app_release.json"
    ).is_file()


def acknowledge_startup(root):
    filename, token = (
        os.environ.get("PDDCHAT_UPDATE_HEALTH_FILE"),
        os.environ.get("PDDCHAT_UPDATE_HEALTH_TOKEN"),
    )
    if filename and token:
        path = Path(filename).resolve()
        cache = Path(root).resolve().parent / "update-cache"
        if path.is_relative_to(cache):
            write_json(path, {"token": token, "pid": os.getpid()})
