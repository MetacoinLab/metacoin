# Scientific workspace session (Order 07) — state
Start: 2026-09-28 00:22 MDT (America/Edmonton); `START` holds the UTC stamp. Target budget 24 h; final 90 min reserved for candidate verification, export, migration rehearsal, live upgrade, delivery.
Delivery path (chosen): `~/metacoin-scientific-workspace-delivery-2026-09-28/`.

## Baseline (resolved from Git and disk, not from screenshots)
- Repo `/home/zhangd2/projects/metacoin`, branch `service/real-features`, base commit for this order: be2d60c95b… (`git rev-parse be2d60c` = full hash recorded in artifacts/BASE_REVISION.txt), working tree clean at start.
- Previous delivery `~/metacoin-24h-expansion-delivery-2026-09-27/`: verified code ead4d68, packaged HEAD 6789a61 (the "56789a61" in the order is a misread of 6789a61); schema 025; labels commit be2d60c came after packaging (docs/labels only).
- Live: tmux session `metacoin-service` (window 0 API on 127.0.0.1:8402 pid 1840173, window 1 `live-worker` pid 1840175), loaded revision ead4d68 (health reports the loaded revision; the on-disk HEAD is newer by docs-only commits), provider mode test-http, schema 025 (`025_metered_upto`). The Claude session is tmux `metacoin` — never touched.
- Interpreters: API `.venv-service` (python 3.12; fastapi, x402 2.24.0, pypdf 6.1.3 added); compute/model child `/usr/bin/python3` user site: torch 2.10.0+cu130, transformers 5.5.0, numpy 2.2.6, PIL 12.1.1, cv2 4.13.0, reportlab 4.1.0 + added this session: pypdf 6.1.3, scipy 1.18.1 (HiGHS milp verified), onnxruntime 1.30.0, rapidocr_onnxruntime 1.4.4 (--no-deps: keeps numpy/cv2), pyclipper 1.4.0, shapely 2.1.2, pyyaml 6.0.3. Wheels cached in scratchpad `scideps/` (141 MB incl. unused opencv/numpy wheels). Pre-existing user-site conflicts (vllm/compressed-tensors want transformers<5) are not from this session. PyMuPDF 1.27.2 present but NOT used (AGPL). No tesseract; poppler `pdftoppm`/`pdftotext` binaries present (used only as separate processes for page rendering).
- Models: Qwen2.5-0.5B-Instruct @7ae5576 (cuda, bf16), all-MiniLM-L6-v2 @1110a24 (cpu) under ~/.local/share/metacoin-models; registered+promoted on the live instance.
- Host: DGX Spark GB10, 119 GiB unified memory (109 GiB available at start), 2.5 TB free disk.
- Step recorder `run_step.sh` self-tested: pass rc=0, fail rc=3 recorded; a trailing success inside a compound command masks the failure (recorded as the shape to avoid).

## Plan (priority order, per §4/§68)
A document intelligence (pypdf native + RapidOCR + pdftoppm rendering in a bounded compute-interpreter child; tables; mapping; units) → B real static generation batching in the runtime child (+ continuous admission as extension 2 if transformers 5.5 CB works on GB10) → C typed intent compilation on the existing planner (catalog filter, clarification, repair, eval set v2) → D robust resource planning (SciPy milp + exact simulator + oracle + witness verification + epsilon sweep + branches + campaigns + sensitivity) → E analysis sessions on notebooks (+ dependency graph, impact, regeneration, reports, projections) → F workflow packages (bundles + composite quotes + verification-gated upto delivery + signed result bundles) → journeys 1-24, campaign, live upgrade, delivery.
Migration numbers: next unused is 026 (verified: db.py ends at 025_metered_upto).

## Done
- baseline inventory, dependency installation (00:22-00:35 MDT)

## Task-owned background producers (identity, log, start, timeout, expected output, exit)
(none yet)

## Next action
Group A: migration 026 (imports/extractions/tables/annotations/mappings + knowledge_chunks page/region columns), documents package with the bounded extraction child, synthetic fixtures generator, API/console/CLI, tests.
