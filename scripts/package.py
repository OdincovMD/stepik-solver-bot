"""Build a source-only archive with an explicit allowlist. Never include state or .env."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent.parent
FILES = ["README.md", "docs/README.md", "docs/SESSION-2026-11-22.md", "docs/SESSION-2026-11-24.md", "bot.py", "audit.py", "stepik.py", "solver.py", "storage.py", "config.py",
         "configure.py", "pi_check.py", "gateway_check.py", "requirements.txt", ".env.example", ".gitignore",
         "stepik-daily-bot.service", "stepik-daily-bot.timer", "scripts/install-systemd.sh", "scripts/package.py",
         "tests/test_bot.py", "tests/test_integration.py", "tests/test_gateway.py", "tests/test_audit.py"]


def main():
    output = ROOT / "dist/stepik-daily-bot-prepared.zip"
    output.parent.mkdir(exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name in FILES:
            path = ROOT / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("invalid_package_source")
            archive.write(path, arcname=name)
    print(output)


if __name__ == "__main__":
    main()
