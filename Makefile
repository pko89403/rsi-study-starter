install:
	uv sync

test:
	uv run python -m scripts.test_eval --runs 3

round:
	uv run python -m loop.run --rounds $(or $(N),20)

results:
	uv run python -m scripts.results --week $(W)

meta-propose:
	uv run python -m loop.meta propose

meta-eval:
	uv run python -m loop.meta eval $(if $(FILE),--file $(FILE)) $(if $(N),--n $(N))

meta-adopt:
	uv run python -m loop.meta adopt $(if $(FILE),--file $(FILE)) $(if $(FORCE),--force --reason "$(REASON)")
