"""test.json으로 현재 코드(= 최고 commit)를 N회 실행·평가해 평균을 낸다. loop와 무관한 별도 명령."""
from __future__ import annotations
import argparse, json, os, subprocess, sys, tempfile
from pathlib import Path
import yaml
from evaluator.evaluate import evaluate
from loop.env import require_llm

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser(); ap.add_argument("--runs", type=int, default=3); ap.add_argument("--config", default="config.yaml")
    a = ap.parse_args(); os.chdir(ROOT); Path("logs").mkdir(exist_ok=True)
    require_llm()
    cfg = yaml.safe_load(Path("config.yaml").read_text())
    problems = json.loads(Path("data/test.json").read_text())
    scores = []
    for i in range(a.runs):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, dir="logs") as f:
            json.dump([{k: p[k] for k in ("id", "db_id", "question")} for p in problems], f); tmp = f.name
        out = f"logs/_test_pred_{i}.jsonl"
        r = subprocess.run([sys.executable, "-m", "sql_agent.run", "--problems", tmp, "--out", out], capture_output=True, text=True, timeout=cfg["agent_timeout_sec"])
        os.unlink(tmp)
        if r.returncode != 0:
            print("agent failed:", r.stderr.strip().splitlines()[-1:]); sys.exit(1)
        res = evaluate(out, "data/test.json", cfg["db_dir"], cfg["sql_timeout_sec"])
        scores.append(res["correct"]); print(f"run {i+1}: {res['correct']}/{res['n']}")
        if i >= 1 and sorted(Path(out).read_text().splitlines()) == sorted(Path(f"logs/_test_pred_{i-1}.jsonl").read_text().splitlines()):
            print(f"run {i+1}: 예측이 run {i}와 완전히 같음 → 남은 run은 같은 점수로 채우고 건너뜀")
            scores += [res["correct"]] * (a.runs - len(scores)); break
    print(json.dumps({"test_n": len(problems), "runs": scores, "mean": sum(scores) / len(scores)}))


if __name__ == "__main__":
    main()
