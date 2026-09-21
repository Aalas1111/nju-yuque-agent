"""用密码登录一次，把本机公钥装到 authorized_keys。之后就走普通 ssh。"""

import os
import pathlib

import paramiko

HOST, USER = "47.96.229.237", "root"
PW = os.environ.get("PROBE_PW", "")
PUB = pathlib.Path.home() / ".ssh" / "id_ed25519.pub"
pub = PUB.read_text(encoding="utf-8").strip()
print("本地公钥:", pub[:40], "...")

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(
    HOST, port=22, username=USER, password=PW, timeout=15, allow_agent=False, look_for_keys=False
)


def run(cmd: str) -> str:
    _, out, err = c.exec_command(cmd, timeout=30)
    o = out.read().decode("utf-8", "replace").strip()
    e = err.read().decode("utf-8", "replace").strip()
    return o or e


run("mkdir -p ~/.ssh && chmod 700 ~/.ssh")
# 幂等：先看有没有，没有才追加
existing = run("cat ~/.ssh/authorized_keys 2>/dev/null")
if pub in existing:
    print("公钥已存在，跳过")
else:
    sftp = c.open_sftp()
    with sftp.file("/root/.ssh/authorized_keys", "a") as f:
        f.write(pub + "\n")
    sftp.close()
    print("公钥已追加")
print(run("chmod 600 ~/.ssh/authorized_keys && wc -l < ~/.ssh/authorized_keys"))
c.close()
