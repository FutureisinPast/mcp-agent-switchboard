"""Tiny line-protocol client for gfy_adapter (used by tests, eval and coverage probe)."""
import json, os, subprocess, time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, "venv", "Scripts", "python.exe")


class Adapter:
    def __init__(self, projects=None):
        env = dict(os.environ, PYTHONHASHSEED="0", PYTHONUTF8="1")
        for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY"):
            env.pop(k, None)
        env.pop("GRAPHIFY_OUT", None)
        cmd = [PY, os.path.join(HERE, "gfy_adapter.py")] + (["--projects", projects] if projects else [])
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env, bufsize=1)
        self.n = 0

    def call(self, **req):
        self.n += 1
        req["id"] = self.n
        t = time.perf_counter()
        self.p.stdin.write(json.dumps(req) + "\n"); self.p.stdin.flush()
        line = self.p.stdout.readline()
        dt = time.perf_counter() - t
        return json.loads(line), line.rstrip("\n"), dt

    def close(self):
        try:
            self.p.stdin.close(); self.p.wait(timeout=10)
        except Exception:
            self.p.kill()
