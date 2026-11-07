"""Deploy only the dedicated log relay; reuse the publisher's trusted SSH profile.

Credentials are kept in ignored state/remote-log-access.json, never printed.
"""

import argparse
import importlib.util
import json
import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "publisher_backend", ROOT / "packaging/publisher_backend.py"
)
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    config = json.loads(
        (ROOT / "packaging/publisher-state/profile.json").read_text(encoding="utf-8")
    )
    client = backend.connect(config)

    def run(command, timeout=60):
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        if stdout.channel.recv_exit_status():
            raise RuntimeError(error[-3000:] or output[-3000:])
        return output

    try:
        if args.preflight:
            print(
                run(
                    "python3 --version; test -d /opt/pddchat-log-relay && echo RELAY_EXISTS; systemctl is-active nginx; ss -lnt '( sport = :8765 )'; python3 -c 'import venv'"
                )
            )
            return
        access_path = ROOT / "state/remote-log-access.json"
        first = not access_path.exists()
        if first:
            access = {
                "url": "https://flowtry.top/pdd-logs/",
                "password": secrets.token_urlsafe(24),
                "device_id": secrets.token_hex(16),
                "token": secrets.token_urlsafe(32),
                "salt": secrets.token_hex(16),
            }
            access_path.parent.mkdir(parents=True, exist_ok=True)
            access_path.write_text(json.dumps(access, indent=2), encoding="utf-8")
        else:
            access = json.loads(access_path.read_text(encoding="utf-8"))
        from server.log_relay.app import digest, password_hash

        credential = {
            "salt": access["salt"],
            "password_hash": password_hash(access["password"], access["salt"]),
            "device": access["device_id"],
            "token_hash": digest(access["token"]),
        }
        # Dedicated directory only; never touches the update packages or main app.
        run(
            "test ! -e /etc/pddchat-log-relay.env || test -d /opt/pddchat-log-relay; install -d -m 755 /opt/pddchat-log-relay; id pdd-log-relay >/dev/null 2>&1 || useradd --system --home-dir /var/lib/pddchat-log-relay --shell /usr/sbin/nologin pdd-log-relay; install -d -o pdd-log-relay -g pdd-log-relay -m 700 /var/lib/pddchat-log-relay"
        )
        sftp = client.open_sftp()

        def upload(raw, target, mode=0o644):
            temporary = target + ".new"
            with sftp.open(temporary, "wb") as stream:
                stream.write(raw)
            sftp.chmod(temporary, mode)
            sftp.posix_rename(temporary, target)

        try:
            for directory in ("static",):
                run("install -d -m 755 /opt/pddchat-log-relay/" + directory)
            for name in (
                "app.py",
                "requirements.txt",
                "static/index.html",
                "static/app.js",
                "static/style.css",
            ):
                upload(
                    (Path(__file__).parent / name).read_bytes(),
                    "/opt/pddchat-log-relay/" + name,
                )
            upload(
                json.dumps(credential).encode(),
                "/opt/pddchat-log-relay/provision.json",
                0o600,
            )
            env = f"LOG_PASSWORD_SALT={credential['salt']}\nLOG_PASSWORD_HASH={credential['password_hash']}\nLOG_DB=/var/lib/pddchat-log-relay/logs.db\nLOG_ORIGIN=https://flowtry.top\nLOG_ENDPOINT=https://flowtry.top/pdd-logs/\n"
            # Fail if an existing service belongs to a different admin credential.
            existing = run(
                "if test -f /etc/pddchat-log-relay.env; then sha256sum /etc/pddchat-log-relay.env; fi"
            )
            if existing:
                import hashlib

                if existing.split()[0] != hashlib.sha256(env.encode()).hexdigest():
                    raise RuntimeError(
                        "服务器已配置不同日志凭证，停止覆盖。请恢复原本地访问文件。"
                    )
            upload(env.encode(), "/etc/pddchat-log-relay.env", 0o600)
            unit = """[Unit]
Description=Pddchat read-only remote log relay
After=network.target
[Service]
User=pdd-log-relay
Group=pdd-log-relay
WorkingDirectory=/opt/pddchat-log-relay
EnvironmentFile=/etc/pddchat-log-relay.env
ExecStart=/opt/pddchat-log-relay/.venv/bin/python -m uvicorn app:create_app --factory --host 127.0.0.1 --port 8765 --root-path /pdd-logs --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-access-log --timeout-graceful-shutdown 5
TimeoutStopSec=15
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/pddchat-log-relay
UMask=0077
[Install]
WantedBy=multi-user.target
"""
            upload(unit.encode(), "/etc/systemd/system/pddchat-log-relay.service")
            snippet = """location = /pdd-logs { return 301 /pdd-logs/; }
location ^~ /pdd-logs/ {
    proxy_pass http://127.0.0.1:8765/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 75s;
    client_max_body_size 16m;
}
"""
            upload(snippet.encode(), "/etc/nginx/snippets/pddchat-log-relay.conf")
            site_path = "/etc/nginx/sites-available/flowtry.top"
            with sftp.open(site_path, "rb") as stream:
                original = stream.read()
            marker = b"    include /etc/nginx/snippets/pddchat-log-relay.conf;\n"
            if marker not in original:
                needle = b"    include /etc/nginx/snippets/pdd-appeal-updates.conf;"
                if original.count(needle) != 1:
                    raise RuntimeError("nginx配置不符合预期，未修改站点。")
                stamp = str(int(time.time()))
                upload(original, site_path + ".before-logs-" + stamp)
                backup = (
                    ROOT
                    / "outputs/log-relay-preflight"
                    / ("flowtry.top." + stamp + ".conf")
                )
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(original)
                upload(
                    original.replace(needle, needle + b"\n" + marker.rstrip(b"\n")),
                    site_path,
                )
                try:
                    run("nginx -t")
                except BaseException:
                    upload(original, site_path)
                    raise
            print("配置已上传，安装独立服务依赖……", flush=True)
            print(
                run(
                    "python3 -m venv /opt/pddchat-log-relay/.venv && /opt/pddchat-log-relay/.venv/bin/python -m pip install --disable-pip-version-check -r /opt/pddchat-log-relay/requirements.txt",
                    timeout=180,
                )[-500:],
                flush=True,
            )
            provision = """import json,sqlite3
p=json.load(open('/opt/pddchat-log-relay/provision.json'))
from app import Store
s=Store('/var/lib/pddchat-log-relay/logs.db')
with s.connection() as db:
 db.execute('INSERT OR IGNORE INTO devices VALUES(?,?,?,0)',(p['device'],'主运行电脑',p['token_hash']))
"""
            upload(provision.encode(), "/opt/pddchat-log-relay/provision.py", 0o600)
            run(
                "/opt/pddchat-log-relay/.venv/bin/python /opt/pddchat-log-relay/provision.py && chown -R pdd-log-relay:pdd-log-relay /var/lib/pddchat-log-relay && rm /opt/pddchat-log-relay/provision.json /opt/pddchat-log-relay/provision.py && systemctl daemon-reload && systemctl enable --now pddchat-log-relay && systemctl restart pddchat-log-relay && nginx -t && systemctl reload nginx"
            )
            print(
                "独立日志服务已启动。访问凭证已保存到 state/remote-log-access.json（不打印密码）。",
                flush=True,
            )
        finally:
            sftp.close()
    finally:
        client.close()


if __name__ == "__main__":
    main()
