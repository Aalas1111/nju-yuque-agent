import os
import sys

import paramiko

HOST = "47.96.229.237"
USER = "root"
PW = os.environ.get("PROBE_PW", "")

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    c.connect(
        HOST,
        port=22,
        username=USER,
        password=PW,
        timeout=15,
        allow_agent=False,
        look_for_keys=False,
    )
except paramiko.AuthenticationException:
    print("AUTH_FAIL: 密码不对")
    sys.exit(2)
except Exception as e:
    print(f"CONN_FAIL: {type(e).__name__}: {e}")
    sys.exit(3)

print("AUTH_OK")
for cmd in (
    "cat /etc/os-release | head -3",
    "uname -m",
    "nproc",
    "free -m | head -2",
    "df -h / | tail -1",
    "date",
    "cat /etc/timezone 2>/dev/null || timedatectl 2>/dev/null | head -3",
):
    _, out, err = c.exec_command(cmd, timeout=15)
    o = out.read().decode("utf-8", "replace").strip()
    e = err.read().decode("utf-8", "replace").strip()
    print(f"  $ {cmd}\n    {o or e}".replace("\n", "\n    "))
c.close()
