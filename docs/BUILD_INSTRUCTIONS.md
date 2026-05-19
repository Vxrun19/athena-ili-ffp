# Build instructions — Athena ILI FFP Tool

How to produce the Windows installer (`Setup_AthenaIliFfp_v<x.y.z>.exe`) on a build machine and verify it on a target machine.

---

## Prerequisites

You need these once, on the build machine:

| Tool | Version | Where to get it |
| --- | --- | --- |
| Python | 3.12 (or 3.11) | <https://www.python.org/downloads/> — install for current user; check "Add Python to PATH" |
| Inno Setup | 6.x | <https://jrsoftware.org/isdl.php> — accept the default install path `C:\Program Files (x86)\Inno Setup 6\` |
| Git for Windows | latest | <https://git-scm.com/download/win> |
| Microsoft Visual C++ Redistributable | 2015–2022 | <https://aka.ms/vs/17/release/vc_redist.x64.exe> (PyInstaller bundles its own, but the target machine still needs this) |

> Python 3.13 isn't supported yet — `pyqt6-qt6` and a couple of scientific-stack wheels lag the release. Stick to 3.12.

---

## One-time setup

```cmd
cd C:\path\to\ili_ffp_tool
python -m venv .venv
.venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

Verify the test suite runs:

```cmd
.venv\Scripts\activate
pytest tests\
```

All ~380 tests should pass before you build a release.

---

## Build

Single command:

```cmd
packaging\build.bat
```

The script:

1. Activates `.venv` (or warns and uses the system Python if missing).
2. Stamps `src\_version.py`'s `__build_date__` to the current UTC timestamp.
3. Runs `pyinstaller --clean --noconfirm packaging\build_windows.spec` — produces `dist\athena_ili_ffp\`.
4. Runs Inno Setup's `ISCC.exe packaging\installer.iss` — produces `dist\Setup_AthenaIliFfp_v0.1.0.exe`.
5. Restores `_version.py` to its checked-in state so `git status` stays clean.

Output:

```
dist\
├── athena_ili_ffp\               <- PyInstaller one-folder dist (folder you'd ship if not using the installer)
│   ├── athena_ili_ffp.exe
│   ├── config\
│   ├── templates\
│   ├── examples\
│   └── ...                        (~300 MB of Python + native deps)
└── Setup_AthenaIliFfp_v0.1.0.exe  <- the installer ship to clients (~120 MB compressed)
```

If the build fails, the script prints the failed stage clearly and exits non-zero. Common failure modes:

| Symptom | Fix |
| --- | --- |
| `pyinstaller: not found` | Activate `.venv` first; `pip install pyinstaller` if needed |
| `ISCC.exe not found` | Install Inno Setup 6 or set `ISCC` env var to its location |
| `Permission denied` writing `dist\...` | Close any Explorer windows / antivirus quarantine on the dist folder |
| `Module not found` at first launch on a target machine | Add the missing module to `hiddenimports` in `packaging\build_windows.spec` and rebuild |

---

## Install + use on a new machine

The installer is per-user — **no admin rights required**.

1. Copy `Setup_AthenaIliFfp_v0.1.0.exe` to the target Windows 10/11 machine.
2. Double-click. The installer drops the files to `%LOCALAPPDATA%\Programs\Athena ILI FFP Tool\` and creates a Start Menu folder.
3. From the Start Menu, "Athena ILI FFP Tool" → **Run FFP Analysis (CMD)** opens a CMD window with the tool on PATH.
4. Try `athena_ili_ffp --version` — should print `Athena ILI FFP Tool v0.1.0 (build YYYY-MM-DDTHH:MM:SS)`.
5. Run the bundled Kandla example end-to-end:
   ```cmd
   athena_ili_ffp --config "%APPDATA%\Athena\ILI_FFP_Tool\projects\kandla_project.yaml" --output-dir "%USERPROFILE%\Desktop\ffp_out"
   ```
6. Open the produced `.xlsx` and `.docx` from the output folder.

Uninstall: Start Menu → "Athena ILI FFP Tool" → "Uninstall Athena ILI FFP Tool". The uninstaller removes the install directory but **preserves** `%APPDATA%\Athena\ILI_FFP_Tool\` (the user's project YAMLs and outputs). Delete that folder manually for a complete wipe.

---

## Update the version number for a new release

Bump three places, in order:

1. **`src\_version.py`** — change `__version__ = "0.1.0"` to the new tag.
2. **`packaging\installer.iss`** — change `#define MyAppVersion "0.1.0"` to match.
3. **CHANGELOG / commit** — note what changed.

Then run `packaging\build.bat`. The installer filename automatically picks up the new version.

The `AppId` GUID in `installer.iss` is deliberately **kept stable across versions** — Inno Setup uses it to detect that the new installer is an upgrade, not a side-by-side install. Don't change it.

---

## Known limitations of v0.1.0

* **CLI only** — no GUI shell yet (planned for v0.2 via PyQt6).
* **Input: pipe-tally Excel only.** No CSV or POF UPT parsers yet.
* **HMEL MAOP-zone assignment** uses strict WT-based lookup. Some published reports use chainage-based or operator-overridden zones — see `docs/FFP_VALIDATION.md` and `memory/project_v02_followups.md`. Until then, override MAOP with `--years` + a custom project YAML if zone behaviour matters.
* **RSTRENG depth profiles** not yet supported — the tool falls back to the 0.85·dL approximation (mathematically equivalent to B31G Modified). Profile-driven RSTRENG is queued for v0.2 when an ILI vendor ships per-feature profiles.
* **Kastner formulation** is the net-section approximation, not the full Kastner 1986 equilibrium form. Sufficient for the small-defect regime in the validation pairs; the full form is queued for v0.2.
* **Windows-only installer.** Linux / macOS users can `pip install -e .` and run `python bin/run_pipeline.py` directly; no installer is packaged for those platforms in v0.1.0.
* **Date-based MAOP changes** (e.g. a derating in 2023) not modelled — MAOP is a single value per zone for the whole projection horizon.

---

## Smoke test on the installer (run before shipping to clients)

```cmd
:: From a fresh user account, with no Python installed:
Setup_AthenaIliFfp_v0.1.0.exe        :: silent OK
athena_ili_ffp --version             :: prints version banner
athena_ili_ffp --config %APPDATA%\Athena\ILI_FFP_Tool\projects\kandla_project.yaml --output-dir %USERPROFILE%\Desktop\smoke_test
```

The smoke test should:

1. Print the per-stage progress lines (~5 stages, finishing in <10 s on Kandla).
2. Produce `%USERPROFILE%\Desktop\smoke_test\FFP_Kandla_Samakhiali_10in_LPG_annexure.xlsx` and `..._report.docx`.
3. Exit with code 0.

If any of those fail, the installer is broken — don't ship.
