install:
	uv sync

test:
	uv run python -m scripts.test_eval --runs 3

round:
	uv run python -m loop.run --rounds $(or $(N),20)

results:
	uv run python -m scripts.results --week $(W)

ruler:   # 운영자만: 지금 commit을 고정 파일의 기준으로. loop/kernel.py의 RULER_TAG 번호를 올린 뒤 실행 (tag를 origin에 push)
	T=$$(uv run python -c "from loop.kernel import RULER_TAG; print(RULER_TAG)"); git tag -f $$T && git push -f origin $$T && echo "tagged $$T"

meta-propose:
	uv run python -m loop.meta propose

meta-eval:
	uv run python -m loop.meta eval $(if $(FILE),--file $(FILE)) $(if $(N),--n $(N)) $(if $(SEED),--seed $(SEED))

meta-adopt:
	uv run python -m loop.meta adopt $(if $(FILE),--file $(FILE)) $(if $(FORCE),--force --reason "$(REASON)")
