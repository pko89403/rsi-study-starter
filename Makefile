install:
	uv sync

test:
	uv run python -m scripts.test_eval --runs 3

round:
	uv run python -m loop.run --rounds $(or $(N),20)

results:
	uv run python -m scripts.results --week $(W)

ruler:   # 운영자만: 지금 commit을 고정 파일의 기준으로 (tag를 origin에 push)
	git tag -f ruler-v1 && git push -f origin ruler-v1

meta-propose:
	uv run python -m loop.meta propose

meta-eval:
	uv run python -m loop.meta eval $(if $(FILE),--file $(FILE)) $(if $(N),--n $(N)) $(if $(SEED),--seed $(SEED))

meta-adopt:
	uv run python -m loop.meta adopt $(if $(FILE),--file $(FILE)) $(if $(FORCE),--force --reason "$(REASON)")
