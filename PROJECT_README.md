# Athena ILI FFP/CGR Desktop Tool

In-house Windows desktop application for automating Fitness-For-Purpose (FFP) and Corrosion Growth Rate (CGR) report generation from In-Line Inspection (ILI) data.

## Status

**Phase 1 — Bootstrap (complete):** project skeleton, config files, data models, example file collection, build plan

**Phase 2 — Implementation (in progress):** see `/docs/CLAUDE_CODE_PROMPTS.md` for the step-by-step build plan

## Quick start (developer)

1. Install Python 3.11+
2. Install requirements: `pip install -r requirements.txt`
3. Place example ILI files in `/examples/` (4 reference projects from real Athena deliveries)
4. Open Claude Code in this directory
5. Follow `/docs/CLAUDE_CODE_PROMPTS.md` step by step — paste Prompt 0 (context), then Prompts 1-13 in order, verifying tests pass after each

## Quick start (end user, once built)

1. Run the installer `Setup_AthenaIliFfp_v0.1.0.exe`
2. Launch from Start Menu
3. Create or open a project
4. Drop in run 1 and run 2 ILI files
5. Generate report

## Architecture

```
src/
├── models/        Dataclasses and enums (Feature, Joint, Pipeline, FFPMethod, etc.)
├── io/            File readers — vendor-format agnostic via column_synonyms.yaml
├── core/          Engines: joint_alignment, defect_matcher, cgr, ffp, repair_predictor
├── reports/       Excel annexure writer + DOCX main report writer
├── gui/           PyQt6 multi-screen desktop app
└── validation/    QA flag aggregator

config/
├── default_project.yaml      Default project config
└── column_synonyms.yaml      Vendor-format-agnostic column mappings (KEY FILE — this is how new formats are supported without code changes)

templates/                    Boilerplate DOCX sections for report
examples/                     Real example files for regression testing
tests/                        pytest suite
packaging/                    PyInstaller spec + Inno Setup installer
docs/                         All documentation including CLAUDE_CODE_PROMPTS.md
```

## Reference standards

- **POF 100 (2021)** — ILI content specifications
- **POF 110 (2021)** — UPT data format
- **ASME B31G-2012** — strength calculation for corroded pipelines
- **DNV-RP-F101 (2017)** — alternative FFP method
- **API 1160** — liquid pipeline integrity management
- **API 570** — in-service pipeline inspection, CGR guidance

## Validation projects

The regression test suite validates against four real Athena deliveries:

| Project | Client | Pipeline | Runs | Format |
|---|---|---|---|---|
| Samakhiali → IP-01 | GAIL | crude | 2018 / 2023 | Annexure B/C/D |
| COT → IPS-01 Mundra-Bhatinda | HMEL | crude | 2019 / 2025 | Annexure E/F |
| IPS-1 → IPS-2 | HMEL | 28" crude, 3 MAOP zones | 2019 / 2025 | Annexure E/F |
| Kandla → Samakhiali | GAIL | 10" LPG | 2018 / 2023 | Annexure C/D, hybrid CGR |

## Adding a new vendor format

The reader uses `config/column_synonyms.yaml` as source of truth. To support a new vendor:

1. Open a sample file from the vendor
2. Identify which column corresponds to each canonical field
3. Add the new column-name variants to `column_synonyms.yaml`
4. Re-run — no code changes needed

## License

Internal Athena PowerTech LLP tool. Not for redistribution.
