"""把两个凭证推到服务器。全程走 stdin，不经过命令行参数，也不打印明文。"""

import json
import pathlib
import subprocess
import sys

home = pathlib.Path.home()
yuque = json.loads((home / ".yuque" / "auth.json").read_text(encoding="utf-8"))
llm = json.loads((home / ".pi" / "agent" / "auth.json").read_text(encoding="utf-8"))
token = yuque["token"]
key = llm["deepseek"]["key"]
assert token and key, "凭证为空"

remote = f"""set -e
umask 077
mkdir -p /root/.yuque
cat > /root/.yuque/auth.json <<'JSONEOF'
{json.dumps({"token": token}, ensure_ascii=False)}
JSONEOF
cat > /root/.yuque/agent.env <<'ENVEOF'
DEEPSEEK_API_KEY={key}
ENVEOF
chmod 600 /root/.yuque/auth.json /root/.yuque/agent.env
chmod 700 /root/.yuque
echo "--- /root/.yuque 内容与权限 ---"
ls -la /root/.yuque/
echo "--- 校验（只看长度，不看内容）---"
python3 - <<'PY'
import json, pathlib
t = json.loads(pathlib.Path("/root/.yuque/auth.json").read_text())["token"]
print("yuque token 长度:", len(t))
env = dict(l.split("=",1) for l in pathlib.Path("/root/.yuque/agent.env").read_text().splitlines() if "=" in l)
print("DEEPSEEK_API_KEY 长度:", len(env.get("DEEPSEEK_API_KEY","")))
PY
"""

p = subprocess.run(
    ["ssh", "-o", "BatchMode=yes", "root@47.96.229.237", "bash -s"],
    input=remote.encode("utf-8"),
    capture_output=True,
    timeout=90,
)
print(p.stdout.decode("utf-8", "replace"))
if p.returncode != 0:
    print("STDERR:", p.stderr.decode("utf-8", "replace")[:500])
    sys.exit(1)
print(f"本地侧：token={len(token)} 字符, key={len(key)} 字符 —— 已推送")
