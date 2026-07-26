.PHONY: help doctor install run fixture demo api web dev clean-dev

help:
	@echo "make doctor              check binaries, python, and API keys"
	@echo "make install             install API deps (pipeline itself needs none)"
	@echo "make run CLIP=path.mp4   run the pipeline in dev profile"
	@echo "make demo CLIP=path.mp4  run in demo profile (strict checks)"
	@echo "make fixture             run the pipeline against the fixture job"
	@echo "make api                 start FastAPI on :8000   (after the gate)"
	@echo "make web                 start Vite on :5173      (after the gate)"
	@echo "make clean-dev           delete runs/dev (never touches runs/demo)"

doctor:
	@python3 scripts/doctor.py

install:
	python3 -m pip install -r requirements.txt

run:
	@test -n "$(CLIP)" || (echo "usage: make run CLIP=demo/clips/clip_a.mp4"; exit 1)
	python3 -m drishti.pipeline --clip "$(CLIP)" --profile dev --language auto

demo:
	@test -n "$(CLIP)" || (echo "usage: make demo CLIP=demo/clips/clip_a.mp4"; exit 1)
	python3 -m drishti.pipeline --clip "$(CLIP)" --profile demo --language auto

fixture:
	python3 -m drishti.pipeline --job fixtures/jobs/english_sample --check

api:
	python3 -m uvicorn api.main:app --reload --port 8000

web:
	cd web && npm run dev

dev:
	@echo "run 'make api' and 'make web' in two terminals"

clean-dev:
	rm -rf runs/dev
	@echo "runs/dev cleared. runs/demo and demo/outputs untouched."
