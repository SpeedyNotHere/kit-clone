
import os
import json
import shutil
import difflib
import tempfile
from datetime import datetime
from pathlib import Path
from groq import Groq

ROOT = Path(__file__).resolve().parent
BACKUP_ROOT = Path.home() / "AppData" / "Local" / "kit" / "backups"
MODEL = "qwen/qwen3.8-27b"

MAX_FILES = 12
MAX_FILE_SIZE = 20_000
MAX_CONTEXT_CHARS = 25_000
MAX_CHANGES = 8

SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".idea", ".vscode", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build",
    "target", "coverage", ".next", ".nuxt", "vendor",
    "bin", "obj", ".tox", ".cache"
}

SKIP_FILES = {
    ".env", ".env.local", ".env.production",
    "id_rsa", "id_ed25519", "credentials",
    "secrets.json", "secrets.yaml", "secrets.yml"
}

SKIP_SUFFIXES = {
    ".pyc", ".pyo", ".exe", ".dll", ".so", ".dylib",
    ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
    ".zip", ".7z", ".rar", ".tar", ".gz",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".ico", ".mp3", ".mp4", ".mov", ".pdf",
    ".woff", ".woff2", ".ttf", ".otf", ".lock"
}

client = Groq(api_key=os.environ["GROQ_API_KEY"])


def safe_path(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("invalid file path")

    if "\\" in name:
        name = name.replace("\\", "/")

    relative = Path(name)

    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe path: {name}")

    path = (ROOT / relative).resolve()

    if not path.is_relative_to(ROOT):
        raise ValueError(f"path escapes project: {name}")

    parts = path.relative_to(ROOT).parts

    if any(part.lower() == ".git" for part in parts):
        raise ValueError("editing .git internals is blocked")

    return path


def is_sensitive(path):
    name = path.name.lower()

    if name in SKIP_FILES:
        return True

    if name.startswith(".env."):
        return True

    if name.endswith((".pem", ".key", ".p12", ".pfx")):
        return True

    if name in {"known_hosts", "authorized_keys"}:
        return True

    return False


def discover_files():
    files = {}

    for directory, dirs, names in os.walk(ROOT, followlinks=False):
        current = Path(directory)

        dirs[:] = [
            name for name in dirs
            if name.lower() not in SKIP_DIRS
            and not (current / name).is_symlink()
        ]

        for name in names:
            path = current / name

            if path.is_symlink() or is_sensitive(path):
                continue

            if path.suffix.lower() in SKIP_SUFFIXES:
                continue

            try:
                if not path.is_file():
                    continue

                size = path.stat().st_size

                if size > MAX_FILE_SIZE:
                    continue

                relative = path.relative_to(ROOT).as_posix()

                files[relative] = {
                    "path": relative,
                    "size": size,
                    "extension": path.suffix.lower() or "(none)"
                }
            except OSError:
                continue

    return files


def read_file(name):
    path = safe_path(name)

    if is_sensitive(path):
        raise ValueError(f"refusing to read sensitive file: {name}")

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"not a regular file: {name}")

    if path.stat().st_size > MAX_FILE_SIZE:
        raise ValueError(f"file too large: {name}")

    data = path.read_bytes()

    if b"\0" in data:
        raise ValueError(f"binary file blocked: {name}")

    return data.decode("utf-8")



def call_ai(messages):
    response = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=messages
    )

    return json.loads(response.choices[0].message.content)

def choose_files(task, index):
    print(f"indexed {len(index)} project files")
    print("asking the AI which files are relevant...")

    system = """
You are the project analysis component of kit, an AI coding editor.

Select the files needed to complete the user's task.
You may select existing files from the supplied project index only.
Consider imports, callers, configuration, tests, documentation,
and related components. Do not select secrets or generated files.

Return JSON:
{
  "files": ["relative/path.py"],
  "reason": "short explanation"
}

Select at most 12 files. Prefer the most relevant files.
If the task is broad, select entry points and important related files.
Do not invent paths.
"""

    result = call_ai([
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                f"Task:\n{task}\n\n"
                f"Project file index:\n"
                f"{json.dumps([item['path'] for item in list(index.values())[:50]], ensure_ascii=False)}"
            )
        }
    ])

    selected = result.get("files", [])

    if not isinstance(selected, list):
        raise ValueError("AI returned an invalid file selection")

    valid = []
    seen = set()

    for name in selected[:MAX_FILES]:
        if not isinstance(name, str) or name not in index:
            continue

        key = name.casefold()

        if key in seen:
            continue

        seen.add(key)
        valid.append(name)

    if not valid:
        raise ValueError(
            "AI could not identify relevant files. "
            "Try a more specific task."
        )

    print(f"selected {len(valid)} files")
    print(result.get("reason", ""))

    return valid


def request_changes(task, selected, index):
    context = {}
    total_chars = 0

    for name in selected:
        try:
            content = read_file(name)
        except (OSError, UnicodeError, ValueError) as error:
            print(f"skipping {name}: {error}")
            continue

        if total_chars + len(content) > MAX_CONTEXT_CHARS:
            print("context limit reached")
            break

        context[name] = content
        total_chars += len(content)

    if not context:
        raise ValueError("no readable project files were selected")

    print(f"sending {len(context)} files to Groq...")

    system = """
You are kit, an AI coding editor working inside an existing project.

You will receive the user's task, a project file index, and selected
source files. Use the supplied files to understand the project.

Return JSON in this shape:
{
  "changes": [
    {
      "path": "relative/path.py",
      "action": "write",
      "content": "complete file contents"
    }
  ],
  "summary": "short explanation"
}

Rules:
- Return complete contents for every changed file.
- You may edit supplied files or create new files.
- You may only write files relevant to the user's task.
- Never delete files.
- Never modify .git internals, secrets, API keys, or credentials.
- Do not invent existing code that you have not been shown.
- Keep changes compatible with the existing project.
- Do not return unchanged files.
- Use only the action "write".
- Return valid JSON without markdown fences.
- If you lack essential context, return no changes and explain what
  additional files or information are needed in the summary.
"""

    result = call_ai([
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                f"Task:\n{task}\n\n"
                f"Project index:\n"
                f"{json.dumps(list(index.values()), ensure_ascii=False)}\n\n"
                f"Selected project files:\n"
                f"{json.dumps(context, ensure_ascii=False)}"
            )
        }
    ])

    changes = result.get("changes")

    if not isinstance(changes, list) or len(changes) > MAX_CHANGES:
        raise ValueError("invalid change list")

    validated = []
    seen = set()

    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("invalid change entry")

        if change.get("action") != "write":
            raise ValueError("only file writes are supported")

        name = change.get("path")
        content = change.get("content")

        if not isinstance(name, str) or not isinstance(content, str):
            raise ValueError("each change needs a path and string content")

        path = safe_path(name)

        if is_sensitive(path):
            raise ValueError(f"refusing to modify sensitive file: {name}")

        if path.suffix.lower() in SKIP_SUFFIXES:
            raise ValueError(f"refusing to modify binary file: {name}")

        if any(part.lower() in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            raise ValueError(f"refusing to modify excluded directory: {name}")

        if path.is_symlink():
            raise ValueError(f"refusing to modify symbolic link: {name}")

        key = str(path).casefold()

        if key in seen:
            raise ValueError(f"duplicate target: {name}")

        seen.add(key)

        if path.exists() and not path.is_file():
            raise ValueError(f"not a regular file: {name}")

        if path.is_file() and path.stat().st_size > MAX_FILE_SIZE:
            raise ValueError(f"target file too large: {name}")

        if len(content.encode("utf-8")) > MAX_FILE_SIZE:
            raise ValueError(f"proposed file too large: {name}")

        validated.append({
            "path": path.relative_to(ROOT).as_posix(),
            "content": content
        })

    return validated, result.get("summary", "")


def show_changes(changes):
    for change in changes:
        path = safe_path(change["path"])
        old = path.read_text(encoding="utf-8") if path.is_file() else ""
        new = change["content"]

        print(f"\n--- {change['path']} ---")

        diff = difflib.unified_diff(
            old.splitlines(True),
            new.splitlines(True),
            fromfile=change["path"],
            tofile=change["path"] + " (proposed)"
        )

        output = "".join(diff)
        print(output if output else "(no differences)")


def apply_changes(changes):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_dir = BACKUP_ROOT / stamp
    staged = []
    backups = {}
    written = []

    try:
        # Prepare all content and backups before modifying project files.
        for change in changes:
            path = safe_path(change["path"])

            if path.exists():
                backups[path] = path.read_bytes()
                backup = backup_dir / change["path"]
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, backup)

        # Stage every new file in its target directory first.
        for change in changes:
            path = safe_path(change["path"])
            path.parent.mkdir(parents=True, exist_ok=True)

            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=path.parent,
                prefix=".kit-",
                suffix=".tmp",
                delete=False
            ) as temp:
                temp.write(change["content"])
                staged.append((path, Path(temp.name)))

        # Replace files only after staging has succeeded.
        for path, temp_path in staged:
            os.replace(temp_path, path)
            written.append(path)

    except Exception:
        # Attempt to restore all previously modified files.
        for path in reversed(written):
            try:
                if path in backups:
                    path.write_bytes(backups[path])
                elif path.exists():
                    path.unlink()
            except OSError as error:
                print(f"restore failed for {path}: {error}")

        print("changes failed; attempted to restore original files")
        raise

    finally:
        for _, temp_path in staged:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    print(f"\napplied {len(changes)} file change(s)")

    if backup_dir.exists():
        print(f"backups: {backup_dir}")


def main():
    print("kit project-wide AI editor")
    print(f"project: {ROOT}")

    if not os.environ.get("GROQ_API_KEY"):
        print("error: set the GROQ_API_KEY environment variable first")
        return

    task = input("\nwhat should change? ").strip()

    if not task:
        print("no task provided")
        return

    try:
        index = discover_files()

        if not index:
            print("no eligible project files found")
            return

        selected = choose_files(task, index)
        changes, summary = request_changes(task, selected, index)

        print(f"\nplan: {summary}")

        if not changes:
            print("no changes proposed")
            print("the AI may need more context to complete this task")
            return

        show_changes(changes)

        answer = input("\napply ALL these changes? (y/n): ").strip().lower()

        if answer != "y":
            print("changes discarded")
            return

        apply_changes(changes)

    except Exception as error:
        print(f"\nerror: {error}")


if __name__ == "__main__":
    main()
