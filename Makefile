install:
	uv sync

test:
	uv run python -m scripts.test_eval --runs 3

round:
	uv run python -m loop.run --rounds $(or $(N),20)

results:
	uv run python -m scripts.results --week $(W)
