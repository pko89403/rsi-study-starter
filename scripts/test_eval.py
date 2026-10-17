"""test.json으로 현재 코드(= 최고 commit)를 N회 실행·평가해 평균을 낸다. loop와 무관한 별도 명령."""
from __future__ import annotations
import argparse, json, os, subprocess, sys, tempfile
from pathlib import Path
import yaml
from evaluator.evaluate import evaluate
from loop.env import require_llm
from loop import kernel as K

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser(); ap.add_argument("--runs", type=int, default=3); ap.add_argument("--config", default="config.yaml")
    a = ap.parse_args(); os.chdir(ROOT); Path("logs").mkdir(exist_ok=True)
    require_llm()
    K.require_ruler()
    cfg = yaml.safe_load(Path("config.yaml").read_text())
    best = json.loads(Path("logs/archive.json").read_text()).get("best") if Path("logs/archive.json").exists() else None
    if best and subprocess.run(["git", "diff", "--quiet", best, "--", *cfg["evolve_files"]]).returncode != 0:
        sys.exit(f"{', '.join(cfg['evolve_files'])}가 champion({best})과 다릅니다. make round가 중간에 끊겼으면:\n"
                 f"  git checkout {best} -- {' '.join(cfg['evolve_files'])}\n그다음 make test를 다시 실행하세요.")
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
        empty = sum(not json.loads(l).get("sql") for l in Path(out).read_text().splitlines() if l.strip())   # 예외·토큰 한도로 빈 답
        if empty > len(problems) // 20:
            fails = [l for l in r.stdout.splitlines() if "failed:" in l]
            sys.exit(f"{empty}/{len(problems)} 문제의 답이 비어 점수를 믿을 수 없다 (.env의 키·모델·LLM_MAX_TOKENS 확인). {fails[0][:200] if fails else ''}")
        res = evaluate(out, "data/test.json", cfg["db_dir"], cfg["sql_timeout_sec"])
        scores.append(res["correct"]); print(f"run {i+1}: {res['correct']}/{res['n']}")
        if i >= 1 and sorted(Path(out).read_text().splitlines()) == sorted(Path(f"logs/_test_pred_{i-1}.jsonl").read_text().splitlines()):
            print(f"run {i+1}: 예측이 run {i}와 완전히 같음 → 남은 run은 같은 점수로 채우고 건너뜀")
            scores += [res["correct"]] * (a.runs - len(scores)); break
    print(json.dumps({"test_n": len(problems), "runs": scores, "mean": sum(scores) / len(scores), "anchors_sha": K.anchors_sha(),
                      "ruler": K.RULER_STATUS, "model": os.environ.get("LLM_MODEL"), "commit": best}))


if __name__ == "__main__":
    main()
